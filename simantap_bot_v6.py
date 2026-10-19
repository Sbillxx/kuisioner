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

# Kredensial default (Isi di sini jika tidak ingin selalu mengetik saat bot dijalankan di terminal)
DEFAULT_NIM = "2302010036"       # Contoh: "2302010036"
DEFAULT_PASSWORD = "kth301295"  # Password akun SIMANTAP kamu

TEXT_JAWABAN = "baik"  # isi textarea / isian saran kuesioner
MAX_KUESIONER_PER_SEMESTER = 30
MAX_TUNGGU_MAINTENANCE_MENIT = 30

JS_FILL_KUESIONER = """
() => {
  let radiosCount = 0;
  let buttonsCount = 0;
  let textareasCount = 0;

  const modal = document.querySelector('.modal.show, .modal-dialog, [role="dialog"], .modal') || document.body;

  function trigger(el) {
    if (!el) return;
    el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
    el.click();
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }

  // 1. Textareas (contoh: "Saran untuk perkuliahan", "Saran")
  const textareas = modal.querySelectorAll('textarea');
  textareas.forEach(ta => {
    ta.value = "baik";
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.dispatchEvent(new Event('change', { bubbles: true }));
    if (window.jQuery) {
      try { window.jQuery(ta).trigger('input').trigger('change'); } catch(e) {}
    }
    textareasCount++;
  });

  // 2. Radio buttons standar (<input type="radio">)
  const radios = Array.from(modal.querySelectorAll('input[type="radio"]'));
  if (radios.length > 0) {
    const groups = {};
    radios.forEach(r => {
      const name = r.name || ('group_' + Math.random());
      groups[name] = groups[name] || [];
      groups[name].push(r);
    });

    for (const name in groups) {
      const list = groups[name];
      // Cari radio opsi 3 ("Baik")
      let target = list.find(r => r.value === "3" || (r.value && r.value.toLowerCase() === "baik"));
      if (!target) {
        target = list.find(r => {
          const lbl = r.closest('label') || document.querySelector(`label[for="${r.id}"]`);
          const txt = lbl ? (lbl.innerText || '').trim() : '';
          return txt.includes('Baik') && !txt.includes('Kurang') && !txt.includes('Cukup') && !txt.includes('Sangat');
        });
      }
      if (!target && list.length >= 4) {
        target = list[2]; // index 2 = opsi ke-3 ("3 Baik")
      }
      if (target) {
        target.checked = true;
        trigger(target);
        const parentBtn = target.closest('label, .btn, .btn-group');
        if (parentBtn) {
          parentBtn.classList.add('active');
          trigger(parentBtn);
        }
        radiosCount++;
      }
    }
  }

  // 3. Opsi pilihan berupa Button / Label / Div per baris tabel pertanyaan
  const rows = modal.querySelectorAll('table tbody tr, tr');
  rows.forEach(tr => {
    const td = tr.querySelector('td:last-child') || tr;
    const candidates = Array.from(td.querySelectorAll('button, label, [role="button"], .btn, div[class*="btn"], span[class*="btn"], div, span'));

    let optBaik = candidates.find(el => {
      const txt = (el.innerText || '').trim();
      if (txt === 'Baik' || txt === '3 Baik' || txt === '3\\nBaik') return true;
      return txt.includes('Baik') && !txt.includes('Kurang') && !txt.includes('Cukup') && !txt.includes('Sangat') && txt.length <= 25;
    });

    if (!optBaik && candidates.length === 4) {
      optBaik = candidates[2];
    }

    if (optBaik) {
      trigger(optBaik);
      buttonsCount++;
    }
  });

  // 4. Sapu bersih: tombol/label di dalam modal yang mewakili opsi "3" / "Baik"
  const allBaik = Array.from(modal.querySelectorAll('button, label, .btn, [role="button"]')).filter(el => {
    if (el.tagName === 'INPUT') return false;
    const txt = (el.innerText || '').trim();
    if (/simpan|batal|tutup/i.test(txt)) return false;
    return txt === 'Baik' || txt === '3 Baik' || txt === '3\\nBaik' ||
           (txt.includes('Baik') && !txt.includes('Kurang') && !txt.includes('Cukup') && !txt.includes('Sangat') && txt.length <= 15);
  });
  allBaik.forEach(el => {
    try {
      trigger(el);
    } catch(e) {}
  });

  return {
    radios: radiosCount,
    buttons: buttonsCount,
    textareas: textareasCount,
    totalBaik: allBaik.length
  };
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
def get_credentials(args=None):
    user = (
        (getattr(args, "nim", None) or getattr(args, "user", None))
        or DEFAULT_NIM
        or os.environ.get("SIMANTAP_USER")
        or input("NIM: ").strip()
    )
    pwd = (
        (getattr(args, "pwd", None) or getattr(args, "password", None))
        or DEFAULT_PASSWORD
        or os.environ.get("SIMANTAP_PASS")
        or getpass("Password: ")
    )
    return str(user).split("@")[0].strip(), str(pwd)


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
    is_headless = os.environ.get("HEADLESS", "").lower() in ("1", "true", "yes") or (
        sys.platform != "win32" and not os.environ.get("DISPLAY")
    )
    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]
    if not is_headless:
        args.append("--start-maximized")

    log(f"Membuka browser (headless={is_headless})...")
    if not is_headless:
        try:
            return await p.chromium.launch(channel="chrome", headless=False, args=args)
        except Exception:
            pass
    return await p.chromium.launch(headless=is_headless, args=args)


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

    # 3) Kuesioner (tampilan tabel kuesioner)
    btn_kuesioner = await dapatkan_tombol_kuesioner(page)
    jml_tombol = await btn_kuesioner.count()

    # Hitung jumlah baris mata kuliah pada tabel kuesioner
    kuesioner_tbl = page.locator('table').filter(
        has=page.locator('th, td', has_text=re.compile(r"matakuliah|dosen|jenis", re.I))
    ).first

    jml_mk = 0
    if await kuesioner_tbl.count() > 0:
        baris_mk = kuesioner_tbl.locator('tbody tr').filter(
            has=page.locator('button, a').filter(has_text=re.compile(r"teori|praktik", re.I))
        )
        jml_mk = await baris_mk.count()
        if jml_mk == 0:
            jml_mk = await kuesioner_tbl.locator('tbody tr').count()
    if jml_mk == 0:
        jml_mk = jml_tombol

    baris_k = [b.strip() for b in teks.splitlines() if re.search(r"kue?isioner", b, re.I)]
    if jml_tombol > 0 or baris_k:
        if jml_mk > 0:
            if jml_mk == jml_tombol:
                alasan = [f"Ada {jml_mk} kuesioner yang harus diisi"]
            else:
                alasan = [f"Ada {jml_mk} mata kuliah ({jml_tombol} kuesioner Teori/Praktikum) yang harus diisi"]
        else:
            alasan = baris_k[:5] if baris_k else ["Kuesioner belum diisi"]
        return "BELUM ISI KUESIONER", alasan, info, teks

    # 4) Tidak dikenali
    potongan = [b.strip() for b in teks.splitlines() if b.strip()][8:16]
    return "TIDAK DIKENALI", potongan, info, teks


async def dapatkan_tombol_kuesioner(page):
    """Mencari semua tombol kuesioner ('Teori' / 'Praktikum') di tabel Kuesioner SIMANTAP."""
    # Beri jeda kecil agar Vue selesai me-render seluruh baris tabel
    try:
        await page.wait_for_selector('table tbody tr td, .table tbody tr td', timeout=3000)
    except Exception:
        pass
    await page.wait_for_timeout(400)

    # 1. Cari di dalam tabel kuesioner (prioritas utama)
    kuesioner_tbl = page.locator('table').filter(
        has=page.locator('th, td', has_text=re.compile(r"matakuliah|dosen|jenis", re.I))
    ).first
    if await kuesioner_tbl.count() > 0:
        tbl_btn = kuesioner_tbl.locator('button, a.btn, a').filter(
            has_text=re.compile(r"^\s*(?:teori|praktik|praktikum)\s*$", re.I)
        )
        if await tbl_btn.count() > 0:
            return tbl_btn

    # 2. Cari tombol/link yang teksnya persis 'Teori' atau 'Praktik' / 'Praktikum' di luar modal
    exact_btn = page.locator('button, a.btn, a').filter(
        has_text=re.compile(r"^\s*(?:teori|praktik|praktikum|isi kuesioner)\s*$", re.I)
    )
    if await exact_btn.count() > 0:
        return exact_btn

    return page.locator('button, a').filter(has_text=re.compile(r"kue?isioner", re.I))


def locator_kuesioner(page):
    """Alias kompatibilitas untuk locator tombol kuesioner."""
    return page.locator(
        'table tr td:last-child button, table tr td:last-child a, '
        'button:has-text("Teori"), a:has-text("Teori")'
    )


async def isi_kuesioner(page, auto=True):
    """
    Mengisi form kuesioner evaluasi dosen/perkuliahan:
    1. Klik tombol 'Teori' di baris kuesioner pertama jika modal belum terbuka.
    2. Tunggu modal 'Form Kuesioner' terbuka.
    3. Pilih opsi '3' ('Baik') untuk semua pertanyaan pilihan ganda.
    4. Isi semua isian esai / saran / textarea dengan 'baik'.
    5. Klik tombol 'Simpan Kuesioner'.
    6. Tunggu dialog konfirmasi & modal tertutup.
    """
    modal_box = page.locator('.modal.show, .modal:visible, [role="dialog"]:visible, div.modal').first
    modal_heading = page.get_by_text("Form Kuesioner").or_(page.get_by_text("Semua Kuesioner Wajib Diisi"))

    # Cek apakah modal sudah terbuka
    is_open = False
    try:
        if await modal_heading.count() > 0 and await modal_heading.first.is_visible():
            is_open = True
        elif await modal_box.count() > 0 and await modal_box.is_visible():
            is_open = True
    except Exception:
        pass

    # Jika modal belum terbuka, klik tombol Teori pertama di tabel
    if not is_open:
        tombols = await dapatkan_tombol_kuesioner(page)
        if await tombols.count() == 0:
            log("      Tombol kuesioner (Teori) tidak ditemukan di halaman ini.")
            return False
        btn = tombols.first
        await btn.scroll_into_view_if_needed()
        try:
            await btn.click(timeout=5000)
        except Exception:
            await btn.click(force=True)
        log("      Membuka Form Kuesioner...")

    try:
        await modal_heading.first.wait_for(state="visible", timeout=15000)
        log("      Form Kuesioner berhasil terbuka.")
    except Exception:
        if await modal_box.count() > 0 and await modal_box.is_visible():
            log("      Modal kuesioner terdeteksi terbuka.")
        else:
            log("      Modal Form Kuesioner tidak terbuka dalam batas waktu.")
            return False

    await page.wait_for_timeout(1000)

    # 1. Isi semua pilihan opsi 'Baik' dan textarea 'baik' via JS
    hasil_fill = await page.evaluate(JS_FILL_KUESIONER)
    log(f"      Pilihan opsi 'Baik' dan saran terisi: {hasil_fill}")
    await page.wait_for_timeout(500)

    # 2. Playwright native fallback: klik semua tombol opsi '3' / 'Baik' langsung
    modal_scope = modal_box if await modal_box.count() > 0 else page
    baik_btns = modal_scope.locator('button, label, .btn, [role="button"], div, span').filter(
        has_text=re.compile(r"^\s*(?:3\s+)?Baik\s*$", re.I)
    )
    count_baik = await baik_btns.count()
    if count_baik > 0:
        for i in range(count_baik):
            try:
                await baik_btns.nth(i).click(force=True, timeout=800)
            except Exception:
                pass

    # 3. Pastikan semua textarea benar-benar terisi teks 'baik'
    tas = page.locator('.modal textarea, textarea')
    ta_count = await tas.count()
    for i in range(ta_count):
        try:
            t = tas.nth(i)
            val = await t.input_value()
            if not val or val.strip() == "":
                await t.fill("baik")
        except Exception:
            pass

    if not auto:
        jawab = await asyncio.to_thread(
            input, "      Periksa form di browser. Tekan Enter untuk simpan, atau ketik n lalu Enter untuk batal: "
        )
        if jawab.strip().lower() == "n":
            log("      Pengisian dibatalkan pengguna.")
            try:
                await page.locator('button:has-text("Batal"), .modal .close, button.close').first.click()
            except Exception:
                pass
            return False

    # Scroll modal ke paling bawah agar tombol 'Simpan Kuesioner' terlihat
    await page.evaluate("""() => {
        const m = document.querySelector('.modal.show, .modal, [role="dialog"]') || document.body;
        m.scrollTop = m.scrollHeight;
        window.scrollTo(0, document.body.scrollHeight);
    }""")
    await page.wait_for_timeout(600)

    btn_simpan = page.locator(
        'button:has-text("Simpan Kuesioner"), '
        'a:has-text("Simpan Kuesioner"), '
        'button:has-text("Simpan"), '
        'input[value*="Simpan"]'
    ).first

    try:
        await btn_simpan.wait_for(state="visible", timeout=8000)
        await btn_simpan.scroll_into_view_if_needed()
        try:
            await btn_simpan.click(timeout=3000)
        except Exception:
            await btn_simpan.click(force=True)
        log("      Tombol 'Simpan Kuesioner' diklik.")
    except Exception as e:
        log(f"      Gagal mengklik tombol Simpan Kuesioner: {e}")
        return False

    # Tangani popup konfirmasi SweetAlert jika ada
    await page.wait_for_timeout(1000)
    swal = page.locator(
        '.swal2-confirm, '
        'button:has-text("Ya, Simpan"), '
        'button:has-text("Ya, Yakin"), '
        'button:has-text("Ya"), '
        'button:has-text("OK")'
    ).first
    if await swal.count() > 0:
        try:
            if await swal.is_visible():
                await swal.click()
                log("      Konfirmasi SweetAlert diklik.")
        except Exception:
            pass

    # Tunggu modal tertutup
    try:
        await modal_heading.first.wait_for(state="hidden", timeout=15000)
        log("      Modal kuesioner berhasil ditutup & disimpan.")
    except Exception:
        pass

    await page.wait_for_timeout(2000)
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
        log(f"      Mendeteksi kuesioner yang harus diisi untuk {opt['text']}...")
        for no in range(1, MAX_KUESIONER_PER_SEMESTER + 1):
            tombols = await dapatkan_tombol_kuesioner(page)
            sisa = await tombols.count()
            if sisa == 0:
                log("      Semua kuesioner pada tabel telah selesai diisi!")
                break
            log(f"      [{no}] Mengisi kuesioner (sisa {sisa} kuesioner)...")
            sukses = await isi_kuesioner(page, args.auto)
            if not sukses:
                log("      Pengisian kuesioner dihentikan atau tidak berhasil.")
                break
            await page.wait_for_timeout(2000)

        # Setelah semua kuesioner selesai diisi, perbarui data nilai
        log("      Memperbarui data nilai setelah pengisian kuesioner...")
        await page.wait_for_timeout(1500)
        status, alasan, info, teks = await klasifikasi(page)
        if "KUESIONER" in status:
            await pilih_semester(page, opt)
            status, alasan, info, teks = await klasifikasi(page)

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
    user, pwd = get_credentials(args)
    log("=== Memulai Bot SIMANTAP v6 ===")

    async with async_playwright() as p:
        browser = await launch_browser(p)
        context = await browser.new_context(no_viewport=True)
        page = await context.new_page()
        page.set_default_timeout(20000)
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

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
    ap.add_argument("--nim", "--user", help='NIM untuk login SIMANTAP (contoh: "2302010036")')
    ap.add_argument("--pwd", "--pass", help="Password untuk login SIMANTAP")
    ap.add_argument("--semester", action="append",
                    help='semester tertentu, contoh: "2025/2026 Genap" (boleh diulang)')
    ap.add_argument("--pilih", action="store_true", help="tampilkan menu untuk memilih semester")
    ap.add_argument("--isi", action="store_true", default=True, help="isi kuesioner yang belum diisi (default: aktif)")
    ap.add_argument("--no-isi", action="store_false", dest="isi", help="jangan isi kuesioner otomatis")
    ap.add_argument("--auto", action="store_true", default=True, help="kirim kuesioner tanpa konfirmasi (default: aktif)")
    ap.add_argument("--konfirmasi", action="store_false", dest="auto", help="minta konfirmasi sebelum simpan kuesioner")
    try:
        asyncio.run(run_bot(ap.parse_args()))
    except KeyboardInterrupt:
        log("\nBot dihentikan oleh pengguna.")