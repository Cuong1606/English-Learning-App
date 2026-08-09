#!/usr/bin/env python3
"""Regression tests for user-data reset, delete, backup, restore, and scope."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server as sv
import restore_helper as rh


@contextmanager
def temp_profile(prefix="english_data_test_"):
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    old = (sv.USER_DIR, sv.USER_DB, sv.USER_AUDIO)
    sv.USER_DIR = tmp / "EnglishLocal" / "user_data"
    sv.USER_DB = sv.USER_DIR / "learning.sqlite"
    sv.USER_AUDIO = tmp / "EnglishLocal" / "user_audio"
    try:
        sv.user_conn().close()
        yield tmp
    finally:
        sv.USER_DIR, sv.USER_DB, sv.USER_AUDIO = old
        shutil.rmtree(tmp, ignore_errors=True)


def first_standard_key():
    with sv.content_conn() as con:
        content_id = con.execute("SELECT content_id FROM sentence_content ORDER BY content_id LIMIT 1").fetchone()[0]
        return sv.item_key_for_content(content_id, con)


def seed_profile():
    key = first_standard_key()
    with sv.user_conn() as con:
        sv.setting_set(con, "test_marker", "keep-me")
        con.execute(
            "INSERT INTO collection_progress(collection_key,last_index,updated_at_ts) VALUES('core:219',7,1)"
        )
        con.execute("INSERT INTO saved_items(item_key,saved_at_ts) VALUES(?,1)", (key,))
        con.execute("INSERT INTO suspended_items(item_key,suspended_at_ts) VALUES(?,1)", (key,))
        con.commit()
    sv.apply_review(key, 4, "data-test")
    island_id = sv.create_my_island("Backup Island", "regression")["id"]
    audio = base64.b64encode(b"ID3-test-user-audio").decode("ascii")
    custom = sv.create_custom_sentence(
        "A private sentence.",
        "Một câu cá nhân.",
        audio_data=audio,
        audio_name="voice.mp3",
        audio_type="audio/mpeg",
        island_id=island_id,
    )
    return {"key": key, "island_id": island_id, "custom": custom}


def backup_bytes(tmp):
    output = tmp / "export" / "backup.zip"
    sv.create_user_backup(output)
    return output.read_bytes()


def prepare_restore(raw, tmp):
    archive = tmp / f"restore-{time.time_ns()}.zip"
    archive.write_bytes(raw)
    return sv.prepare_user_restore(
        archive,
    )


def apply_restore(raw, tmp, after_swap=None):
    prepared = prepare_restore(raw, tmp)
    return rh.apply_pending_restore(
        Path(prepared["pendingFile"]),
        after_swap=after_swap,
    )


def archive_parts(raw):
    with zipfile.ZipFile(io.BytesIO(raw), "r") as archive:
        return {info.filename: archive.read(info.filename) for info in archive.infolist()}


def build_archive(parts):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return out.getvalue()


def mutate_manifest(raw, callback):
    parts = archive_parts(raw)
    manifest = json.loads(parts["manifest.json"].decode("utf-8"))
    callback(manifest)
    parts["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    return build_archive(parts)


def table_count(table):
    with sv.user_conn() as con:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_reset_progress_returns_fsrs_to_new():
    with temp_profile():
        data = seed_profile()
        sv.reset_learning_progress()
        assert table_count("fsrs_cards") == 0
        assert sv.get_srs_info(item_key=data["key"])["state"] == "New"


def test_reset_progress_deletes_review_log():
    with temp_profile():
        seed_profile()
        assert table_count("review_log") > 0
        sv.reset_learning_progress()
        assert table_count("review_log") == 0


def test_reset_progress_deletes_suspended_state():
    with temp_profile():
        seed_profile()
        sv.reset_learning_progress()
        assert table_count("suspended_items") == 0


def test_reset_progress_keeps_saved():
    with temp_profile():
        seed_profile()
        sv.reset_learning_progress()
        assert table_count("saved_items") == 1


def test_reset_progress_keeps_my_islands():
    with temp_profile():
        seed_profile()
        sv.reset_learning_progress()
        assert table_count("my_islands") == 1
        assert table_count("my_island_members") == 1


def test_reset_progress_keeps_custom_sentences():
    with temp_profile():
        seed_profile()
        sv.reset_learning_progress()
        assert table_count("custom_sentences") == 1


def test_reset_progress_keeps_settings():
    with temp_profile():
        seed_profile()
        sv.reset_learning_progress()
        with sv.user_conn() as con:
            assert sv.setting_get(con, "test_marker") == "keep-me"


def test_reset_progress_keeps_user_audio():
    with temp_profile():
        data = seed_profile()
        audio = sv.USER_AUDIO / data["custom"]["audio_file"]
        before = audio.read_bytes()
        sv.reset_learning_progress()
        assert audio.read_bytes() == before


def test_delete_all_scope_and_runtime_files():
    with temp_profile() as tmp:
        seed_profile()
        base = sv.USER_DIR.parent
        log = base / "launcher_server.log"
        runtime = base / "runtime.json"
        log.write_text("keep log", encoding="utf-8")
        runtime.write_text("{}", encoding="utf-8")
        sv.delete_all_user_data()
        for table in sv.REQUIRED_USER_SCHEMA:
            if table != "app_settings":
                assert table_count(table) == 0, table
        assert list(sv.USER_AUDIO.iterdir()) == []
        assert log.read_text(encoding="utf-8") == "keep log"
        assert runtime.read_text(encoding="utf-8") == "{}"
        assert not list(base.glob(".delete_all_stage_*"))


def test_delete_all_creates_valid_fresh_profile():
    with temp_profile():
        seed_profile()
        result = sv.delete_all_user_data()
        assert result["schemaVersion"] == sv.USER_DB_SCHEMA_VERSION
        with sv.user_conn() as con:
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert con.execute("PRAGMA user_version").fetchone()[0] == sv.USER_DB_SCHEMA_VERSION
            settings = dict(con.execute("SELECT key,value FROM app_settings"))
        assert settings == sv.USER_SETTING_DEFAULTS


def test_backup_with_active_wal_is_consistent():
    with temp_profile() as tmp:
        raw_con = sqlite3.connect(sv.USER_DB)
        try:
            raw_con.execute("PRAGMA journal_mode=WAL")
            raw_con.execute("INSERT INTO app_settings(key,value) VALUES('wal_marker','committed-in-wal')")
            raw_con.commit()
            assert Path(str(sv.USER_DB) + "-wal").exists()
            raw = backup_bytes(tmp)
        finally:
            raw_con.close()
        parts = archive_parts(raw)
        db = tmp / "snapshot.sqlite"
        db.write_bytes(parts["user_data/learning.sqlite"])
        con = sqlite3.connect(db)
        try:
            assert con.execute("SELECT value FROM app_settings WHERE key='wal_marker'").fetchone()[0] == "committed-in-wal"
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            con.close()


def test_backup_contains_database_manifest_checksums_and_audio_only():
    with temp_profile() as tmp:
        data = seed_profile()
        raw = backup_bytes(tmp)
        parts = archive_parts(raw)
        manifest = json.loads(parts["manifest.json"].decode("utf-8"))
        audio_name = f'user_audio/{data["custom"]["audio_file"]}'
        assert set(parts) == {"manifest.json", "user_data/learning.sqlite", audio_name}
        assert manifest["appVersion"] == sv.APP_VERSION
        assert manifest["backupFormatVersion"] == sv.BACKUP_FORMAT_VERSION
        assert manifest["userDbSchemaVersion"] == sv.USER_DB_SCHEMA_VERSION
        for name, meta in manifest["files"].items():
            assert meta["size"] == len(parts[name])
            assert meta["sha256"] == hashlib.sha256(parts[name]).hexdigest()
        assert not any("wal" in name or "shm" in name or "runtime" in name or "log" in name for name in parts)


def test_restore_success_recovers_all_user_data():
    with temp_profile() as tmp:
        data = seed_profile()
        raw = backup_bytes(tmp)
        sv.delete_all_user_data()
        assert table_count("saved_items") == 0
        result = apply_restore(raw, tmp)
        assert result["ok"]
        with sv.user_conn() as con:
            assert sv.setting_get(con, "test_marker") == "keep-me"
        assert table_count("saved_items") == 1
        assert table_count("my_islands") == 1
        assert table_count("custom_sentences") == 1
        assert (sv.USER_AUDIO / data["custom"]["audio_file"]).exists()


def test_restore_http_accepts_zip_directly_and_schedules_shutdown():
    with temp_profile() as tmp:
        seed_profile()
        raw = backup_bytes(tmp)
        shutdown_called = threading.Event()
        old_lifecycle = sv.RESTORE_SHUTDOWN_CALLBACK
        httpd = sv.ThreadingHTTPServer((sv.HOST, 0), sv.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        try:
            sv.configure_restore_lifecycle(shutdown_called.set)
            thread.start()
            request = urllib.request.Request(
                f"http://{sv.HOST}:{httpd.server_address[1]}/api/data/restore",
                data=raw,
                method="POST",
                headers={"Content-Type": "application/zip"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
                assert response.status == 202
            assert payload["accepted"] and payload["closing"]
            markers = sv.pending_restore_markers()
            assert len(markers) == 1 and markers[0].name == "pending.json"
            assert shutdown_called.wait(5)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            for marker in sv.pending_restore_markers():
                shutil.rmtree(marker.parent, ignore_errors=True)
            sv.RESTORE_SHUTDOWN_CALLBACK = old_lifecycle
            sv.RESTORE_SHUTDOWN_PENDING.clear()


def test_restore_bad_checksum_keeps_current_data():
    with temp_profile() as tmp:
        seed_profile()
        raw = backup_bytes(tmp)
        parts = archive_parts(raw)
        audio_name = next(name for name in parts if name.startswith("user_audio/"))
        parts[audio_name] += b"tampered"
        bad = build_archive(parts)
        with sv.user_conn() as con:
            sv.setting_set(con, "current_marker", "must-survive")
            con.commit()
        try:
            prepare_restore(bad, tmp)
            raise AssertionError("checksum mismatch was accepted")
        except ValueError as exc:
            assert "Checksum" in str(exc) or "kích thước" in str(exc)
        with sv.user_conn() as con:
            assert sv.setting_get(con, "current_marker") == "must-survive"


def test_restore_rejects_zip_traversal():
    with temp_profile() as tmp:
        seed_profile()
        parts = archive_parts(backup_bytes(tmp))
        parts["../escape.txt"] = b"blocked"
        try:
            prepare_restore(build_archive(parts), tmp)
            raise AssertionError("ZIP traversal was accepted")
        except ValueError as exc:
            assert "traversal" in str(exc)
        assert not (tmp / "escape.txt").exists()
        with sv.user_conn() as con:
            assert sv.setting_get(con, "test_marker") == "keep-me"


def test_restore_rejects_corrupt_database():
    with temp_profile() as tmp:
        seed_profile()
        parts = archive_parts(backup_bytes(tmp))
        corrupt = b"not a sqlite database"
        parts["user_data/learning.sqlite"] = corrupt
        manifest = json.loads(parts["manifest.json"].decode("utf-8"))
        manifest["files"]["user_data/learning.sqlite"] = {
            "size": len(corrupt), "sha256": hashlib.sha256(corrupt).hexdigest()
        }
        parts["manifest.json"] = json.dumps(manifest).encode("utf-8")
        try:
            prepare_restore(build_archive(parts), tmp)
            raise AssertionError("corrupt database was accepted")
        except ValueError as exc:
            assert "Database" in str(exc) or "database" in str(exc)
        with sv.user_conn() as con:
            assert sv.setting_get(con, "test_marker") == "keep-me"


def test_restore_rejects_extreme_compression_zip_bomb():
    with temp_profile() as tmp:
        seed_profile()
        parts = archive_parts(backup_bytes(tmp))
        payload = b"\0" * (4 * 1024 * 1024)
        parts["user_data/learning.sqlite"] = payload
        manifest = json.loads(parts["manifest.json"].decode("utf-8"))
        manifest["files"]["user_data/learning.sqlite"] = {
            "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()
        }
        parts["manifest.json"] = json.dumps(manifest).encode("utf-8")
        try:
            prepare_restore(build_archive(parts), tmp)
            raise AssertionError("Extreme compression ZIP bomb was accepted")
        except ValueError as exc:
            assert "ZIP bomb" in str(exc)
        with sv.user_conn() as con:
            assert sv.setting_get(con, "test_marker") == "keep-me"


def test_restore_version_and_schema_compatibility():
    with temp_profile() as tmp:
        seed_profile()
        raw = backup_bytes(tmp)
        bad_format = mutate_manifest(raw, lambda m: m.update(backupFormatVersion=sv.BACKUP_FORMAT_VERSION + 1))
        bad_schema = mutate_manifest(raw, lambda m: m.update(userDbSchemaVersion=sv.USER_DB_SCHEMA_VERSION + 1))
        for candidate in (bad_format, bad_schema):
            try:
                prepare_restore(candidate, tmp)
                raise AssertionError("incompatible backup was accepted")
            except ValueError:
                pass
        other_app_version = mutate_manifest(raw, lambda m: m.update(appVersion="99.0.0"))
        prepared = prepare_restore(other_app_version, tmp)
        assert prepared["restoredAppVersion"] == "99.0.0"
        result = rh.apply_pending_restore(Path(prepared["pendingFile"]))
        assert result["ok"] and result["restoredAppVersion"] == "99.0.0"


def test_restore_rolls_back_if_install_fails():
    with temp_profile() as tmp:
        seed_profile()
        raw = backup_bytes(tmp)
        with sv.user_conn() as con:
            sv.setting_set(con, "current_marker", "rollback-me")
            con.commit()
        def fail_after_swap():
            raise RuntimeError("forced post-swap failure")
        result = apply_restore(raw, tmp, after_swap=fail_after_swap)
        assert not result["ok"]
        assert result["rollbackOk"]
        assert "forced post-swap failure" in result["error"]
        with sv.user_conn() as con:
            assert sv.setting_get(con, "current_marker") == "rollback-me"


def test_restore_lock_timeout_never_touches_current_profile():
    with temp_profile() as tmp:
        seed_profile()
        original = backup_bytes(tmp)
        with sv.user_conn() as con:
            sv.setting_set(con, "current_marker", "untouched-on-timeout")
            con.commit()
        prepared = prepare_restore(original, tmp)
        pending = Path(prepared["pendingFile"])
        old_wait = rh.wait_for_profile_unlock
        try:
            rh.wait_for_profile_unlock = lambda *_args, **_kwargs: False
            result = rh.apply_pending_restore(pending, lock_timeout=0.01)
        finally:
            rh.wait_for_profile_unlock = old_wait
        assert not result["ok"] and result["rollbackOk"]
        assert "SQLite/WAL" in result["error"]
        with sv.user_conn() as con:
            assert sv.setting_get(con, "current_marker") == "untouched-on-timeout"
        assert not pending.parent.exists()


def test_prepared_session_is_completed_on_next_startup_without_background_helper():
    with temp_profile() as tmp:
        seed_profile()
        original = backup_bytes(tmp)
        with sv.user_conn() as con:
            sv.setting_set(con, "current_marker", "must-be-replaced")
            con.commit()
        prepared = prepare_restore(original, tmp)
        pending = Path(prepared["pendingFile"])
        if os.name == "nt":
            for path in [sv.USER_DB, *sv._db_sidecars()]:
                assert rh._windows_file_is_exclusive(path), f"Unexpected lock: {path}"
        result = sv.complete_pending_restore_if_needed(lock_timeout=1.0)
        assert result and result["ok"]
        assert not pending.parent.exists()
        with sv.user_conn() as con:
            assert sv.setting_get(con, "test_marker") == "keep-me"
            assert sv.setting_get(con, "current_marker") is None


def test_interrupted_swapping_session_automatically_rolls_back_safety_snapshot():
    with temp_profile() as tmp:
        seed_profile()
        original = backup_bytes(tmp)
        with sv.user_conn() as con:
            sv.setting_set(con, "current_marker", "safety-must-win")
            con.commit()
        prepared = prepare_restore(original, tmp)
        pending = Path(prepared["pendingFile"])
        state = pending.parent / "restore_state.json"
        state.write_text(json.dumps({"status": "swapping", "updatedAt": time.time()}), encoding="utf-8")
        sv.USER_DB.write_bytes(b"partial database left by killed helper")
        shutil.rmtree(sv.USER_AUDIO)
        result = rh.apply_pending_restore(pending)
        assert not result["ok"] and result["rollbackOk"]
        with sv.user_conn() as con:
            assert sv.setting_get(con, "current_marker") == "safety-must-win"
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not pending.parent.exists()


def test_restore_can_complete_three_consecutive_sessions():
    with temp_profile() as tmp:
        seed_profile()
        original = backup_bytes(tmp)
        for index in range(3):
            with sv.user_conn() as con:
                sv.setting_set(con, "current_marker", f"mutation-{index}")
                con.commit()
            result = apply_restore(original, tmp)
            assert result["ok"]
            with sv.user_conn() as con:
                assert sv.setting_get(con, "test_marker") == "keep-me"
                assert sv.setting_get(con, "current_marker") is None
            assert not sv.pending_restore_markers()


def test_windows_restore_waits_for_sqlite_wal_lock_before_swap():
    if os.name != "nt":
        return
    with temp_profile("english_windows_restore_") as tmp:
        seed_profile()
        original = backup_bytes(tmp)
        with sv.user_conn() as con:
            sv.setting_set(con, "current_marker", "must-remain-while-locked")
            con.commit()

        ready = tmp / "locker.ready"
        release = tmp / "locker.release"
        holder = ROOT / "tests" / "windows_sqlite_lock_holder.py"
        locker = subprocess.Popen(
            [sys.executable, str(holder), str(sv.USER_DB), str(ready), str(release)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        helper = None
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and time.monotonic() < deadline:
                if locker.poll() is not None:
                    raise AssertionError(locker.stderr.read())
                time.sleep(0.05)
            assert ready.exists(), "SQLite lock holder did not start"
            wal = Path(str(sv.USER_DB) + "-wal")
            assert wal.exists() and wal.stat().st_size > 0
            assert not rh._windows_file_is_exclusive(sv.USER_DB)

            prepared = prepare_restore(original, tmp)
            helper = subprocess.Popen(
                [
                    sys.executable, str(ROOT / "restore_helper.py"),
                    "--pending", prepared["pendingFile"],
                    "--wait-pid", str(locker.pid),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(1.0)
            assert helper.poll() is None, "Helper swapped while SQLite/WAL was still locked"
            with sv.user_conn() as con:
                assert sv.setting_get(con, "current_marker") == "must-remain-while-locked"

            release.write_text("release", encoding="utf-8")
            assert locker.wait(timeout=15) == 0
            assert helper.wait(timeout=30) == 0, helper.stderr.read()
            with sv.user_conn() as con:
                assert sv.setting_get(con, "test_marker") == "keep-me"
                assert sv.setting_get(con, "current_marker") is None
                assert sv.setting_get(con, "windows_lock_marker") is None
            assert not wal.exists()
        finally:
            release.touch(exist_ok=True)
            if locker.poll() is None:
                locker.terminate()
                locker.wait(timeout=10)
            if helper is not None and helper.poll() is None:
                helper.terminate()
                helper.wait(timeout=10)


def test_content_database_is_unchanged():
    before = hashlib.sha256(sv.CONTENT_DB.read_bytes()).hexdigest()
    with temp_profile() as tmp:
        seed_profile()
        raw = backup_bytes(tmp)
        sv.reset_learning_progress()
        sv.delete_all_user_data()
        assert apply_restore(raw, tmp)["ok"]
    after = hashlib.sha256(sv.CONTENT_DB.read_bytes()).hexdigest()
    assert after == before


def test_bundled_audio_directories_are_unchanged():
    with temp_profile() as tmp:
        old = (sv.AUDIO, sv.COURSE_AUDIO)
        sv.AUDIO = tmp / "bundled-audio"
        sv.COURSE_AUDIO = tmp / "bundled-course-audio"
        sv.AUDIO.mkdir()
        sv.COURSE_AUDIO.mkdir()
        (sv.AUDIO / "keep.mp3").write_bytes(b"core")
        (sv.COURSE_AUDIO / "keep.mp3").write_bytes(b"course")
        try:
            seed_profile()
            sv.reset_learning_progress()
            sv.delete_all_user_data()
            assert (sv.AUDIO / "keep.mp3").read_bytes() == b"core"
            assert (sv.COURSE_AUDIO / "keep.mp3").read_bytes() == b"course"
        finally:
            sv.AUDIO, sv.COURSE_AUDIO = old


def test_open_user_data_folder_uses_windows_profile_root():
    with temp_profile():
        opened = []
        result = sv.open_user_data_folder(opener=opened.append, platform_name="nt")
        assert opened == [str(sv.USER_DIR.parent.resolve())]
        assert result["path"] == opened[0]


def run_all():
    tests = [
        test_reset_progress_returns_fsrs_to_new,
        test_reset_progress_deletes_review_log,
        test_reset_progress_deletes_suspended_state,
        test_reset_progress_keeps_saved,
        test_reset_progress_keeps_my_islands,
        test_reset_progress_keeps_custom_sentences,
        test_reset_progress_keeps_settings,
        test_reset_progress_keeps_user_audio,
        test_delete_all_scope_and_runtime_files,
        test_delete_all_creates_valid_fresh_profile,
        test_backup_with_active_wal_is_consistent,
        test_backup_contains_database_manifest_checksums_and_audio_only,
        test_restore_success_recovers_all_user_data,
        test_restore_http_accepts_zip_directly_and_schedules_shutdown,
        test_restore_bad_checksum_keeps_current_data,
        test_restore_rejects_zip_traversal,
        test_restore_rejects_corrupt_database,
        test_restore_rejects_extreme_compression_zip_bomb,
        test_restore_version_and_schema_compatibility,
        test_restore_rolls_back_if_install_fails,
        test_restore_lock_timeout_never_touches_current_profile,
        test_prepared_session_is_completed_on_next_startup_without_background_helper,
        test_interrupted_swapping_session_automatically_rolls_back_safety_snapshot,
        test_restore_can_complete_three_consecutive_sessions,
        test_windows_restore_waits_for_sqlite_wal_lock_before_swap,
        test_content_database_is_unchanged,
        test_bundled_audio_directories_are_unchanged,
        test_open_user_data_folder_uses_windows_profile_root,
    ]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"PASS ALL: {len(tests)} data-management test groups")


if __name__ == "__main__":
    run_all()
