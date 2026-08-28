#!/usr/bin/env python3
"""Focused M2 tests for portable individual My Island packages."""

import base64
import json
import shutil
import sys
import tempfile
import threading
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import island_portability as ip
import server as sv


@contextmanager
def temp_profile(prefix="english_island_portability_"):
    root = Path(tempfile.mkdtemp(prefix=prefix))
    old = (sv.USER_DIR, sv.USER_DB, sv.USER_AUDIO)
    sv.USER_DIR = root / "EnglishLocal" / "user_data"
    sv.USER_DB = sv.USER_DIR / "learning.sqlite"
    sv.USER_AUDIO = root / "EnglishLocal" / "user_audio"
    try:
        sv.user_conn().close()
        yield root
    finally:
        sv.USER_DIR, sv.USER_DB, sv.USER_AUDIO = old
        shutil.rmtree(root, ignore_errors=True)


def standard_items(count=2):
    with sv.content_conn() as con:
        rows = con.execute("SELECT content_id FROM sentence_content ORDER BY content_id LIMIT ?", (count,)).fetchall()
        return [sv.get_standard_item(row[0], con) for row in rows]


def archive_parts(path):
    with zipfile.ZipFile(path, "r") as archive:
        return {info.filename: archive.read(info.filename) for info in archive.infolist()}


def build_archive(path, parts):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)


def table_count(table):
    with sv.user_conn() as con:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def seed_audio_island(name="Portable Round Trip"):
    first, second = standard_items(2)
    island_id = sv.create_my_island(name, "portable description")["id"]
    sv.add_to_my_island(island_id, first["item_key"])
    audio = sv.create_custom_sentence(
        "A portable custom sentence.", "Một câu tùy chỉnh di động.",
        usage_note="usage", literal_note="literal", audio_key="PORT-001",
        audio_expected="PORT-001.mp3", note="private note",
        audio_data=base64.b64encode(b"ID3-portable-audio").decode("ascii"),
        audio_name="voice.mp3", audio_type="audio/mpeg", island_id=island_id,
    )
    sv.add_to_my_island(island_id, second["item_key"])
    silent = sv.create_custom_sentence(
        "A silent portable sentence.", "Một câu không audio.", audio_key="PORT-002", island_id=island_id,
    )
    return island_id, first, second, audio, silent


def test_round_trip_order_content_audio_state_exclusion_and_reuse():
    with temp_profile() as root:
        island_id, first, second, _audio, _silent = seed_audio_island()
        with sv.user_conn() as con:
            sv.setting_set(con, "portable_export_marker", "must-not-export")
            con.commit()
        sv.bookmark_item(first["item_key"], True)
        sv.apply_review(first["item_key"], 4, "portable-export-test")
        package = root / "Portable Round Trip.island.zip"
        result = sv.create_island_export(island_id, package)
        assert result["itemCount"] == 4 and result["audioFileCount"] == 1

        parts = archive_parts(package)
        assert set(parts) == {"manifest.json", "island.json", "audio/custom-000002.mp3"}
        manifest = json.loads(parts["manifest.json"].decode("utf-8"))
        island_json = json.loads(parts["island.json"].decode("utf-8"))
        assert manifest["packageType"] == ip.PACKAGE_TYPE and manifest["formatVersion"] == ip.FORMAT_VERSION
        assert [item["kind"] for item in island_json["items"]] == ["canonical", "custom", "canonical", "custom"]
        serialized = json.dumps({"manifest": manifest, "island": island_json})
        for forbidden in ("fsrs", "progress", "saved", "history", "settings", "portable_export_marker"):
            assert forbidden not in serialized.lower()

        sv.delete_all_user_data()
        sv.bookmark_item(first["item_key"], True)
        sv.apply_review(first["item_key"], 3, "receiving-profile")
        with sv.user_conn() as con:
            sv.setting_set(con, "receiving_marker", "keep")
            con.commit()
        before_saved, before_cards = table_count("saved_items"), table_count("fsrs_cards")
        preview = sv.prepare_island_import_path(package)
        assert not preview["nameConflict"] and preview["suggestedName"] == "Portable Round Trip"
        imported = sv.confirm_island_import(preview["token"], preview["name"])
        collection = sv.get_my_collection(imported["id"])
        assert [item["en_us"] for item in collection["items"]] == [first["en_us"], "A portable custom sentence.", second["en_us"], "A silent portable sentence."]
        assert collection["items"][0]["item_key"] == first["item_key"] and collection["items"][2]["item_key"] == second["item_key"]
        custom_audio = collection["items"][1]
        assert custom_audio["audio"] and (sv.USER_AUDIO / f"custom_{custom_audio['custom_id']:06d}.mp3").read_bytes() == b"ID3-portable-audio"
        assert collection["items"][3]["audio"] is None
        assert table_count("saved_items") == before_saved and table_count("fsrs_cards") == before_cards
        with sv.user_conn() as con:
            assert sv.setting_get(con, "receiving_marker") == "keep"

        existing_keys = [item["item_key"] for item in collection["items"]]
        conflict = sv.prepare_island_import_path(package)
        assert conflict["nameConflict"] and conflict["suggestedName"] == "Portable Round Trip (2)"
        second_import = sv.confirm_island_import(conflict["token"], conflict["suggestedName"])
        assert [item["item_key"] for item in sv.get_my_collection(second_import["id"])["items"]] == existing_keys
        assert second_import["customReused"] == 2 and second_import["audioRestored"] == 0

        cancel_preview = sv.prepare_island_import_path(package)
        island_count = table_count("my_islands")
        sv.cancel_island_import(cancel_preview["token"])
        assert table_count("my_islands") == island_count
        try:
            ip.ISLAND_IMPORT_SESSIONS.get(cancel_preview["token"])
            raise AssertionError("cancelled import session remained live")
        except ValueError:
            pass


def test_custom_without_private_metadata_reuses_canonical_content():
    with temp_profile() as root:
        standard = standard_items(1)[0]
        island_id = sv.create_my_island("Canonical Reuse", "")["id"]
        sv.create_custom_sentence(
            standard["en_us"], standard["vi_vn"], standard["usage_note"], standard["literal_note"],
            island_id=island_id,
        )
        package = root / "Canonical Reuse.island.zip"
        sv.create_island_export(island_id, package)
        sv.delete_all_user_data()
        preview = sv.prepare_island_import_path(package)
        result = sv.confirm_island_import(preview["token"], preview["name"])
        collection = sv.get_my_collection(result["id"])
        assert [item["item_key"] for item in collection["items"]] == [standard["item_key"]]
        assert result["canonicalReused"] == 1 and result["customCreated"] == 0
        assert table_count("custom_sentences") == 0


def test_corrupt_unsafe_and_limited_zip_rejected_before_user_changes():
    with temp_profile() as root:
        island_id, *_ = seed_audio_island("Unsafe Package Source")
        package = root / "Unsafe Package Source.island.zip"
        sv.create_island_export(island_id, package)
        parts = archive_parts(package)
        before = (table_count("my_islands"), table_count("custom_sentences"), sorted(p.name for p in sv.USER_AUDIO.iterdir()))

        corrupt = root / "Corrupt.island.zip"
        corrupt.write_bytes(b"not a zip")
        candidates = [corrupt]
        traversal = root / "Traversal.island.zip"
        build_archive(traversal, {**parts, "../escape.txt": b"blocked"})
        candidates.append(traversal)
        bad_version = root / "Version.island.zip"
        version_parts = dict(parts)
        manifest = json.loads(version_parts["manifest.json"].decode("utf-8"))
        manifest["formatVersion"] = ip.FORMAT_VERSION + 1
        version_parts["manifest.json"] = json.dumps(manifest).encode("utf-8")
        build_archive(bad_version, version_parts)
        candidates.append(bad_version)
        bad_checksum = root / "Checksum.island.zip"
        checksum_parts = dict(parts)
        checksum_parts["island.json"] += b" "
        build_archive(bad_checksum, checksum_parts)
        candidates.append(bad_checksum)

        for candidate in candidates:
            try:
                sv.prepare_island_import_path(candidate)
                raise AssertionError(f"unsafe package was accepted: {candidate.name}")
            except (ValueError, zipfile.BadZipFile):
                pass
        assert not (root / "escape.txt").exists()

        old_members, old_total = ip.MAX_MEMBER_COUNT, ip.MAX_UNCOMPRESSED_BYTES
        try:
            ip.MAX_MEMBER_COUNT = 2
            try:
                sv.prepare_island_import_path(package)
                raise AssertionError("member limit was not enforced")
            except ValueError as exc:
                assert "quá nhiều" in str(exc)
            ip.MAX_MEMBER_COUNT = old_members
            ip.MAX_UNCOMPRESSED_BYTES = 10
            try:
                sv.prepare_island_import_path(package)
                raise AssertionError("uncompressed limit was not enforced")
            except ValueError as exc:
                assert "giải nén" in str(exc)
        finally:
            ip.MAX_MEMBER_COUNT, ip.MAX_UNCOMPRESSED_BYTES = old_members, old_total
        after = (table_count("my_islands"), table_count("custom_sentences"), sorted(p.name for p in sv.USER_AUDIO.iterdir()))
        assert after == before


def test_database_failure_after_audio_install_rolls_back_without_orphans():
    with temp_profile() as root:
        island_id, *_ = seed_audio_island("Atomic Portable Import")
        package = root / "Atomic Portable Import.island.zip"
        sv.create_island_export(island_id, package)
        sv.delete_all_user_data()
        preview = sv.prepare_island_import_path(package)
        with sv.user_conn() as con:
            con.execute(
                """CREATE TRIGGER fail_portable_audio BEFORE UPDATE OF audio_file ON custom_sentences
                   WHEN NEW.audio_file IS NOT NULL AND NEW.audio_file<>''
                   BEGIN SELECT RAISE(ABORT,'forced portable rollback'); END"""
            )
            con.commit()
        try:
            sv.confirm_island_import(preview["token"], preview["name"])
            raise AssertionError("forced post-install database failure was accepted")
        except Exception as exc:
            assert "forced portable rollback" in str(exc)
        assert table_count("my_islands") == 0
        assert table_count("my_island_members") == 0
        assert table_count("custom_sentences") == 0
        assert not list(sv.USER_AUDIO.iterdir())
        assert not any(path.name.startswith(".island_import_") for path in sv._island_import_stage_root().glob("*"))


def test_native_dialog_and_web_ui_contract():
    import types
    old_webview = sys.modules.get("webview")
    fake_webview = types.ModuleType("webview")
    fake_webview.windows = []
    fake_webview.FileDialog = types.SimpleNamespace(OPEN="open", FOLDER="folder", SAVE="save")
    sys.modules["webview"] = fake_webview
    sys.modules.pop("desktop_app", None)
    try:
        import desktop_app
        with temp_profile() as root:
            island_id, *_ = seed_audio_island("Native Portable")

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
            assert api.pick_island_import() == {"ok": False, "cancelled": True}
            window.selection = [str(root / "native-output")]
            exported = api.export_island(island_id, "Native Portable.island.zip")
            package = root / "native-output.island.zip"
            assert exported["ok"] and package.is_file()
            window.selection = [str(package)]
            preview = api.pick_island_import()
            assert preview["ok"] and preview["nameConflict"]
            sv.cancel_island_import(preview["token"])
            assert [call[0] for call in window.calls] == ["open", "save", "open"]

        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        flow = js[js.index("async function beginImportIsland"):js.index("function openModal")]
        assert "Import Island" in js and "Export Island" in js
        assert "nameConflict" in flow and "suggestedName" in flow and "Hủy" in flow
        assert "button.disabled=true" in flow and "Đang import..." in flow
        assert "body:file" in flow and "fileToBase64" not in flow
        assert "/api/my-island/import-cancel" in js
    finally:
        sys.modules.pop("desktop_app", None)
        if old_webview is not None:
            sys.modules["webview"] = old_webview
        else:
            sys.modules.pop("webview", None)


def test_live_http_ui_export_upload_preview_and_confirm_flow():
    with temp_profile():
        island_id, *_ = seed_audio_island("Đảo HTTP Portable")
        httpd = sv.ThreadingHTTPServer((sv.HOST, 0), sv.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://{sv.HOST}:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/", timeout=10) as response:
                assert response.status == 200 and b"app.js" in response.read()
            with urllib.request.urlopen(base + f"/api/my-island/export?id={island_id}", timeout=30) as response:
                package = response.read()
                assert response.status == 200 and package.startswith(b"PK")
                assert ".island.zip" in response.headers.get("Content-Disposition", "")
            upload = urllib.request.Request(
                base + "/api/my-island/import", data=package, method="POST",
                headers={"Content-Type": "application/zip"},
            )
            with urllib.request.urlopen(upload, timeout=30) as response:
                preview = json.loads(response.read().decode("utf-8"))
            assert preview["nameConflict"] and preview["suggestedName"] == "Đảo HTTP Portable (2)"
            body = json.dumps({"token": preview["token"], "name": preview["suggestedName"]}).encode("utf-8")
            confirm = urllib.request.Request(
                base + "/api/my-island/import-confirm", data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(confirm, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
            assert result["ok"] and result["itemCount"] == 4
            assert len(sv.get_my_collection(result["id"])["items"]) == 4
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


def run_all():
    tests = [
        test_round_trip_order_content_audio_state_exclusion_and_reuse,
        test_custom_without_private_metadata_reuses_canonical_content,
        test_corrupt_unsafe_and_limited_zip_rejected_before_user_changes,
        test_database_failure_after_audio_install_rolls_back_without_orphans,
        test_native_dialog_and_web_ui_contract,
        test_live_http_ui_export_upload_preview_and_confirm_flow,
    ]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"PASS ALL: {len(tests)} island-portability test groups")


if __name__ == "__main__":
    run_all()
