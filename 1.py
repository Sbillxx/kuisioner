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
import os
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
PORT = int(os.environ.get("PORT", 8765))


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
        self.session_id = uuid.uuid4().hex[:8]
        self.reset(clean_files=True)
        self.phase, self.message = "connecting", f"Sesi #{self.session_id}: Membuka Chrome dan login..."
        self.log(f"=== Sesi Baru #{self.session_id} Dimulai ===")
        self.log("Folder hasil_cek telah dikosongkan untuk sesi ini.")
        threading.Thread(target=lambda: asyncio.run(self.main(nim, pwd)), daemon=True).start()

    async def main(self, nim, pwd):
        self.loop, self.q = asyncio.get_running_loop(), asyncio.Queue()
        page = None
        try:
            async with async_playwright() as p:
                is_headless = os.environ.get("HEADLESS", "").lower() in ("1", "true", "yes") or (
                    sys.platform != "win32" and not os.environ.get("DISPLAY")
                )
                ctx_opts = {"viewport": {"width": 1920, "height": 1080}} if is_headless else {"no_viewport": True}
                ctx = await self.browser.new_context(**ctx_opts)
                page = await ctx.new_page()
                page.set_default_timeout(20000)
                page.on("dialog", lambda d: asyncio.create_task(d.accept()))
                await core.login(page, nim, pwd)
                await core.buka_halaman_nilai(page)
                self.semesters = await core.ambil_semester(page)
                self.phase, self.message = "ready", "Pilih semester yang mau dicek."
                while True:
                    cmd = await self.q.get()
                    if cmd["type"] == "close":
                        break
                    elif cmd["type"] == "fill_single":
                        await self.run_fill_single(page, cmd["semester"])
                    else:
                        await self.run_checks(page, cmd)
        except Exception as e:
            if not self.cancelled:
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
                await self.browser.close()
            except Exception:
                pass
            if self.phase != "error":
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f4f4f1;--panel:#fbfbf9;--ink:#12151a;--mut:#6b717b;--line:#dcdcd6;--accent:#2d3cf5;
  --ok:#17795a;--pay:#b86a0b;--quiz:#6a4fd6;--warn:#7a828e;
  --f:"Inter Tight","Segoe UI Variable","Segoe UI",system-ui,sans-serif;
  --m:"JetBrains Mono",ui-monospace,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0d0f13;--panel:#13161b;--ink:#e8eaee;--mut:#8b919c;--line:#242932;--accent:#8591ff;
  --ok:#3fcf9f;--pay:#f0a043;--quiz:#a893ff;--warn:#97a0ad}}
*{box-sizing:border-box}
html{color-scheme:light dark}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--f);font-feature-settings:"ss01","cv11"}
[hidden]{display:none!important}
button,input{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{max-width:1120px;margin:0 auto;padding:0 24px 72px}
header{display:flex;align-items:center;justify-content:space-between;min-height:64px;flex-wrap:wrap;gap:8px 16px;padding:10px 0;border-bottom:1px solid var(--line);margin-bottom:40px}
.brand{white-space:nowrap;font-weight:700;letter-spacing:-.01em;display:flex;align-items:center;gap:10px}
.brand i{width:14px;height:14px;background:var(--ink);display:block;border-radius:3px;box-shadow:5px 5px 0 -1px var(--accent)}
.hd{display:flex;align-items:center;flex-wrap:wrap;gap:6px 14px;font-size:13px;color:var(--mut);white-space:nowrap}
.mono{font-family:var(--m);font-size:12px}
.state{display:flex;align-items:center;gap:7px;color:var(--ink);font-weight:500}
.state i{width:8px;height:8px;border-radius:2px;background:var(--warn)}
.state[data-p=ready] i,.state[data-p=done] i{background:var(--ok)}
.state[data-p=running] i,.state[data-p=connecting] i{background:var(--accent);animation:p 1s ease-in-out infinite}
.state[data-p=error] i{background:var(--pay)}
@keyframes p{50%{opacity:.25}}
.txt{background:none;border:0;padding:0;cursor:pointer;color:var(--mut);font-size:13px;font-weight:500}
.txt:hover{color:var(--ink)}
.btn{border:1px solid var(--ink);border-radius:6px;background:var(--ink);color:var(--bg);font-weight:600;padding:10px 16px;cursor:pointer}
.btn:hover{background:var(--accent);border-color:var(--accent);color:#fff}
.btn:disabled{opacity:.35;cursor:not-allowed;background:var(--ink);border-color:var(--ink);color:var(--bg)}
.btn.ghost{background:transparent;color:var(--ink);border-color:var(--line)}
.btn.ghost:hover{border-color:var(--ink);background:transparent;color:var(--ink)}
/* login */
#vLogin{display:grid;grid-template-columns:1.15fr 1fr;gap:72px;align-items:center;min-height:60vh}
@media(max-width:820px){#vLogin{grid-template-columns:1fr;gap:32px}}
h1{font-weight:600;letter-spacing:-.035em;line-height:1.04;margin:0 0 16px;font-size:clamp(36px,5.4vw,60px)}
.lead{color:var(--mut);max-width:46ch;margin:0 0 28px;font-size:16px}
.key{list-style:none;margin:0;padding:0;display:grid;gap:10px;font-size:14px}
.key li{display:flex;gap:10px;align-items:baseline}
.sq{width:9px;height:9px;border-radius:2px;display:inline-block;flex:none;background:var(--warn)}
.sq.ok{background:var(--ok)}.sq.pay{background:var(--pay)}.sq.quiz{background:var(--quiz)}
.key b{font-weight:600;min-width:88px;display:inline-block}.key span{color:var(--mut)}
.form{border:1px solid var(--line);border-radius:10px;background:var(--panel);padding:26px}
.form h2{margin:0 0 20px;font-size:17px;font-weight:600;letter-spacing:-.01em}
.form label{display:block;font-size:13px;font-weight:500;color:var(--mut);margin:0 0 6px}
.form input{width:100%;padding:11px 12px;border-radius:6px;border:1px solid var(--line);background:var(--bg);margin-bottom:16px}
.form input:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 22%,transparent)}
.form .btn{width:100%}
.note{font-size:12.5px;color:var(--mut);margin:14px 0 0}
.err{margin-top:14px;padding:10px 12px;border-left:3px solid var(--pay);background:color-mix(in srgb,var(--pay) 10%,transparent);font-size:13.5px;word-break:break-word}
#vBusy{text-align:center;margin-top:22vh;color:var(--mut)}
.spin{width:22px;height:22px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:s .8s linear infinite;margin:0 auto 14px}
@keyframes s{to{transform:rotate(360deg)}}
/* dashboard */
#vDash{display:grid;grid-template-columns:268px 1fr;gap:48px;align-items:start}
@media(max-width:860px){#vDash{grid-template-columns:1fr;gap:28px}}
#vDash>*{min-width:0}
@media(min-width:861px){aside{position:sticky;top:20px}}
.go{display:block}
aside h2{font-size:13px;font-weight:600;margin:0 0 12px;color:var(--mut)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{border:1px solid var(--line);background:transparent;border-radius:6px;padding:5px 10px;font-size:13px;font-weight:500;cursor:pointer;color:var(--mut);font-variant-numeric:tabular-nums}
.chip:hover{border-color:var(--ink);color:var(--ink)}
.chip.on{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.sel{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.chk{display:flex;gap:9px;align-items:center;font-size:13.5px;margin:20px 0 18px;cursor:pointer}
aside .btn{width:100%}
aside .txt{margin-top:14px;display:block}
.sum h1{font-size:clamp(28px,3.6vw,40px);margin:0 0 6px}
.sum p{margin:0 0 20px;color:var(--mut)}
.seg{display:flex;gap:3px;margin-bottom:12px}
.seg i{flex:1;height:6px;border-radius:2px;background:var(--line);display:block}
.seg i.ok{background:var(--ok)}.seg i.pay{background:var(--pay)}.seg i.quiz{background:var(--quiz)}.seg i.warn{background:var(--warn)}
.seg i.now{background:var(--accent);animation:p 1s ease-in-out infinite}
.cnt{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:13.5px;margin-bottom:30px}
.cnt span{display:flex;align-items:center;gap:7px}
.cnt b{font-variant-numeric:tabular-nums;font-weight:600}
.ledger{border-top:1px solid var(--ink)}
.row{display:grid;grid-template-columns:minmax(150px,1.3fr) minmax(130px,1fr) 52px 52px 52px 132px;gap:0 14px;align-items:center;padding:15px 6px;border-bottom:1px solid var(--line)}
.row.th{padding:9px 6px;font-size:12.5px;color:var(--mut);font-weight:500}
.row:not(.th):hover{background:color-mix(in srgb,var(--ink) 3.5%,transparent)}
.nm b{display:block;font-weight:600;letter-spacing:-.01em;font-size:16px;font-variant-numeric:tabular-nums}
.nm span{color:var(--mut);font-size:13px}
.st{display:flex;align-items:center;gap:9px;font-weight:500;font-size:14px}
.st.pend{color:var(--mut);font-weight:400}
.n{font-family:var(--m);font-size:13px;text-align:right;font-variant-numeric:tabular-nums}
.ac{display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap}
.todo{grid-column:1/-1;display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.sm{border:1px solid var(--line);background:transparent;border-radius:6px;padding:4px 10px;font-size:12.5px;font-weight:500;cursor:pointer}
.sm:hover{border-color:var(--ink)}
.sm.p{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.sm.p:hover{background:var(--accent);border-color:var(--accent);color:#fff}
.sm:disabled{opacity:.35;cursor:not-allowed}
.why{grid-column:1/-1;margin:12px 0 0;padding:2px 0 2px 14px;border-left:2px solid var(--warn);font-size:13.5px;color:var(--mut);max-width:70ch}
.why p{margin:0}.why p+p{margin-top:3px}
.k-pay .why{border-color:var(--pay)}.k-quiz .why{border-color:var(--quiz)}.k-ok .why{border-color:var(--ok)}
details{margin-top:28px}
summary{cursor:pointer;color:var(--mut);font-size:13px}
pre{border:1px solid var(--line);border-radius:6px;background:var(--panel);padding:12px;max-height:240px;overflow:auto;font:12px/1.55 var(--m);white-space:pre-wrap;color:var(--mut)}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--ink);padding:22px;max-width:min(92vw,960px)}
dialog::backdrop{background:rgba(8,10,14,.6)}
dialog img{max-width:100%;border-radius:6px;display:block;margin-bottom:14px;border:1px solid var(--line)}
dialog .btn{margin-right:8px}
.mt{display:contents}
.n small{display:none}
.ac:empty{display:none}
@media(max-width:860px){
  .wrap{padding:0 16px 56px}
  header{margin-bottom:24px}
  #vDash{gap:22px}
  .chips{flex-wrap:nowrap;overflow-x:auto;margin:0 -16px;padding:0 16px 6px;scrollbar-width:none}
  .chips::-webkit-scrollbar{display:none}
  .chip{flex:none;padding:8px 12px}
  .go{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-top:14px}
  .chk{margin:0}
  aside .btn{width:auto;padding:11px 26px}
  aside .txt{display:inline-block;margin-top:10px}
}
@media(max-width:760px){
  .row{grid-template-columns:1fr auto;grid-template-areas:"nm st" "mt mt" "ac ac" "why why" "todo todo";gap:10px 12px;padding:16px 2px}
  .row.th{display:none}
  .nm{grid-area:nm}
  .st{grid-area:st;align-self:start}
  .mt{display:flex;grid-area:mt;gap:22px}
  .n{text-align:left}
  .n small{display:inline;font-family:var(--f);color:var(--mut);margin-right:6px;font-size:12.5px}
  .ac{grid-area:ac;justify-content:flex-start}
  .sm{padding:8px 12px}
  .why{grid-area:why;margin-top:2px}
  .todo{grid-area:todo;margin-top:2px}
  #vLogin{min-height:0}
}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="brand"><i></i>SIMANTAP Checker</div>
  <div class="hd">
    <span class="mono" id="sessPill">Sesi: -</span>
    <span class="state" id="pill" data-p="idle"><i></i><span id="pillT">Belum terhubung</span></span>
    <button class="txt" id="btnReset" title="Kosongkan hasil_cek dan mulai sesi baru">Sesi baru</button>
  </div>
</header>

<section id="vLogin">
  <div>
    <h1>Status akademik, per semester.</h1>
    <p class="lead">Bot membuka SIMANTAP di Chrome kamu, memeriksa setiap Tahun Akademik, lalu melaporkan hasilnya di sini.</p>
    <ul class="key">
      <li><i class="sq ok"></i><b>Aman</b><span>Kartu hasil studi dan nilai bisa dibuka.</span></li>
      <li><i class="sq pay"></i><b>Belum bayar</b><span>Nilai terkunci karena tunggakan.</span></li>
      <li><i class="sq quiz"></i><b>Kuesioner</b><span>Ada kuesioner yang belum diisi.</span></li>
      <li><i class="sq"></i><b>Perlu dicek</b><span>Tampilan lain atau gagal dibaca.</span></li>
    </ul>
  </div>
  <div class="form">
    <h2>Masuk ke SIMANTAP</h2>
    <label for="nim">NIM</label>
    <input id="nim" type="text" autocomplete="username" placeholder="2303010252">
    <label for="pwd">Password</label>
    <input id="pwd" type="password" autocomplete="current-password">
    <button class="btn" id="go">Masuk dan ambil semester</button>
    <div class="err" id="err" hidden></div>
    <p class="note">Data login hanya dipakai di komputer kamu dan tidak disimpan.</p>
  </div>
</section>

<section id="vBusy" hidden><div class="spin"></div><div id="busyMsg"></div></section>

<section id="vDash" hidden>
  <aside>
    <div class="sel"><h2 style="margin:0">Semester</h2><button class="txt" id="all">Pilih semua</button></div>
    <div class="chips" id="chips"></div>
    <div class="go"><label class="chk" title="Pilih opsi 3 Baik, saran baik, lalu simpan"><input type="checkbox" id="isi" checked>Isi kuesioner otomatis</label>
    <button class="btn" id="run">Mulai cek</button></div>
    <button class="txt" id="stop">Tutup sesi</button>
  </aside>
  <div>
    <div class="sum">
      <h1 id="title">Pilih semester</h1>
      <p id="sub">Pilih semester yang mau dicek, lalu mulai.</p>
      <div class="seg" id="seg"></div>
      <div class="cnt">
        <span><i class="sq ok"></i><b id="cOk">0</b> aman</span>
        <span><i class="sq pay"></i><b id="cPay">0</b> belum bayar</span>
        <span><i class="sq quiz"></i><b id="cQuiz">0</b> kuesioner</span>
        <span><i class="sq"></i><b id="cWarn">0</b> perlu dicek</span>
      </div>
    </div>
    <div class="ledger" id="grid"></div>
    <details id="logWrap" hidden><summary>Log proses</summary><pre id="log"></pre></details>
  </div>
</section>
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

function kinds(st){
  if(st==="OK")return[["ok","Aman"]];
  if(st==="DILEWATI")return[["warn","Dilewati"]];
  const k=[];
  if(/BAYAR/.test(st))k.push(["pay","Belum bayar"]);
  if(/KUESIONER/.test(st))k.push(["quiz","Kuesioner"]);
  return k.length?k:[["warn",st==="GAGAL"?"Gagal dicek":"Perlu dicek"]];
}
const main=r=>kinds(r.status)[0][0];
const split=t=>{const m=t.match(/(\d{4}\/\d{4})\s*(.*)/);return m?[m[1],m[2]]:[t,""]};

function render(){
  const p=S.phase,busy=p==="running";
  $("#pillT").textContent=LABEL[p];$("#pill").dataset.p=p;
  $("#sessPill").textContent=S.session_id?`Sesi #${S.session_id}`:"Sesi: -";
  $("#vLogin").hidden=!(p==="idle"||p==="error");
  $("#vBusy").hidden=p!=="connecting";$("#busyMsg").textContent=S.message||"";
  $("#vDash").hidden=!["ready","running","done"].includes(p);
  $("#err").hidden=!S.error;$("#err").textContent=S.error||"";
  if(S.semesters.length&&!sel)sel=new Set(S.semesters.map((_,i)=>i));
  if(!S.semesters.length)sel=null;
  put($("#chips"),S.semesters.map((s,i)=>{const [a,b]=split(s.text);return `<button class="chip ${sel&&sel.has(i)?"on":""}" data-i="${i}">${esc(a)} ${esc(b.replace("Semester ",""))}</button>`}).join(""));
  $("#run").disabled=busy||!sel||!sel.size;$("#run").textContent=busy?"Mengecek...":(p==="done"?"Cek lagi":"Mulai cek");

  const R=S.results,n=S.targets.length,c={ok:0,pay:0,quiz:0,warn:0};
  R.forEach(r=>c[main(r)]++);
  $("#cOk").textContent=c.ok;$("#cPay").textContent=c.pay;$("#cQuiz").textContent=c.quiz;$("#cWarn").textContent=c.warn;
  $("#title").textContent=busy?"Sedang mengecek":(R.length?`${c.ok} dari ${R.length} semester aman`:"Pilih semester");
  $("#sub").textContent=busy?(S.current||""):(R.length?(c.pay?`${c.pay} semester terkunci karena pembayaran.`:"Semua hasil sudah masuk."):"Pilih semester yang mau dicek, lalu mulai.");
  put($("#seg"),S.targets.map(t=>{const r=R.find(x=>x.text===t);return `<i class="${r?main(r):(t===S.current?"now":"")}" title="${esc(t)}"></i>`}).join(""));

  const head=n?`<div class="row th"><div>Tahun akademik</div><div>Status</div><div class="n">Smt</div><div class="n">IP</div><div class="n">IPK</div><div></div></div>`:"";
  put($("#grid"),head+S.targets.map((t,i)=>{
    const r=R.find(x=>x.text===t)||R[i],[a,b]=split(t);
    const nm=`<div class="nm"><b>${esc(a)}</b><span>${esc(b)}</span></div>`;
    if(!r)return `<div class="row">${nm}<div class="st pend"><i class="sq ${t===S.current?"":""}" style="${t===S.current?"background:var(--accent)":"background:var(--line)"}"></i>${t===S.current?"Sedang dicek":"Menunggu"}</div><div class="mt"><div class="n"><small>Smt</small>-</div><div class="n"><small>IP</small>-</div><div class="n"><small>IPK</small>-</div></div><div class="ac"></div></div>`;
    const k=main(r),lab=kinds(r.status).map(x=>x[1]).join(" + "),I=r.info||{};
    const why=(r.alasan||[]).length?`<div class="why">${r.alasan.map(x=>`<p>${esc(x)}</p>`).join("")}</div>`:"";
    const act=/KUESIONER/.test(r.status)?`<button class="sm p" data-act="fill" data-sem="${esc(r.text)}" ${busy?"disabled":""}>Isi kuesioner</button><button class="sm" data-act="skip" data-sem="${esc(r.text)}" ${busy?"disabled":""}>Lewati</button>`:"";
    return `<div class="row k-${k}">${nm}<div class="st"><i class="sq ${k}"></i>${esc(lab)}</div><div class="mt"><div class="n"><small>Smt</small>${esc(I.semester||"-")}</div><div class="n"><small>IP</small>${esc(I.ip||"-")}</div><div class="n"><small>IPK</small>${esc(I.ipk||"-")}</div></div><div class="ac"><button class="sm" data-shot="${esc(r.shot)}">Tangkapan layar</button></div>${why}${act?`<div class="todo">${act}</div>`:""}</div>`;
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
$("#stop").onclick=async()=>{const r=await post("/api/reset");if(r.ok){sel=null;tick()}};
$("#btnReset").onclick=async()=>{
  if(S.phase==="running"&&!confirm("Proses cek sedang berjalan. Batalkan dan mulai sesi baru?"))return;
  const r=await post("/api/reset");
  if(r.ok){sel=null;$("#nim").value="";$("#pwd").value="";tick()}};
$("#askOk").onclick=()=>post("/api/continue");
$("#askX").onclick=()=>post("/api/stop");
$("#grid").onclick=async e=>{
  const b=e.target.closest("[data-shot]");
  if(b){$("#shotImg").src="/shots/"+encodeURIComponent(b.dataset.shot)+"?t="+Date.now();$("#shot").showModal();return}
  const f=e.target.closest('[data-act="fill"]');
  if(f){if(confirm(`Isi semua kuesioner untuk ${f.dataset.sem}?`)){f.disabled=true;await post("/api/fill_single",{semester:f.dataset.sem});tick()}return}
  const k=e.target.closest('[data-act="skip"]');
  if(k){await post("/api/skip_single",{semester:k.dataset.sem});tick()}
};
$("#shotX").onclick=()=>$("#shot").close();
$("#pwd").onkeydown=e=>{if(e.key==="Enter")$("#go").click()};
async function tick(){try{S=await (await fetch("/api/state")).json();render()}catch(e){}setTimeout(tick,900)}
tick();
const nav=performance.getEntriesByType&&performance.getEntriesByType("navigation")[0];
if(nav&&nav.type==="reload"){
  fetch("/api/state").then(r=>r.json()).then(st=>{if(st.phase==="done"||st.phase==="error")post("/api/reset")}).catch(()=>{});
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    HOST = os.environ.get("HOST", "0.0.0.0")
    PORT = int(os.environ.get("PORT", PORT))
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    display_url = f"http://localhost:{PORT}" if HOST in ("0.0.0.0", "127.0.0.1") else f"http://{HOST}:{PORT}"
    print(f"SIMANTAP Web berjalan di {display_url}  (Ctrl+C untuk berhenti)")
    if sys.platform == "win32" and not os.environ.get("RENDER") and not os.environ.get("DOCKER"):
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServer dihentikan.")