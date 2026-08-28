#!/usr/bin/env python3
"""Focused M2.1 regressions for atomic My Island deletion."""

import base64
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server as sv


@contextmanager
def temp_profile():
    root = Path(tempfile.mkdtemp(prefix="english_island_delete_"))
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


def first_standard_key():
    with sv.content_conn() as con:
        content_id = con.execute("SELECT content_id FROM sentence_content ORDER BY content_id LIMIT 1").fetchone()[0]
        return sv.item_key_for_content(content_id, con)


def custom_with_audio(island_id, english="Delete-only custom"):
    return sv.create_custom_sentence(
        english, "Câu tự tạo", island_id=island_id,
        audio_data=base64.b64encode(b"ID3-delete-audio").decode("ascii"),
        audio_name="delete.mp3", audio_type="audio/mpeg",
    )


def item_state_counts(key):
    with sv.user_conn() as con:
        return {
            table: con.execute(f"SELECT COUNT(*) FROM {table} WHERE item_key=?", (key,)).fetchone()[0]
            for table in ("fsrs_cards", "review_log", "saved_items", "suspended_items")
        }


def seed_item_state(key):
    sv.apply_review(key, 3, "island-delete-test")
    sv.bookmark_item(key, True)
    sv.manage_srs("suspend", item_key=key)


def test_delete_keeps_canonical_and_removes_unshared_custom_audio_and_state():
    with temp_profile():
        canonical = first_standard_key()
        island = sv.create_my_island("Delete Cleanup", "")
        sv.add_to_my_island(island["id"], canonical)
        custom = custom_with_audio(island["id"])
        seed_item_state(canonical)
        seed_item_state(custom["item_key"])
        sv.save_position(f"my:{island['id']}", 2)
        sv.save_position("core:219", 4)
        sv.set_active_source(f"my:{island['id']}")
        audio_path = sv.USER_AUDIO / custom["audio_file"]
        assert audio_path.is_file()

        result = sv.delete_my_island(island["id"])
        assert result == {"ok": True, "deletedCustomSentences": 1, "deletedCustomAudio": 1}
        assert not audio_path.exists()
        assert item_state_counts(custom["item_key"]) == {table: 0 for table in ("fsrs_cards", "review_log", "saved_items", "suspended_items")}
        assert all(value == 1 for value in item_state_counts(canonical).values())
        with sv.user_conn() as con:
            assert con.execute("SELECT 1 FROM custom_sentences WHERE id=?", (custom["custom_id"],)).fetchone() is None
            assert con.execute("SELECT 1 FROM collection_progress WHERE collection_key=?", (f"my:{island['id']}",)).fetchone() is None
            assert con.execute("SELECT last_index FROM collection_progress WHERE collection_key='core:219'").fetchone()[0] == 4
            assert sv.setting_get(con, "active_collection_key") == "core:219"
        with sv.content_conn() as con:
            assert sv.resolve_item(canonical, con, None) is not None


def test_delete_keeps_custom_audio_and_state_when_another_island_references_it():
    with temp_profile():
        first = sv.create_my_island("Shared Source", "")
        second = sv.create_my_island("Shared Destination", "")
        custom = custom_with_audio(first["id"], "Shared custom")
        sv.add_to_my_island(second["id"], custom["item_key"])
        seed_item_state(custom["item_key"])
        audio_path = sv.USER_AUDIO / custom["audio_file"]

        result = sv.delete_my_island(first["id"])
        assert result == {"ok": True, "deletedCustomSentences": 0, "deletedCustomAudio": 0}
        assert audio_path.read_bytes() == b"ID3-delete-audio"
        assert all(value == 1 for value in item_state_counts(custom["item_key"]).values())
        with sv.user_conn() as con:
            assert con.execute("SELECT 1 FROM custom_sentences WHERE id=?", (custom["custom_id"],)).fetchone()
            assert con.execute("SELECT 1 FROM my_island_members WHERE island_id=? AND item_key=?", (second["id"], custom["item_key"])).fetchone()


def test_delete_failure_restores_database_audio_and_progress():
    with temp_profile():
        island = sv.create_my_island("Rollback Delete", "")
        custom = custom_with_audio(island["id"], "Rollback custom")
        seed_item_state(custom["item_key"])
        audio_path = sv.USER_AUDIO / custom["audio_file"]
        with sv.user_conn() as con:
            con.execute(
                """CREATE TRIGGER fail_island_cleanup BEFORE DELETE ON custom_sentences
                   BEGIN SELECT RAISE(ABORT,'forced island cleanup rollback'); END"""
            )
            con.commit()
        try:
            sv.delete_my_island(island["id"])
            raise AssertionError("forced cleanup failure was accepted")
        except Exception as exc:
            assert "forced island cleanup rollback" in str(exc)
        assert audio_path.read_bytes() == b"ID3-delete-audio"
        assert all(value == 1 for value in item_state_counts(custom["item_key"]).values())
        with sv.user_conn() as con:
            assert con.execute("SELECT 1 FROM my_islands WHERE id=?", (island["id"],)).fetchone()
            assert con.execute("SELECT 1 FROM custom_sentences WHERE id=?", (custom["custom_id"],)).fetchone()
            assert con.execute("SELECT 1 FROM my_island_members WHERE island_id=?", (island["id"],)).fetchone()
        stage_root = sv._user_base_dir()
        assert not any(path.name.startswith(".delete_island_") for path in stage_root.iterdir())


def test_delete_modal_contract_has_no_browser_confirm():
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    flow = js[js.index("function deleteIsland"):js.index("async function managerSearch")]
    assert "confirm(" not in flow
    assert "openModal(" in flow and "confirmDeleteIsland" in flow
    assert "Sẽ xóa:" in flow and "Vẫn giữ:" in flow
    assert "Hủy" in flow and "Đang xóa..." in flow and "button.disabled=true" in flow


def run_all():
    tests = [
        test_delete_keeps_canonical_and_removes_unshared_custom_audio_and_state,
        test_delete_keeps_custom_audio_and_state_when_another_island_references_it,
        test_delete_failure_restores_database_audio_and_progress,
        test_delete_modal_contract_has_no_browser_confirm,
    ]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"PASS ALL: {len(tests)} island-delete test groups")


if __name__ == "__main__":
    run_all()
