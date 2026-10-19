#!/usr/bin/env python3
"""
SIMANTAP Web - dashboard lokal untuk bot cek nilai/kuesioner.

Pakai:
  1. Taruh file ini di folder yang SAMA dengan simantap_bot_v6.py (versi yang works).
  2. python simantap_web.py            (atau: python simantap_web.py path/ke/bot.py)
  3. Browser terbuka di http://127.0.0.1:8765

Tanpa library tambahan selain playwright. Server hanya aktif di komputer kamu (127.0.0.1);
NIM dan password hanya dipakai untuk login dan tidak disimpan ke disk.
"""
import asyncio
import importlib.util
import json
import re
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
PORT = 8765


def load_core():
    cands = [Path(a) for a in sys.argv[1:] if a.endswith(".py")] + [
        HERE / "simantap_bot_v6.py",
        HERE / "main2.py",
        HERE / "main.py",
    ]
    for c in cands:
        if c.exists():
            sys.path.insert(0, str(c.resolve().parent))
            spec = importlib.util.spec_from_file_location("simantap_core", c)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    sys.exit("File bot tidak ditemukan. Taruh simantap_web.py di folder yang sama dengan "
             "simantap_bot_v6.py atau main2.py, atau jalankan: python simantap_web.py path/ke/bot.py")


core = load_core()
core.OUT_DIR = HERE / "hasil_cek"
from playwright.async_api import async_playwright  # noqa: E402


def clean_output_dir():
    """Mengosongkan isi folder hasil_cek agar data sesi sebelumnya bersih."""
    try:
        if core.OUT_DIR.exists():
            for item in core.OUT_DIR.iterdir():
                try:
                    if item.is_file() or item.is_symlink():
                        item.unlink()
                    elif item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                except Exception:
                    pass
        core.OUT_DIR.mkdir(exist_ok=True)
    except Exception as e:
        print(f"Catatan clean_output_dir: {e}", flush=True)


class Job:
    def __init__(self):
        self.ev = threading.Event()
        self.session_id = uuid.uuid4().hex[:8]
        self.reset(clean_files=False)

    def reset(self, clean_files=False):
        self.phase, self.message, self.error = "idle", "", None
        self.semesters, self.targets, self.results = [], [], []
        self.done, self.current, self.prompt = 0, None, None
        self.cancelled = False
        self.logs = deque(maxlen=300)
        self.loop = self.q = self.browser = None
        self.ev.clear()
        if clean_files:
            clean_output_dir()

    def log(self, msg):
        self.logs.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        try:
            print(msg, flush=True)
        except Exception:
            pass

    def state(self):
        return dict(session_id=self.session_id, phase=self.phase, message=self.message, error=self.error,
                    semesters=self.semesters, targets=[t["text"] for t in self.targets],
                    results=self.results, done=self.done, current=self.current,
                    prompt=self.prompt, log=list(self.logs)[-120:])

    # ---------------------------------------------------------- worker (thread)
    def start(self, nim, pwd):
        self.stop()
        self.session_id = uuid.uuid4().hex[:8]
        self.reset(clean_files=True)
        self.phase, self.message = "connecting", f"Sesi #{self.session_id}: Membuka Chrome dan login..."
        self.log(f"=== Sesi Baru #{self.session_id} Dimulai ===")
        self.log("Folder hasil_cek telah dikosongkan untuk sesi ini.")
        threading.Thread(target=lambda: asyncio.run(self.main(nim, pwd, self.session_id)), daemon=True).start()

    async def main(self, nim, pwd, session_id):
        self.loop, self.q = asyncio.get_running_loop(), asyncio.Queue()
        page = None
        browser = None
        try:
            async with async_playwright() as p:
                browser = await core.launch_browser(p)
                if self.session_id != session_id:
                    await browser.close()
                    return
                self.browser = browser
                ctx = await browser.new_context(no_viewport=True)
                page = await ctx.new_page()
                page.set_default_timeout(20000)
                page.on("dialog", lambda d: asyncio.create_task(d.accept()))
                await core.login(page, nim, pwd)
                if self.session_id != session_id:
                    return
                await core.buka_halaman_nilai(page)
                if self.session_id != session_id:
                    return
                self.semesters = await core.ambil_semester(page)
                self.phase, self.message = "ready", "Pilih semester yang mau dicek."
                while True:
                    cmd = await self.q.get()
                    if self.session_id != session_id or cmd["type"] == "close":
                        break
                    elif cmd["type"] == "fill_single":
                        await self.run_fill_single(page, cmd["semester"])
                    else:
                        await self.run_checks(page, cmd)
        except Exception as e:
            if not self.cancelled and self.session_id == session_id:
                self.error, self.phase = str(e), "error"
                self.log(f"Error: {e}")
                try:
                    core.OUT_DIR.mkdir(exist_ok=True)
                    if page and not page.is_closed():
                        await page.screenshot(path=str(core.OUT_DIR / "error.png"), full_page=True)
                except Exception:
                    pass
        finally:
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            if self.browser is browser:
                self.browser = None
            if self.session_id == session_id and self.phase != "error":
                self.phase, self.message = "idle", "Sesi ditutup."

    async def run_checks(self, page, cmd):
        args = SimpleNamespace(isi=cmd["isi"], auto=True, semester=None, pilih=False)
        for n, opt in enumerate(self.targets, 1):
            self.current = opt["text"]
            self.log(f"[{n}/{len(self.targets)}] {opt['text']}")
            try:
                status, alasan, info = await core.proses_semester(page, opt, args)
            except Exception as e:
                if self.cancelled:
                    return
                status, alasan, info = "GAGAL", [str(e)], {}
            shot = re.sub(r"[^\w]+", "_", opt["text"]).strip("_") + ".png"
            self.results.append(dict(text=opt["text"], status=status, alasan=alasan,
                                     info=info, shot=shot))
            self.done = n
            self.log(f"  -> {status}")
        self.current, self.phase = None, "done"

    async def run_fill_single(self, page, semester_text):
        opt = next((s for s in self.semesters if s["text"] == semester_text), None)
        if not opt:
            self.log(f"Semester {semester_text} tidak ditemukan.")
            self.phase = "done"
            return

        self.current = opt["text"]
        self.phase = "running"
        self.log(f"=== Mengisi kuesioner untuk {opt['text']} ===")
        args = SimpleNamespace(isi=True, auto=True, semester=None, pilih=False)
        try:
            status, alasan, info = await core.proses_semester(page, opt, args)
        except Exception as e:
            if self.cancelled:
                return
            status, alasan, info = "GAGAL", [str(e)], {}

        shot = re.sub(r"[^\w]+", "_", opt["text"]).strip("_") + ".png"
        updated = False
        for r in self.results:
            if r["text"] == opt["text"]:
                r["status"] = status
                r["alasan"] = alasan
                r["info"] = info
                r["shot"] = shot
                updated = True
                break
        if not updated:
            self.results.append(dict(text=opt["text"], status=status, alasan=alasan, info=info, shot=shot))

        self.log(f"  -> Selesai mengisi: {status}")
        self.current = None
        self.phase = "done"

    def stop(self):
        self.cancelled = True
        self.ev.set()
        if self.loop and self.q:
            asyncio.run_coroutine_threadsafe(self.q.put({"type": "close"}), self.loop)
        if self.loop and self.browser:
            asyncio.run_coroutine_threadsafe(self.browser.close(), self.loop)


JOB = Job()
core.log = JOB.log


def web_input(prompt=""):
    """Pengganti input() di bot: minta user lanjut lewat tombol di web."""
    JOB.prompt = str(prompt).strip() or "Selesaikan langkah di jendela Chrome, lalu lanjut."
    JOB.ev.clear()
    JOB.ev.wait()
    JOB.prompt = None
    return ""


core.input = web_input


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body, ctype="application/json", code=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.reply(HTML, "text/html")
        elif path == "/api/state":
            self.reply(JOB.state())
        elif path == "/api/reset":
            JOB.stop()
            JOB.session_id = uuid.uuid4().hex[:8]
            JOB.reset(clean_files=True)
            self.reply({"ok": True, "session_id": JOB.session_id})
        elif path.startswith("/shots/"):
            f = core.OUT_DIR / Path(unquote(path[7:])).name
            if f.suffix == ".png" and f.exists():
                self.reply(f.read_bytes(), "image/png")
            else:
                self.reply({"error": "tidak ada"}, code=404)
        else:
            self.reply({"error": "tidak ada"}, code=404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            data = {}
        path = self.path
        if path == "/api/connect" and JOB.phase in ("idle", "error"):
            nim, pwd = str(data.get("nim", "")).strip().split("@")[0], str(data.get("password", ""))
            if not nim or not pwd:
                return self.reply({"ok": False, "error": "NIM dan password wajib diisi."}, code=400)
            JOB.start(nim, pwd)
        elif path == "/api/run" and JOB.phase in ("ready", "done"):
            idx = [i for i in data.get("indices", []) if isinstance(i, int) and 0 <= i < len(JOB.semesters)]
            if not idx:
                return self.reply({"ok": False, "error": "Pilih minimal satu semester."}, code=400)
            JOB.targets = [JOB.semesters[i] for i in sorted(idx)]
            JOB.results, JOB.done, JOB.phase = [], 0, "running"
            asyncio.run_coroutine_threadsafe(
                JOB.q.put({"type": "run", "isi": bool(data.get("isi"))}), JOB.loop)
        elif path == "/api/continue":
            JOB.ev.set()
        elif path == "/api/fill_single" and JOB.phase in ("ready", "done"):
            sem = str(data.get("semester", "")).strip()
            if not sem:
                return self.reply({"ok": False, "error": "Nama semester tidak valid."}, code=400)
            JOB.phase = "running"
            asyncio.run_coroutine_threadsafe(
                JOB.q.put({"type": "fill_single", "semester": sem}), JOB.loop
            )
            return self.reply({"ok": True})
        elif path == "/api/skip_single":
            sem = str(data.get("semester", "")).strip()
            for r in JOB.results:
                if r["text"] == sem:
                    r["status"] = "DILEWATI"
                    r["alasan"] = ["Kuesioner dilewati oleh pengguna."]
                    break
            return self.reply({"ok": True})
        elif path in ("/api/stop", "/api/reset"):
            JOB.stop()
            JOB.session_id = uuid.uuid4().hex[:8]
            JOB.reset(clean_files=True)
            return self.reply({"ok": True, "session_id": JOB.session_id})
        else:
            return self.reply({"ok": False, "error": "Aksi tidak tersedia saat ini."}, code=409)
        self.reply({"ok": True})


HTML = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SIMANTAP Checker</title>
<style>
:root{
  --bg:#edf2ef;--panel:#fbfdfc;--ink:#10231c;--mut:#62746b;--line:#d7e0db;
  --brand:#006a4e;--brand-ink:#fff;--ok:#0e8f62;--pay:#c8741a;--quiz:#6f52cc;--warn:#76848c;
  --ok-bg:#dff3ea;--pay-bg:#fbe8d2;--quiz-bg:#e9e3fa;--warn-bg:#e6eaed;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  --sans:"Segoe UI Variable","Segoe UI",Inter,system-ui,-apple-system,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0c1713;--panel:#13231d;--ink:#e6f1ec;--mut:#8fa49a;--line:#244036;
  --brand:#2fbf93;--brand-ink:#06251b;--ok:#3ccf9c;--pay:#e6993f;--quiz:#9b83ec;--warn:#93a2ab;
  --ok-bg:#123a2d;--pay-bg:#3b2a12;--quiz-bg:#2a2250;--warn-bg:#1f2d33;}}
*{box-sizing:border-box}
html{color-scheme:light dark}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 var(--sans)}
[hidden]{display:none!important}
button,input{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 64px}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px}
.brand{display:flex;align-items:center;gap:10px;font-weight:650;letter-spacing:.01em}
.mark{width:28px;height:28px;border-radius:9px;background:var(--brand);display:grid;place-items:center}
.mark i{width:10px;height:10px;border:2px solid var(--brand-ink);border-radius:50%;display:block}
.pill{font-size:13px;padding:4px 12px;border-radius:99px;background:var(--panel);border:1px solid var(--line);color:var(--mut)}
.pill[data-p=running],.pill[data-p=connecting]{color:var(--brand);border-color:var(--brand)}
.pill[data-p=error]{color:var(--pay);border-color:var(--pay)}
main{display:grid;grid-template-columns:320px 1fr;gap:36px;align-items:start}
@media(max-width:860px){main{grid-template-columns:1fr}}
aside{position:sticky;top:20px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px}
@media(max-width:860px){aside{position:static}}
aside h2{font:600 15px var(--sans);margin:0 0 4px}
aside p.hint{margin:0 0 16px;color:var(--mut);font-size:13px}
label.f{display:block;font-size:13px;color:var(--mut);margin:12px 0 5px}
input[type=text],input[type=password]{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--bg)}
.btn{width:100%;margin-top:16px;padding:11px 14px;border:0;border-radius:10px;background:var(--brand);color:var(--brand-ink);font-weight:600;cursor:pointer}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.ghost{background:transparent;color:var(--mut);border:1px solid var(--line);margin-top:8px;font-weight:500}
.err{margin-top:12px;padding:10px 12px;border-radius:10px;background:var(--pay-bg);color:var(--pay);font-size:13px;word-break:break-word}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 4px}
.chip{border:1px solid var(--line);background:var(--bg);border-radius:8px;padding:5px 9px;font-size:12.5px;cursor:pointer;color:var(--mut)}
.chip.on{background:var(--brand);border-color:var(--brand);color:var(--brand-ink)}
.row{display:flex;justify-content:space-between;align-items:center}
.lnk{background:none;border:0;color:var(--brand);cursor:pointer;padding:0;font-size:13px}
.chk{display:flex;gap:8px;align-items:flex-start;margin-top:14px;font-size:13px;color:var(--mut)}
.spin{width:18px;height:18px;border:2px solid var(--line);border-top-color:var(--brand);border-radius:50%;animation:s 1s linear infinite;display:inline-block;vertical-align:-4px;margin-right:8px}
@keyframes s{to{transform:rotate(360deg)}}
.btn-act{display:inline-flex;align-items:center;gap:6px;padding:6px 13px;border-radius:8px;font-size:12.5px;font-weight:600;cursor:pointer;border:1px solid transparent;transition:all .15s}
.btn-act.fill{background:var(--brand);color:var(--brand-ink)}
.btn-act.fill:hover{filter:brightness(1.08)}
.btn-act.skip{background:transparent;border-color:var(--line);color:var(--mut)}
.btn-act.skip:hover{background:var(--bg);color:var(--ink)}
.btn-act:disabled{opacity:.5;cursor:not-allowed}
h1{font:500 clamp(28px,4vw,40px)/1.15 var(--serif);margin:0 0 8px;letter-spacing:-.01em}
.sub{color:var(--mut);margin:0 0 22px;max-width:56ch}
.bar{height:6px;background:var(--line);border-radius:9px;overflow:hidden;margin:0 0 26px}
.bar b{display:block;height:100%;background:var(--brand);width:0;transition:width .4s}
.rail{list-style:none;margin:0;padding:0;position:relative}
.rail:before{content:"";position:absolute;left:11px;top:8px;bottom:8px;width:2px;background:var(--line)}
.node{position:relative;padding:0 0 22px 40px}
.dot{position:absolute;left:3px;top:4px;width:18px;height:18px;border-radius:50%;background:var(--bg);border:2px solid var(--line)}
.node.k-ok .dot{background:var(--ok);border-color:var(--ok)}
.node.k-pay .dot{background:var(--pay);border-color:var(--pay)}
.node.k-quiz .dot{background:var(--quiz);border-color:var(--quiz)}
.node.k-warn .dot{background:var(--warn);border-color:var(--warn)}
.node.now .dot{border-color:var(--brand);box-shadow:0 0 0 5px color-mix(in srgb,var(--brand) 22%,transparent)}
.node h3{font:500 19px/1.3 var(--serif);margin:0}
.head{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center}
.b{font-size:12.5px;padding:2px 9px;border-radius:99px;font-weight:600}
.b.ok{background:var(--ok-bg);color:var(--ok)}.b.pay{background:var(--pay-bg);color:var(--pay)}
.b.quiz{background:var(--quiz-bg);color:var(--quiz)}.b.warn{background:var(--warn-bg);color:var(--warn)}
.meta{display:flex;gap:16px;color:var(--mut);font-size:13.5px;margin-top:3px}
.why{margin:8px 0 0;padding:10px 12px;background:var(--panel);border:1px solid var(--line);border-radius:10px;font-size:13.5px;list-style:none;max-width:62ch}
.why li+li{margin-top:4px}
.pending{color:var(--mut);font-size:13.5px;margin-top:2px}
details{margin-top:10px}
summary{cursor:pointer;color:var(--mut);font-size:13px}
pre{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;max-height:240px;overflow:auto;font:12px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap;color:var(--mut)}
dialog{border:1px solid var(--line);border-radius:16px;background:var(--panel);color:var(--ink);padding:22px;max-width:min(92vw,960px)}
dialog::backdrop{background:rgba(6,20,15,.55)}
dialog img{max-width:100%;border-radius:8px;display:block;margin-bottom:12px}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="brand"><span class="mark"><i></i></span>SIMANTAP Checker</div>
  <div style="display:flex;align-items:center;gap:10px">
    <span class="pill" id="sessPill" style="font-family:monospace;font-size:12px">Sesi: -</span>
    <span class="pill" id="pill" data-p="idle">Belum terhubung</span>
    <button class="lnk" id="btnReset" title="Kosongkan hasil_cek dan mulai sesi baru" style="font-size:12.5px;padding:4px 10px;border:1px solid var(--line);border-radius:99px;background:var(--panel);color:var(--mut);cursor:pointer;display:inline-flex;align-items:center;gap:4px">🔄 Sesi Baru</button>
  </div>
</header>
<main>
<aside>
  <section id="login">
    <h2>Masuk ke SIMANTAP</h2>
    <p class="hint">Chrome akan terbuka di komputer kamu. Data login tidak disimpan.</p>
    <label class="f" for="nim">NIM</label>
    <input id="nim" type="text" autocomplete="username" placeholder="2303010252">
    <label class="f" for="pwd">Password</label>
    <input id="pwd" type="password" autocomplete="current-password">
    <button class="btn" id="go">Masuk dan ambil semester</button>
    <div class="err" id="err" hidden></div>
  </section>
  <section id="busy" hidden><span class="spin"></span><span id="busyMsg"></span></section>
  <section id="work" hidden>
    <div class="row"><h2>Semester</h2><button class="lnk" id="all">Pilih semua</button></div>
    <div class="chips" id="chips"></div>
    <label class="chk"><input type="checkbox" id="isi" checked>
      <span>Isi kuesioner otomatis (pilih opsi 3 "Baik", saran "baik", lalu simpan).</span></label>
    <button class="btn" id="run">Mulai cek</button>
    <button class="btn ghost" id="stop">Tutup sesi</button>
  </section>
</aside>
<section>
  <h1 id="title">Cek status nilai per semester</h1>
  <p class="sub" id="sub">Masuk dulu, lalu pilih semester. Hasilnya muncul berurutan di sini, lengkap dengan alasan kalau nilai belum bisa dilihat.</p>
  <div class="bar" id="barWrap" hidden><b id="bar"></b></div>
  <ol class="rail" id="rail"></ol>
  <details id="logWrap" hidden><summary>Log proses</summary><pre id="log"></pre></details>
</section>
</main>
</div>

<dialog id="shot"><img id="shotImg" alt="Tangkapan layar halaman nilai"><button class="btn ghost" id="shotX">Tutup</button></dialog>
<dialog id="ask"><p id="askMsg" style="margin-top:0"></p><button class="btn" id="askOk">Lanjut</button><button class="btn ghost" id="askX">Batalkan sesi</button></dialog>

<script>
const $=s=>document.querySelector(s);
const post=(p,b)=>fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})}).then(r=>r.json());
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const put=(el,h)=>{if(el._h!==h){el.innerHTML=h;el._h=h}};
const LABEL={idle:"Belum terhubung",connecting:"Menghubungkan",ready:"Siap",running:"Mengecek",done:"Selesai",error:"Perlu perhatian"};
let S={session_id:"",phase:"idle",semesters:[],targets:[],results:[],log:[]},sel=null;

// Deteksi jika halaman baru saja di-refresh (reload)
const perfEntries = performance.getEntriesByType && performance.getEntriesByType("navigation");
const isReload = (perfEntries && perfEntries[0] && perfEntries[0].type === "reload") || (performance.navigation && performance.navigation.type === 1);

// Bila user me-refresh halaman saat sesi sudah selesai (done) atau error/idle,
// otomatis reset sesi agar folder hasil_cek dikosongkan dan hasil lama di web langsung hilang
if(isReload){
  fetch("/api/state").then(r=>r.json()).then(st=>{
    if(st.phase === "done" || st.phase === "error"){
      post("/api/reset").then(res=>{ if(res && res.ok) tick(); });
    }
  }).catch(()=>{});
}

function kinds(st){
  if(st==="OK")return[["ok","Aman"]];
  if(st==="DILEWATI")return[["warn","Dilewati"]];
  const k=[];
  if(/BAYAR/.test(st))k.push(["pay","Belum bayar"]);
  if(/KUESIONER/.test(st))k.push(["quiz","Kuesioner"]);
  return k.length?k:[["warn",st==="GAGAL"?"Gagal dicek":"Perlu dicek"]];
}
const main=r=>kinds(r.status)[0][0];

function render(){
  const p=S.phase,pill=$("#pill");
  pill.textContent=LABEL[p];pill.dataset.p=p;
  if($("#sessPill")){
    $("#sessPill").textContent = S.session_id ? `ID Sesi: #${S.session_id}` : "Sesi: -";
  }
  $("#login").hidden=!(p==="idle"||p==="error");
  $("#busy").hidden=p!=="connecting";$("#busyMsg").textContent=S.message||"";
  $("#work").hidden=!["ready","running","done"].includes(p);
  $("#err").hidden=!S.error;$("#err").textContent=S.error||"";
  if(S.semesters.length&&!sel)sel=new Set(S.semesters.map((_,i)=>i));
  if(!S.semesters.length)sel=null;
  put($("#chips"),S.semesters.map((s,i)=>`<button class="chip ${sel&&sel.has(i)?"on":""}" data-i="${i}">${esc(s.text.replace(/^TA\s+/,"").replace("Semester ",""))}</button>`).join(""));
  const busy=p==="running";
  $("#run").disabled=busy||!sel||!sel.size;$("#run").textContent=busy?"Sedang mengecek...":(p==="done"?"Cek lagi":"Mulai cek");

  // judul
  const R=S.results,n=S.targets.length;
  if(R.length){
    const c={ok:0,pay:0,quiz:0,warn:0};R.forEach(r=>c[main(r)]++);
    const t=[];
    if(c.ok)t.push(`${c.ok} aman`);if(c.pay)t.push(`${c.pay} belum bayar`);
    if(c.quiz)t.push(`${c.quiz} menunggu kuesioner`);if(c.warn)t.push(`${c.warn} perlu dicek`);
    $("#title").textContent=(p==="running"?"Sementara: ":"")+t.join(", ");
    $("#sub").textContent=p==="running"?`Sudah ${S.done} dari ${n} semester.`:`${n} semester selesai dicek.`;
  }else if(p==="running"){$("#title").textContent="Mengecek semester...";$("#sub").textContent=S.current||""}
  $("#barWrap").hidden=!n;$("#bar").style.width=n?(S.done/n*100)+"%":"0";

  // rail
  put($("#rail"),S.targets.map((t,i)=>{
    const r=R.find(x=>x.text===t)||R[i];
    if(!r)return `<li class="node ${t===S.current?"now":""}"><span class="dot"></span><h3>${esc(t)}</h3><div class="pending">${t===S.current?"Sedang dicek...":"Menunggu giliran"}</div></li>`;
    const b=kinds(r.status).map(([k,l])=>`<span class="b ${k}">${l}</span>`).join("");
    const m=[r.info.semester&&`Semester ${r.info.semester}`,r.info.ip&&`IP ${r.info.ip}`,r.info.ipk&&`IPK ${r.info.ipk}`].filter(Boolean).map(x=>`<span>${esc(x)}</span>`).join("");
    const why=r.alasan.length?`<ul class="why">${r.alasan.map(a=>`<li>${esc(a)}</li>`).join("")}</ul>`:"";
    const canAct=/KUESIONER/.test(r.status);
    const actBtns=canAct?`<div style="display:flex;gap:8px;margin-top:10px;align-items:center;flex-wrap:wrap"><button class="btn-act fill" data-act="fill" data-sem="${esc(r.text)}" ${busy?"disabled":""}>✍️ Lanjut Isi Kuesioner</button><button class="btn-act skip" data-act="skip" data-sem="${esc(r.text)}" ${busy?"disabled":""}>⏩ Lewati (Skip)</button></div>`:"";
    return `<li class="node k-${main(r)}"><span class="dot"></span><div class="head"><h3>${esc(r.text)}</h3>${b}</div>${m?`<div class="meta">${m}</div>`:""}${why}${actBtns}<button class="lnk" data-shot="${esc(r.shot)}" style="margin-top:8px">Lihat tangkapan layar</button></li>`;
  }).join(""));

  $("#logWrap").hidden=!S.log.length;
  const pre=$("#log");pre.textContent=S.log.join("\n");
  if($("#logWrap").open)pre.scrollTop=pre.scrollHeight;

  const ask=$("#ask");
  if(S.prompt){$("#askMsg").textContent=S.prompt;if(!ask.open)ask.showModal()}else if(ask.open)ask.close();
}

$("#chips").onclick=e=>{const b=e.target.closest(".chip");if(!b||!sel)return;const i=+b.dataset.i;sel.has(i)?sel.delete(i):sel.add(i);render()};
$("#all").onclick=()=>{if(!sel)return;sel=sel.size===S.semesters.length?new Set():new Set(S.semesters.map((_,i)=>i));render()};
$("#go").onclick=async()=>{const r=await post("/api/connect",{nim:$("#nim").value,password:$("#pwd").value});if(!r.ok){S.error=r.error;render()}};
$("#run").onclick=async()=>{
  const isi=$("#isi").checked;
  const r=await post("/api/run",{indices:[...sel],isi});if(!r.ok){S.error=r.error;render()}};
$("#btnReset").onclick=async()=>{
  if(S.phase==="running"&&!confirm("Proses cek sedang berjalan. Yakin ingin membatalkan dan mulai sesi baru?"))return;
  const r=await post("/api/reset");
  if(r.ok){sel=null;$("#nim").value="";$("#pwd").value="";tick()}
};
$("#stop").onclick=async()=>{const r=await post("/api/reset");if(r.ok)tick()};
$("#askOk").onclick=()=>post("/api/continue");
$("#askX").onclick=()=>post("/api/stop");
$("#rail").onclick=async e=>{
  const b=e.target.closest("[data-shot]");
  if(b){
    $("#shotImg").src="/shots/"+encodeURIComponent(b.dataset.shot)+"?t="+Date.now();
    $("#shot").showModal();
    return;
  }
  const f=e.target.closest('[data-act="fill"]');
  if(f){
    const sem=f.dataset.sem;
    if(confirm(`Yakin ingin otomatis mengisi seluruh kuesioner untuk ${sem}?`)){
      f.disabled=true;
      await post("/api/fill_single",{semester:sem});
      tick();
    }
    return;
  }
  const s=e.target.closest('[data-act="skip"]');
  if(s){
    const sem=s.dataset.sem;
    await post("/api/skip_single",{semester:sem});
    tick();
    return;
  }
};
$("#shotX").onclick=()=>$("#shot").close();
$("#pwd").onkeydown=e=>{if(e.key==="Enter")$("#go").click()};

async function tick(){try{S=await (await fetch("/api/state")).json();render()}catch(e){}setTimeout(tick,900)}
tick();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"SIMANTAP Web berjalan di {url}  (Ctrl+C untuk berhenti)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServer dihentikan.")