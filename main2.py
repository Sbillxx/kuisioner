"""
Bot SIMANTAP v6
Alur: login -> AKADEMIK > Data Nilai -> loop dropdown Tahun Akademik (atas ke bawah)
Default: LANGSUNG cek SEMUA semester tanpa menu/prompt.

Status per semester (disesuaikan dengan tampilan asli SIMANTAP):
    OK                   -> tab "Kartu Hasil Studi / Transkrip Nilai / Sejarah Nilai" tampil
    BELUM BAYAR          -> "Kamu belum bisa melihat Nilai" + alasan pembayaran/tunggakan
    BELUM ISI KUESIONER  -> ada kuesioner yang belum diisi
    TIDAK DIKENALI       -> tampilan lain (teks halaman disimpan untuk dicocokkan)

Contoh:
    python simantap_bot_v6.py                          # cek semua semester
    python simantap_bot_v6.py --semester "2025/2026 Genap" --semester "2024/2025"
    python simantap_bot_v6.py --pilih                  # menu pilih nomor semester
    python simantap_bot_v6.py --isi                    # + isi kuesioner (konfirmasi dulu)
    python simantap_bot_v6.py --isi --auto             # isi tanpa konfirmasi
"""
import argparse
import asyncio
import os
import re
import sys
from getpass import getpass
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

BASE = "https://simantap.unper.ac.id/"
OUT_DIR = Path("hasil_cek")

RATING_INDEX = -1            # -1 = radio terakhir tiap grup, 0 = radio pertama
TEXT_JAWABAN = "Sudah baik"  # isi textarea kosong di kuesioner
MAX_KUESIONER_PER_SEMESTER = 30
MAX_TUNGGU_MAINTENANCE_MENIT = 30

JS_FILL = """
([idx, teks]) => {
  const groups = {};
  document.querySelectorAll('input[type=radio]').forEach(r => {
    (groups[r.name] = groups[r.name] || []).push(r);
  });
  let n = 0;
  for (const g of Object.values(groups)) { g.at(idx).click(); n++; }
  document.querySelectorAll('textarea').forEach(t => {
    if (!t.value) { t.value = teks; t.dispatchEvent(new Event('input', {bubbles: true})); }
  });
  return n;
}
"""

JS_SET_SELECT = """
(el, i) => {
  el.selectedIndex = i;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  if (window.jQuery) window.jQuery(el).trigger('change');
}
"""


# ----------------------------------------------------------------- util umum
def get_credentials():
    user = os.environ.get("SIMANTAP_USER") or input("NIM: ").strip()
    pwd = os.environ.get("SIMANTAP_PASS") or getpass("Password: ")
    return user.split("@")[0], pwd  # SIMANTAP hanya menerima NIM


def log(msg):
    try:
        print(msg, flush=True)
    except Exception:
        clean = str(msg).encode("ascii", errors="replace").decode("ascii")
        print(clean, flush=True)


async def sedang_maintenance(page):
    try:
        judul = (await page.title()).lower()
        teks = (await page.inner_text("body")).lower()
    except Exception:
        return False
    return "pemeliharaan" in judul or "sedang dalam pemeliharaan" in teks


async def tunggu_maintenance(page):
    for menit in range(MAX_TUNGGU_MAINTENANCE_MENIT):
        if not await sedang_maintenance(page):
            return
        log(f"  Situs maintenance. Coba lagi 60 detik... ({menit + 1}/{MAX_TUNGGU_MAINTENANCE_MENIT})")
        await asyncio.sleep(60)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=25000)
        except Exception:
            pass
    raise RuntimeError("Situs masih maintenance setelah menunggu. Coba lagi nanti.")


# --------------------------------------------------------------------- login
async def launch_browser(p):
    log("Membuka Google Chrome...")
    try:
        return await p.chromium.launch(channel="chrome", headless=False, args=["--start-maximized"])
    except Exception:
        return await p.chromium.launch(headless=False, args=["--start-maximized"])


async def login(page, user, pwd):
    log(f"Membuka {BASE}")
    await page.goto(BASE, wait_until="domcontentloaded")
    await tunggu_maintenance(page)

    log(f"Login dengan NIM: {user}")
    f_user = page.locator('#identity, input[type="text"], input[placeholder*="Username" i]').first
    await f_user.wait_for(state="visible", timeout=15000)
    await f_user.fill(user)
    await page.locator('#password, input[type="password"]').first.fill(pwd)
    await page.locator('button.bg-unper, button[type="submit"], button:has-text("Masuk")').first.click()

    log("Menunggu 3 detik lalu refresh (mengatasi loading macet)...")
    await asyncio.sleep(3)
    try:
        await page.reload(wait_until="domcontentloaded", timeout=25000)
    except Exception as e:
        log(f"Catatan reload: {e}")
    await asyncio.sleep(2)

    if await page.locator('input[type="password"]').count() > 0:
        await asyncio.to_thread(
            input, "Masih di halaman login (password salah/CAPTCHA/OTP?). Selesaikan manual, lalu Enter..."
        )
    await tunggu_maintenance(page)
    log(f"Berhasil masuk ({page.url})")


async def buka_halaman_nilai(page):
    """Navigasi ke Data Nilai."""
    await tunggu_maintenance(page)
    if "/akademik/nilai" not in page.url:
        link = page.locator('a:has-text("Data Nilai"), a[href*="nilai"]').first
        if not await link.is_visible():
            menu = page.locator('a:has-text("AKADEMIK"), span:has-text("AKADEMIK")').first
            await menu.wait_for(state="visible", timeout=15000)
            await menu.click()
            await asyncio.sleep(1)
        await link.wait_for(state="visible", timeout=10000)
        await link.click()
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2)
        await tunggu_maintenance(page)


# ----------------------------------------------------------------- dropdown
def vselect_toggle(page):
    """Elemen pemicu dropdown Vue (v-select) Tahun Akademik."""
    return page.locator('.v-select .vs__dropdown-toggle, .vs__search').first


async def buka_dropdown(page):
    """Buka listbox dropdown jika belum terbuka."""
    menu = page.locator('.vs__dropdown-menu, ul[role="listbox"]').first
    if not await menu.is_visible():
        trig = vselect_toggle(page)
        await trig.wait_for(state="visible", timeout=15000)
        await trig.click()
        try:
            await menu.wait_for(state="visible", timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def ambil_semester(page):
    OUT_DIR.mkdir(exist_ok=True)
    daftar = []

    # 1) Komponen Vue v-select (SIMANTAP asli)
    v_trig = vselect_toggle(page)
    if await v_trig.count() > 0:
        try:
            await v_trig.wait_for(state="visible", timeout=15000)
            await buka_dropdown(page)
            options_locator = page.locator('.vs__dropdown-menu li, [role="listbox"] li, .vs__dropdown-option')
            count = await options_locator.count()
            for i in range(count):
                txt = (await options_locator.nth(i).inner_text()).strip()
                if re.search(r"\d{4}/\d{4}", txt):
                    daftar.append({"index": i, "text": " ".join(txt.split())})
            await page.keyboard.press("Escape")
            if daftar:
                return daftar
        except Exception as e:
            log(f"      (Peringatan v-select: {e})")

    # 2) Cadangan: <select> native / Select2
    sel = page.locator("select").filter(
        has=page.locator("option", has_text=re.compile(r"\d{4}/\d{4}"))
    ).first
    if await sel.count() > 0:
        teks = await sel.locator("option").all_inner_texts()
        daftar = [
            {"index": i, "text": " ".join(t.split())}
            for i, t in enumerate(teks)
            if re.search(r"\d{4}/\d{4}", t)
        ]
        if daftar:
            return daftar

    # Jika tidak ditemukan sama sekali
    (OUT_DIR / "debug_halaman.html").write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path=str(OUT_DIR / "debug_halaman.png"), full_page=True)
    raise RuntimeError(
        "Dropdown Tahun Akademik tidak ditemukan. "
        "HTML halaman disimpan di hasil_cek/debug_halaman.html"
    )


async def pilih_semester(page, opt):
    sebelum = await page.inner_text("body")
    berhasil = False

    # 1) Vue v-select
    v_trig = vselect_toggle(page)
    if await v_trig.count() > 0:
        try:
            await buka_dropdown(page)
            options = page.locator('.vs__dropdown-menu li, [role="listbox"] li, .vs__dropdown-option')
            count = await options.count()
            idx = opt["index"]
            if idx < count:
                item = options.nth(idx)
                await item.wait_for(state="visible", timeout=5000)
                await item.dispatch_event("mousedown")
                try:
                    await item.click(force=True, timeout=2000)
                except Exception:
                    pass
                await asyncio.sleep(0.3)
                await page.keyboard.press("Escape")
                berhasil = True
            else:
                await page.keyboard.press("Escape")
        except Exception as e:
            log(f"      (v-select klik gagal: {e}, mencoba cadangan...)")
            await page.keyboard.press("Escape")

    # 2) Cadangan: Select2 / native <select>
    if not berhasil:
        sel = page.locator("select").filter(
            has=page.locator("option", has_text=re.compile(r"\d{4}/\d{4}"))
        ).first
        if await sel.count() > 0:
            try:
                await sel.select_option(index=opt["index"], timeout=3000)
                berhasil = True
            except Exception:
                await sel.evaluate(JS_SET_SELECT, opt["index"])
                berhasil = True

    # Tunggu konten berubah (maks 6 dtk)
    await asyncio.sleep(1)
    try:
        await page.wait_for_function(
            "(old) => document.body.innerText !== old", arg=sebelum, timeout=6000
        )
    except PWTimeout:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except PWTimeout:
        pass
    await tunggu_data_selesai(page)


async def tunggu_data_selesai(page, timeout=30000):
    """SIMANTAP menampilkan 'Sedang mengambil data...' dulu; tunggu sampai hilang
    dan salah satu hasil akhir muncul (tabel nilai / pesan terkunci / kuesioner)."""
    await page.wait_for_timeout(800)
    try:
        await page.wait_for_function(
            """() => {
                const t = document.body.innerText.toLowerCase();
                if (t.includes('sedang mengambil data')) return false;
                return t.includes('belum bisa melihat nilai')
                    || t.includes('matakuliah')
                    || t.includes('kartu hasil studi')
                    || t.includes('ip semester')
                    || /kue?isioner/.test(t)
                    || t.includes('belum ada data')
                    || t.includes('tidak ada data');
            }""",
            timeout=timeout,
        )
    except PWTimeout:
        log("      (timeout menunggu data selesai dimuat)")
    await page.wait_for_timeout(500)


# --------------------------------------------------------------- klasifikasi
async def klasifikasi(page):
    teks = await page.inner_text("body")
    low = teks.lower()
    info = {}

    # 1) Terkunci: "Kamu belum bisa melihat Nilai, silahkan lengkapi persyaratan..."
    if "belum bisa melihat nilai" in low:
        baris = [b.strip() for b in teks.splitlines() if b.strip()]
        i = next(n for n, b in enumerate(baris) if "belum bisa melihat nilai" in b.lower())
        alasan = baris[i + 1:i + 7]
        gab = " ".join(alasan).lower()
        status = []
        if re.search(r"pembayaran|tunggakan|biaya|bayar", gab):
            status.append("BELUM BAYAR")
        if re.search(r"kue?isioner|angket|evaluasi", gab):
            status.append("BELUM ISI KUESIONER")
        if not status:
            status.append("TERKUNCI (LAINNYA)")
        return " + ".join(status), alasan, info, teks

    # 2) OK: tab hasil studi tampil
    if "sedang mengambil data" in low:
        return "MASIH LOADING", [], info, teks

    has_transkrip = "transkrip nilai" in low or "transkip nilai" in low
    if "kartu hasil studi" in low and has_transkrip and "matakuliah" not in low:
        potongan = [b.strip() for b in teks.splitlines() if b.strip()][-6:]
        return "TAB ADA, TABEL NILAI KOSONG", potongan, info, teks

    if "kartu hasil studi" in low and has_transkrip:
        for kunci, pola in {
            "semester": r"Semester:\s*(\d+)",
            "ip": r"IP Semester:\s*([\d.,]+)",
            "ipk": r"IPK(?:\s+Pada\s+Semester\s+Ini)?:\s*([\d.,]+)",
        }.items():
            m = re.search(pola, teks, re.M | re.I)
            if m:
                info[kunci] = m.group(1)
        return "OK", [], info, teks

    # 3) Kuesioner (tampilan lain)
    baris_k = [b.strip() for b in teks.splitlines() if re.search(r"kue?isioner", b, re.I)]
    if baris_k or await locator_kuesioner(page).count() > 0:
        return "BELUM ISI KUESIONER", baris_k[:5], info, teks

    # 4) Tidak dikenali
    potongan = [b.strip() for b in teks.splitlines() if b.strip()][8:16]
    return "TIDAK DIKENALI", potongan, info, teks


def locator_kuesioner(page):
    return page.locator("a, button").filter(has_text=re.compile(r"kue?isioner", re.I))


async def isi_kuesioner(page, auto):
    if await locator_kuesioner(page).count() == 0:
        log("      Tombol/link kuesioner tidak ditemukan di halaman ini.")
        return False
    await locator_kuesioner(page).first.click()
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(1500)

    jumlah = await page.evaluate(JS_FILL, [RATING_INDEX, TEXT_JAWABAN])
    log(f"      {jumlah} pertanyaan (radio) terisi.")
    if jumlah == 0:
        log("      Tidak ada radio button; form mungkin beda. Lewati.")
        return False

    if not auto:
        jawab = await asyncio.to_thread(
            input, "      Cek form di browser. Enter = kirim, ketik n = batal: "
        )
        if jawab.strip().lower() == "n":
            return False

    await page.locator(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Simpan"), button:has-text("Kirim")'
    ).first.click()
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(1500)
    return True


# ------------------------------------------------------------------ alur
def parse_pilihan(teks, total):
    teks = teks.strip()
    if not teks:
        return list(range(total))
    hasil = set()
    for bagian in teks.split(","):
        bagian = bagian.strip()
        if "-" in bagian:
            a, b = bagian.split("-", 1)
            hasil.update(range(int(a) - 1, int(b)))
        elif bagian:
            hasil.add(int(bagian) - 1)
    return sorted(i for i in hasil if 0 <= i < total)


async def tentukan_target(opts, args):
    if args.semester:
        target = []
        for o in opts:
            text_low = o["text"].lower()
            for s in args.semester:
                kata_kunci = s.lower().split()
                if all(k in text_low for k in kata_kunci):
                    target.append(o)
                    break
        return target
    if args.pilih:
        pilih = await asyncio.to_thread(
            input, "\nPilih nomor semester (contoh 1,3 atau 2-5). Enter = semua: "
        )
        return [opts[i] for i in parse_pilihan(pilih, len(opts))]
    return opts  # default: semua


async def proses_semester(page, opt, args):
    await pilih_semester(page, opt)
    status, alasan, info, teks = await klasifikasi(page)

    if "KUESIONER" in status and args.isi:
        for _ in range(MAX_KUESIONER_PER_SEMESTER):
            if not await isi_kuesioner(page, args.auto):
                break
            await buka_halaman_nilai(page)
            await pilih_semester(page, opt)
            status, alasan, info, teks = await klasifikasi(page)
            if "KUESIONER" not in status:
                break

    OUT_DIR.mkdir(exist_ok=True)
    nama = re.sub(r"[^\w]+", "_", opt["text"]).strip("_")
    await page.screenshot(path=str(OUT_DIR / f"{nama}.png"), full_page=True)
    (OUT_DIR / f"{nama}.txt").write_text(teks, encoding="utf-8")
    return status, alasan, info


def ringkas_info(info):
    bagian = []
    if "semester" in info:
        bagian.append(f"Semester {info['semester']}")
    if "ip" in info:
        bagian.append(f"IP {info['ip']}")
    if "ipk" in info:
        bagian.append(f"IPK {info['ipk']}")
    return " | ".join(bagian)


async def run_bot(args):
    user, pwd = get_credentials()
    log("=== Memulai Bot SIMANTAP v6 ===")

    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await browser.new_context(no_viewport=True)
        page = await context.new_page()
        page.set_default_timeout(20000)

        try:
            await login(page, user, pwd)
            await buka_halaman_nilai(page)

            opts = await ambil_semester(page)
            log("\nDaftar semester di dropdown:")
            for n, o in enumerate(opts, 1):
                log(f"  {n}. {o['text']}")

            target = await tentukan_target(opts, args)
            if not target:
                log("Tidak ada semester yang cocok.")
                return
            log(f"\nMulai cek {len(target)} semester...")

            hasil = []
            for n, opt in enumerate(target, 1):
                log(f"\n[{n}/{len(target)}] {opt['text']}")
                status, alasan, info = await proses_semester(page, opt, args)
                log(f"  -> {status}  {ringkas_info(info)}")
                for a in alasan:
                    log(f"       {a}")
                hasil.append((opt["text"], status, alasan, info))

            log("\n=============== RINGKASAN ===============")
            for nama, status, alasan, info in hasil:
                log(f"[{status}] {nama}  {ringkas_info(info)}")
                for a in alasan:
                    log(f"      {a}")
            log(f"\nScreenshot + teks halaman per semester: {OUT_DIR.resolve()}")

            await asyncio.to_thread(input, "\nTekan Enter untuk menutup browser...")

        except Exception as e:
            log(f"Terjadi kesalahan: {e}")
            if not page.is_closed():
                OUT_DIR.mkdir(exist_ok=True)
                await page.screenshot(path="error.png", full_page=True)
                log("Screenshot error disimpan di error.png")
            await asyncio.to_thread(input, "Tekan Enter untuk menutup browser...")
        finally:
            try:
                await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Bot cek nilai/kuesioner SIMANTAP")
    ap.add_argument("--semester", action="append",
                    help='semester tertentu, contoh: "2025/2026 Genap" (boleh diulang)')
    ap.add_argument("--pilih", action="store_true", help="tampilkan menu untuk memilih semester")
    ap.add_argument("--isi", action="store_true", help="isi kuesioner yang belum diisi")
    ap.add_argument("--auto", action="store_true", help="kirim kuesioner tanpa konfirmasi")
    try:
        asyncio.run(run_bot(ap.parse_args()))
    except KeyboardInterrupt:
        log("\nBot dihentikan oleh pengguna.")