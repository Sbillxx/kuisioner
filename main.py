import sys
import os
import subprocess

if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Otomatis gunakan .venv jika dipanggil dari global Python
venv_python = os.path.join(os.path.dirname(__file__), ".venv", "Scripts", "python.exe")
if os.path.exists(venv_python) and os.path.abspath(sys.executable).lower() != os.path.abspath(venv_python).lower():
    try:
        sys.exit(subprocess.call([venv_python] + sys.argv))
    except Exception as e:
        print(f"Peringatan: Gagal berpindah ke virtual environment: {e}")

import webbrowser
import uvicorn

def main():
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    port = 8000
    host = "127.0.0.1"
    url = f"http://{host}:{port}"

    print("=" * 60)
    print("🚀 SIMANTAP UNPER - AUTOMATION BOT HUB")
    print(f"🌐 Membuka Web Dashboard di: {url}")
    print("💡 Kredensial dimasukkan secara dinamis melalui browser.")
    print("💡 Tekan Ctrl + C di terminal ini untuk mematikan server.")
    print("=" * 60)

    # Otomatis buka browser ke alamat dashboard
    try:
        webbrowser.open(url)
    except Exception:
        pass

    # Pada Windows, reload=False wajib agar Uvicorn mempertahankan ProactorEventLoop untuk Playwright subprocess
    uvicorn.run("server:app", host=host, port=port, reload=False)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nServer SIMANTAP Bot dimatikan.")
