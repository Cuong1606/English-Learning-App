#!/usr/bin/env python3
import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path

if __name__ == "__main__" and "--restore-helper" in sys.argv:
    from restore_helper import main as restore_helper_main
    marker = sys.argv.index("--restore-helper")
    raise SystemExit(restore_helper_main(sys.argv[marker + 1:]))

import webview
import server

APP_TITLE = "English Learning App"
APP_MUTEX = "EnglishLearningApp_SingleInstance"


class DesktopApi:
    def save_user_backup(self, suggested_name):
        """Use the native Windows Save dialog when running in pywebview."""
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"ok": False, "error": "Cửa sổ ứng dụng chưa sẵn sàng"}
        paths = window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=str(suggested_name or "EnglishLearningApp-user-data.zip"),
            file_types=("English Learning App backup (*.zip)",),
        )
        if not paths:
            return {"ok": False, "cancelled": True}
        selected = paths[0] if not isinstance(paths, str) else paths
        output = Path(selected)
        if output.suffix.lower() != ".zip":
            output = output.with_suffix(".zip")
        result = server.create_user_backup(output)
        return {"ok": True, "path": result["path"], "filename": result["filename"]}

    def pick_bulk_audio(self, scope, target, mode):
        """Select a ZIP/folder natively and stage it without exposing paths to JS."""
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"ok": False, "error": "Cửa sổ ứng dụng chưa sẵn sàng"}
        try:
            if mode == "folder":
                paths = window.create_file_dialog(webview.FileDialog.FOLDER)
            else:
                paths = window.create_file_dialog(
                    webview.FileDialog.OPEN,
                    allow_multiple=False,
                    file_types=("ZIP audio (*.zip)",),
                )
            if not paths:
                return {"ok": False, "cancelled": True}
            selected = paths[0] if not isinstance(paths, str) else paths
            return server.prepare_bulk_audio_path(str(scope), str(target), selected, str(mode))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def export_island(self, island_id, suggested_name):
        """Export one My Island through the native Windows Save dialog."""
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"ok": False, "error": "Cửa sổ ứng dụng chưa sẵn sàng"}
        try:
            filename = str(suggested_name or "My Island.island.zip")
            paths = window.create_file_dialog(
                webview.FileDialog.SAVE,
                save_filename=filename,
                file_types=("My Island package (*.island.zip)",),
            )
            if not paths:
                return {"ok": False, "cancelled": True}
            selected = paths[0] if not isinstance(paths, str) else paths
            selected_text = str(selected)
            if not selected_text.lower().endswith(".island.zip"):
                if selected_text.lower().endswith(".zip"):
                    selected_text = selected_text[:-4]
                selected_text += ".island.zip"
            result = server.create_island_export(int(island_id), Path(selected_text))
            return {
                "ok": True, "filename": result["filename"],
                "itemCount": result["itemCount"], "audioFileCount": result["audioFileCount"],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def pick_island_import(self):
        """Select and fully validate a portable Island before any profile change."""
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"ok": False, "error": "Cửa sổ ứng dụng chưa sẵn sàng"}
        try:
            paths = window.create_file_dialog(
                webview.FileDialog.OPEN,
                allow_multiple=False,
                file_types=("My Island package (*.island.zip)",),
            )
            if not paths:
                return {"ok": False, "cancelled": True}
            selected = paths[0] if not isinstance(paths, str) else paths
            return server.prepare_island_import_path(selected)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


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
    server_thread = None
    app_shutdown_complete = threading.Event()
    try:
        validate_resources()
        server.configure_restore_lifecycle(None)
        # Complete a validated restore synchronously on this manual launch,
        # before the app opens any user DB connection or starts the HTTP server.
        restore_result = server.complete_pending_restore_if_needed(lock_timeout=5.0)
        if restore_result and not restore_result.get("ok") and not restore_result.get("rollbackOk", True):
            show_message(
                "Khôi phục dữ liệu và rollback đều thất bại. App sẽ không mở để tránh làm hỏng dữ liệu.\n\n"
                + str(restore_result.get("error") or "Không rõ lỗi"),
                flags=0x10,
            )
            return 1
        server.cleanup_orphan_bulk_audio_staging()
        server.cleanup_orphan_island_import_staging()
        server.user_conn().close()

        # Port 0 lets Windows choose an available local port automatically.
        httpd = server.ThreadingHTTPServer((server.HOST, 0), server.Handler)
        httpd.daemon_threads = True
        httpd.block_on_close = False
        port = int(httpd.server_address[1])
        url = f"http://{server.HOST}:{port}"
        runtime_file = server._user_base_dir() / "runtime.json"
        runtime_partial = runtime_file.with_name("runtime.json.partial")
        runtime_partial.write_text(
            json.dumps({"port": port, "pid": os.getpid(), "mode": "desktop", "time": time.time()}),
            encoding="utf-8",
        )
        os.replace(runtime_partial, runtime_file)

        restore_shutdown_started = threading.Event()
        http_shutdown_started = threading.Event()

        def shutdown_http_once():
            if http_shutdown_started.is_set():
                return
            http_shutdown_started.set()
            httpd.shutdown()

        window_close_started = threading.Event()

        def request_window_shutdown():
            if window_close_started.is_set():
                return
            window_close_started.set()

            def close_worker():
                try:
                    shutdown_http_once()
                finally:
                    if app_shutdown_complete.wait(6):
                        return
                    try:
                        info = json.loads(runtime_file.read_text(encoding="utf-8")) if runtime_file.exists() else {}
                        if int(info.get("pid", -1)) == os.getpid():
                            runtime_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                    os._exit(0)

            threading.Thread(target=close_worker, name="WindowCloseShutdown", daemon=True).start()

        def request_restore_shutdown():
            if restore_shutdown_started.is_set():
                return
            restore_shutdown_started.set()

            # A restore has already been fully validated/staged. Guarantee that
            # this process does not linger invisibly after the WebView closes.
            def force_exit_watchdog():
                time.sleep(10)
                os._exit(0)

            threading.Thread(target=force_exit_watchdog, name="RestoreExitWatchdog", daemon=True).start()
            try:
                shutdown_http_once()
            finally:
                for window in list(webview.windows):
                    try:
                        window.destroy()
                    except Exception:
                        pass

        server.configure_restore_lifecycle(request_restore_shutdown)

        server_thread = threading.Thread(target=httpd.serve_forever, name="EnglishLocalHTTP", daemon=True)
        server_thread.start()

        webview.settings["ALLOW_DOWNLOADS"] = True
        window = webview.create_window(
            APP_TITLE,
            url,
            js_api=DesktopApi(),
            width=1380,
            height=860,
            min_size=(900, 600),
            resizable=True,
            background_color="#f8fafc",
            text_select=True,
        )
        # Stop the local server as soon as the native window closes. Waiting for
        # webview.start() to unwind first can leave an invisible process briefly.
        window.events.closing += request_window_shutdown
        window.events.closed += request_window_shutdown

        if os.name == "nt":
            def monitor_native_window_lifetime():
                user32 = ctypes.windll.user32
                callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                seen_visible = False
                missing_checks = 0
                while not window_close_started.is_set():
                    visible = []

                    @callback_type
                    def enum_window(hwnd, _lparam):
                        owner = ctypes.c_uint32()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                        if owner.value == os.getpid() and user32.IsWindowVisible(hwnd):
                            length = user32.GetWindowTextLengthW(hwnd)
                            title = ctypes.create_unicode_buffer(length + 1)
                            user32.GetWindowTextW(hwnd, title, length + 1)
                            if title.value == APP_TITLE:
                                visible.append(hwnd)
                        return True

                    user32.EnumWindows(enum_window, 0)
                    if visible:
                        seen_visible = True
                        missing_checks = 0
                    elif seen_visible:
                        missing_checks += 1
                        if missing_checks >= 3:
                            request_window_shutdown()
                            return
                    time.sleep(0.2)

            threading.Thread(target=monitor_native_window_lifetime, name="NativeWindowMonitor", daemon=True).start()
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
        if server_thread is not None:
            server_thread.join(timeout=5)
        try:
            runtime_file = server._user_base_dir() / "runtime.json"
            if runtime_file.exists():
                info = json.loads(runtime_file.read_text(encoding="utf-8"))
                if int(info.get("pid", -1)) == os.getpid():
                    runtime_file.unlink(missing_ok=True)
        except Exception:
            pass
        # Keep mutex alive until the application exits.
        _ = mutex
        app_shutdown_complete.set()


if __name__ == "__main__":
    raise SystemExit(main())
