#!/usr/bin/env python3
"""Destructive end-to-end Restore smoke test against the packaged Windows EXE.

The caller must pass a disposable LOCALAPPDATA root. The real user profile is
never read or written by this script or by the EXE processes it launches.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_HOLDER = ROOT / "tests" / "windows_sqlite_lock_holder.py"
TEST_AUDIO = b"ID3\x04\x00\x00\x00\x00\x00\x15english-app-exe-test-audio"


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def pid_alive(pid):
    if not pid:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_uint32()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def terminate_pid(pid):
    if pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 10
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if pid_alive(pid):
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def read_runtime(profile_base):
    path = profile_base / "runtime.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def http_raw(port, path, data=None, content_type=None, timeout=60):
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}{path}", data=data, headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"HTTP {exc.code} {path}: {payload}") from exc


def http_json(port, path, payload=None, timeout=60):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    status, _headers, raw = http_raw(
        port, path, data, "application/json" if data is not None else None, timeout
    )
    assert_true(200 <= status < 300, f"Unexpected HTTP {status}: {path}")
    return json.loads(raw.decode("utf-8"))


def wait_app(profile_base, previous_pid=None, timeout=60):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        runtime = read_runtime(profile_base)
        if runtime and runtime.get("pid") != previous_pid and pid_alive(runtime.get("pid")):
            try:
                boot = http_json(runtime["port"], "/api/bootstrap", timeout=2)
                if boot.get("appVersion") == "1.4.0":
                    return runtime, boot
            except Exception as exc:
                last = exc
        time.sleep(0.15)
    raise TimeoutError(f"EXE did not become ready: {last}")


def wait_pid_gone(pid, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    raise TimeoutError(f"Process {pid} did not exit")


def close_window_with_x(pid, timeout=10):
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    deadline = time.monotonic() + timeout
    windows = []
    while not windows and time.monotonic() < deadline:
        @callback_type
        def callback(hwnd, _lparam):
            owner = ctypes.c_uint32()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == int(pid) and user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                if title.value == "English Learning App":
                    windows.append(hwnd)
            return True

        user32.EnumWindows(callback, 0)
        if not windows:
            time.sleep(0.1)
    assert_true(windows, f"No visible window found for PID {pid}")
    for hwnd in windows:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE, same path as the X button.


def wait_restore_finished(profile_base, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root = profile_base / "restore_pending"
        entries = list(root.iterdir()) if root.exists() else []
        if not entries and not packaged_app_pids():
            return
        time.sleep(0.1)
    raise TimeoutError("Restore helper/pending session did not finish")


def open_app_manually(exe, env, profile_base, previous_pid=None):
    process = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env)
    runtime, boot = wait_app(profile_base, previous_pid=previous_pid)
    return process, runtime, boot


def setting(db, key):
    con = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True, timeout=20)
    try:
        row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def table_count(db, table):
    con = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True, timeout=20)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def start_locker(db, work, name):
    ready = work / f"{name}.ready"
    release = work / f"{name}.release"
    process = subprocess.Popen(
        [sys.executable, str(LOCK_HOLDER), str(db), str(ready), str(release)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 15
    while not ready.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(process.stderr.read())
        time.sleep(0.05)
    assert_true(ready.exists(), "WAL lock holder did not become ready")
    wal = Path(str(db) + "-wal")
    assert_true(wal.exists() and wal.stat().st_size > 0, "SQLite WAL was not created")
    return process, release, wal


def release_locker(process, release):
    release.write_text("release", encoding="utf-8")
    assert_true(process.wait(timeout=20) == 0, process.stderr.read())


def find_pending(profile_base, timeout=10):
    root = profile_base / "restore_pending"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        markers = list(root.glob("*/pending.json")) if root.exists() else []
        if len(markers) == 1:
            return markers[0]
        time.sleep(0.05)
    raise AssertionError("Expected exactly one pending restore")


def wait_helper_state(pending, timeout=15):
    state_file = Path(pending).parent / "restore_state.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if state.get("helperPid") and state.get("status") in ("waiting_process", "waiting_locks", "swapping"):
                return state
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise AssertionError("Restore helper did not publish its PID/state")


def packaged_app_pids():
    command = "@(Get-Process -Name 'English Learning App' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id) -join ','"
    raw = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", command], text=True
    ).strip()
    return {int(value) for value in raw.split(",") if value.strip().isdigit()}


def assert_pending_clean(profile_base):
    root = profile_base / "restore_pending"
    entries = list(root.iterdir()) if root.exists() else []
    assert_true(not entries, f"Restore lifecycle files were not cleaned: {entries}")


def create_custom(port, island_id, english, audio_bytes):
    return http_json(port, "/api/custom/create", {
        "en_us": english,
        "vi_vn": "Câu kiểm thử EXE",
        "audio_data": base64.b64encode(audio_bytes).decode("ascii"),
        "audio_name": "test.mp3",
        "audio_type": "audio/mpeg",
        "island_id": island_id,
    })


def exercise_english_by_topic(port, boot, report):
    courses = boot.get("courses") or []
    assert_true(
        [course.get("name") for course in courses] == [
            "4000 Essential English Words",
            "Common English Phrases",
            "English by Topic",
        ],
        f"Packaged Courses mismatch: {courses}",
    )
    topic = next(course for course in courses if course.get("key") == "english_by_topic")
    assert_true(len(topic.get("units") or []) == 30, "English by Topic unit count mismatch")
    assert_true(topic.get("sentence_count") == 990, "English by Topic sentence count mismatch")
    assert_true(topic.get("audio_available") == 990 and topic.get("audio_missing") == 0, "English by Topic audio total mismatch")

    expected_units = {
        800: ("U1 · Family", "Gia đình", 20),
        814: ("U15 · Directions", "Chỉ đường", 36),
        829: ("U30 · Wedding", "Đám cưới", 39),
    }
    opened = {}
    for collection_id, (name, description, count) in expected_units.items():
        collection = http_json(port, f"/api/collection?kind=core&id={collection_id}")
        opened[collection_id] = collection
        assert_true(collection["collection"]["name"] == name, f"Collection name mismatch: {collection_id}")
        assert_true(collection["collection"]["description"] == description, f"Collection topic mismatch: {collection_id}")
        assert_true(len(collection["items"]) == count, f"Collection count mismatch: {collection_id}")
        for index in (0, len(collection["items"]) // 2, len(collection["items"]) - 1):
            item = collection["items"][index]
            status, _headers, audio = http_raw(port, item["audio"])
            assert_true(status == 200 and len(audio) > 100, f"English by Topic audio failed: {collection_id}/{index}")

    status, _headers, app_js = http_raw(port, "/app.js")
    assert_true(status == 200, "Packaged app.js unavailable")
    for marker in (b"function renderShadowTab", b"function renderRecallSetup", b"function courseCard", b"function openCourse"):
        assert_true(marker in app_js, f"Packaged learning UI marker missing: {marker!r}")

    first = opened[800]["items"][0]
    assert_true(http_json(port, "/api/bookmark", {"item_key": first["item_key"], "saved": True})["saved"], "Saved failed")
    saved = http_json(port, "/api/saved")["items"]
    assert_true(any(item["item_key"] == first["item_key"] for item in saved), "Saved item not returned")
    review = http_json(port, "/api/review", {"item_key": first["item_key"], "rating": 3, "source_mode": "active_recall"})
    assert_true(review.get("ok") and review.get("item_key") == first["item_key"], "Active Recall/FSRS review failed")

    item_query = urllib.parse.quote(first["item_key"], safe="")
    item_srs = http_json(port, f"/api/srs/info?item_key={item_query}")
    assert_true(item_srs.get("review_count") == 1, "Single-item SRS info mismatch")
    assert_true(http_json(port, "/api/srs/manage", {"action": "review_now", "item_key": first["item_key"]})["affected"] == 1, "Single-item SRS action failed")

    unit_srs = http_json(port, "/api/srs/info?collection_key=core%3A814")
    assert_true(unit_srs.get("total") == 36, "Unit SRS scope mismatch")
    assert_true(http_json(port, "/api/srs/manage", {"action": "suspend", "collection_key": "core:814"})["affected"] == 36, "Unit SRS suspend failed")
    assert_true(http_json(port, "/api/srs/manage", {"action": "resume", "collection_key": "core:814"})["affected"] == 36, "Unit SRS resume failed")

    course_srs = http_json(port, "/api/srs/info?group_key=course%3Aenglish_by_topic")
    assert_true(course_srs.get("total") == 990, "Course SRS scope mismatch")
    assert_true(http_json(port, "/api/srs/manage", {"action": "suspend", "group_key": "course:english_by_topic"})["affected"] == 990, "Course SRS suspend failed")
    assert_true(http_json(port, "/api/srs/manage", {"action": "resume", "group_key": "course:english_by_topic"})["affected"] == 990, "Course SRS resume failed")

    search = http_json(port, "/api/search?q=Thanksgiving")
    assert_true(any(item.get("en_us") == "My whole family gets together for Thanksgiving." for item in search.get("items", [])), "Search did not find English by Topic")

    http_json(port, "/api/bookmark", {"item_key": first["item_key"], "saved": False})
    report["checks"].append("Packaged Courses: 3 courses; English by Topic 30 Units / 990 sentences / 990 audio")
    report["checks"].append("U1/U15/U30 metadata and first/middle/last audio")
    report["checks"].append("Learn/Shadowing/Active Recall, Saved, item/Unit/Course SRS, and Search")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True)
    parser.add_argument("--profile-root", required=True)
    args = parser.parse_args(argv)
    exe = Path(args.exe).resolve()
    localappdata = Path(args.profile_root).resolve()
    profile_base = localappdata / "EnglishLocal"
    db = profile_base / "user_data" / "learning.sqlite"
    report = {"exe": str(exe), "profileRoot": str(localappdata), "checks": []}
    app_pids = []
    lockers = []

    assert_true(exe.is_file(), f"Missing EXE: {exe}")
    assert_true(localappdata != Path(os.environ.get("LOCALAPPDATA", "")).resolve(), "Refusing real LOCALAPPDATA")
    if localappdata.exists():
        shutil.rmtree(localappdata)
    localappdata.mkdir(parents=True)
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(localappdata)
    env["ENGLISH_APP_EXE_TEST"] = "1"

    try:
        initial = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env)
        runtime, boot = wait_app(profile_base)
        app_pids.append(runtime["pid"])
        port = runtime["port"]
        assert_true(initial.poll() is None, "Packaged EXE exited during startup")
        report["checks"].append("EXE startup on isolated profile")

        close_window_with_x(runtime["pid"])
        wait_pid_gone(runtime["pid"], timeout=10)
        deadline = time.monotonic() + 10
        while packaged_app_pids() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert_true(not packaged_app_pids(), "App/helper process remained after closing with X")
        assert_true(not read_runtime(profile_base) or not pid_alive(read_runtime(profile_base).get("pid")), "Runtime PID survived X close")
        report["checks"].append("Window X closes app/server process completely")

        initial, runtime, boot = open_app_manually(exe, env, profile_base, previous_pid=runtime["pid"])
        app_pids.append(runtime["pid"])
        port = runtime["port"]

        assert_true(boot.get("appVersion") == "1.4.0", "About/bootstrap version mismatch")
        exercise_english_by_topic(port, boot, report)
        report["checks"].append("About/bootstrap version 1.4.0")

        first_collection = next(c for c in boot["collections"] if int(c.get("sentence_count") or 0) > 0)
        collection = http_json(port, f"/api/collection?kind=core&id={first_collection['id']}")
        first_item = collection["items"][0]
        http_json(port, "/api/setting", {"key": "new_per_day", "value": 37})
        http_json(port, "/api/bookmark", {"item_key": first_item["item_key"], "saved": True})
        http_json(port, "/api/review", {"item_key": first_item["item_key"], "rating": 4, "source_mode": "exe-test"})
        island = http_json(port, "/api/my-island/create", {"name": "EXE Backup Island", "description": "backup-state"})
        custom = create_custom(port, island["id"], "EXE backup sentence.", TEST_AUDIO)
        user_audio_name = custom["audio_file"]
        status, _headers, user_audio_before = http_raw(port, f"/user-audio/{user_audio_name}")
        assert_true(status == 200 and user_audio_before == TEST_AUDIO, "Seed user_audio mismatch")
        report["checks"].append("Seed progress/settings/Saved/My Island/user_audio")

        http_json(port, "/api/data/reset-progress", {})
        assert_true(table_count(db, "review_log") == 0, "Reset Progress retained review history")
        assert_true(table_count(db, "saved_items") == 1, "Reset Progress removed Saved items")
        assert_true(table_count(db, "my_islands") == 1, "Reset Progress removed My Island data")
        assert_true(setting(db, "new_per_day") == "37", "Reset Progress changed Settings")
        status, _headers, reset_audio = http_raw(port, f"/user-audio/{user_audio_name}")
        assert_true(status == 200 and reset_audio == TEST_AUDIO, "Reset Progress removed user_audio")
        report["checks"].append("Reset Progress clears learning history and preserves personal data")

        # Recreate one review so Backup/Restore can prove Learn/Review state is
        # included in the packaged EXE round trip.
        http_json(port, "/api/review", {"item_key": first_item["item_key"], "rating": 4, "source_mode": "exe-test"})

        status, _headers, backup_raw = http_raw(port, "/api/data/backup", timeout=120)
        assert_true(status == 200 and backup_raw.startswith(b"PK"), "Backup ZIP download failed")
        backup_file = localappdata / "exe-user-data-backup.zip"
        backup_file.write_bytes(backup_raw)
        with zipfile.ZipFile(backup_file) as archive:
            names = set(archive.namelist())
            assert_true("manifest.json" in names and f"user_audio/{user_audio_name}" in names, "Backup contents incomplete")
        report["checks"].append("Backup ZIP from packaged EXE")

        http_json(port, "/api/data/delete-all", {})
        http_json(port, "/api/setting", {"key": "new_per_day", "value": 5})
        mutated = http_json(port, "/api/my-island/create", {"name": "EXE Mutated Island", "description": "must disappear"})
        create_custom(port, mutated["id"], "Mutated sentence.", b"ID3-mutated")
        mutated_audio = (profile_base / "user_audio" / user_audio_name).read_bytes()
        assert_true(mutated_audio != TEST_AUDIO, "Delete All retained backed-up audio bytes")

        # Normal restore leaves validated staging for the next manual launch;
        # it must not spawn a hidden packaged helper process.
        old_pid = runtime["pid"]
        status, _headers, response_raw = http_raw(
            port, "/api/data/restore", backup_raw, "application/zip", timeout=180
        )
        response = json.loads(response_raw.decode("utf-8"))
        assert_true(status == 202 and response.get("closing"), "Restore was not accepted")
        wait_pid_gone(old_pid, timeout=15)
        pending = find_pending(profile_base)
        assert_true(pending.exists(), "Prepared restore disappeared before next launch")
        deadline = time.monotonic() + 10
        while packaged_app_pids() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert_true(not packaged_app_pids(), "EXE remained running after restore shutdown")

        initial, runtime, boot = open_app_manually(exe, env, profile_base, previous_pid=old_pid)
        app_pids.append(runtime["pid"])
        port = runtime["port"]
        result = boot.get("restoreResult") or {}
        assert_true(result.get("ok") and result.get("message") == "Khôi phục dữ liệu thành công.", "Restore result mismatch")
        assert_pending_clean(profile_base)
        assert_true(setting(db, "new_per_day") == "37", "Setting was not restored")
        assert_true(table_count(db, "saved_items") == 1, "Saved item was not restored")
        assert_true(table_count(db, "review_log") >= 1, "Review history was not restored")
        assert_true(table_count(db, "my_islands") == 1, "My Island state was not restored")
        status, _headers, user_audio_after = http_raw(port, f"/user-audio/{user_audio_name}")
        assert_true(status == 200 and user_audio_after == TEST_AUDIO, "user_audio was not restored byte-for-byte")
        report["checks"].append("Restore closes EXE completely; next manual launch restores before server starts")

        for cycle in (2, 3):
            http_json(port, "/api/setting", {"key": "new_per_day", "value": cycle})
            old_pid = runtime["pid"]
            status, _headers, _raw = http_raw(
                port, "/api/data/restore", backup_raw, "application/zip", timeout=180
            )
            assert_true(status == 202, f"Consecutive restore {cycle} was not accepted")
            wait_pid_gone(old_pid, timeout=15)
            assert_true(find_pending(profile_base).exists(), f"Consecutive restore {cycle} did not leave prepared state")
            assert_true(not packaged_app_pids(), f"Hidden process remained after restore shutdown {cycle}")
            initial, runtime, boot = open_app_manually(exe, env, profile_base, previous_pid=old_pid)
            app_pids.append(runtime["pid"])
            port = runtime["port"]
            assert_true((boot.get("restoreResult") or {}).get("ok"), f"Consecutive restore {cycle} result failed")
            assert_true(setting(db, "new_per_day") == "37", f"Consecutive restore {cycle} data mismatch")
            assert_pending_clean(profile_base)
        report["checks"].append("Three consecutive restore cycles without background helper")

        status, _headers, xlsx = http_raw(port, "/template/my-island.xlsx")
        xlsx_file = localappdata / "template.xlsx"
        xlsx_file.write_bytes(xlsx)
        with zipfile.ZipFile(xlsx_file) as archive:
            assert_true("xl/workbook.xml" in archive.namelist(), "Downloaded XLSX is invalid")
        audio_urls = [item.get("audio") for item in collection["items"] if item.get("audio")][:3]
        assert_true(len(audio_urls) >= 3, "Not enough bundled audio URLs for smoke test")
        for url in audio_urls:
            status, _headers, audio = http_raw(port, url)
            assert_true(status == 200 and len(audio) > 100, f"Bundled audio failed: {url}")
        report["checks"].append("XLSX download and 3 bundled audio files after restore")

        http_json(port, "/api/setting", {"key": "new_per_day", "value": 88})
        rollback_island = http_json(port, "/api/my-island/create", {"name": "EXE Rollback Current", "description": "safety-state"})
        rollback_custom = create_custom(port, rollback_island["id"], "Rollback safety sentence.", b"ID3-rollback-safety")
        rollback_audio = rollback_custom["audio_file"]
        old_pid = runtime["pid"]
        status, _headers, _raw = http_raw(port, "/api/data/restore", backup_raw, "application/zip", timeout=180)
        assert_true(status == 202, "Forced rollback restore was not accepted")
        wait_pid_gone(old_pid, timeout=15)
        pending = find_pending(profile_base)
        pending_data = json.loads(pending.read_text(encoding="utf-8"))
        Path(pending_data["incomingDb"]).write_bytes(b"forced corrupt database after validation")
        initial, runtime, boot = open_app_manually(exe, env, profile_base, previous_pid=old_pid)
        app_pids.append(runtime["pid"])
        port = runtime["port"]
        result = boot.get("restoreResult") or {}
        assert_true(not result.get("ok") and result.get("rollbackOk"), "Forced rollback result mismatch")
        assert_pending_clean(profile_base)
        assert_true(setting(db, "new_per_day") == "88", "Safety rollback did not restore current setting")
        status, _headers, rollback_audio_raw = http_raw(port, f"/user-audio/{rollback_audio}")
        assert_true(rollback_audio_raw == b"ID3-rollback-safety", "Safety rollback did not restore user_audio")
        report["checks"].append("Forced corruption rolls back on next manual launch")

        http_json(port, "/api/setting", {"key": "new_per_day", "value": 91})
        locker, release, _wal = start_locker(db, localappdata, "external-lock")
        lockers.append((locker, release))
        old_pid = runtime["pid"]
        status, _headers, _raw = http_raw(port, "/api/data/restore", backup_raw, "application/zip", timeout=180)
        assert_true(status == 202, "External-lock restore was not accepted")
        wait_pid_gone(old_pid, timeout=15)
        started_at = time.monotonic()
        initial, runtime, boot = open_app_manually(exe, env, profile_base, previous_pid=old_pid)
        elapsed = time.monotonic() - started_at
        app_pids.append(runtime["pid"])
        port = runtime["port"]
        result = boot.get("restoreResult") or {}
        assert_true(not result.get("ok") and result.get("rollbackOk"), "External lock did not produce safe failure")
        assert_true("SQLite/WAL" in str(result.get("error", "")), "External lock error was not explicit")
        assert_true(4.0 <= elapsed <= 9.0, f"External lock timeout was not near 5 seconds: {elapsed:.2f}s")
        assert_true(setting(db, "new_per_day") == "91", "External lock changed current profile")
        assert_pending_clean(profile_base)
        release_locker(locker, release)
        lockers.remove((locker, release))
        report["checks"].append(f"External SQLite/WAL owner fails safely in {elapsed:.2f}s; app remains usable")

        active_packaged = packaged_app_pids()
        assert_true(active_packaged == {int(runtime["pid"])}, f"Orphan EXE processes remain: {active_packaged}")
        report["checks"].append("No pending session or orphan hidden EXE process")

        report["exeSha256"] = hashlib.sha256(exe.read_bytes()).hexdigest()
        report["finalPid"] = runtime["pid"]
        report["ok"] = True
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        for process, release in lockers:
            release.touch(exist_ok=True)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
        runtime = read_runtime(profile_base)
        if runtime:
            terminate_pid(runtime.get("pid"))
        for pid in app_pids:
            terminate_pid(pid)


if __name__ == "__main__":
    raise SystemExit(main())
