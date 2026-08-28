import base64
import io
import os
import shutil
import sys
import tempfile
import threading
import types
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server as sv
import bulk_audio as bulk_audio_module
from audio_index import BUNDLED_AUDIO_INDEX
from bulk_audio import BulkAudioSessions


def temp_user(prefix):
    root = Path(tempfile.mkdtemp(prefix=prefix))
    old = (sv.USER_DIR, sv.USER_DB, sv.USER_AUDIO)
    sv.USER_DIR = root / "EnglishLocal" / "user_data"
    sv.USER_DB = sv.USER_DIR / "learning.sqlite"
    sv.USER_AUDIO = root / "EnglishLocal" / "user_audio"
    sv.user_conn().close()
    return root, old


def restore_user(root, old):
    sv.USER_DIR, sv.USER_DB, sv.USER_AUDIO = old
    shutil.rmtree(root, ignore_errors=True)


def test_audio_index_builds_once_and_invalidates_explicitly():
    root = Path(tempfile.mkdtemp(prefix="english_audio_index_"))
    try:
        core, course = root / "audio", root / "course_audio"
        core.mkdir(); (course / "book").mkdir(parents=True)
        (core / "000001.mp3").write_bytes(b"core")
        (course / "book" / "one.mp3").write_bytes(b"course")
        BUNDLED_AUDIO_INDEX.invalidate()
        before = BUNDLED_AUDIO_INDEX.diagnostics()["buildCount"]
        assert BUNDLED_AUDIO_INDEX.has_core("000001.mp3", core, course)
        assert BUNDLED_AUDIO_INDEX.has_course("book/one.mp3", core, course)
        assert BUNDLED_AUDIO_INDEX.diagnostics()["buildCount"] == before + 1
        (course / "book" / "two.mp3").write_bytes(b"new")
        assert not BUNDLED_AUDIO_INDEX.has_course("book/two.mp3", core, course)
        BUNDLED_AUDIO_INDEX.invalidate()
        assert BUNDLED_AUDIO_INDEX.has_course("book/two.mp3", core, course)
    finally:
        BUNDLED_AUDIO_INDEX.invalidate()
        shutil.rmtree(root, ignore_errors=True)


def test_user_audio_availability_is_live_not_cached():
    root, old = temp_user("english_user_audio_live_")
    try:
        created = sv.create_custom_sentence(
            "Live user audio", "Audio người dùng", audio_data=base64.b64encode(b"audio").decode("ascii"),
            audio_name="live.mp3", audio_type="audio/mpeg",
        )
        custom_id = int(created["custom_id"])
        assert sv.get_custom_item(custom_id)["audio"]
        path = sv.USER_AUDIO / created["audio_file"]
        path.unlink()
        assert sv.get_custom_item(custom_id)["audio"] is None
        path.write_bytes(b"restored")
        assert sv.get_custom_item(custom_id)["audio"]
    finally:
        restore_user(root, old)


def test_custom_sentence_audio_rolls_back_with_database_failure():
    root, old = temp_user("english_custom_audio_atomic_")
    try:
        island = sv.create_my_island("Atomic Audio", "")
        with sv.user_conn() as u:
            u.execute(
                "CREATE TRIGGER fail_atomic_member BEFORE INSERT ON my_island_members "
                "BEGIN SELECT RAISE(ABORT, 'simulated island failure'); END"
            )
            u.commit()
        try:
            sv.create_custom_sentence(
                "Must roll back", "Phải rollback", island_id=island["id"],
                audio_data=base64.b64encode(b"atomic-audio").decode("ascii"),
                audio_name="atomic.mp3", audio_type="audio/mpeg",
            )
            raise AssertionError("simulated database failure was accepted")
        except Exception as exc:
            assert "simulated island failure" in str(exc)
        assert not list(sv.USER_AUDIO.iterdir())
        with sv.user_conn() as u:
            assert u.execute("SELECT COUNT(*) FROM custom_sentences WHERE en_us='Must roll back'").fetchone()[0] == 0
    finally:
        restore_user(root, old)


def _minimal_import_xlsx(extra_members=None, worksheet_override=None):
    workbook = b'''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="Import" sheetId="1" r:id="rId1"/></sheets>
</workbook>'''
    relationships = b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
</Relationships>'''
    worksheet = worksheet_override or b'''<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
 <row r="1"><c r="A1" t="inlineStr"><is><t>Audio&#9;   Key</t></is></c><c r="B1" t="inlineStr"><is><t>English</t></is></c></row>
 <row r="2"><c r="A2" t="inlineStr"><is><t>T001</t></is></c><c r="B2" t="inlineStr"><is><t>Hello from XLSX</t></is></c></row>
</sheetData></worksheet>'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
        for name, data in extra_members or []:
            archive.writestr(name, data)
    return output.getvalue()


def test_xlsx_headers_and_archive_limits():
    rows = sv.parse_import_xlsx(_minimal_import_xlsx())
    assert rows == [{
        "audio_key": "T001", "en_us": "Hello from XLSX", "vi_vn": "",
        "audio_expected": "", "note": "",
    }]

    old_limits = (
        sv.MAX_XLSX_ARCHIVE_BYTES,
        sv.MAX_XLSX_ZIP_MEMBERS,
        sv.MAX_XLSX_XML_MEMBER_BYTES,
        sv.MAX_XLSX_TOTAL_XML_BYTES,
    )
    try:
        sv.MAX_XLSX_ARCHIVE_BYTES = 32
        try:
            sv.parse_import_xlsx(_minimal_import_xlsx())
            raise AssertionError("XLSX archive limit was not enforced")
        except ValueError as exc:
            assert "XLSX quá lớn" in str(exc)

        sv.MAX_XLSX_ARCHIVE_BYTES = old_limits[0]
        sv.MAX_XLSX_ZIP_MEMBERS = 3
        try:
            sv.parse_import_xlsx(_minimal_import_xlsx([("extra.xml", b"<x/>")]))
            raise AssertionError("XLSX member limit was not enforced")
        except ValueError as exc:
            assert "quá nhiều thành phần" in str(exc)

        sv.MAX_XLSX_ZIP_MEMBERS = old_limits[1]
        sv.MAX_XLSX_XML_MEMBER_BYTES = 1024
        try:
            sv.parse_import_xlsx(_minimal_import_xlsx([("bomb.xml", b"x" * 4096)]))
            raise AssertionError("XLSX XML member limit was not enforced")
        except ValueError as exc:
            assert "XML" in str(exc) and "quá lớn" in str(exc)

        sv.MAX_XLSX_XML_MEMBER_BYTES = old_limits[2]
        sv.MAX_XLSX_TOTAL_XML_BYTES = 512
        try:
            sv.parse_import_xlsx(_minimal_import_xlsx())
            raise AssertionError("XLSX total XML limit was not enforced")
        except ValueError as exc:
            assert "Tổng XML" in str(exc)

        sv.MAX_XLSX_TOTAL_XML_BYTES = old_limits[3]
        try:
            sv.parse_import_xlsx(_minimal_import_xlsx([("../escape.xml", b"<x/>")]))
            raise AssertionError("unsafe XLSX member path was accepted")
        except ValueError as exc:
            assert "không an toàn" in str(exc)

        try:
            sv.parse_import_xlsx(_minimal_import_xlsx(worksheet_override=b"<worksheet"))
            raise AssertionError("malformed XLSX XML was accepted")
        except ValueError as exc:
            assert "XLSX không hợp lệ" in str(exc)
    finally:
        (
            sv.MAX_XLSX_ARCHIVE_BYTES,
            sv.MAX_XLSX_ZIP_MEMBERS,
            sv.MAX_XLSX_XML_MEMBER_BYTES,
            sv.MAX_XLSX_TOTAL_XML_BYTES,
        ) = old_limits


def test_recall_rating_matches_fsrs_and_returns_exact_local_counters():
    root, old = temp_user("english_recall_local_")
    original_now = sv.now_ts
    fixed = 1_800_000_000.0
    try:
        sv.now_ts = lambda: fixed
        item = sv.daily_session()["items"][0]
        for rating in (1, 2, 3, 4):
            with sv.user_conn() as u:
                u.execute("DELETE FROM review_log"); u.execute("DELETE FROM fsrs_cards"); u.commit()
            expected = sv.schedule_fsrs(None, rating, 0.90, fixed)
            result = sv.apply_review(item["item_key"], rating, "v130_test")
            assert result["state"] == expected["state"] and result["step"] == expected["step"]
            assert abs(result["srs"]["stability"] - expected["stability"]) < 1e-12
            boot = sv.get_bootstrap()
            for key, value in result["stats"].items():
                assert boot["stats"][key] == value, (rating, key, value, boot["stats"][key])
        source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        rate_body = source.split("async function rateRecall", 1)[1].split("async function finishRecall", 1)[0]
        assert "refreshBoot" not in rate_body
        assert "await refreshBoot()" in source.split("async function finishRecall", 1)[1].split("function renderRecallDone", 1)[0]
    finally:
        sv.now_ts = original_now
        restore_user(root, old)


def test_search_filters_and_canonical_multi_location():
    root, old = temp_user("english_search_filters_")
    try:
        with sv.content_conn() as c:
            multi = c.execute(
                """SELECT COALESCE(a.canonical_content_id,m.content_id) canonical_id,COUNT(DISTINCT m.island_id) locations
                   FROM content_membership m LEFT JOIN srs_alias a ON a.content_id=m.content_id
                   GROUP BY COALESCE(a.canonical_content_id,m.content_id) HAVING COUNT(DISTINCT m.island_id)>1
                   ORDER BY locations DESC LIMIT 1"""
            ).fetchone()
            canonical_id = int(multi[0])
            sentence = c.execute("SELECT en_us,vi_vn FROM sentence_content WHERE content_id=?", (canonical_id,)).fetchone()
            course_row = c.execute(
                "SELECT s.en_us FROM content_membership m JOIN collections co ON co.id=m.island_id JOIN sentence_content s ON s.content_id=m.content_id WHERE co.source_group LIKE 'course:%' LIMIT 1"
            ).fetchone()
            vocab_row = c.execute(
                "SELECT s.en_us FROM content_membership m JOIN collections co ON co.id=m.island_id JOIN sentence_content s ON s.content_id=m.content_id WHERE co.is_vocabulary=1 LIMIT 1"
            ).fetchone()
        query = sentence["en_us"][:50]
        results = sv.search_content(query, "all")
        keys = [item["item_key"] for item in results]
        assert len(keys) == len(set(keys))
        target = next(item for item in results if item["item_key"] == f"s:{canonical_id}")
        assert target["other_location_count"] >= 1
        if sentence["vi_vn"]:
            assert any(item["item_key"] == f"s:{canonical_id}" for item in sv.search_content(sentence["vi_vn"][:40], "all"))
        assert any(any(location["kind"] == "courses" for location in item["locations"]) for item in sv.search_content(course_row[0][:50], "courses"))
        assert any(any(location["kind"] == "vocabulary" for location in item["locations"]) for item in sv.search_content(vocab_row[0][:50], "vocabulary"))

        island = sv.create_my_island("Search Filter Island", "")
        sv.add_to_my_island(island["id"], target["item_key"])
        assert any(item["item_key"] == target["item_key"] for item in sv.search_content(query, "my"))
        sv.bookmark_item(target["item_key"], True)
        assert any(item["item_key"] == target["item_key"] for item in sv.search_content(query, "saved"))
        custom = sv.create_custom_sentence("Nebula Search Unique", "Tìm kiếm tinh vân", island_id=island["id"], audio_key="NEBULA")
        assert any(item["item_key"] == custom["item_key"] for item in sv.search_content("Nebula Search", "my"))
    finally:
        restore_user(root, old)


def test_search_scopes_are_applied_before_limit():
    root, old = temp_user("english_search_scope_limit_")
    try:
        requested = 80
        courses = sv.search_content("to", "courses", requested)
        assert len(courses) == requested
        assert all(any(location["kind"] == "courses" for location in item["locations"]) for item in courses)

        with sv.content_conn() as c:
            old_window = {
                int(row[0]) for row in c.execute(
                    """SELECT COALESCE(a.canonical_content_id,s.content_id) canonical_id
                       FROM sentence_content s LEFT JOIN srs_alias a ON a.content_id=s.content_id
                       WHERE s.en_us LIKE '%to%' COLLATE NOCASE OR s.vi_vn LIKE '%to%' COLLATE NOCASE
                       ORDER BY CASE WHEN s.en_us LIKE '%to%' COLLATE NOCASE THEN 0 ELSE 1 END,s.content_id
                       LIMIT 480"""
                )
            }
            all_matches = [
                int(row[0]) for row in c.execute(
                    """SELECT COALESCE(a.canonical_content_id,s.content_id) canonical_id
                       FROM sentence_content s LEFT JOIN srs_alias a ON a.content_id=s.content_id
                       WHERE s.en_us LIKE '%to%' COLLATE NOCASE OR s.vi_vn LIKE '%to%' COLLATE NOCASE
                       GROUP BY COALESCE(a.canonical_content_id,s.content_id)
                       ORDER BY MIN(CASE WHEN s.en_us LIKE '%to%' COLLATE NOCASE THEN 0 ELSE 1 END),canonical_id"""
                )
            ]
        outside = [canonical_id for canonical_id in all_matches if canonical_id not in old_window]
        assert len(outside) >= 2, "test corpus no longer has matches outside the old 480-row window"
        saved_key = sv.item_key_standard(outside[0])
        my_key = sv.item_key_standard(outside[1])
        sv.bookmark_item(saved_key, True)
        island = sv.create_my_island("Outside Search Window", "")
        sv.add_to_my_island(island["id"], my_key)
        assert any(item["item_key"] == saved_key for item in sv.search_content("to", "saved", requested))
        assert any(item["item_key"] == my_key for item in sv.search_content("to", "my", requested))
    finally:
        restore_user(root, old)


def _create_audio_sentence(island_id, key, old_bytes=None):
    result = sv.create_custom_sentence(f"Sentence {key}", f"Câu {key}", island_id=island_id, audio_key=key, audio_expected=f"{key}.mp3")
    row_id = int(result["custom_id"])
    if old_bytes is not None:
        filename = f"custom_{row_id:06d}.mp3"
        (sv.USER_AUDIO / filename).write_bytes(old_bytes)
        with sv.user_conn() as u:
            u.execute("UPDATE custom_sentences SET audio_file=? WHERE id=?", (filename, row_id)); u.commit()
    return row_id


def test_bulk_audio_zip_folder_security_large_set_and_rollback():
    root, old = temp_user("english_bulk_audio_")
    try:
        island = sv.create_my_island("Bulk Audio", "")
        first = _create_audio_sentence(island["id"], "T001")
        zip_path = root / "audio.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("T001.mp3", b"new-audio")
            archive.writestr("nested/T001.mp3", b"duplicate")
            archive.writestr("unmatched.mp3", b"unused")
        preview = sv.prepare_bulk_audio_path("my", island["id"], zip_path, "zip")
        assert preview["matched"] == 1 and preview["duplicateCount"] == 1 and preview["unmatchedCount"] == 1
        result = sv.confirm_bulk_audio_session(preview["token"])
        assert result["imported"] == 1
        assert (sv.USER_AUDIO / f"custom_{first:06d}.mp3").read_bytes() == b"new-audio"

        second = _create_audio_sentence(island["id"], "T002")
        folder = root / "folder"; folder.mkdir(); (folder / "T002.mp3").write_bytes(b"folder-audio")
        preview = sv.prepare_bulk_audio_path("my", island["id"], folder, "folder")
        assert preview["matched"] == 1
        sv.confirm_bulk_audio_session(preview["token"])
        assert (sv.USER_AUDIO / f"custom_{second:06d}.mp3").read_bytes() == b"folder-audio"

        unsafe = root / "unsafe.zip"
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("../escape.mp3", b"bad")
        try:
            sv.prepare_bulk_audio_path("my", island["id"], unsafe, "zip")
            raise AssertionError("ZIP traversal was accepted")
        except ValueError as exc:
            assert "không an toàn" in str(exc)

        large = root / "large"; large.mkdir()
        for index in range(1000):
            (large / f"X{index:04d}.mp3").write_bytes(b"x")
        large_file = large / "large-file.mp3"
        with large_file.open("wb") as output:
            output.seek(32 * 1024 * 1024 - 1); output.write(b"x")
        sessions = BulkAudioSessions()
        record = sessions.from_folder(root / "large-stage", "my", island["id"], large)
        assert len(record["files"]) == 1001
        assert max(item["size"] for item in record["files"]) == 32 * 1024 * 1024
        sessions.cleanup(record["token"])

        rollback_island = sv.create_my_island("Rollback", "")
        row1 = _create_audio_sentence(rollback_island["id"], "R001", b"old-one")
        row2 = _create_audio_sentence(rollback_island["id"], "R002", b"old-two")
        rollback_folder = root / "rollback-input"; rollback_folder.mkdir()
        (rollback_folder / "R001.mp3").write_bytes(b"new-one")
        (rollback_folder / "R002.mp3").write_bytes(b"new-two")
        preview = sv.prepare_bulk_audio_path("my", rollback_island["id"], rollback_folder, "folder")
        original_replace = os.replace
        moves = {"count": 0}
        def fail_second_install(source, destination):
            if Path(source).parent.name == "prepared" and Path(destination).parent.resolve() == sv.USER_AUDIO.resolve():
                moves["count"] += 1
                if moves["count"] == 2:
                    raise OSError("simulated install failure")
            return original_replace(source, destination)
        sv.os.replace = fail_second_install
        try:
            try:
                sv.confirm_bulk_audio_session(preview["token"])
                raise AssertionError("simulated failure did not abort")
            except OSError as exc:
                assert "simulated" in str(exc)
        finally:
            sv.os.replace = original_replace
        assert (sv.USER_AUDIO / f"custom_{row1:06d}.mp3").read_bytes() == b"old-one"
        assert (sv.USER_AUDIO / f"custom_{row2:06d}.mp3").read_bytes() == b"old-two"
        with sv.user_conn() as u:
            names = [row[0] for row in u.execute("SELECT audio_file FROM custom_sentences WHERE id IN (?,?) ORDER BY id", (row1, row2))]
        assert names == [f"custom_{row1:06d}.mp3", f"custom_{row2:06d}.mp3"]
        sv.cancel_bulk_audio_session(preview["token"])

        frontend = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        bulk_section = frontend.split("function pickBrowserAudio", 1)[1].split("async function showMissingAudio", 1)[0]
        assert "fileToBase64" not in bulk_section and "readAsDataURL" not in bulk_section
        assert "application/octet-stream" in bulk_section and "application/zip" in bulk_section

        native_zip = root / "native-oversized.zip"
        with zipfile.ZipFile(native_zip, "w") as archive:
            archive.writestr("T001.mp3", b"too-large-for-test-limit")
        old_total = bulk_audio_module.MAX_TOTAL_BYTES
        old_copy = bulk_audio_module.shutil.copyfile
        copied = {"value": False}
        def track_copy(*args, **kwargs):
            copied["value"] = True
            return old_copy(*args, **kwargs)
        bulk_audio_module.MAX_TOTAL_BYTES = 8
        bulk_audio_module.shutil.copyfile = track_copy
        native_stage = root / "native-oversized-stage"
        try:
            try:
                BulkAudioSessions().from_zip(native_stage, "my", island["id"], native_zip)
                raise AssertionError("oversized native ZIP was accepted")
            except ValueError as exc:
                assert "ZIP audio vượt giới hạn" in str(exc)
            assert not copied["value"] and not native_stage.exists()
        finally:
            bulk_audio_module.MAX_TOTAL_BYTES = old_total
            bulk_audio_module.shutil.copyfile = old_copy
    finally:
        restore_user(root, old)


def test_bulk_audio_http_binary_stream_endpoints():
    root, old = temp_user("english_bulk_http_")
    httpd = None
    thread = None
    try:
        island = sv.create_my_island("HTTP Bulk", "")
        _create_audio_sentence(island["id"], "HTTP1")
        httpd = sv.ThreadingHTTPServer((sv.HOST, 0), sv.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://{sv.HOST}:{httpd.server_address[1]}"

        def request(path, body, content_type):
            req = urllib.request.Request(base + path, data=body, method="POST", headers={"Content-Type": content_type})
            with urllib.request.urlopen(req, timeout=15) as response:
                return __import__("json").loads(response.read().decode("utf-8"))

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("HTTP1.mp3", b"zip-stream")
        preview = request(f"/api/bulk-audio/zip?scope=my&target={island['id']}", buffer.getvalue(), "application/zip")
        assert preview["matched"] == 1 and preview["token"]
        request("/api/bulk-audio/cancel", __import__("json").dumps({"token": preview["token"]}).encode(), "application/json")

        session = request("/api/bulk-audio/start", __import__("json").dumps({"scope": "my", "target": island["id"]}).encode(), "application/json")
        uploaded = request(f"/api/bulk-audio/file?token={session['token']}&name=HTTP1.mp3", b"folder-stream", "application/octet-stream")
        assert uploaded["uploaded"] == 1
        preview = request("/api/bulk-audio/preview", __import__("json").dumps({"token": session["token"]}).encode(), "application/json")
        assert preview["matched"] == 1
        request("/api/bulk-audio/cancel", __import__("json").dumps({"token": session["token"]}).encode(), "application/json")
    finally:
        if httpd:
            httpd.shutdown(); httpd.server_close()
        if thread:
            thread.join(timeout=5)
        restore_user(root, old)


def test_bulk_audio_startup_removes_only_orphan_staging():
    root, old = temp_user("english_bulk_orphan_")
    try:
        stage_root = sv._bulk_audio_stage_root()
        orphan = stage_root / ".bulk_audio_crashed-process"
        orphan.mkdir(parents=True)
        (orphan / "partial.mp3").write_bytes(b"partial")
        unrelated_stage = stage_root / "keep-user-file"
        unrelated_stage.mkdir()
        (unrelated_stage / "keep.txt").write_text("keep", encoding="utf-8")
        sv.USER_AUDIO.mkdir(parents=True, exist_ok=True)
        (sv.USER_AUDIO / "custom_000001.mp3").write_bytes(b"user-audio")
        restore_stage = sv._user_base_dir() / "restore_pending" / "staged"
        restore_stage.mkdir(parents=True)
        (restore_stage / "manifest.json").write_text("{}", encoding="utf-8")

        result = sv.cleanup_orphan_bulk_audio_staging()
        assert result == {"ok": True, "removed": 1}
        assert not orphan.exists()
        assert (unrelated_stage / "keep.txt").read_text(encoding="utf-8") == "keep"
        assert (sv.USER_AUDIO / "custom_000001.mp3").read_bytes() == b"user-audio"
        assert (restore_stage / "manifest.json").read_text(encoding="utf-8") == "{}"
    finally:
        restore_user(root, old)


def test_desktop_native_picker_bridge_zip_folder_cancel_and_import():
    root, old = temp_user("english_native_picker_bridge_")
    old_webview = sys.modules.get("webview")
    try:
        fake_webview = types.ModuleType("webview")
        fake_webview.windows = []
        fake_webview.FileDialog = types.SimpleNamespace(OPEN="open", FOLDER="folder", SAVE="save")
        sys.modules["webview"] = fake_webview
        sys.modules.pop("desktop_app", None)
        import desktop_app

        island = sv.create_my_island("Native Picker Bridge", "")
        folder_row = _create_audio_sentence(island["id"], "NATIVE_FOLDER")
        zip_row = _create_audio_sentence(island["id"], "NATIVE_ZIP")
        folder = root / "picker-folder"
        folder.mkdir()
        (folder / "NATIVE_FOLDER.mp3").write_bytes(b"folder-native")
        archive = root / "picker.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("NATIVE_ZIP.mp3", b"zip-native")

        class FakeWindow:
            def __init__(self):
                self.selection = None
                self.calls = []

            def create_file_dialog(self, dialog_type, **kwargs):
                self.calls.append((dialog_type, kwargs))
                return self.selection

        window = FakeWindow()
        fake_webview.windows.append(window)
        api = desktop_app.DesktopApi()

        assert api.pick_bulk_audio("my", island["id"], "zip") == {"ok": False, "cancelled": True}
        window.selection = [str(folder)]
        folder_preview = api.pick_bulk_audio("my", island["id"], "folder")
        assert folder_preview["ok"] and folder_preview["matched"] == 1
        sv.confirm_bulk_audio_session(folder_preview["token"])
        assert (sv.USER_AUDIO / f"custom_{folder_row:06d}.mp3").read_bytes() == b"folder-native"

        window.selection = [str(archive)]
        zip_preview = api.pick_bulk_audio("my", island["id"], "zip")
        assert zip_preview["ok"] and zip_preview["matched"] == 1
        sv.confirm_bulk_audio_session(zip_preview["token"])
        assert (sv.USER_AUDIO / f"custom_{zip_row:06d}.mp3").read_bytes() == b"zip-native"
        assert window.calls[0][0] == "open" and window.calls[1][0] == "folder" and window.calls[2][0] == "open"
    finally:
        sys.modules.pop("desktop_app", None)
        if old_webview is not None:
            sys.modules["webview"] = old_webview
        else:
            sys.modules.pop("webview", None)
        restore_user(root, old)


def test_m1_webview_selection_and_speed_control_contract():
    desktop = (ROOT / "desktop_app.py").read_text(encoding="utf-8")
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "text_select=True" in desktop
    assert "-webkit-user-select:text;user-select:text" in css
    control = js[js.index("function speedControlHtml"):js.index("function setPlaybackSpeedState")]
    assert 'type="range"' in control and 'type="number"' in control
    assert 'min="0.25"' in control and 'max="2.00"' in control and 'step="0.05"' in control
    preview = js[js.index("function previewPlaybackSpeed"):js.index("async function commitPlaybackSpeed")]
    assert "playbackRate" not in preview and "post(" not in preview and ".src" not in preview
    state_update = js[js.index("function setPlaybackSpeedState"):js.index("function syncPlaybackSpeedControls")]
    assert "player().playbackRate=speed" in state_update
    commit = js[js.index("async function commitPlaybackSpeed"):js.index("function showPlayer")]
    assert "'list_speed':'shadow_speed'" in commit and "post('/api/setting'" in commit
    assert "speedControlHtml('list'" in js and "speedControlHtml('shadow'" in js
    assert ".speed-number input{width:6.5em;min-width:6.5em}" in css


def run_all():
    tests = [
        test_audio_index_builds_once_and_invalidates_explicitly,
        test_user_audio_availability_is_live_not_cached,
        test_custom_sentence_audio_rolls_back_with_database_failure,
        test_xlsx_headers_and_archive_limits,
        test_recall_rating_matches_fsrs_and_returns_exact_local_counters,
        test_search_filters_and_canonical_multi_location,
        test_search_scopes_are_applied_before_limit,
        test_bulk_audio_zip_folder_security_large_set_and_rollback,
        test_bulk_audio_http_binary_stream_endpoints,
        test_bulk_audio_startup_removes_only_orphan_staging,
        test_desktop_native_picker_bridge_zip_folder_cancel_and_import,
        test_m1_webview_selection_and_speed_control_contract,
    ]
    for test in tests:
        test(); print("PASS", test.__name__)
    print(f"PASS ALL: {len(tests)} v1.3 feature test groups")


if __name__ == "__main__":
    run_all()
