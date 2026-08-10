#!/usr/bin/env python3
"""One-time, validated import for the bundled English by Topic course."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import zipfile
from collections import Counter
from pathlib import Path


COURSE_NAME = "English by Topic"
SOURCE_GROUP = "course:english_by_topic"
COLLECTION_ID_START = 800
CONTENT_ID_START = 30001
EXPECTED_UNITS = 30
EXPECTED_SENTENCES = 990
EXPECTED_SLASH_ROWS = 135
EXPECTED_ALIASES = {
    "He rides his bike to school every day.": 20086,
    "We go to the beach every summer.": 1012,
    "Can you swim?": 24018,
    "Excuse me, is this seat taken?": 1687,
    "Is this area safe for tourists at night?": 2903,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unit_number(value: str) -> int:
    if not value.startswith("U") or not value[1:].isdigit():
        raise ValueError(f"Invalid unit: {value!r}")
    number = int(value[1:])
    if not 1 <= number <= EXPECTED_UNITS:
        raise ValueError(f"Unit out of range: {value!r}")
    return number


def read_package(package: Path) -> tuple[list[dict[str, str]], list[dict[str, str]], dict, dict[str, bytes]]:
    with zipfile.ZipFile(package) as archive:
        required = {"course_sentences.csv", "topics.csv", "manifest.json"}
        missing = required.difference(archive.namelist())
        if missing:
            raise ValueError(f"Package is missing: {sorted(missing)}")

        rows = list(
            csv.DictReader(
                io.StringIO(archive.read("course_sentences.csv").decode("utf-8-sig"))
            )
        )
        topics = list(
            csv.DictReader(io.StringIO(archive.read("topics.csv").decode("utf-8-sig")))
        )
        manifest = json.loads(archive.read("manifest.json").decode("utf-8-sig"))
        audio = {
            name.removeprefix("audio/"): archive.read(name)
            for name in archive.namelist()
            if name.startswith("audio/") and name.lower().endswith(".mp3")
        }

    expected_headers = [
        "course", "unit", "topic_en", "topic_vi", "index", "audio_file",
        "english", "vietnamese",
    ]
    if not rows or list(rows[0]) != expected_headers:
        raise ValueError("course_sentences.csv has an unexpected schema")
    if len(rows) != EXPECTED_SENTENCES:
        raise ValueError(f"Expected {EXPECTED_SENTENCES} rows, found {len(rows)}")
    if len(topics) != EXPECTED_UNITS:
        raise ValueError(f"Expected {EXPECTED_UNITS} topics, found {len(topics)}")
    if manifest.get("course_id") != "english_by_topic" or manifest.get("course_name") != COURSE_NAME:
        raise ValueError("Manifest course identity does not match")
    if manifest.get("sentence_count") != EXPECTED_SENTENCES or manifest.get("audio_count") != EXPECTED_SENTENCES:
        raise ValueError("Manifest totals do not match")

    topic_by_unit = {topic["unit"]: topic for topic in topics}
    if len(topic_by_unit) != EXPECTED_UNITS:
        raise ValueError("topics.csv contains duplicate units")
    if sorted(map(unit_number, topic_by_unit)) != list(range(1, EXPECTED_UNITS + 1)):
        raise ValueError("topics.csv must contain U1 through U30")

    english_counts = Counter(row["english"] for row in rows)
    audio_counts = Counter(row["audio_file"] for row in rows)
    if any(count != 1 for count in english_counts.values()):
        raise ValueError("Package contains duplicate English rows")
    if any(count != 1 for count in audio_counts.values()):
        raise ValueError("Package contains duplicate audio mappings")
    if len(audio) != EXPECTED_SENTENCES or set(audio) != set(audio_counts):
        raise ValueError("Package audio files do not match CSV one-to-one")
    if any(not payload for payload in audio.values()):
        raise ValueError("Package contains an empty MP3")
    if sum("/" in row["english"] for row in rows) != EXPECTED_SLASH_ROWS:
        raise ValueError("Slash-row count changed; refusing to split or alter source rows")
    if any(not row["english"].strip() or not row["vietnamese"].strip() for row in rows):
        raise ValueError("English or Vietnamese text is blank")

    rows_by_unit: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if row["course"] != COURSE_NAME:
            raise ValueError("Unexpected course name in CSV")
        rows_by_unit.setdefault(row["unit"], []).append(row)
    for unit, unit_rows in rows_by_unit.items():
        topic = topic_by_unit.get(unit)
        if topic is None:
            raise ValueError(f"Missing topic metadata for {unit}")
        indexes = [int(row["index"]) for row in unit_rows]
        if indexes != list(range(1, len(unit_rows) + 1)):
            raise ValueError(f"Non-contiguous row indexes in {unit}")
        if any(row["topic_en"] != topic["topic_en"] or row["topic_vi"] != topic["topic_vi"] for row in unit_rows):
            raise ValueError(f"Topic names disagree in {unit}")
        if len(unit_rows) != int(topic["sentence_count"]):
            raise ValueError(f"Sentence count disagrees in {unit}")
        manifest_unit = next((item for item in manifest["units"] if item["unit"] == unit), None)
        if not manifest_unit or manifest_unit["topic_en"] != topic["topic_en"] or manifest_unit["topic_vi"] != topic["topic_vi"]:
            raise ValueError(f"Manifest topic metadata disagrees in {unit}")
        if manifest_unit["sentence_count"] != len(unit_rows):
            raise ValueError(f"Manifest sentence count disagrees in {unit}")

    rows.sort(key=lambda row: (unit_number(row["unit"]), int(row["index"])))
    topics.sort(key=lambda topic: unit_number(topic["unit"]))
    return rows, topics, manifest, audio


def validate_backup(database: Path, backup: Path) -> str:
    if not backup.is_file():
        raise ValueError(f"Backup does not exist: {backup}")
    database_hash = sha256(database)
    backup_hash = sha256(backup)
    if database_hash != backup_hash:
        raise ValueError("Backup checksum does not match the pre-import database")
    return database_hash


def stage_audio(audio: dict[str, bytes], course_audio_root: Path) -> Path:
    course_audio_root.mkdir(parents=True, exist_ok=True)
    destination = (course_audio_root / "english_by_topic").resolve()
    if destination.parent != course_audio_root.resolve():
        raise ValueError("Unsafe audio destination")
    if destination.exists():
        raise ValueError(f"Audio destination already exists: {destination}")

    temp_dir = Path(tempfile.mkdtemp(prefix=".english_by_topic_", dir=course_audio_root))
    try:
        for filename, payload in audio.items():
            if Path(filename).name != filename:
                raise ValueError(f"Unsafe audio filename: {filename}")
            (temp_dir / filename).write_bytes(payload)
        staged = sorted(temp_dir.glob("*.mp3"))
        if len(staged) != EXPECTED_SENTENCES:
            raise ValueError(f"Staged {len(staged)} MP3 instead of {EXPECTED_SENTENCES}")
        temp_dir.replace(destination)
        return destination
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise


def canonical_id(connection: sqlite3.Connection, content_id: int) -> int:
    seen: set[int] = set()
    current = content_id
    while current not in seen:
        seen.add(current)
        row = connection.execute(
            "SELECT canonical_content_id FROM srs_alias WHERE content_id=?", (current,)
        ).fetchone()
        if row is None or int(row[0]) == current:
            return current
        current = int(row[0])
    raise ValueError(f"Canonical alias cycle at content_id={content_id}")


def assert_free_ranges(connection: sqlite3.Connection, audio_paths: list[str]) -> None:
    collection_end = COLLECTION_ID_START + EXPECTED_UNITS - 1
    content_end = CONTENT_ID_START + EXPECTED_SENTENCES - 1
    checks = [
        ("collections", "id", COLLECTION_ID_START, collection_end),
        ("sentence_content", "content_id", CONTENT_ID_START, content_end),
        ("content_membership", "sentence_id", CONTENT_ID_START, content_end),
        ("content_membership", "content_id", CONTENT_ID_START, content_end),
        ("content_audio", "content_id", CONTENT_ID_START, content_end),
        ("srs_alias", "content_id", CONTENT_ID_START, content_end),
    ]
    for table, column, start, end in checks:
        count = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} BETWEEN ? AND ?", (start, end)
        ).fetchone()[0]
        if count:
            raise ValueError(f"ID collision in {table}.{column}: {count} rows")
    if connection.execute(
        "SELECT COUNT(*) FROM collections WHERE source_group=?", (SOURCE_GROUP,)
    ).fetchone()[0]:
        raise ValueError("English by Topic is already present")
    if connection.execute(
        "SELECT COUNT(*) FROM sentence_content WHERE content_key LIKE 'course:english_by_topic:%'"
    ).fetchone()[0]:
        raise ValueError("English by Topic content keys already exist")
    placeholders = ",".join("?" for _ in audio_paths)
    if connection.execute(
        f"SELECT COUNT(*) FROM content_audio WHERE audio_path IN ({placeholders})", audio_paths
    ).fetchone()[0]:
        raise ValueError("English by Topic audio path collision")


def validate_exact_aliases(connection: sqlite3.Connection, rows: list[dict[str, str]]) -> dict[str, int]:
    found: dict[str, int] = {}
    for row in rows:
        matches = connection.execute(
            "SELECT content_id FROM sentence_content WHERE en_us=? ORDER BY content_id",
            (row["english"],),
        ).fetchall()
        if matches:
            if len(matches) != 1:
                raise ValueError(f"Ambiguous exact canonical match: {row['english']!r}")
            found[row["english"]] = canonical_id(connection, int(matches[0][0]))
    if found != EXPECTED_ALIASES:
        raise ValueError(f"Exact canonical matches changed: {found!r}")
    return found


def import_database(database: Path, rows: list[dict[str, str]], topics: list[dict[str, str]]) -> dict[str, object]:
    audio_paths = [f"english_by_topic/{row['audio_file']}" for row in rows]
    connection = sqlite3.connect(database, timeout=60, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        assert_free_ranges(connection, audio_paths)
        aliases = validate_exact_aliases(connection, rows)

        for topic in topics:
            unit_no = unit_number(topic["unit"])
            connection.execute(
                """
                INSERT INTO collections(
                    id,topic_id,topic_name,source_group,name,description,category,
                    difficulty_level,sentence_count,island_type,is_vocabulary
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    COLLECTION_ID_START + unit_no - 1,
                    None,
                    COURSE_NAME,
                    SOURCE_GROUP,
                    f"{topic['unit']} · {topic['topic_en']}",
                    topic["topic_vi"],
                    "Course",
                    1,
                    int(topic["sentence_count"]),
                    "course_unit",
                    0,
                ),
            )

        for offset, row in enumerate(rows):
            content_id = CONTENT_ID_START + offset
            unit_no = unit_number(row["unit"])
            collection_id = COLLECTION_ID_START + unit_no - 1
            item_index = int(row["index"])
            content_key = f"course:english_by_topic:{offset + 1:04d}"
            context = f"{row['unit']} / {row['topic_en']} / #{item_index}"
            connection.execute(
                """
                INSERT INTO sentence_content(
                    content_id,content_key,en_us,vi_vn,usage_note,literal_note,
                    translation_status,tts_text,tts_status,needs_tts_review,
                    tts_review_reasons,usage_note_candidate,short_or_elliptical,
                    long_sentence,source_sentence_count,membership_count,source_types,
                    source_sentence_ids,source_contexts,source_text_variants,
                    has_context_duplicates,grammar_fix_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    content_id, content_key, row["english"], row["vietnamese"], "", "",
                    "complete", row["english"], "source_audio", 0, "", 0, 0, 0,
                    1, 1, SOURCE_GROUP, str(content_id), context, row["english"], 0, 0,
                ),
            )
            connection.execute(
                """
                INSERT INTO content_membership(
                    source_type,source_group,topic_name,island_id,collection_name,
                    order_index,sentence_id,content_id,en_us_clean
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    "course", SOURCE_GROUP, COURSE_NAME, collection_id,
                    f"{row['unit']} · {row['topic_en']}", item_index, content_id,
                    content_id, row["english"],
                ),
            )
            connection.execute(
                "INSERT INTO content_audio(content_id,audio_path,audio_key,source) VALUES(?,?,?,?)",
                (
                    content_id,
                    f"english_by_topic/{row['audio_file']}",
                    Path(row["audio_file"]).stem,
                    COURSE_NAME,
                ),
            )
            target = aliases.get(row["english"])
            if target is not None:
                connection.execute(
                    "INSERT INTO srs_alias(content_id,canonical_content_id) VALUES(?,?)",
                    (content_id, target),
                )

        collection_count = connection.execute(
            "SELECT COUNT(*) FROM collections WHERE source_group=?", (SOURCE_GROUP,)
        ).fetchone()[0]
        membership_count = connection.execute(
            "SELECT COUNT(*) FROM content_membership WHERE source_group=?", (SOURCE_GROUP,)
        ).fetchone()[0]
        audio_count = connection.execute(
            "SELECT COUNT(*) FROM content_audio WHERE audio_path LIKE 'english_by_topic/%'"
        ).fetchone()[0]
        alias_count = connection.execute(
            "SELECT COUNT(*) FROM srs_alias WHERE content_id BETWEEN ? AND ? AND content_id<>canonical_content_id",
            (CONTENT_ID_START, CONTENT_ID_START + EXPECTED_SENTENCES - 1),
        ).fetchone()[0]
        own_canonical_count = connection.execute(
            """
            SELECT COUNT(*) FROM sentence_content s
            LEFT JOIN srs_alias a ON a.content_id=s.content_id
            WHERE s.content_id BETWEEN ? AND ? AND a.content_id IS NULL
            """,
            (CONTENT_ID_START, CONTENT_ID_START + EXPECTED_SENTENCES - 1),
        ).fetchone()[0]
        if (collection_count, membership_count, audio_count, alias_count, own_canonical_count) != (
            EXPECTED_UNITS, EXPECTED_SENTENCES, EXPECTED_SENTENCES, len(EXPECTED_ALIASES),
            EXPECTED_SENTENCES - len(EXPECTED_ALIASES),
        ):
            raise ValueError(
                "Post-import totals failed: "
                f"{collection_count=}, {membership_count=}, {audio_count=}, "
                f"{alias_count=}, {own_canonical_count=}"
            )
        foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if foreign_key_issues:
            raise ValueError(f"foreign_key_check failed: {foreign_key_issues!r}")
        if integrity != [("ok",)]:
            raise ValueError(f"integrity_check failed: {integrity!r}")
        connection.execute("COMMIT")
        return {
            "units": collection_count,
            "sentences": membership_count,
            "audio_rows": audio_count,
            "exact_aliases": alias_count,
            "own_canonical": own_canonical_count,
            "integrity_check": "ok",
            "foreign_key_check": "ok",
        }
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--course-audio-root", required=True, type=Path)
    args = parser.parse_args()

    rows, topics, _manifest, audio = read_package(args.package.resolve())
    original_hash = validate_backup(args.database.resolve(), args.backup.resolve())
    audio_destination = stage_audio(audio, args.course_audio_root.resolve())
    try:
        result = import_database(args.database.resolve(), rows, topics)
    except Exception:
        if (
            audio_destination.exists()
            and audio_destination.name == "english_by_topic"
            and audio_destination.parent == args.course_audio_root.resolve()
        ):
            shutil.rmtree(audio_destination)
        raise

    result.update(
        {
            "backup_sha256": original_hash,
            "database_sha256": sha256(args.database.resolve()),
            "audio_files": len(list(audio_destination.glob("*.mp3"))),
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
