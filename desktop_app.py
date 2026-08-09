#!/usr/bin/env python3
import ctypes
import os
import sys
import threading
from pathlib import Path

import webview
import server

APP_TITLE = "English Learning App"
APP_MUTEX = "EnglishLearningApp_V2_4_3"


def show_message(text, title=APP_TITLE, flags=0x40):
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, str(text), title, flags)
            return
        except Exception:
            pass
    try:
        print(text)
    except Exception:
        pass


def ensure_single_instance():
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    handle = create_mutex(None, False, APP_MUTEX)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        show_message("English Learning App đang mở rồi.")
        raise SystemExit(0)
    return handle


def validate_resources():
    required = [
        server.CONTENT_DB,
        server.WEB / "index.html",
        server.AUDIO,
        server.COURSE_AUDIO,
        server.TEMPLATE_XLSX,
    ]
    missing = [str(p) for p in required if not Path(p).exists()]
    if missing:
        raise FileNotFoundError("Thiếu dữ liệu ứng dụng:\n" + "\n".join(missing))


def main():
    mutex = ensure_single_instance()
    httpd = None
    try:
        validate_resources()
        server.user_conn().close()

        # Port 0 lets Windows choose an available local port automatically.
        httpd = server.ThreadingHTTPServer((server.HOST, 0), server.Handler)
        port = int(httpd.server_address[1])
        url = f"http://{server.HOST}:{port}"

        thread = threading.Thread(target=httpd.serve_forever, name="EnglishLocalHTTP", daemon=True)
        thread.start()

        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.create_window(
            APP_TITLE,
            url,
            width=1380,
            height=860,
            min_size=(900, 600),
            resizable=True,
            background_color="#f8fafc",
        )
        # Force the modern Edge/WebView2 engine. This prevents silent fallback to IE/MSHTML.
        webview.start(gui="edgechromium", debug=False)
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        show_message(
            "Không thể mở English Learning App.\n\n"
            f"Chi tiết: {exc}\n\n"
            "Nếu lỗi nhắc WebView2, hãy cài Microsoft Edge WebView2 Runtime rồi mở lại app.",
            flags=0x10,
        )
        return 1
    finally:
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        # Keep mutex alive until the application exits.
        _ = mutex


if __name__ == "__main__":
    raise SystemExit(main())
