import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
SERVER = BASE / "server.py"
APP_VERSION = "2.4.3"
HOST = "127.0.0.1"
PORTS = range(8767, 8800)

if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
    RUNTIME_DIR = Path(os.environ["LOCALAPPDATA"]) / "EnglishLocal"
else:
    RUNTIME_DIR = BASE / ".runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
LOG = RUNTIME_DIR / "launcher_server.log"
RUNTIME = RUNTIME_DIR / "runtime.json"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DETACHED_PROCESS = 0x00000008 if os.name == "nt" else 0


def message(text, title="English Learning App"):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(text), title, 0x40)
            return
        except Exception:
            pass
    try:
        print(text)
    except Exception:
        pass


def bootstrap(port, timeout=0.8):
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/bootstrap", timeout=timeout) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read().decode("utf-8"))
            return data
    except Exception:
        return None


def find_existing():
    # Prefer the last port used by this app.
    try:
        info = json.loads(RUNTIME.read_text(encoding="utf-8"))
        port = int(info.get("port", 0))
        data = bootstrap(port)
        if data and str(data.get("appVersion", "")) == APP_VERSION:
            return port
    except Exception:
        pass
    for port in PORTS:
        data = bootstrap(port, 0.25)
        if data and str(data.get("appVersion", "")) == APP_VERSION:
            return port
    return None


def free_port():
    for port in PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((HOST, port))
            return port
        except OSError:
            pass
        finally:
            s.close()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(port):
    try:
        log = open(LOG, "a", encoding="utf-8", buffering=1)
        log.write("\n===== launcher %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        cmd = [sys.executable, str(SERVER), "--port", str(port), "--no-browser"]
        kwargs = dict(cwd=str(BASE), stdout=log, stderr=log)
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        p = subprocess.Popen(cmd, **kwargs)
        return p, log
    except Exception as e:
        message(f"Không thể khởi động server.\n\n{e}\n\nLog: {LOG}")
        return None, None


def wait_server(port, proc, seconds=25):
    end = time.time() + seconds
    while time.time() < end:
        data = bootstrap(port, 1.0)
        if data:
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def browser_candidates():
    env = os.environ
    vals = []
    for p in [
        Path(env.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(env.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("ProgramFiles(x86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]:
        if str(p) and p.exists():
            vals.append(p)
    return vals


def open_app(port):
    url = f"http://{HOST}:{port}"
    for browser in browser_candidates():
        try:
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = CREATE_NO_WINDOW
            subprocess.Popen([str(browser), f"--app={url}", "--start-maximized"], **kwargs)
            return True
        except Exception:
            pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def main():
    if not SERVER.exists():
        message(f"Thiếu file server.py trong:\n{BASE}")
        return 2

    port = find_existing()
    proc = None
    log = None
    if port is None:
        port = free_port()
        proc, log = start_server(port)
        if proc is None:
            return 3
        if not wait_server(port, proc):
            try:
                if log:
                    log.flush()
                    log.close()
            except Exception:
                pass
            tail = ""
            try:
                lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
                tail = "\n".join(lines[-12:])
            except Exception:
                pass
            message("App không khởi động được.\n\n" + (tail or "Không có chi tiết lỗi.") + f"\n\nLog: {LOG}")
            return 4

    try:
        RUNTIME.write_text(json.dumps({"port": port, "time": time.time()}), encoding="utf-8")
    except Exception:
        pass

    if not open_app(port):
        message(f"Server đã chạy nhưng không mở được trình duyệt.\nHãy mở thủ công:\nhttp://{HOST}:{port}")
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
