#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

from app_version import APP_VERSION, BACKUP_FORMAT_VERSION, USER_DB_SCHEMA_VERSION
from audio_index import BUNDLED_AUDIO_INDEX
from bulk_audio import ALLOWED_AUDIO_EXTENSIONS, BULK_AUDIO_SESSIONS

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
AUDIO = ROOT / "audio"
COURSE_AUDIO = ROOT / "course_audio"
TEMPLATE_XLSX = ROOT / "templates" / "mau_import_my_island.xlsx"
CONTENT_DB = ROOT / "data" / "content.sqlite"
ORDER_FILE = ROOT / "data" / "collection_order.json"
COURSE_CATALOG_FILE = ROOT / "data" / "course_catalog.json"
if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
    USER_BASE = Path(os.environ["LOCALAPPDATA"]) / "EnglishLocal"
else:
    USER_BASE = ROOT
USER_AUDIO = USER_BASE / "user_audio"
USER_DIR = USER_BASE / "user_data"
USER_DB = USER_DIR / "learning.sqlite"
HOST = "127.0.0.1"
DEFAULT_PORT = 8767
USER_DATA_LOCK = threading.RLock()
BACKUP_TYPE = "english-learning-app-user-data"
MAX_BACKUP_ARCHIVE_BYTES = 16 * 1024 * 1024 * 1024
MAX_BACKUP_UNCOMPRESSED_BYTES = 64 * 1024 * 1024 * 1024
MAX_BACKUP_FILE_COUNT = 100000
MAX_USER_AUDIO_FILE_BYTES = 25 * 1024 * 1024
MAX_BACKUP_COMPRESSION_RATIO = 500
RESTORE_DISK_MARGIN_BYTES = 512 * 1024 * 1024
MAX_XLSX_ARCHIVE_BYTES = 30 * 1024 * 1024
MAX_XLSX_ZIP_MEMBERS = 2048
MAX_XLSX_XML_MEMBER_BYTES = 64 * 1024 * 1024
MAX_XLSX_TOTAL_XML_BYTES = 128 * 1024 * 1024
SUPPORTED_USER_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".webm"}
RESTORE_SHUTDOWN_CALLBACK = None
RESTORE_SHUTDOWN_PENDING = threading.Event()

USER_SETTING_DEFAULTS = {
    "new_per_day": "20",
    "desired_retention": "0.90",
    "reschedule_on_retention_change": "0",
    "recall_mode": "vi_en",
    "list_speed": "1.00",
    "shadow_speed": "1.00",
    "shadow_repeat": "3",
    "shadow_pause": "2",
    "list_auto_delay": "1",
    "active_collection_key": "core:219",
}

REQUIRED_USER_SCHEMA = {
    "app_settings": {"key", "value"},
    "collection_progress": {"collection_key", "last_index", "updated_at_ts"},
    "saved_items": {"item_key", "saved_at_ts"},
    "suspended_items": {"item_key", "suspended_at_ts"},
    "fsrs_cards": {
        "item_key", "state", "step", "stability", "difficulty", "due_ts",
        "last_review_ts", "introduced_at_ts", "review_count", "lapse_count", "last_rating",
    },
    "review_log": {
        "id", "item_key", "rating", "review_ts", "state_before", "state_after",
        "due_after_ts", "stability_after", "difficulty_after", "source_mode",
    },
    "my_islands": {"id", "name", "description", "created_at_ts", "updated_at_ts"},
    "my_island_members": {"id", "island_id", "order_index", "item_key"},
    "custom_sentences": {
        "id", "en_us", "vi_vn", "usage_note", "literal_note", "audio_file",
        "audio_key", "audio_expected", "note", "created_at_ts", "updated_at_ts",
    },
}

# FSRS-6 default parameters used by current Py-FSRS documentation.
# Core scheduling math follows the open-source FSRS DSR model. Fuzzing is disabled
# deliberately so this personal offline app produces deterministic schedules.
FSRS_W = (
    0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194,
    0.001, 1.8722, 0.1666, 0.796, 1.4835, 0.0614, 0.2629,
    1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
)
FSRS_DECAY = -FSRS_W[20]
FSRS_FACTOR = 0.9 ** (1 / FSRS_DECAY) - 1
LEARNING_STEPS = (60, 600)       # 1 min, 10 min
RELEARNING_STEPS = (600,)        # 10 min
STATE_LEARNING = 1
STATE_REVIEW = 2
STATE_RELEARNING = 3
RATING_NAMES = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}


def utc_now():
    return datetime.now(timezone.utc)


def now_ts():
    return time.time()


def iso_from_ts(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds")


def local_day_bounds_ts():
    local_now = datetime.now().astimezone()
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.timestamp(), end_local.timestamp()


def content_conn():
    con = sqlite3.connect(f"file:{CONTENT_DB}?mode=ro", uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    return con


class _UserConnection(sqlite3.Connection):
    """SQLite connection that owns the user-data lock until it is closed."""

    _holds_user_data_lock = False

    def close(self):
        try:
            super().close()
        finally:
            if self._holds_user_data_lock:
                self._holds_user_data_lock = False
                USER_DATA_LOCK.release()

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _initialize_user_connection(con):
    current_version = int(con.execute("PRAGMA user_version").fetchone()[0])
    if current_version > USER_DB_SCHEMA_VERSION:
        raise ValueError(
            f"Database người dùng dùng schema {current_version}, mới hơn schema "
            f"{USER_DB_SCHEMA_VERSION} mà ứng dụng hỗ trợ"
        )
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS collection_progress (
            collection_key TEXT PRIMARY KEY,
            last_index INTEGER NOT NULL DEFAULT 0,
            updated_at_ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS saved_items (
            item_key TEXT PRIMARY KEY,
            saved_at_ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS suspended_items (
            item_key TEXT PRIMARY KEY,
            suspended_at_ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fsrs_cards (
            item_key TEXT PRIMARY KEY,
            state INTEGER NOT NULL DEFAULT 1,
            step INTEGER,
            stability REAL,
            difficulty REAL,
            due_ts REAL NOT NULL,
            last_review_ts REAL,
            introduced_at_ts REAL NOT NULL,
            review_count INTEGER NOT NULL DEFAULT 0,
            lapse_count INTEGER NOT NULL DEFAULT 0,
            last_rating INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_fsrs_due ON fsrs_cards(due_ts);
        CREATE TABLE IF NOT EXISTS review_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_key TEXT NOT NULL,
            rating INTEGER NOT NULL,
            review_ts REAL NOT NULL,
            state_before INTEGER,
            state_after INTEGER,
            due_after_ts REAL,
            stability_after REAL,
            difficulty_after REAL,
            source_mode TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_review_item ON review_log(item_key, review_ts);
        CREATE INDEX IF NOT EXISTS idx_review_time ON review_log(review_ts);
        CREATE TABLE IF NOT EXISTS my_islands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at_ts REAL NOT NULL,
            updated_at_ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS my_island_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            island_id INTEGER NOT NULL,
            order_index INTEGER NOT NULL,
            item_key TEXT NOT NULL,
            FOREIGN KEY(island_id) REFERENCES my_islands(id) ON DELETE CASCADE,
            UNIQUE(island_id, item_key)
        );
        CREATE INDEX IF NOT EXISTS idx_my_members ON my_island_members(island_id, order_index);
        CREATE TABLE IF NOT EXISTS custom_sentences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            en_us TEXT NOT NULL,
            vi_vn TEXT NOT NULL DEFAULT '',
            usage_note TEXT NOT NULL DEFAULT '',
            literal_note TEXT NOT NULL DEFAULT '',
            audio_file TEXT,
            audio_key TEXT,
            audio_expected TEXT,
            note TEXT NOT NULL DEFAULT '',
            created_at_ts REAL NOT NULL,
            updated_at_ts REAL NOT NULL
        );
        """
    )
    # Migrate user databases created by V2.3 and earlier.
    existing_cols = {r[1] for r in con.execute("PRAGMA table_info(custom_sentences)")}
    for col, ddl in (("audio_key", "TEXT"), ("audio_expected", "TEXT"), ("note", "TEXT NOT NULL DEFAULT ''")):
        if col not in existing_cols:
            con.execute(f"ALTER TABLE custom_sentences ADD COLUMN {col} {ddl}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_custom_audio_key ON custom_sentences(audio_key)")
    for k, v in USER_SETTING_DEFAULTS.items():
        con.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)", (k, v))
    con.execute(f"PRAGMA user_version={int(USER_DB_SCHEMA_VERSION)}")
    con.commit()


def _create_user_database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=20)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        _initialize_user_connection(con)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        con.execute("PRAGMA journal_mode=DELETE").fetchone()
    finally:
        con.close()


def user_conn():
    USER_DATA_LOCK.acquire()
    con = None
    try:
        USER_DIR.mkdir(parents=True, exist_ok=True)
        USER_AUDIO.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(USER_DB, timeout=20, factory=_UserConnection)
        con._holds_user_data_lock = True
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        _initialize_user_connection(con)
    except Exception:
        if con is not None:
            con.close()
        else:
            USER_DATA_LOCK.release()
        raise
    return con


def setting_get(con, key, default=None):
    r = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def setting_set(con, key, value):
    con.execute(
        "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def load_order_meta():
    try:
        return json.loads(ORDER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"topic_order": [], "core_order": {}, "vocab_order": []}


ORDER_META = load_order_meta()


def load_course_catalog():
    try:
        catalog = json.loads(COURSE_CATALOG_FILE.read_text(encoding="utf-8"))
        return catalog if isinstance(catalog, list) else []
    except Exception:
        return []


COURSE_CATALOG = load_course_catalog()


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def collection_key(kind, ident):
    return f"{'my' if kind == 'my' else 'core'}:{int(ident)}"


def parse_collection_key(key):
    try:
        kind, sid = str(key).split(":", 1)
        return ("my" if kind == "my" else "core", int(sid))
    except Exception:
        return ("core", 219)


def item_key_standard(content_id):
    return f"s:{int(content_id)}"


def canonical_content_id(content_id, c=None):
    own = c is None
    if own:
        c = content_conn()
    try:
        try:
            r = c.execute("SELECT canonical_content_id FROM srs_alias WHERE content_id=?", (int(content_id),)).fetchone()
        except sqlite3.OperationalError:
            r = None
        return int(r[0]) if r else int(content_id)
    finally:
        if own:
            c.close()


def item_key_for_content(content_id, c=None):
    return item_key_standard(canonical_content_id(content_id, c))


def audio_url_for(content_id, c=None):
    own = c is None
    if own:
        c = content_conn()
    try:
        try:
            r = c.execute("SELECT audio_path FROM content_audio WHERE content_id=?", (int(content_id),)).fetchone()
        except sqlite3.OperationalError:
            r = None
        if r:
            rel = str(r[0]).replace("\\", "/").lstrip("/")
            path = (COURSE_AUDIO / rel).resolve()
            if COURSE_AUDIO.resolve() in path.parents and BUNDLED_AUDIO_INDEX.has_course(rel, AUDIO, COURSE_AUDIO):
                return "/course-audio/" + urllib.parse.quote(rel, safe="/")
            return None
        filename = f"{int(content_id):06d}.mp3"
        return f"/audio/{filename}" if BUNDLED_AUDIO_INDEX.has_core(filename, AUDIO, COURSE_AUDIO) else None
    finally:
        if own:
            c.close()


def item_key_custom(custom_id):
    return f"c:{int(custom_id)}"


def user_audio_url(filename):
    """User audio stays live (not cached) because users can restore/replace it at runtime."""
    name = str(filename or "")
    if not re.fullmatch(r"custom_\d{6}\.(mp3|wav|m4a|aac|ogg|webm)", name, re.I):
        return None
    path = (USER_AUDIO / name).resolve()
    if path.parent != USER_AUDIO.resolve() or not path.is_file():
        return None
    return f"/user-audio/{name}"


def parse_item_key(key):
    try:
        kind, sid = str(key).split(":", 1)
        ident = int(sid)
        if kind not in ("s", "c"):
            raise ValueError
        return kind, ident
    except Exception:
        raise ValueError("item_key không hợp lệ")


def get_standard_item(content_id, c=None):
    own = c is None
    if own:
        c = content_conn()
    try:
        r = c.execute(
            "SELECT content_id,en_us,vi_vn,usage_note,literal_note FROM sentence_content WHERE content_id=?",
            (int(content_id),),
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        d.update({
            "item_key": item_key_for_content(content_id, c),
            "content_id": int(content_id),
            "is_custom": False,
            "audio": audio_url_for(content_id, c),
        })
        return d
    finally:
        if own:
            c.close()


def get_custom_item(custom_id, u=None):
    own = u is None
    if own:
        u = user_conn()
    try:
        r = u.execute(
            "SELECT id,en_us,vi_vn,usage_note,literal_note,audio_file,audio_key,audio_expected,note FROM custom_sentences WHERE id=?",
            (int(custom_id),),
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        d.update({
            "item_key": item_key_custom(custom_id),
            "content_id": None,
            "custom_id": int(custom_id),
            "is_custom": True,
            "audio": user_audio_url(d.get("audio_file")),
        })
        return d
    finally:
        if own:
            u.close()


def resolve_item(key, c=None, u=None):
    k, ident = parse_item_key(key)
    return get_standard_item(ident, c) if k == "s" else get_custom_item(ident, u)


def attach_user_state(items, u):
    if not items:
        return items
    keys = [x["item_key"] for x in items]
    # Chunk to stay below SQLite variable limits on older Python/SQLite builds.
    saved = set()
    suspended = set()
    cards = {}
    for i in range(0, len(keys), 700):
        chunk = keys[i:i+700]
        ph = ",".join("?" for _ in chunk)
        saved.update(r[0] for r in u.execute(f"SELECT item_key FROM saved_items WHERE item_key IN ({ph})", chunk))
        suspended.update(r[0] for r in u.execute(f"SELECT item_key FROM suspended_items WHERE item_key IN ({ph})", chunk))
        for r in u.execute(
            f"SELECT item_key,state,step,stability,difficulty,due_ts,last_review_ts,introduced_at_ts,review_count,lapse_count,last_rating FROM fsrs_cards WHERE item_key IN ({ph})",
            chunk,
        ):
            cards[r["item_key"]] = dict(r)
    now = now_ts()
    for x in items:
        x["saved"] = x["item_key"] in saved
        x["suspended"] = x["item_key"] in suspended
        cr = cards.get(x["item_key"])
        if cr:
            interval_days = None
            if cr["last_review_ts"] is not None:
                interval_days = max(0, int(round((float(cr["due_ts"]) - float(cr["last_review_ts"])) / 86400)))
            x["srs"] = {
                "state": cr["state"],
                "step": cr["step"],
                "stability": cr["stability"],
                "difficulty": cr["difficulty"],
                "due": iso_from_ts(cr["due_ts"]),
                "due_ts": cr["due_ts"],
                "due_now": float(cr["due_ts"]) <= now and not x["suspended"],
                "last_review": iso_from_ts(cr["last_review_ts"]),
                "last_review_ts": cr["last_review_ts"],
                "introduced_at": iso_from_ts(cr["introduced_at_ts"]),
                "introduced_at_ts": cr["introduced_at_ts"],
                "review_count": cr["review_count"],
                "lapse_count": cr["lapse_count"],
                "last_rating": cr["last_rating"],
                "interval_days": interval_days,
                "suspended": x["suspended"],
            }
        else:
            x["srs"] = None
    return items


def ordered_collections(c):
    topics = rows_to_dicts(c.execute(
        "SELECT id,name,description,display_order,declared_island_count FROM topics ORDER BY display_order,id"
    ))
    raw = rows_to_dicts(c.execute(
        "SELECT id,topic_id,topic_name,source_group,name,description,category,difficulty_level,sentence_count,is_vocabulary FROM collections"
    ))
    by_id = {int(x["id"]): x for x in raw}
    result = []
    # Core: exact topic and island order from captured source catalogs.
    for t in topics:
        ids = ORDER_META.get("core_order", {}).get(str(int(t["id"])), [])
        for cid in ids:
            if int(cid) in by_id:
                result.append(by_id[int(cid)])
    # Fallback any uncatalogued core.
    seen = {int(x["id"]) for x in result}
    for x in sorted((z for z in raw if not z["is_vocabulary"] and not str(z.get("source_group") or "").startswith("course:") and int(z["id"]) not in seen), key=lambda z:(z.get("topic_id") or 9999, z["id"])):
        result.append(x)
    # Vocabulary: exact captured catalog response order.
    for cid in ORDER_META.get("vocab_order", []):
        if int(cid) in by_id and int(cid) not in seen:
            result.append(by_id[int(cid)])
            seen.add(int(cid))
    for x in sorted((z for z in raw if z["is_vocabulary"] and int(z["id"]) not in seen), key=lambda z:z["id"]):
        result.append(x)
    for idx, x in enumerate(result):
        x["sequence"] = idx
    return topics, result


def get_core_collection(collection_id):
    with content_conn() as c, user_conn() as u:
        col = c.execute(
            "SELECT id,topic_id,topic_name,source_group,name,description,category,difficulty_level,sentence_count,is_vocabulary FROM collections WHERE id=?",
            (int(collection_id),),
        ).fetchone()
        if not col:
            return None
        rows = rows_to_dicts(c.execute(
            """
            SELECT m.order_index,m.sentence_id,m.content_id,
                   s.en_us,
                   COALESCE(o.vi_vn_override,s.vi_vn) AS vi_vn,
                   COALESCE(o.usage_note_override,s.usage_note) AS usage_note,
                   COALESCE(o.literal_note_override,s.literal_note) AS literal_note
            FROM content_membership m
            JOIN sentence_content s ON s.content_id=m.content_id
            LEFT JOIN translation_override o
              ON o.source_type=m.source_type AND o.island_id=m.island_id
             AND o.source_sentence_id=m.sentence_id AND o.content_id=m.content_id
            WHERE m.island_id=?
            ORDER BY m.order_index, m.sentence_id
            """,
            (int(collection_id),),
        ))
        for i, x in enumerate(rows):
            x.update({
                "display_index": i + 1,
                "item_key": item_key_for_content(x["content_id"], c),
                "is_custom": False,
                "audio": audio_url_for(x["content_id"], c),
            })
        attach_user_state(rows, u)
        ck = collection_key("core", collection_id)
        pr = u.execute("SELECT last_index,updated_at_ts FROM collection_progress WHERE collection_key=?", (ck,)).fetchone()
        active = setting_get(u, "active_collection_key", "core:219") == ck
        return {
            "kind": "core",
            "collectionKey": ck,
            "collection": dict(col),
            "items": rows,
            "lastIndex": int(pr["last_index"]) if pr else 0,
            "activeDailySource": active,
        }


def get_my_collection(island_id):
    with content_conn() as c, user_conn() as u:
        isl = u.execute("SELECT id,name,description,created_at_ts,updated_at_ts FROM my_islands WHERE id=?", (int(island_id),)).fetchone()
        if not isl:
            return None
        members = rows_to_dicts(u.execute(
            "SELECT id,order_index,item_key FROM my_island_members WHERE island_id=? ORDER BY order_index,id",
            (int(island_id),),
        ))
        items = []
        for i, m in enumerate(members):
            item = resolve_item(m["item_key"], c, u)
            if not item:
                continue
            item.update({"member_id": m["id"], "order_index": m["order_index"], "display_index": i + 1})
            items.append(item)
        attach_user_state(items, u)
        ck = collection_key("my", island_id)
        pr = u.execute("SELECT last_index,updated_at_ts FROM collection_progress WHERE collection_key=?", (ck,)).fetchone()
        active = setting_get(u, "active_collection_key", "core:219") == ck
        col = dict(isl)
        col.update({"sentence_count": len(items), "is_vocabulary": 0, "topic_name": "My Islands"})
        return {
            "kind": "my",
            "collectionKey": ck,
            "collection": col,
            "items": items,
            "lastIndex": int(pr["last_index"]) if pr else 0,
            "activeDailySource": active,
        }


def get_collection_any(kind, ident):
    return get_my_collection(ident) if kind == "my" else get_core_collection(ident)


def collection_summary_from_key(key, c=None, u=None):
    ownc = c is None
    ownu = u is None
    if ownc: c = content_conn()
    if ownu: u = user_conn()
    try:
        kind, ident = parse_collection_key(key)
        if kind == "my":
            r = u.execute("SELECT id,name,description FROM my_islands WHERE id=?", (ident,)).fetchone()
            if not r:
                return None
            count = u.execute("SELECT COUNT(*) FROM my_island_members WHERE island_id=?", (ident,)).fetchone()[0]
            return {"kind":"my","id":ident,"collectionKey":key,"name":r["name"],"description":r["description"],"sentence_count":count,"topic_name":"My Islands"}
        r = c.execute("SELECT id,name,description,topic_name,sentence_count,is_vocabulary FROM collections WHERE id=?", (ident,)).fetchone()
        if not r:
            return None
        d = dict(r); d.update({"kind":"core","collectionKey":key}); return d
    finally:
        if ownc: c.close()
        if ownu: u.close()


def source_items(collection_key_value, unscheduled_only=False, limit=None):
    kind, ident = parse_collection_key(collection_key_value)
    data = get_collection_any(kind, ident)
    if not data:
        return []
    items = data["items"]
    if unscheduled_only:
        items = [x for x in items if not x.get("srs") and not x.get("suspended")]
    if limit is not None:
        items = items[:limit]
    return items


def _course_rows_match(rows, match):
    mode = str((match or {}).get("type") or "exact")
    value = str((match or {}).get("value") or "")
    if mode == "prefix":
        return [x for x in rows if str(x.get("source_group") or "").startswith(value)]
    return [x for x in rows if str(x.get("source_group") or "") == value]


def _course_audio_counts(c, collections):
    ids = [int(x["id"]) for x in collections]
    if not ids:
        return 0, 0
    placeholders = ",".join("?" for _ in ids)
    paths = {
        str(r[0]).replace("\\", "/").lstrip("/")
        for r in c.execute(
            f"SELECT DISTINCT ca.audio_path FROM content_membership m "
            f"JOIN content_audio ca ON ca.content_id=m.content_id WHERE m.island_id IN ({placeholders})",
            ids,
        )
    }
    available = BUNDLED_AUDIO_INDEX.count_course(paths, AUDIO, COURSE_AUDIO)
    total = sum(int(x.get("sentence_count") or 0) for x in collections)
    return available, max(0, total - available)


def get_courses(c):
    rows = rows_to_dicts(c.execute(
        "SELECT id,topic_id,topic_name,source_group,name,description,category,difficulty_level,sentence_count,is_vocabulary FROM collections WHERE source_group LIKE 'course:%' ORDER BY id"
    ))
    courses = []
    for meta in COURSE_CATALOG:
        collections = _course_rows_match(rows, meta.get("match"))
        collections.sort(key=lambda x: int(x["id"]))
        if not collections:
            continue
        available, missing = _course_audio_counts(c, collections)
        course = {
            "key": str(meta.get("key") or ""),
            "name": str(meta.get("name") or "Course"),
            "description": str(meta.get("description") or ""),
            "layout": str(meta.get("layout") or "units"),
            "group_key": meta.get("group_key"),
            "audio_import": bool(meta.get("audio_import")),
            "sentence_count": sum(int(x.get("sentence_count") or 0) for x in collections),
            "audio_available": available,
            "audio_missing": missing,
            "collections": collections,
            "sections": [],
        }
        if course["layout"] == "books":
            grouped = {}
            for collection in collections:
                match = re.search(r":book(\d+)$", str(collection.get("source_group") or ""))
                number = int(match.group(1)) if match else 0
                grouped.setdefault(number, []).append(collection)
            course["sections"] = [
                {
                    "key": f"book-{number}",
                    "name": f"Book {number}",
                    "group_key": f"book:{course['key']}:{number}",
                    "sentence_count": sum(int(x.get("sentence_count") or 0) for x in units),
                    "collections": units,
                }
                for number, units in sorted(grouped.items())
            ]
            course["books"] = [
                {"book": int(section["key"].split("-", 1)[1]), "sentence_count": section["sentence_count"], "units": section["collections"]}
                for section in course["sections"]
            ]
        elif course["layout"] == "collection":
            course["collection"] = collections[0]
        else:
            course["sections"] = [{
                "key": "units",
                "name": f"{len(collections)} Units",
                "group_key": course.get("group_key"),
                "sentence_count": course["sentence_count"],
                "collections": collections,
            }]
            course["units"] = collections
        courses.append(course)
    return courses


def active_source_or_fallback(c, u):
    stored_key = setting_get(u, "active_collection_key", USER_SETTING_DEFAULTS["active_collection_key"])
    source = collection_summary_from_key(stored_key, c, u)
    if source:
        return stored_key, source
    fallback_key = USER_SETTING_DEFAULTS["active_collection_key"]
    source = collection_summary_from_key(fallback_key, c, u)
    if not source:
        _, collections = ordered_collections(c)
        fallback = next((x for x in collections if not str(x.get("source_group") or "").startswith("course:")), None)
        if not fallback:
            return stored_key, None
        fallback_key = collection_key("core", fallback["id"])
        source = collection_summary_from_key(fallback_key, c, u)
    setting_set(u, "active_collection_key", fallback_key)
    u.commit()
    return fallback_key, source


def get_bootstrap():
    with content_conn() as c, user_conn() as u:
        topics, collections = ordered_collections(c)
        total_sentences = c.execute("SELECT COUNT(*) FROM sentence_content").fetchone()[0]
        core_count = sum(1 for x in collections if not x["is_vocabulary"])
        vocab_count = sum(1 for x in collections if x["is_vocabulary"])
        my_islands = rows_to_dicts(u.execute(
            """SELECT i.id,i.name,i.description,i.updated_at_ts,COUNT(m.id) sentence_count
               FROM my_islands i LEFT JOIN my_island_members m ON m.island_id=i.id
               GROUP BY i.id ORDER BY i.updated_at_ts DESC"""
        ))
        now = now_ts()
        due_count = u.execute(
            "SELECT COUNT(*) FROM fsrs_cards f LEFT JOIN suspended_items s ON s.item_key=f.item_key WHERE f.due_ts<=? AND s.item_key IS NULL",
            (now,),
        ).fetchone()[0]
        started = u.execute("SELECT COUNT(*) FROM fsrs_cards").fetchone()[0]
        review_state = u.execute("SELECT COUNT(*) FROM fsrs_cards WHERE state=2").fetchone()[0]
        saved_count = u.execute("SELECT COUNT(*) FROM saved_items").fetchone()[0]
        b0, b1 = local_day_bounds_ts()
        reviewed_today = u.execute("SELECT COUNT(*) FROM review_log WHERE review_ts>=? AND review_ts<?", (b0,b1)).fetchone()[0]
        introduced_today = u.execute("SELECT COUNT(*) FROM fsrs_cards WHERE introduced_at_ts>=? AND introduced_at_ts<?", (b0,b1)).fetchone()[0]
        settings = {r["key"]: r["value"] for r in u.execute("SELECT key,value FROM app_settings")}
        for key in ("list_speed", "shadow_speed"):
            settings[key] = normalize_playback_speed(settings.get(key), strict=False)
        active_key, active_source = active_source_or_fallback(c, u)
        settings["active_collection_key"] = active_key
        new_limit = int(settings.get("new_per_day", "20"))
        remaining_slots = -1 if new_limit < 0 else max(0, new_limit - introduced_today)
        active_unscheduled = 0
        if active_source:
            # Count without resolving all text when possible.
            kind, ident = parse_collection_key(active_key)
            if kind == "core":
                keys = [item_key_for_content(r[0], c) for r in c.execute("SELECT content_id FROM content_membership WHERE island_id=? ORDER BY order_index,sentence_id", (ident,))]
            else:
                keys = [r[0] for r in u.execute("SELECT item_key FROM my_island_members WHERE island_id=? ORDER BY order_index,id", (ident,))]
            if keys:
                # A sentence can appear more than once in the same source collection; SRS is still global per item_key.
                keys = list(dict.fromkeys(keys))
                scheduled = set()
                suspended = set()
                for i in range(0,len(keys),700):
                    ch=keys[i:i+700]; ph=','.join('?' for _ in ch)
                    scheduled.update(r[0] for r in u.execute(f"SELECT item_key FROM fsrs_cards WHERE item_key IN ({ph})", ch))
                    suspended.update(r[0] for r in u.execute(f"SELECT item_key FROM suspended_items WHERE item_key IN ({ph})", ch))
                active_unscheduled = sum(1 for k in keys if k not in scheduled and k not in suspended)
        today_new = active_unscheduled if remaining_slots < 0 else min(active_unscheduled, remaining_slots)

        recent_rows = rows_to_dicts(u.execute("SELECT collection_key,last_index,updated_at_ts FROM collection_progress ORDER BY updated_at_ts DESC LIMIT 6"))
        recent = []
        for r in recent_rows:
            s = collection_summary_from_key(r["collection_key"], c, u)
            if s:
                s.update({"last_index":r["last_index"],"updated_at_ts":r["updated_at_ts"]})
                recent.append(s)

        # Per-collection progress: seen position and SRS count within collection.
        progress = {r["collection_key"]: {"last_index":r["last_index"],"updated_at_ts":r["updated_at_ts"]} for r in u.execute("SELECT * FROM collection_progress")}
        return {
            "appVersion": APP_VERSION,
            "backupFormatVersion": BACKUP_FORMAT_VERSION,
            "userDbSchemaVersion": USER_DB_SCHEMA_VERSION,
            "restoreResult": read_restore_result(),
            "topics": topics,
            "collections": collections,
            "courses": get_courses(c),
            "myIslands": my_islands,
            "recent": recent,
            "collectionProgress": progress,
            "activeSource": active_source,
            "settings": settings,
            "stats": {
                "totalSentences": total_sentences,
                "islands": core_count,
                "vocabularyCollections": vocab_count,
                "myIslands": len(my_islands),
                "startedSrs": started,
                "reviewState": review_state,
                "dueToday": due_count,
                "newToday": today_new,
                "introducedToday": introduced_today,
                "reviewedToday": reviewed_today,
                "saved": saved_count,
            },
            "scheduler": {"name":"FSRS-6","desiredRetention":float(settings.get("desired_retention","0.90")),"fuzzing":False},
        }


def search_content(q, filter_name="all", limit=80):
    q = (q or "").strip()
    if not q:
        return []
    filter_name = str(filter_name or "all").lower()
    if filter_name not in {"all", "learn", "vocabulary", "courses", "my", "saved"}:
        filter_name = "all"
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 80
    like = f"%{q}%"
    out = []
    with content_conn() as c, user_conn() as u:
        scoped_keys = None
        if filter_name == "saved":
            scoped_keys = {row[0] for row in u.execute("SELECT item_key FROM saved_items")}
        elif filter_name == "my":
            scoped_keys = {row[0] for row in u.execute("SELECT DISTINCT item_key FROM my_island_members")}

        if filter_name in {"learn", "vocabulary", "courses"}:
            scope_sql = {
                "learn": "COALESCE(co.is_vocabulary,0)=0 AND COALESCE(co.source_group,'') NOT LIKE 'course:%'",
                "vocabulary": "COALESCE(co.is_vocabulary,0)=1",
                "courses": "COALESCE(co.source_group,'') LIKE 'course:%'",
            }[filter_name]
            candidate_sql = f"""
                SELECT s.content_id,COALESCE(a.canonical_content_id,s.content_id) canonical_id
                FROM sentence_content s
                LEFT JOIN srs_alias a ON a.content_id=s.content_id
                JOIN content_membership m ON m.content_id=s.content_id
                JOIN collections co ON co.id=m.island_id
                WHERE (s.en_us LIKE ? COLLATE NOCASE OR s.vi_vn LIKE ? COLLATE NOCASE)
                  AND {scope_sql}
                ORDER BY CASE WHEN s.en_us LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END,s.content_id
                LIMIT ? OFFSET ?
                """
            candidates = []
            candidate_ids = set()
            batch_size = limit * 6
            offset = 0
            while len(candidate_ids) < limit:
                page = rows_to_dicts(c.execute(candidate_sql, (like, like, like, batch_size, offset)))
                candidates.extend(page)
                candidate_ids.update(int(row["canonical_id"]) for row in page)
                if len(page) < batch_size:
                    break
                offset += batch_size
        else:
            candidate_sql = """SELECT s.content_id,COALESCE(a.canonical_content_id,s.content_id) canonical_id
                    FROM sentence_content s LEFT JOIN srs_alias a ON a.content_id=s.content_id
                    WHERE s.en_us LIKE ? COLLATE NOCASE OR s.vi_vn LIKE ? COLLATE NOCASE
                    ORDER BY CASE WHEN s.en_us LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END,s.content_id"""
            if scoped_keys is not None:
                candidates = rows_to_dicts(c.execute(candidate_sql, (like, like, like)))
            else:
                candidates = []
                candidate_ids = set()
                batch_size = limit * 6
                offset = 0
                while len(candidate_ids) < limit:
                    page = rows_to_dicts(c.execute(candidate_sql + " LIMIT ? OFFSET ?", (like, like, like, batch_size, offset)))
                    candidates.extend(page)
                    candidate_ids.update(int(row["canonical_id"]) for row in page)
                    if len(page) < batch_size:
                        break
                    offset += batch_size

        seen = set()
        standard_ids = []
        for candidate in candidates:
            canonical_id = int(candidate["canonical_id"])
            item_key = item_key_standard(canonical_id)
            if scoped_keys is not None and item_key not in scoped_keys:
                continue
            if item_key in seen:
                continue
            seen.add(item_key)
            standard_ids.append(canonical_id)
            if len(standard_ids) >= limit:
                break

        core_locations = {canonical_id: [] for canonical_id in standard_ids}
        my_locations = {item_key_standard(canonical_id): [] for canonical_id in standard_ids}
        for offset in range(0, len(standard_ids), 700):
            chunk = standard_ids[offset:offset + 700]
            placeholders = ",".join("?" for _ in chunk)
            for row in c.execute(
                f"""SELECT DISTINCT COALESCE(a.canonical_content_id,m.content_id) canonical_id,
                           m.island_id,m.collection_name,m.topic_name,m.order_index,co.is_vocabulary,co.source_group
                    FROM content_membership m
                    LEFT JOIN srs_alias a ON a.content_id=m.content_id
                    LEFT JOIN collections co ON co.id=m.island_id
                    WHERE COALESCE(a.canonical_content_id,m.content_id) IN ({placeholders})
                    ORDER BY m.island_id,m.order_index""",
                chunk,
            ):
                location = dict(row)
                canonical_id = int(location.pop("canonical_id"))
                source_group = str(location.get("source_group") or "")
                location["kind"] = "courses" if source_group.startswith("course:") else ("vocabulary" if location.get("is_vocabulary") else "learn")
                location["collection_kind"] = "core"
                core_locations[canonical_id].append(location)

            keys = [item_key_standard(canonical_id) for canonical_id in chunk]
            for row in u.execute(
                f"""SELECT m.item_key,i.id island_id,i.name collection_name,'My Islands' topic_name,m.order_index
                    FROM my_island_members m JOIN my_islands i ON i.id=m.island_id
                    WHERE m.item_key IN ({placeholders})
                    ORDER BY i.updated_at_ts DESC,m.order_index""",
                keys,
            ):
                location = dict(row)
                item_key = location.pop("item_key")
                location.update({"kind": "my", "collection_kind": "my", "is_vocabulary": 0, "source_group": "my"})
                my_locations[item_key].append(location)

        for canonical_id in standard_ids:
            item_key = item_key_standard(canonical_id)
            item = get_standard_item(canonical_id, c)
            if not item:
                continue
            locations = core_locations[canonical_id] + my_locations[item_key]
            unique_locations = {}
            for location in locations:
                unique_locations.setdefault((location["collection_kind"], int(location["island_id"])), location)
            locations = list(unique_locations.values())
            item["locations"] = locations
            out.append(item)
            if len(out) >= limit:
                break
        if filter_name in {"all", "my", "saved"} and len(out) < limit:
            custom_limit = "" if scoped_keys is not None else "LIMIT ?"
            custom_params = [like, like, like]
            if scoped_keys is None:
                custom_params.append(limit - len(out))
            for r in u.execute(
                f"""SELECT id,en_us,vi_vn,usage_note,literal_note,audio_file,audio_key,audio_expected,note
                    FROM custom_sentences
                    WHERE en_us LIKE ? COLLATE NOCASE OR vi_vn LIKE ? COLLATE NOCASE
                    ORDER BY CASE WHEN en_us LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END,id DESC {custom_limit}""",
                custom_params,
            ):
                d = dict(r)
                key = item_key_custom(d["id"])
                if scoped_keys is not None and key not in scoped_keys:
                    continue
                locations = []
                for row in u.execute(
                    """SELECT i.id island_id,i.name collection_name,'My Islands' topic_name,m.order_index
                       FROM my_island_members m JOIN my_islands i ON i.id=m.island_id
                       WHERE m.item_key=? ORDER BY i.updated_at_ts DESC,m.order_index""", (key,)
                ):
                    location = dict(row)
                    location.update({"kind": "my", "collection_kind": "my", "is_vocabulary": 0, "source_group": "my"})
                    locations.append(location)
                d.update({"item_key":key,"custom_id":d["id"],"is_custom":True,"audio":user_audio_url(d.get("audio_file")),"locations":locations})
                out.append(d)
                if len(out) >= limit:
                    break
        attach_user_state(out, u)
        filtered = []
        priority = {"learn": 0, "vocabulary": 1, "courses": 2, "my": 3}
        for item in out:
            locations = sorted(item.get("locations") or [], key=lambda location: (priority.get(location.get("kind"), 9), int(location.get("island_id") or 0), int(location.get("order_index") or 0)))
            if filter_name == "saved":
                include = bool(item.get("saved"))
            elif filter_name == "all":
                include = True
            else:
                include = any(location.get("kind") == filter_name for location in locations)
            if not include:
                continue
            preferred = next((location for location in locations if filter_name not in {"all", "saved"} and location.get("kind") == filter_name), None)
            item["locations"] = locations
            item["primary_location"] = preferred or (locations[0] if locations else None)
            item["other_location_count"] = max(0, len(locations) - (1 if locations else 0))
            filtered.append(item)
            if len(filtered) >= limit:
                break
    return filtered


def get_saved_items():
    with content_conn() as c, user_conn() as u:
        rows = rows_to_dicts(u.execute("SELECT item_key,saved_at_ts FROM saved_items ORDER BY saved_at_ts DESC"))
        out=[]
        for r in rows:
            x=resolve_item(r["item_key"],c,u)
            if x:
                x["saved_at_ts"]=r["saved_at_ts"]
                out.append(x)
        attach_user_state(out,u)
        return out


def get_my_islands():
    with user_conn() as u:
        return rows_to_dicts(u.execute(
            """SELECT i.id,i.name,i.description,i.created_at_ts,i.updated_at_ts,COUNT(m.id) sentence_count
               FROM my_islands i LEFT JOIN my_island_members m ON m.island_id=i.id
               GROUP BY i.id ORDER BY i.updated_at_ts DESC"""
        ))


# ---------------- FSRS-6 scheduling core ----------------
def clamp_difficulty(d):
    return min(max(float(d), 1.0), 10.0)


def clamp_stability(s):
    return max(float(s), 0.001)


def initial_stability(rating):
    return clamp_stability(FSRS_W[rating-1])


def initial_difficulty(rating, clamp=True):
    d = FSRS_W[4] - math.exp(FSRS_W[5] * (rating - 1)) + 1
    return clamp_difficulty(d) if clamp else d


def short_term_stability(stability, rating):
    inc = math.exp(FSRS_W[17] * (rating - 3 + FSRS_W[18])) * (stability ** -FSRS_W[19])
    if rating >= 2:
        inc = max(inc, 1.0)
    return clamp_stability(stability * inc)


def next_difficulty(difficulty, rating):
    linear = (10.0 - difficulty) * (-(FSRS_W[6] * (rating - 3))) / 9.0
    arg1 = initial_difficulty(4, clamp=False)
    arg2 = difficulty + linear
    d = FSRS_W[7] * arg1 + (1 - FSRS_W[7]) * arg2
    return clamp_difficulty(d)


def retrievability(stability, last_review_ts, current_ts):
    if stability is None or last_review_ts is None:
        return 0.0
    elapsed_days = max(0, int((current_ts - last_review_ts) // 86400))
    return (1 + FSRS_FACTOR * elapsed_days / stability) ** FSRS_DECAY


def next_stability(difficulty, stability, r, rating):
    if rating == 1:
        long_term = (
            FSRS_W[11]
            * (difficulty ** -FSRS_W[12])
            * (((stability + 1) ** FSRS_W[13]) - 1)
            * math.exp((1 - r) * FSRS_W[14])
        )
        short_cap = stability / math.exp(FSRS_W[17] * FSRS_W[18])
        return clamp_stability(min(long_term, short_cap))
    hard_penalty = FSRS_W[15] if rating == 2 else 1.0
    easy_bonus = FSRS_W[16] if rating == 4 else 1.0
    s = stability * (
        1
        + math.exp(FSRS_W[8])
        * (11 - difficulty)
        * (stability ** -FSRS_W[9])
        * (math.exp((1 - r) * FSRS_W[10]) - 1)
        * hard_penalty
        * easy_bonus
    )
    return clamp_stability(s)


def next_interval_days(stability, desired_retention):
    retention = min(max(float(desired_retention), 0.70), 0.99)
    ivl = (stability / FSRS_FACTOR) * ((retention ** (1 / FSRS_DECAY)) - 1)
    return min(max(round(ivl), 1), 36500)


def schedule_fsrs(existing, rating, desired_retention, ts=None):
    if rating not in (1,2,3,4):
        raise ValueError("rating phải là 1..4")
    ts = float(ts or now_ts())
    if existing:
        state = int(existing["state"])
        step = existing["step"]
        stability = existing["stability"]
        difficulty = existing["difficulty"]
        last_review = existing["last_review_ts"]
        review_count = int(existing["review_count"] or 0)
        lapse_count = int(existing["lapse_count"] or 0)
        introduced_at = float(existing["introduced_at_ts"])
    else:
        state = STATE_LEARNING
        step = 0
        stability = None
        difficulty = None
        last_review = None
        review_count = 0
        lapse_count = 0
        introduced_at = ts
    state_before = state
    days_since = None if last_review is None else int((ts - float(last_review)) // 86400)

    if state == STATE_LEARNING:
        step = 0 if step is None else int(step)
        if stability is None or difficulty is None:
            stability = initial_stability(rating)
            difficulty = initial_difficulty(rating)
        elif days_since is not None and days_since < 1:
            stability = short_term_stability(stability, rating)
            difficulty = next_difficulty(difficulty, rating)
        else:
            r = retrievability(stability, last_review, ts)
            stability = next_stability(difficulty, stability, r, rating)
            difficulty = next_difficulty(difficulty, rating)
        if rating == 1:
            step = 0; due = ts + LEARNING_STEPS[0]
        elif rating == 2:
            if step == 0 and len(LEARNING_STEPS) >= 2:
                due = ts + (LEARNING_STEPS[0] + LEARNING_STEPS[1]) / 2
            else:
                due = ts + LEARNING_STEPS[min(step, len(LEARNING_STEPS)-1)]
        elif rating == 3:
            if step + 1 == len(LEARNING_STEPS):
                state = STATE_REVIEW; step = None
                due = ts + next_interval_days(stability, desired_retention) * 86400
            else:
                step += 1; due = ts + LEARNING_STEPS[step]
        else:  # Easy
            state = STATE_REVIEW; step = None
            due = ts + next_interval_days(stability, desired_retention) * 86400

    elif state == STATE_REVIEW:
        if stability is None or difficulty is None:
            # Defensive repair for an incomplete old record.
            stability = initial_stability(rating)
            difficulty = initial_difficulty(rating)
        elif days_since is not None and days_since < 1:
            stability = short_term_stability(stability, rating)
        else:
            r = retrievability(stability, last_review, ts)
            stability = next_stability(difficulty, stability, r, rating)
        difficulty = next_difficulty(difficulty, rating)
        if rating == 1:
            lapse_count += 1
            state = STATE_RELEARNING; step = 0
            due = ts + RELEARNING_STEPS[0]
        else:
            due = ts + next_interval_days(stability, desired_retention) * 86400

    elif state == STATE_RELEARNING:
        step = 0 if step is None else int(step)
        if stability is None or difficulty is None:
            stability = initial_stability(rating)
            difficulty = initial_difficulty(rating)
        elif days_since is not None and days_since < 1:
            stability = short_term_stability(stability, rating)
            difficulty = next_difficulty(difficulty, rating)
        else:
            r = retrievability(stability, last_review, ts)
            stability = next_stability(difficulty, stability, r, rating)
            difficulty = next_difficulty(difficulty, rating)
        if rating == 1:
            step = 0; due = ts + RELEARNING_STEPS[0]
        elif rating == 2:
            due = ts + (RELEARNING_STEPS[0] * 1.5)
        else:
            state = STATE_REVIEW; step = None
            due = ts + next_interval_days(stability, desired_retention) * 86400
    else:
        raise ValueError("Trạng thái SRS không hợp lệ")

    return {
        "state": state,
        "step": step,
        "stability": float(stability),
        "difficulty": float(difficulty),
        "due_ts": float(due),
        "last_review_ts": ts,
        "introduced_at_ts": introduced_at,
        "review_count": review_count + 1,
        "lapse_count": lapse_count,
        "last_rating": rating,
        "state_before": state_before,
    }


def apply_review(item_key, rating, source_mode="active_recall"):
    parse_item_key(item_key)
    with content_conn() as c, user_conn() as u:
        if not resolve_item(item_key, c, u):
            raise ValueError("Không tìm thấy câu")
        old = u.execute("SELECT * FROM fsrs_cards WHERE item_key=?", (item_key,)).fetchone()
        retention = float(setting_get(u, "desired_retention", "0.90"))
        card = schedule_fsrs(dict(old) if old else None, int(rating), retention)
        u.execute(
            """
            INSERT INTO fsrs_cards(item_key,state,step,stability,difficulty,due_ts,last_review_ts,introduced_at_ts,review_count,lapse_count,last_rating)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_key) DO UPDATE SET
                state=excluded.state, step=excluded.step, stability=excluded.stability,
                difficulty=excluded.difficulty, due_ts=excluded.due_ts,
                last_review_ts=excluded.last_review_ts, review_count=excluded.review_count,
                lapse_count=excluded.lapse_count, last_rating=excluded.last_rating
            """,
            (item_key,card["state"],card["step"],card["stability"],card["difficulty"],card["due_ts"],card["last_review_ts"],card["introduced_at_ts"],card["review_count"],card["lapse_count"],card["last_rating"]),
        )
        u.execute(
            """INSERT INTO review_log(item_key,rating,review_ts,state_before,state_after,due_after_ts,stability_after,difficulty_after,source_mode)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (item_key,int(rating),card["last_review_ts"],card["state_before"],card["state"],card["due_ts"],card["stability"],card["difficulty"],source_mode),
        )
        u.commit()
        stats = study_stats_snapshot(c, u)
        return {
            "ok": True,
            "item_key": item_key,
            "rating": int(rating),
            "rating_name": RATING_NAMES[int(rating)],
            "state": card["state"],
            "step": card["step"],
            "due": iso_from_ts(card["due_ts"]),
            "due_ts": card["due_ts"],
            "due_in_seconds": max(0, int(card["due_ts"] - now_ts())),
            "review_count": card["review_count"],
            "lapse_count": card["lapse_count"],
            "srs": {
                "state": card["state"], "step": card["step"],
                "stability": card["stability"], "difficulty": card["difficulty"],
                "due": iso_from_ts(card["due_ts"]), "due_ts": card["due_ts"],
                "due_now": card["due_ts"] <= now_ts(),
                "last_review": iso_from_ts(card["last_review_ts"]),
                "last_review_ts": card["last_review_ts"],
                "introduced_at": iso_from_ts(card["introduced_at_ts"]),
                "introduced_at_ts": card["introduced_at_ts"],
                "review_count": card["review_count"], "lapse_count": card["lapse_count"],
                "last_rating": card["last_rating"], "suspended": False,
            },
            "stats": stats,
            # Learning/Relearning steps are due-aware. The UI may keep them in the session queue,
            # but must never show them before due_ts.
            "learning_pending": card["state"] in (STATE_LEARNING, STATE_RELEARNING),
        }


def study_stats_snapshot(c, u):
    """Return the small, exact counter set needed after a rating; never scans audio."""
    now = now_ts()
    b0, b1 = local_day_bounds_ts()
    settings = {r["key"]: r["value"] for r in u.execute("SELECT key,value FROM app_settings")}
    active_key, source = active_source_or_fallback(c, u)
    introduced_today = u.execute(
        "SELECT COUNT(*) FROM fsrs_cards WHERE introduced_at_ts>=? AND introduced_at_ts<?", (b0, b1)
    ).fetchone()[0]
    new_limit = int(settings.get("new_per_day", "20"))
    remaining = -1 if new_limit < 0 else max(0, new_limit - introduced_today)
    unscheduled = 0
    if source:
        kind, ident = parse_collection_key(active_key)
        if kind == "core":
            keys = [item_key_for_content(r[0], c) for r in c.execute(
                "SELECT content_id FROM content_membership WHERE island_id=? ORDER BY order_index,sentence_id", (ident,)
            )]
        else:
            keys = [r[0] for r in u.execute(
                "SELECT item_key FROM my_island_members WHERE island_id=? ORDER BY order_index,id", (ident,)
            )]
        keys = list(dict.fromkeys(keys))
        scheduled, suspended = set(), set()
        for index in range(0, len(keys), 700):
            chunk = keys[index:index + 700]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            scheduled.update(r[0] for r in u.execute(
                f"SELECT item_key FROM fsrs_cards WHERE item_key IN ({placeholders})", chunk
            ))
            suspended.update(r[0] for r in u.execute(
                f"SELECT item_key FROM suspended_items WHERE item_key IN ({placeholders})", chunk
            ))
        unscheduled = sum(1 for key in keys if key not in scheduled and key not in suspended)
    return {
        "dueToday": u.execute(
            "SELECT COUNT(*) FROM fsrs_cards f LEFT JOIN suspended_items s ON s.item_key=f.item_key WHERE f.due_ts<=? AND s.item_key IS NULL", (now,)
        ).fetchone()[0],
        "newToday": unscheduled if remaining < 0 else min(unscheduled, remaining),
        "startedSrs": u.execute("SELECT COUNT(*) FROM fsrs_cards").fetchone()[0],
        "reviewState": u.execute("SELECT COUNT(*) FROM fsrs_cards WHERE state=?", (STATE_REVIEW,)).fetchone()[0],
        "introducedToday": introduced_today,
        "reviewedToday": u.execute(
            "SELECT COUNT(*) FROM review_log WHERE review_ts>=? AND review_ts<?", (b0, b1)
        ).fetchone()[0],
    }


def _collection_item_keys(collection_key_value, c, u):
    kind, ident = parse_collection_key(collection_key_value)
    if kind == "core":
        if not c.execute("SELECT 1 FROM collections WHERE id=?", (ident,)).fetchone():
            raise ValueError("Không tìm thấy bộ học")
        keys = [item_key_for_content(r[0], c) for r in c.execute(
            "SELECT content_id FROM content_membership WHERE island_id=? ORDER BY order_index,sentence_id",
            (ident,),
        )]
    else:
        if not u.execute("SELECT 1 FROM my_islands WHERE id=?", (ident,)).fetchone():
            raise ValueError("Không tìm thấy My Island")
        keys = [r[0] for r in u.execute(
            "SELECT item_key FROM my_island_members WHERE island_id=? ORDER BY order_index,id",
            (ident,),
        )]
    return list(dict.fromkeys(keys))


def _group_item_keys(group_key, c, u):
    group_key = str(group_key or "")
    if group_key == "course:essential4000":
        rows = c.execute(
            "SELECT m.content_id FROM content_membership m JOIN collections co ON co.id=m.island_id WHERE co.source_group LIKE 'course:essential4000:%' ORDER BY co.id,m.order_index,m.sentence_id"
        )
        return list(dict.fromkeys(item_key_for_content(r[0], c) for r in rows))
    if group_key == "course:english_by_topic":
        rows = c.execute(
            "SELECT m.content_id FROM content_membership m JOIN collections co ON co.id=m.island_id WHERE co.source_group=? ORDER BY co.id,m.order_index,m.sentence_id",
            ("course:english_by_topic",),
        )
        return list(dict.fromkeys(item_key_for_content(r[0], c) for r in rows))
    m = re.fullmatch(r"book:essential4000:([1-6])", group_key)
    if m:
        source_group = f"course:essential4000:book{int(m.group(1))}"
        rows = c.execute(
            "SELECT m.content_id FROM content_membership m JOIN collections co ON co.id=m.island_id WHERE co.source_group=? ORDER BY co.id,m.order_index,m.sentence_id",
            (source_group,),
        )
        return list(dict.fromkeys(item_key_for_content(r[0], c) for r in rows))
    raise ValueError("Nhóm Course không hợp lệ")


def _group_name(group_key):
    if group_key == "course:essential4000":
        return "4000 Essential English Words"
    if group_key == "course:english_by_topic":
        return "English by Topic"
    m = re.fullmatch(r"book:essential4000:([1-6])", str(group_key or ""))
    if m:
        return f"4000 Essential English Words · Book {int(m.group(1))}"
    return str(group_key)


def _srs_state_label(card):
    if not card:
        return "New"
    state = int(card["state"])
    if state == STATE_LEARNING:
        return "Learning"
    if state == STATE_RELEARNING:
        return "Relearning"
    if state == STATE_REVIEW:
        if card["last_review_ts"] is not None:
            interval_days = (float(card["due_ts"]) - float(card["last_review_ts"])) / 86400
            if interval_days >= 21:
                return "Mature"
        return "Review"
    return "Unknown"


def _item_srs_info(item_key, c, u):
    parse_item_key(item_key)
    item = resolve_item(item_key, c, u)
    if not item:
        raise ValueError("Không tìm thấy câu")
    card = u.execute("SELECT * FROM fsrs_cards WHERE item_key=?", (item_key,)).fetchone()
    suspended = u.execute("SELECT suspended_at_ts FROM suspended_items WHERE item_key=?", (item_key,)).fetchone()
    history_count = u.execute("SELECT COUNT(*) FROM review_log WHERE item_key=?", (item_key,)).fetchone()[0]
    last_log = u.execute(
        "SELECT rating,review_ts,state_before,state_after,due_after_ts,stability_after,difficulty_after,source_mode FROM review_log WHERE item_key=? ORDER BY review_ts DESC,id DESC LIMIT 1",
        (item_key,),
    ).fetchone()
    now = now_ts()
    out = {
        "scope": "item",
        "item_key": item_key,
        "en_us": item.get("en_us", ""),
        "vi_vn": item.get("vi_vn", ""),
        "state": _srs_state_label(card),
        "state_code": int(card["state"]) if card else 0,
        "suspended": bool(suspended),
        "suspended_at": iso_from_ts(suspended["suspended_at_ts"]) if suspended else None,
        "history_count": int(history_count),
        "due": iso_from_ts(card["due_ts"]) if card else None,
        "due_ts": float(card["due_ts"]) if card else None,
        "due_now": bool(card and float(card["due_ts"]) <= now and not suspended),
        "stability": float(card["stability"]) if card and card["stability"] is not None else None,
        "difficulty": float(card["difficulty"]) if card and card["difficulty"] is not None else None,
        "review_count": int(card["review_count"] or 0) if card else 0,
        "lapse_count": int(card["lapse_count"] or 0) if card else 0,
        "last_rating": int(card["last_rating"]) if card and card["last_rating"] is not None else None,
        "last_review": iso_from_ts(card["last_review_ts"]) if card else None,
        "introduced_at": iso_from_ts(card["introduced_at_ts"]) if card else None,
    }
    if last_log:
        out["last_log"] = {
            "rating": int(last_log["rating"]),
            "review": iso_from_ts(last_log["review_ts"]),
            "source_mode": last_log["source_mode"],
        }
    else:
        out["last_log"] = None
    return out


def _keys_srs_summary(keys, u):
    cards = {}
    suspended = set()
    for i in range(0, len(keys), 700):
        ch = keys[i:i+700]
        if not ch:
            continue
        ph = ",".join("?" for _ in ch)
        for r in u.execute(f"SELECT * FROM fsrs_cards WHERE item_key IN ({ph})", ch):
            cards[r["item_key"]] = r
        suspended.update(r[0] for r in u.execute(f"SELECT item_key FROM suspended_items WHERE item_key IN ({ph})", ch))
    counts = {"new":0,"learning":0,"review":0,"mature":0,"relearning":0,"suspended":len(suspended),"due":0,"started":len(cards)}
    now = now_ts()
    for k in keys:
        card = cards.get(k)
        label = _srs_state_label(card)
        counts[label.lower()] = counts.get(label.lower(), 0) + 1
        if card and k not in suspended and float(card["due_ts"]) <= now:
            counts["due"] += 1
    return counts


def _collection_srs_info(collection_key_value, c, u):
    keys = _collection_item_keys(collection_key_value, c, u)
    summary = collection_summary_from_key(collection_key_value, c, u)
    return {
        "scope":"collection",
        "collection_key":collection_key_value,
        "name":summary["name"] if summary else collection_key_value,
        "total":len(keys),
        "counts":_keys_srs_summary(keys, u),
    }


def _group_srs_info(group_key, c, u):
    keys = _group_item_keys(group_key, c, u)
    return {
        "scope":"group",
        "group_key":group_key,
        "name":_group_name(group_key),
        "total":len(keys),
        "counts":_keys_srs_summary(keys, u),
    }


def get_srs_info(item_key=None, collection_key_value=None, group_key=None):
    with content_conn() as c, user_conn() as u:
        if item_key:
            return _item_srs_info(str(item_key), c, u)
        if collection_key_value:
            return _collection_srs_info(str(collection_key_value), c, u)
        if group_key:
            return _group_srs_info(str(group_key), c, u)
        raise ValueError("Thiếu câu hoặc bộ học")


def manage_srs(action, item_key=None, collection_key_value=None, group_key=None):
    action = str(action or "").strip().lower()
    if action not in ("review_now", "reset", "suspend", "resume"):
        raise ValueError("Thao tác SRS không hợp lệ")
    with content_conn() as c, user_conn() as u:
        if item_key:
            parse_item_key(item_key)
            if not resolve_item(item_key, c, u):
                raise ValueError("Không tìm thấy câu")
            keys = [str(item_key)]
            scope = "item"
        elif collection_key_value:
            keys = _collection_item_keys(str(collection_key_value), c, u)
            scope = "collection"
        elif group_key:
            keys = _group_item_keys(str(group_key), c, u)
            scope = "group"
        else:
            raise ValueError("Thiếu câu hoặc bộ học")
        affected = 0
        skipped_new = 0
        skipped_suspended = 0
        ts = now_ts()
        if action == "review_now":
            for i in range(0, len(keys), 700):
                ch = keys[i:i+700]
                if not ch:
                    continue
                ph = ",".join("?" for _ in ch)
                existing = {r[0] for r in u.execute(f"SELECT item_key FROM fsrs_cards WHERE item_key IN ({ph})", ch)}
                suspended = {r[0] for r in u.execute(f"SELECT item_key FROM suspended_items WHERE item_key IN ({ph})", ch)}
                targets = [k for k in ch if k in existing and k not in suspended]
                skipped_new += sum(1 for k in ch if k not in existing)
                skipped_suspended += sum(1 for k in ch if k in suspended)
                if targets:
                    tph = ",".join("?" for _ in targets)
                    cur = u.execute(f"UPDATE fsrs_cards SET due_ts=? WHERE item_key IN ({tph})", [ts - 0.001, *targets])
                    affected += int(cur.rowcount if cur.rowcount >= 0 else len(targets))
        elif action == "reset":
            for i in range(0, len(keys), 700):
                ch = keys[i:i+700]
                if not ch:
                    continue
                ph = ",".join("?" for _ in ch)
                cur = u.execute(f"DELETE FROM fsrs_cards WHERE item_key IN ({ph})", ch)
                affected += int(cur.rowcount if cur.rowcount >= 0 else 0)
            # Review history and suspension state are intentionally preserved.
        elif action == "suspend":
            for k in keys:
                cur = u.execute("INSERT OR IGNORE INTO suspended_items(item_key,suspended_at_ts) VALUES(?,?)", (k, ts))
                affected += int(cur.rowcount if cur.rowcount >= 0 else 0)
        elif action == "resume":
            for i in range(0, len(keys), 700):
                ch = keys[i:i+700]
                if not ch:
                    continue
                ph = ",".join("?" for _ in ch)
                cur = u.execute(f"DELETE FROM suspended_items WHERE item_key IN ({ph})", ch)
                affected += int(cur.rowcount if cur.rowcount >= 0 else 0)
        u.commit()
        return {
            "ok":True,
            "action":action,
            "scope":scope,
            "total":len(keys),
            "affected":affected,
            "skipped_new":skipped_new,
            "skipped_suspended":skipped_suspended,
            "history_preserved":action=="reset",
        }


def daily_session(extra=None):
    with content_conn() as c, user_conn() as u:
        now = now_ts()
        due_keys = [r[0] for r in u.execute(
            "SELECT f.item_key FROM fsrs_cards f LEFT JOIN suspended_items s ON s.item_key=f.item_key WHERE f.due_ts<=? AND s.item_key IS NULL ORDER BY f.due_ts,f.state,f.item_key",
            (now,),
        )]
        active_key, source = active_source_or_fallback(c, u)
        new_limit = int(setting_get(u, "new_per_day", "20"))
        b0,b1 = local_day_bounds_ts()
        introduced_today = u.execute("SELECT COUNT(*) FROM fsrs_cards WHERE introduced_at_ts>=? AND introduced_at_ts<?",(b0,b1)).fetchone()[0]
        if extra is None:
            slots = 10**9 if new_limit < 0 else max(0, new_limit - introduced_today)
        else:
            if str(extra).lower() == "all": slots = 10**9
            else: slots = max(0, min(int(extra), 5000))
        # Get unscheduled items from active source in collection order.
        kind, ident = parse_collection_key(active_key)
        source_data = get_collection_any(kind, ident)
        source_items_list = source_data["items"] if source_data else []
        # One SRS card per sentence: dedupe repeated memberships before applying the daily limit.
        new_items = []
        new_seen = set()
        for x in source_items_list:
            k = x.get("item_key")
            if not k or k in new_seen or x.get("srs") or x.get("suspended"):
                continue
            new_seen.add(k)
            new_items.append(x)
            if len(new_items) >= slots:
                break

        due_items=[]
        for k in due_keys:
            x=resolve_item(k,c,u)
            if x: due_items.append(x)
        attach_user_state(due_items,u)
        # Avoid duplicates if an item somehow appears both lists.
        seen=set(); combined=[]
        for x in due_items + new_items:
            if x["item_key"] in seen: continue
            seen.add(x["item_key"]); combined.append(x)
        return {
            "items": combined,
            "dueCount": len(due_items),
            "newCount": len(new_items),
            "activeSource": source,
            "extra": extra,
        }


def bookmark_item(item_key, saved=None):
    with content_conn() as c, user_conn() as u:
        if not resolve_item(item_key,c,u): raise ValueError("Không tìm thấy câu")
        exists = u.execute("SELECT 1 FROM saved_items WHERE item_key=?",(item_key,)).fetchone() is not None
        target = (not exists) if saved is None else bool(saved)
        if target:
            u.execute("INSERT OR REPLACE INTO saved_items(item_key,saved_at_ts) VALUES(?,?)",(item_key,now_ts()))
        else:
            u.execute("DELETE FROM saved_items WHERE item_key=?",(item_key,))
        u.commit(); return {"ok":True,"saved":target}


def save_position(collection_key_value, index):
    with user_conn() as u:
        u.execute(
            "INSERT INTO collection_progress(collection_key,last_index,updated_at_ts) VALUES(?,?,?) ON CONFLICT(collection_key) DO UPDATE SET last_index=excluded.last_index,updated_at_ts=excluded.updated_at_ts",
            (collection_key_value,max(0,int(index)),now_ts()),
        ); u.commit()
    return {"ok":True}


def set_active_source(key):
    kind, ident = parse_collection_key(key)
    if not get_collection_any(kind,ident): raise ValueError("Không tìm thấy Island")
    with user_conn() as u:
        setting_set(u,"active_collection_key",key); u.commit()
    return {"ok":True,"collectionKey":key}


def _reschedule_review_cards(u, desired_retention):
    """Recompute due dates for established Review cards only.

    Learning/Relearning short steps are intentionally left untouched because desired
    retention governs long-term FSRS intervals, not same-day learning steps.
    """
    rows = rows_to_dicts(u.execute(
        "SELECT item_key,stability,last_review_ts,due_ts FROM fsrs_cards WHERE state=? AND stability IS NOT NULL AND last_review_ts IS NOT NULL",
        (STATE_REVIEW,),
    ))
    changed = 0
    for r in rows:
        due = float(r["last_review_ts"]) + next_interval_days(float(r["stability"]), desired_retention) * 86400
        if abs(float(r["due_ts"]) - due) > 0.5:
            u.execute("UPDATE fsrs_cards SET due_ts=? WHERE item_key=?", (due, r["item_key"]))
            changed += 1
    return changed


def normalize_playback_speed(value, strict=True):
    try:
        speed = float(value)
    except (TypeError, ValueError):
        if strict:
            raise ValueError("Speed không hợp lệ")
        return "1.00"
    if not math.isfinite(speed):
        if strict:
            raise ValueError("Speed không hợp lệ")
        return "1.00"
    speed = round(min(max(speed, 0.25), 2.0) * 20) / 20
    return f"{speed:.2f}"


def update_setting(key,value):
    allowed={"new_per_day","desired_retention","reschedule_on_retention_change","recall_mode","list_speed","shadow_speed","shadow_repeat","shadow_pause","list_auto_delay"}
    if key not in allowed: raise ValueError("Setting không được hỗ trợ")
    if key=="new_per_day":
        n=int(value); value=str(-1 if n<0 else min(n,500))
    elif key=="desired_retention":
        x=float(value)
        if not math.isfinite(x): raise ValueError("Desired retention không hợp lệ")
        value=str(min(max(x,0.70),0.99))
    elif key=="reschedule_on_retention_change":
        value="1" if str(value).strip().lower() in ("1","true","yes","on") else "0"
    elif key in ("list_speed", "shadow_speed"):
        value=normalize_playback_speed(value)
    elif key=="shadow_repeat": value=str(min(max(int(value),1),10))
    elif key=="shadow_pause":
        x=float(value)
        if not math.isfinite(x): raise ValueError("Khoảng nghỉ không hợp lệ")
        value=str(min(max(x,0),10))
    elif key=="list_auto_delay":
        x=float(value)
        if not math.isfinite(x): raise ValueError("Auto next không hợp lệ")
        value=str(min(max(x,0),10))
    elif key=="recall_mode":
        if value not in ("vi_en","audio_en","en_meaning"): value="vi_en"
    rescheduled=0
    with user_conn() as u:
        old=setting_get(u,key)
        setting_set(u,key,value)
        if key=="desired_retention" and old is not None and abs(float(old)-float(value))>1e-12 and setting_get(u,"reschedule_on_retention_change","0")=="1":
            rescheduled=_reschedule_review_cards(u,float(value))
        u.commit()
    return {"ok":True,"key":key,"value":value,"rescheduled":rescheduled}


def create_my_island(name,description=""):
    name=(name or "").strip()
    if not name: raise ValueError("Tên Island không được trống")
    with user_conn() as u:
        ts=now_ts(); cur=u.execute("INSERT INTO my_islands(name,description,created_at_ts,updated_at_ts) VALUES(?,?,?,?)",(name[:120],(description or "")[:500],ts,ts)); u.commit()
        return {"ok":True,"id":cur.lastrowid}


def update_my_island(island_id,name,description=""):
    name=(name or "").strip()
    if not name: raise ValueError("Tên Island không được trống")
    with user_conn() as u:
        u.execute("UPDATE my_islands SET name=?,description=?,updated_at_ts=? WHERE id=?",(name[:120],(description or "")[:500],now_ts(),int(island_id))); u.commit()
    return {"ok":True}


def delete_my_island(island_id):
    with user_conn() as u:
        u.execute("DELETE FROM my_islands WHERE id=?",(int(island_id),));
        if setting_get(u,"active_collection_key","")==collection_key("my",island_id): setting_set(u,"active_collection_key","core:219")
        u.commit()
    return {"ok":True}


def add_to_my_island(island_id,item_key):
    parse_item_key(item_key)
    with content_conn() as c,user_conn() as u:
        if not resolve_item(item_key,c,u): raise ValueError("Không tìm thấy câu")
        if not u.execute("SELECT 1 FROM my_islands WHERE id=?",(int(island_id),)).fetchone(): raise ValueError("Không tìm thấy Island")
        nxt=u.execute("SELECT COALESCE(MAX(order_index),0)+1 FROM my_island_members WHERE island_id=?",(int(island_id),)).fetchone()[0]
        try: u.execute("INSERT INTO my_island_members(island_id,order_index,item_key) VALUES(?,?,?)",(int(island_id),nxt,item_key))
        except sqlite3.IntegrityError: return {"ok":True,"already":True}
        u.execute("UPDATE my_islands SET updated_at_ts=? WHERE id=?",(now_ts(),int(island_id))); u.commit()
    return {"ok":True}


def remove_from_my_island(island_id,item_key):
    with user_conn() as u:
        u.execute("DELETE FROM my_island_members WHERE island_id=? AND item_key=?",(int(island_id),item_key))
        rows=[r[0] for r in u.execute("SELECT id FROM my_island_members WHERE island_id=? ORDER BY order_index,id",(int(island_id),))]
        for i,mid in enumerate(rows,1): u.execute("UPDATE my_island_members SET order_index=? WHERE id=?",(i,mid))
        u.execute("UPDATE my_islands SET updated_at_ts=? WHERE id=?",(now_ts(),int(island_id))); u.commit()
    return {"ok":True}


def reorder_my_island(island_id,item_keys):
    if not isinstance(item_keys,list): raise ValueError("Thứ tự không hợp lệ")
    with user_conn() as u:
        existing=[r[0] for r in u.execute("SELECT item_key FROM my_island_members WHERE island_id=? ORDER BY order_index,id",(int(island_id),))]
        if set(existing)!=set(item_keys) or len(existing)!=len(item_keys): raise ValueError("Danh sách câu không khớp")
        for i,k in enumerate(item_keys,1): u.execute("UPDATE my_island_members SET order_index=? WHERE island_id=? AND item_key=?",(i,int(island_id),k))
        u.execute("UPDATE my_islands SET updated_at_ts=? WHERE id=?",(now_ts(),int(island_id))); u.commit()
    return {"ok":True}


def safe_audio_ext(filename,mime=""):
    ext=Path(filename or "").suffix.lower()
    allowed={".mp3",".wav",".m4a",".aac",".ogg",".webm"}
    if ext in allowed: return ext
    by_mime={"audio/mpeg":".mp3","audio/mp3":".mp3","audio/wav":".wav","audio/x-wav":".wav","audio/mp4":".m4a","audio/aac":".aac","audio/ogg":".ogg","audio/webm":".webm"}
    return by_mime.get((mime or "").split(";")[0].lower(),".mp3")


def create_custom_sentence(en_us,vi_vn="",usage_note="",literal_note="",audio_data=None,audio_name="",audio_type="",island_id=None,audio_key=None,audio_expected=None,note=""):
    en=(en_us or "").strip(); vi=(vi_vn or "").strip()
    if not en: raise ValueError("English không được trống")
    raw=None
    if audio_data:
        try:
            if "," in audio_data and audio_data.startswith("data:"): audio_data=audio_data.split(",",1)[1]
            raw=base64.b64decode(audio_data,validate=True)
        except Exception: raise ValueError("File audio không hợp lệ")
        if len(raw)>25*1024*1024: raise ValueError("Audio tối đa 25 MB")
    audio_file=None
    audio_path=None
    try:
        with user_conn() as u:
            ts=now_ts(); cur=u.execute("INSERT INTO custom_sentences(en_us,vi_vn,usage_note,literal_note,audio_file,audio_key,audio_expected,note,created_at_ts,updated_at_ts) VALUES(?,?,?,?,NULL,?,?,?,?,?)",(en[:1000],vi[:1500],(usage_note or "")[:1000],(literal_note or "")[:1000],(audio_key or None),(audio_expected or None),(note or "")[:1000],ts,ts)); cid=cur.lastrowid
            if raw is not None:
                ext=safe_audio_ext(audio_name,audio_type); audio_file=f"custom_{cid:06d}{ext}"; audio_path=USER_AUDIO/audio_file; audio_path.write_bytes(raw)
                u.execute("UPDATE custom_sentences SET audio_file=? WHERE id=?",(audio_file,cid))
            if island_id:
                nxt=u.execute("SELECT COALESCE(MAX(order_index),0)+1 FROM my_island_members WHERE island_id=?",(int(island_id),)).fetchone()[0]
                u.execute("INSERT INTO my_island_members(island_id,order_index,item_key) VALUES(?,?,?)",(int(island_id),nxt,item_key_custom(cid)))
                u.execute("UPDATE my_islands SET updated_at_ts=? WHERE id=?",(ts,int(island_id)))
            u.commit()
    except Exception:
        if audio_path is not None:
            audio_path.unlink(missing_ok=True)
        raise
    return {"ok":True,"custom_id":cid,"item_key":item_key_custom(cid),"audio_file":audio_file}


def decode_base64_blob(data, max_bytes, label="File"):
    if not data:
        raise ValueError(f"Thiếu {label}")
    try:
        if isinstance(data, str) and data.startswith("data:") and "," in data:
            data = data.split(",", 1)[1]
        raw = base64.b64decode(data, validate=True)
    except Exception:
        raise ValueError(f"{label} không hợp lệ")
    if len(raw) > max_bytes:
        raise ValueError(f"{label} quá lớn")
    return raw


def _xlsx_col_index(ref):
    m = re.match(r"([A-Z]+)", ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _validated_xlsx_archive(raw):
    if len(raw) > MAX_XLSX_ARCHIVE_BYTES:
        raise ValueError("XLSX quá lớn")
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        infos = z.infolist()
        if len(infos) > MAX_XLSX_ZIP_MEMBERS:
            raise ValueError("XLSX có quá nhiều thành phần")
        seen = set()
        total_xml = 0
        for info in infos:
            pure = PurePosixPath(info.filename.replace('\\', '/'))
            if pure.is_absolute() or ".." in pure.parts:
                raise ValueError("XLSX chứa đường dẫn không an toàn")
            name = str(pure)
            if name in seen:
                raise ValueError("XLSX chứa thành phần trùng lặp")
            seen.add(name)
            if name.lower().endswith((".xml", ".rels")):
                if info.file_size < 0 or info.file_size > MAX_XLSX_XML_MEMBER_BYTES:
                    raise ValueError("Thành phần XML trong XLSX quá lớn")
                total_xml += info.file_size
                if total_xml > MAX_XLSX_TOTAL_XML_BYTES:
                    raise ValueError("Tổng XML trong XLSX quá lớn")
        return z
    except ValueError:
        try: z.close()
        except Exception: pass
        raise
    except Exception:
        raise ValueError("XLSX không hợp lệ")


def _read_xlsx_xml(z, name):
    try:
        info = z.getinfo(name)
    except KeyError:
        raise ValueError("XLSX thiếu thành phần bắt buộc")
    with z.open(info) as source:
        raw = source.read(MAX_XLSX_XML_MEMBER_BYTES + 1)
    if len(raw) > MAX_XLSX_XML_MEMBER_BYTES:
        raise ValueError("Thành phần XML trong XLSX quá lớn")
    return raw


def _parse_xlsx_xml(z, name):
    try:
        return ET.fromstring(_read_xlsx_xml(z, name))
    except ValueError:
        raise
    except (ET.ParseError, RuntimeError, zipfile.BadZipFile, OSError) as exc:
        raise ValueError("XLSX không hợp lệ") from exc


def parse_import_xlsx(raw):
    z = _validated_xlsx_archive(raw)
    ns = {"m":"http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r":"http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = _parse_xlsx_xml(z, "xl/sharedStrings.xml")
        for si in root.findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
    wb = _parse_xlsx_xml(z, "xl/workbook.xml")
    relroot = _parse_xlsx_xml(z, "xl/_rels/workbook.xml.rels")
    rels = {r.attrib.get("Id"): r.attrib.get("Target") for r in relroot}
    sheets = wb.find("m:sheets", ns)
    target = None
    for sh in sheets:
        if sh.attrib.get("name", "").strip().casefold() == "import":
            target = rels.get(sh.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")); break
    if not target and len(sheets):
        sh = sheets[0]; target = rels.get(sh.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"))
    if not target:
        raise ValueError("Không tìm thấy sheet Import")
    if target.startswith("/"):
        sheet_path = target.lstrip("/")
    elif target.startswith("xl/"):
        sheet_path = target
    else:
        sheet_path = "xl/" + target.lstrip("/")
    root = _parse_xlsx_xml(z, sheet_path)
    z.close()
    matrix = []
    for row in root.findall(".//m:sheetData/m:row", ns):
        vals = {}
        for cell in row.findall("m:c", ns):
            idx = _xlsx_col_index(cell.attrib.get("r", "A1")); typ = cell.attrib.get("t")
            v = cell.find("m:v", ns)
            if typ == "inlineStr":
                node = cell.find("m:is", ns); txt = "" if node is None else "".join(t.text or "" for t in node.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
            elif v is None:
                txt = ""
            elif typ == "s":
                try: txt = shared[int(v.text)]
                except Exception: txt = ""
            else:
                txt = v.text or ""
            vals[idx] = txt
        if vals:
            maxidx = max(vals)
            matrix.append([vals.get(i, "") for i in range(maxidx + 1)])
    if not matrix:
        return []
    header_idx = next((i for i,r in enumerate(matrix) if any(str(x).strip() for x in r)), None)
    if header_idx is None: return []
    headers = [re.sub(r"\s+", " ", str(x).strip()).casefold() for x in matrix[header_idx]]
    aliases = {
        "audio key":"audio_key","english":"en_us","vietnamese":"vi_vn",
        "audio file (optional)":"audio_expected","audio file":"audio_expected",
        "note (optional)":"note","note":"note",
    }
    mapping = {i:aliases.get(h) for i,h in enumerate(headers) if aliases.get(h)}
    if "en_us" not in mapping.values():
        raise ValueError("File XLSX phải có cột English")
    rows = []
    for r in matrix[header_idx+1:]:
        obj = {"audio_key":"","en_us":"","vi_vn":"","audio_expected":"","note":""}
        for i,key in mapping.items():
            if i < len(r): obj[key] = str(r[i] or "").strip()
        if not any(obj.values()): continue
        if not obj["en_us"]: continue
        rows.append(obj)
    return rows


def _normalize_audio_key(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (s or "").strip()).strip("-._")
    return s[:80]


def import_xlsx_to_island(island_id, xlsx_data):
    raw = decode_base64_blob(xlsx_data, MAX_XLSX_ARCHIVE_BYTES, "XLSX")
    rows = parse_import_xlsx(raw)
    if not rows: raise ValueError("XLSX không có câu để import")
    if len(rows) > 10000: raise ValueError("Mỗi lần import tối đa 10.000 câu")
    with user_conn() as u:
        if not u.execute("SELECT 1 FROM my_islands WHERE id=?", (int(island_id),)).fetchone(): raise ValueError("Không tìm thấy Island")
        used = {str(r[0]).casefold() for r in u.execute("SELECT cs.audio_key FROM my_island_members m JOIN custom_sentences cs ON m.item_key='c:'||cs.id WHERE m.island_id=? AND cs.audio_key IS NOT NULL AND cs.audio_key<>''", (int(island_id),))}
        nxt = u.execute("SELECT COALESCE(MAX(order_index),0)+1 FROM my_island_members WHERE island_id=?", (int(island_id),)).fetchone()[0]
        added = 0; generated = 0; warnings = []; ts=now_ts()
        for pos,row in enumerate(rows,1):
            key = _normalize_audio_key(row.get("audio_key"))
            if not key:
                n = nxt + added
                key = f"MI{int(island_id):03d}-{n:04d}"; generated += 1
            base = key; k=2
            while key.casefold() in used:
                key = f"{base}-{k}"; k += 1
            if key != base and row.get("audio_key"):
                warnings.append(f"Audio Key {base} bị trùng → đổi thành {key}")
            used.add(key.casefold())
            expected = Path(row.get("audio_expected") or "").name[:180] or None
            # If a duplicate Audio Key was renamed (T001 -> T001-2) and the expected
            # filename was simply T001.mp3, keep the filename aligned with the new key.
            if key != base and expected:
                ep = Path(expected)
                if ep.stem.casefold() == base.casefold() and ep.suffix.lower() in {".mp3",".wav",".m4a",".aac",".ogg",".webm"}:
                    expected = key + ep.suffix.lower()
            cur = u.execute("INSERT INTO custom_sentences(en_us,vi_vn,usage_note,literal_note,audio_file,audio_key,audio_expected,note,created_at_ts,updated_at_ts) VALUES(?,?,?,?,NULL,?,?,?,?,?)",
                            (row["en_us"][:1000], row.get("vi_vn","")[:1500], "", "", key, expected, row.get("note","")[:1000], ts, ts))
            cid=cur.lastrowid
            u.execute("INSERT INTO my_island_members(island_id,order_index,item_key) VALUES(?,?,?)", (int(island_id), nxt+added, item_key_custom(cid)))
            added += 1
        u.execute("UPDATE my_islands SET updated_at_ts=? WHERE id=?", (ts,int(island_id)))
        u.commit()
    return {"ok":True,"added":added,"generatedAudioKeys":generated,"missingAudio":added,"warnings":warnings[:20]}


def missing_audio_my_island(island_id):
    with user_conn() as u:
        return rows_to_dicts(u.execute("SELECT cs.audio_key,cs.en_us,cs.vi_vn,cs.audio_expected FROM my_island_members m JOIN custom_sentences cs ON m.item_key='c:'||cs.id WHERE m.island_id=? AND (cs.audio_file IS NULL OR cs.audio_file='') ORDER BY m.order_index",(int(island_id),)))


def _user_base_dir():
    """Derive the profile root from patchable test paths, not USER_BASE."""
    base = Path(USER_DIR).resolve().parent
    if Path(USER_DB).resolve().parent != Path(USER_DIR).resolve():
        raise RuntimeError("Đường dẫn database người dùng không hợp lệ")
    if Path(USER_AUDIO).resolve().parent != base:
        raise RuntimeError("Đường dẫn user_audio không hợp lệ")
    return base


def _bulk_audio_stage_root():
    return _user_base_dir() / "bulk_audio_staging"


def cleanup_orphan_bulk_audio_staging():
    """Discard non-resumable Bulk Audio stages left by an earlier process."""
    return {"ok": True, "removed": BULK_AUDIO_SESSIONS.cleanup_orphans(_bulk_audio_stage_root())}


def _bulk_inventory(record):
    by_name, duplicates = {}, []
    for index, entry in enumerate(record["files"]):
        key = Path(entry["name"]).name.casefold()
        if key in by_name:
            duplicates.append(entry["name"])
        else:
            by_name[key] = (index, entry)
    return by_name, duplicates


def _preview_my_island_audio(record):
    island_id = int(record["target"])
    by_name, duplicates = _bulk_inventory(record)
    with user_conn() as u:
        if not u.execute("SELECT 1 FROM my_islands WHERE id=?", (island_id,)).fetchone():
            raise ValueError("Không tìm thấy Island")
        rows = rows_to_dicts(u.execute(
            "SELECT cs.id,cs.audio_file,cs.audio_key,cs.audio_expected FROM my_island_members m "
            "JOIN custom_sentences cs ON m.item_key='c:'||cs.id WHERE m.island_id=? ORDER BY m.order_index", (island_id,)
        ))
    used, matches, conflicts = set(), [], []
    for row in rows:
        candidates = []
        if row.get("audio_expected"):
            candidates.append(Path(row["audio_expected"]).name.casefold())
        if row.get("audio_key"):
            key = str(row["audio_key"]).casefold()
            candidates.extend(key + ext for ext in sorted(ALLOWED_AUDIO_EXTENSIONS))
        hit = next((candidate for candidate in candidates if candidate in by_name and candidate not in used), None)
        if not hit:
            if any(candidate in by_name for candidate in candidates):
                conflicts.append(str(row.get("audio_key") or row.get("audio_expected") or row["id"]))
            continue
        index, entry = by_name[hit]
        used.add(hit)
        matches.append({"row_id": int(row["id"]), "entry_index": index, "old_audio": row.get("audio_file")})
    record["matches"] = matches
    unmatched = [entry["name"] for entry in record["files"] if Path(entry["name"]).name.casefold() not in used]
    matched_ids = {match["row_id"] for match in matches}
    missing = sum(1 for row in rows if not row.get("audio_file") and int(row["id"]) not in matched_ids)
    return {"ok": True, "token": record["token"], "matched": len(matches), "missing": missing,
            "unmatched": unmatched[:100], "unmatchedCount": len(unmatched),
            "duplicates": duplicates[:100], "duplicateCount": len(duplicates),
            "conflicts": conflicts[:100], "conflictCount": len(conflicts)}


def _course_audio_rows(course_key):
    with content_conn() as c:
        course = next((item for item in get_courses(c) if item["key"] == course_key and item.get("audio_import")), None)
        if not course:
            raise ValueError("Course này không hỗ trợ import audio")
        ids = [int(item["id"]) for item in course["collections"]]
        placeholders = ",".join("?" for _ in ids)
        rows = rows_to_dicts(c.execute(
            f"SELECT DISTINCT ca.content_id,ca.audio_path,ca.audio_key FROM content_audio ca "
            f"JOIN content_membership m ON m.content_id=ca.content_id WHERE m.island_id IN ({placeholders}) ORDER BY m.island_id,m.order_index", ids
        ))
    return rows


def _preview_course_audio(record):
    rows = _course_audio_rows(record["target"])
    by_name, duplicates = _bulk_inventory(record)
    used, matches = set(), []
    for row in rows:
        extension = Path(str(row.get("audio_path") or "")).suffix.lower() or ".mp3"
        candidates = [Path(str(row.get("audio_path") or "")).name.casefold()]
        if row.get("audio_key"):
            candidates.insert(0, (str(row["audio_key"]) + extension).casefold())
        hit = next((candidate for candidate in candidates if candidate in by_name and candidate not in used), None)
        if not hit:
            continue
        index, _entry = by_name[hit]
        used.add(hit)
        matches.append({"content_id": int(row["content_id"]), "entry_index": index, "audio_path": str(row["audio_path"])})
    record["matches"] = matches
    available = BUNDLED_AUDIO_INDEX.count_course([row["audio_path"] for row in rows], AUDIO, COURSE_AUDIO)
    unmatched = [entry["name"] for entry in record["files"] if Path(entry["name"]).name.casefold() not in used]
    return {"ok": True, "token": record["token"], "matched": len(matches),
            "available": available, "missing": max(0, len(rows) - available - len(matches)),
            "unmatched": unmatched[:100], "unmatchedCount": len(unmatched),
            "duplicates": duplicates[:100], "duplicateCount": len(duplicates), "conflictCount": 0}


def preview_bulk_audio_session(token):
    record = BULK_AUDIO_SESSIONS.get(token)
    if not record["files"]:
        raise ValueError("Không có file audio để kiểm tra")
    return _preview_my_island_audio(record) if record["scope"] == "my" else _preview_course_audio(record)


def prepare_bulk_audio_path(scope, target, selected_path, mode):
    if scope not in {"my", "course"} or mode not in {"zip", "folder"}:
        raise ValueError("Loại Bulk Audio không hợp lệ")
    if scope == "course" and mode != "zip":
        raise ValueError("Course chỉ hỗ trợ ZIP audio")
    record = BULK_AUDIO_SESSIONS.from_zip(_bulk_audio_stage_root(), scope, target, selected_path) if mode == "zip" else BULK_AUDIO_SESSIONS.from_folder(_bulk_audio_stage_root(), scope, target, selected_path)
    try:
        return preview_bulk_audio_session(record["token"])
    except Exception:
        BULK_AUDIO_SESSIONS.cleanup(record["token"])
        raise


def start_bulk_audio_folder_session(scope, target):
    if scope not in {"my", "course"} or scope == "course":
        raise ValueError("Folder audio chỉ hỗ trợ My Islands")
    record = BULK_AUDIO_SESSIONS.create(_bulk_audio_stage_root(), scope, target)
    return {"ok": True, "token": record["token"]}


def _install_files_atomically(installations, stage):
    rollback = Path(stage) / "rollback"
    rollback.mkdir(exist_ok=True)
    backups, installed = [], []
    try:
        for index, (prepared, destination, obsolete) in enumerate(installations):
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            candidates = [destination] + ([Path(obsolete)] if obsolete and Path(obsolete) != destination else [])
            for original in candidates:
                if original.exists():
                    backup = rollback / f"{index:05d}-{len(backups):03d}{original.suffix}"
                    os.replace(original, backup)
                    backups.append((original, backup))
            os.replace(prepared, destination)
            installed.append(destination)
        return backups, installed
    except Exception:
        for path in reversed(installed):
            path.unlink(missing_ok=True)
        for original, backup in reversed(backups):
            original.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, original)
        raise


def _rollback_installed(backups, installed):
    for path in reversed(installed):
        path.unlink(missing_ok=True)
    for original, backup in reversed(backups):
        original.parent.mkdir(parents=True, exist_ok=True)
        os.replace(backup, original)


def confirm_bulk_audio_session(token):
    record = BULK_AUDIO_SESSIONS.get(token)
    if not record.get("matches"):
        raise ValueError("Không có audio khớp để import")
    output = record["stage"] / "prepared"
    output.mkdir(exist_ok=True)
    installations = []
    if record["scope"] == "my":
        island_id = int(record["target"])
        USER_AUDIO.mkdir(parents=True, exist_ok=True)
        with user_conn() as u:
            valid_ids = {int(row[0]) for row in u.execute(
                "SELECT cs.id FROM my_island_members m JOIN custom_sentences cs ON m.item_key='c:'||cs.id WHERE m.island_id=?", (island_id,)
            )}
            for index, match in enumerate(record["matches"]):
                if match["row_id"] not in valid_ids:
                    raise ValueError("My Island đã thay đổi; hãy chọn lại audio")
                entry = record["files"][match["entry_index"]]
                extension = Path(entry["name"]).suffix.lower()
                filename = f"custom_{match['row_id']:06d}{extension}"
                prepared = output / f"{index:05d}{extension}"
                shutil.copyfile(entry["path"], prepared)
                old = USER_AUDIO / match["old_audio"] if match.get("old_audio") else None
                installations.append((prepared, USER_AUDIO / filename, old))
                match["filename"] = filename
            backups, installed = _install_files_atomically(installations, record["stage"])
            try:
                timestamp = now_ts()
                for match in record["matches"]:
                    u.execute("UPDATE custom_sentences SET audio_file=?,updated_at_ts=? WHERE id=?", (match["filename"], timestamp, match["row_id"]))
                u.execute("UPDATE my_islands SET updated_at_ts=? WHERE id=?", (timestamp, island_id))
                u.commit()
            except Exception:
                u.rollback()
                _rollback_installed(backups, installed)
                raise
        result = _preview_my_island_audio(record)
        result.update({"ok": True, "imported": len(installations), "matched": len(installations)})
    else:
        root = COURSE_AUDIO.resolve()
        for index, match in enumerate(record["matches"]):
            entry = record["files"][match["entry_index"]]
            destination = (COURSE_AUDIO / str(match["audio_path"]).replace("\\", "/").lstrip("/")).resolve()
            if root not in destination.parents:
                raise ValueError("Đường dẫn course audio không an toàn")
            prepared = output / f"{index:05d}{destination.suffix.lower()}"
            shutil.copyfile(entry["path"], prepared)
            installations.append((prepared, destination, None))
        backups, installed = _install_files_atomically(installations, record["stage"])
        try:
            BUNDLED_AUDIO_INDEX.invalidate()
            rows = _course_audio_rows(record["target"])
            available = BUNDLED_AUDIO_INDEX.count_course([row["audio_path"] for row in rows], AUDIO, COURSE_AUDIO)
        except Exception:
            _rollback_installed(backups, installed)
            BUNDLED_AUDIO_INDEX.invalidate()
            raise
        result = {"ok": True, "imported": len(installations), "matched": len(installations), "available": available, "missing": max(0, len(rows)-available)}
    BULK_AUDIO_SESSIONS.cleanup(token)
    return result


def cancel_bulk_audio_session(token):
    BULK_AUDIO_SESSIONS.cleanup(token)
    return {"ok": True}


def _db_sidecars(path=None):
    db = Path(path or USER_DB)
    return [Path(str(db) + suffix) for suffix in ("-wal", "-shm", "-journal")]


def _remove_db_sidecars(path=None):
    for sidecar in _db_sidecars(path):
        sidecar.unlink(missing_ok=True)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_user_audio_name(name):
    name = str(name or "")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    return bool(
        name
        and name not in (".", "..")
        and "/" not in name
        and "\\" not in name
        and not any(ch in name for ch in '<>:"|?*')
        and all(ord(ch) >= 32 for ch in name)
        and not name.endswith((" ", "."))
        and Path(name).stem.upper() not in reserved
        and Path(name).name == name
        and Path(name).suffix.lower() in SUPPORTED_USER_AUDIO_EXTENSIONS
    )


def _list_user_audio_files():
    if not USER_AUDIO.exists():
        return []
    files = []
    for path in sorted(USER_AUDIO.rglob("*"), key=lambda p: p.name.casefold()):
        if path.is_dir():
            continue
        rel = path.relative_to(USER_AUDIO)
        if len(rel.parts) != 1 or not _safe_user_audio_name(rel.name):
            raise ValueError(f"Tên file trong user_audio không hợp lệ: {rel.as_posix()}")
        size = path.stat().st_size
        if size > MAX_USER_AUDIO_FILE_BYTES:
            raise ValueError(f"File user_audio vượt giới hạn 25 MB: {rel.name}")
        files.append(path)
    return files


def _validate_user_database(path, expected_schema=USER_DB_SCHEMA_VERSION, audio_names=None):
    path = Path(path)
    try:
        con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=20)
    except sqlite3.Error as exc:
        raise ValueError(f"Không thể mở database trong bản sao lưu: {exc}") from exc
    try:
        con.row_factory = sqlite3.Row
        integrity = con.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise ValueError("Database trong bản sao lưu không vượt qua integrity_check")
        actual_schema = int(con.execute("PRAGMA user_version").fetchone()[0])
        if actual_schema != int(expected_schema):
            raise ValueError(
                f"Schema database ({actual_schema}) không khớp manifest ({expected_schema})"
            )
        if actual_schema > USER_DB_SCHEMA_VERSION:
            raise ValueError(
                f"Schema database {actual_schema} mới hơn phiên bản ứng dụng hỗ trợ "
                f"({USER_DB_SCHEMA_VERSION})"
            )
        for table, required_columns in REQUIRED_USER_SCHEMA.items():
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                raise ValueError(f"Database thiếu bảng bắt buộc: {table}")
            columns = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
            missing = required_columns - columns
            if missing:
                raise ValueError(f"Bảng {table} thiếu cột: {', '.join(sorted(missing))}")
        restored_audio = set(audio_names) if audio_names is not None else None
        for row in con.execute(
            "SELECT audio_file FROM custom_sentences WHERE audio_file IS NOT NULL AND audio_file<>''"
        ):
            name = str(row[0])
            if not _safe_user_audio_name(name):
                raise ValueError(f"Database chứa tên user_audio không hợp lệ: {name}")
            if restored_audio is not None and name not in restored_audio:
                raise ValueError(f"Bản sao lưu thiếu user_audio được database tham chiếu: {name}")
        return {"schemaVersion": actual_schema, "integrity": "ok"}
    except sqlite3.Error as exc:
        raise ValueError(f"Database trong bản sao lưu bị lỗi: {exc}") from exc
    finally:
        con.close()


def _backup_filename():
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return f"EnglishLearningApp-user-data-{stamp}.zip"


def create_user_backup(output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with USER_DATA_LOCK:
        base = _user_base_dir()
        base.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=".backup_stage_", dir=base))
        partial = None
        try:
            snapshot = work / "learning.sqlite"
            with user_conn() as source:
                destination = sqlite3.connect(snapshot)
                try:
                    source.backup(destination)
                    destination.commit()
                finally:
                    destination.close()
            audio_files = _list_user_audio_files()
            audio_names = {p.name for p in audio_files}
            _validate_user_database(snapshot, USER_DB_SCHEMA_VERSION, audio_names)
            if len(audio_files) + 1 > MAX_BACKUP_FILE_COUNT:
                raise ValueError("User data có quá nhiều file để sao lưu")
            total_size = snapshot.stat().st_size + sum(p.stat().st_size for p in audio_files)
            if total_size > MAX_BACKUP_UNCOMPRESSED_BYTES:
                raise ValueError("User data vượt giới hạn an toàn 64 GB")
            files = {
                "user_data/learning.sqlite": {
                    "size": snapshot.stat().st_size,
                    "sha256": _sha256_file(snapshot),
                }
            }
            for audio_path in audio_files:
                archive_name = f"user_audio/{audio_path.name}"
                files[archive_name] = {
                    "size": audio_path.stat().st_size,
                    "sha256": _sha256_file(audio_path),
                }
            manifest = {
                "backupType": BACKUP_TYPE,
                "backupFormatVersion": BACKUP_FORMAT_VERSION,
                "appVersion": APP_VERSION,
                "userDbSchemaVersion": USER_DB_SCHEMA_VERSION,
                "createdAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "files": files,
            }
            fd, partial_name = tempfile.mkstemp(
                prefix=f".{output_path.name}.", suffix=".partial", dir=output_path.parent
            )
            os.close(fd)
            partial = Path(partial_name)
            with zipfile.ZipFile(partial, "w", allowZip64=True) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                    compress_type=zipfile.ZIP_DEFLATED,
                )
                archive.write(snapshot, "user_data/learning.sqlite", compress_type=zipfile.ZIP_DEFLATED)
                for audio_path in audio_files:
                    archive.write(
                        audio_path,
                        f"user_audio/{audio_path.name}",
                        compress_type=zipfile.ZIP_STORED,
                    )
            if partial.stat().st_size > MAX_BACKUP_ARCHIVE_BYTES:
                raise ValueError("File backup vượt giới hạn an toàn 16 GB")
            os.replace(partial, output_path)
            partial = None
            return {
                "ok": True,
                "path": str(output_path),
                "filename": output_path.name,
                "fileCount": len(files),
                "size": output_path.stat().st_size,
                "manifest": manifest,
            }
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)


def _validated_zip_member(info):
    name = str(info.filename or "")
    if not name or "\\" in name or name.startswith("/"):
        raise ValueError(f"Đường dẫn trong ZIP không hợp lệ: {name or '(trống)'}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise ValueError(f"Phát hiện ZIP path traversal: {name}")
    if len(pure.parts) == 1:
        if name != "manifest.json":
            raise ValueError(f"File không được hỗ trợ trong ZIP: {name}")
    elif tuple(pure.parts) == ("user_data", "learning.sqlite"):
        pass
    elif len(pure.parts) == 2 and pure.parts[0] == "user_audio" and _safe_user_audio_name(pure.parts[1]):
        pass
    else:
        raise ValueError(f"Đường dẫn không được hỗ trợ trong ZIP: {name}")
    unix_mode = (info.external_attr >> 16) & 0o170000
    if unix_mode == 0o120000:
        raise ValueError(f"ZIP không được chứa symbolic link: {name}")
    if info.flag_bits & 0x1:
        raise ValueError("ZIP được mã hóa không được hỗ trợ")
    return name


def _profile_size_bytes():
    total = 0
    for path in [USER_DB, *_db_sidecars()]:
        if Path(path).exists() and Path(path).is_file():
            total += Path(path).stat().st_size
    if USER_AUDIO.exists():
        total += sum(path.stat().st_size for path in USER_AUDIO.rglob("*") if path.is_file())
    return total


def _ensure_restore_disk_capacity(base, incoming_uncompressed, incoming_archive_size):
    # During prepare we temporarily hold the uploaded ZIP, both extracted profiles,
    # a safety ZIP, and SQLite's snapshot workspace. Keep a fixed operating margin.
    current_size = _profile_size_bytes()
    required = (
        int(incoming_uncompressed)
        + current_size * 2
        + RESTORE_DISK_MARGIN_BYTES
    )
    free = shutil.disk_usage(Path(base)).free
    if free < required:
        raise ValueError(
            "Không đủ dung lượng trống để khôi phục an toàn. "
            f"Cần khoảng {required / (1024 ** 3):.1f} GB, hiện còn {free / (1024 ** 3):.1f} GB."
        )


def _extract_and_validate_backup(archive_path, stage, check_disk=True):
    archive_path = Path(archive_path)
    archive_size = archive_path.stat().st_size
    if archive_size <= 0:
        raise ValueError("File backup trống")
    if archive_size > MAX_BACKUP_ARCHIVE_BYTES:
        raise ValueError("File backup vượt giới hạn an toàn 16 GB")
    stage = Path(stage)
    stage.mkdir(parents=True, exist_ok=True)
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("File backup không phải ZIP hợp lệ") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_BACKUP_FILE_COUNT + 1:
            raise ValueError("File backup chứa quá nhiều file")
        names = set()
        casefolded_names = set()
        total_size = 0
        total_compressed = 0
        for info in infos:
            if info.is_dir():
                raise ValueError("ZIP backup không được chứa directory entry rời")
            name = _validated_zip_member(info)
            if name in names:
                raise ValueError(f"ZIP chứa file trùng tên: {name}")
            if name.casefold() in casefolded_names:
                raise ValueError(f"ZIP chứa tên file xung đột trên Windows: {name}")
            names.add(name)
            casefolded_names.add(name.casefold())
            total_size += int(info.file_size)
            total_compressed += int(info.compress_size)
            if total_size > MAX_BACKUP_UNCOMPRESSED_BYTES:
                raise ValueError("Dữ liệu giải nén vượt giới hạn an toàn 64 GB")
            if (
                info.file_size > 100 * 1024 * 1024
                and info.file_size > max(1, info.compress_size) * MAX_BACKUP_COMPRESSION_RATIO
            ):
                raise ValueError(f"Tỷ lệ nén bất thường, có thể là ZIP bomb: {name}")
            if name.startswith("user_audio/") and info.file_size > MAX_USER_AUDIO_FILE_BYTES:
                raise ValueError(f"File user_audio vượt giới hạn 25 MB: {name}")
        if total_size > max(1, total_compressed) * MAX_BACKUP_COMPRESSION_RATIO:
            raise ValueError("Tỷ lệ nén tổng thể bất thường, có thể là ZIP bomb")
        if check_disk:
            _ensure_restore_disk_capacity(_user_base_dir(), total_size, archive_size)
        required = {"manifest.json", "user_data/learning.sqlite"}
        if not required <= names:
            raise ValueError("Backup thiếu manifest.json hoặc learning.sqlite")
        if archive.getinfo("manifest.json").file_size > 1024 * 1024:
            raise ValueError("Manifest vượt giới hạn 1 MB")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except Exception as exc:
            raise ValueError("Manifest không phải JSON UTF-8 hợp lệ") from exc
        if manifest.get("backupType") != BACKUP_TYPE:
            raise ValueError("File ZIP không phải backup của English Learning App")
        if not isinstance(manifest.get("appVersion"), str) or not manifest.get("appVersion"):
            raise ValueError("Manifest thiếu appVersion")
        if not isinstance(manifest.get("createdAtUtc"), str) or not manifest.get("createdAtUtc"):
            raise ValueError("Manifest thiếu thời điểm tạo backup")
        if int(manifest.get("backupFormatVersion", -1)) != BACKUP_FORMAT_VERSION:
            raise ValueError(
                f"Backup format {manifest.get('backupFormatVersion')} không được hỗ trợ; "
                f"ứng dụng hỗ trợ format {BACKUP_FORMAT_VERSION}"
            )
        manifest_schema = int(manifest.get("userDbSchemaVersion", -1))
        if manifest_schema < 1 or manifest_schema > USER_DB_SCHEMA_VERSION:
            raise ValueError(
                f"User database schema {manifest_schema} không tương thích với schema "
                f"{USER_DB_SCHEMA_VERSION}"
            )
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise ValueError("Manifest thiếu danh sách checksum")
        expected_files = names - {"manifest.json"}
        if set(files) != expected_files:
            raise ValueError("Danh sách file trong manifest không khớp nội dung ZIP")
        for info in infos:
            if info.filename == "manifest.json":
                continue
            target = stage.joinpath(*PurePosixPath(info.filename).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
            meta = files.get(info.filename)
            if not isinstance(meta, dict):
                raise ValueError(f"Manifest không hợp lệ cho {info.filename}")
            if int(meta.get("size", -1)) != target.stat().st_size:
                raise ValueError(f"Sai kích thước file backup: {info.filename}")
            if str(meta.get("sha256", "")).lower() != _sha256_file(target):
                raise ValueError(f"Checksum không khớp: {info.filename}")
        audio_dir = stage / "user_audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        audio_names = {p.name for p in audio_dir.iterdir() if p.is_file()}
        _validate_user_database(
            stage / "user_data" / "learning.sqlite", manifest_schema, audio_names
        )
        return manifest


def _checkpoint_user_database():
    with user_conn() as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


def _install_user_profile(staged_db, staged_audio, work_root, after_install=None):
    staged_db = Path(staged_db)
    staged_audio = Path(staged_audio)
    work_root = Path(work_root)
    rollback_db = work_root / "rollback-learning.sqlite"
    rollback_audio = work_root / "rollback-user_audio"
    old_db_moved = False
    old_audio_moved = False
    installed = False
    _user_base_dir()
    USER_DIR.mkdir(parents=True, exist_ok=True)
    staged_audio.mkdir(parents=True, exist_ok=True)
    try:
        _checkpoint_user_database()
        _remove_db_sidecars()
        if USER_DB.exists():
            os.replace(USER_DB, rollback_db)
            old_db_moved = True
        if USER_AUDIO.exists():
            os.replace(USER_AUDIO, rollback_audio)
            old_audio_moved = True
        os.replace(staged_db, USER_DB)
        os.replace(staged_audio, USER_AUDIO)
        with user_conn() as con:
            if str(con.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
                raise RuntimeError("Database mới không vượt qua integrity_check sau khi cài đặt")
        if after_install is not None:
            after_install()
        installed = True
    except Exception as original_error:
        rollback_error = None
        try:
            _remove_db_sidecars()
            if USER_DB.exists():
                USER_DB.unlink()
            if USER_AUDIO.exists():
                shutil.rmtree(USER_AUDIO)
            if old_db_moved and rollback_db.exists():
                os.replace(rollback_db, USER_DB)
            if old_audio_moved and rollback_audio.exists():
                os.replace(rollback_audio, USER_AUDIO)
            else:
                USER_AUDIO.mkdir(parents=True, exist_ok=True)
            with user_conn() as con:
                if str(con.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
                    raise RuntimeError("Database cũ bị lỗi sau rollback")
        except Exception as exc:
            rollback_error = exc
        if rollback_error is not None:
            raise RuntimeError(
                f"Cài đặt dữ liệu mới thất bại ({original_error}); rollback cũng thất bại ({rollback_error})"
            ) from original_error
        raise
    finally:
        if installed:
            rollback_db.unlink(missing_ok=True)
            if rollback_audio.exists():
                shutil.rmtree(rollback_audio, ignore_errors=True)


def reset_learning_progress():
    tables = ("collection_progress", "fsrs_cards", "review_log", "suspended_items")
    with USER_DATA_LOCK, user_conn() as con:
        before = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        con.execute("BEGIN IMMEDIATE")
        try:
            for table in tables:
                con.execute(f"DELETE FROM {table}")
            con.commit()
        except Exception:
            con.rollback()
            raise
    return {"ok": True, "deleted": before}


def delete_all_user_data(after_install=None):
    with USER_DATA_LOCK:
        base = _user_base_dir()
        base.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=".delete_all_stage_", dir=base))
        try:
            staged_db = work / "fresh" / "learning.sqlite"
            staged_audio = work / "fresh_audio"
            staged_audio.mkdir(parents=True)
            _create_user_database(staged_db)
            _validate_user_database(staged_db, USER_DB_SCHEMA_VERSION, set())
            _install_user_profile(staged_db, staged_audio, work, after_install=after_install)
            return {"ok": True, "schemaVersion": USER_DB_SCHEMA_VERSION}
        finally:
            shutil.rmtree(work, ignore_errors=True)


def configure_restore_lifecycle(shutdown_callback):
    global RESTORE_SHUTDOWN_CALLBACK
    RESTORE_SHUTDOWN_CALLBACK = shutdown_callback


def _restore_root():
    root = _user_base_dir() / "restore_pending"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _restore_result_file():
    return _user_base_dir() / "restore_result.json"


def read_restore_result():
    path = _restore_result_file()
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def acknowledge_restore_result():
    _restore_result_file().unlink(missing_ok=True)
    return {"ok": True}


def _assert_no_pending_restore(except_dir=None):
    root = _restore_root()
    for marker in root.glob("*/pending.json"):
        if except_dir is None or marker.parent.resolve() != Path(except_dir).resolve():
            raise RuntimeError("Ứng dụng đang tự hoàn tất phiên khôi phục trước")


def pending_restore_markers():
    root = _restore_root()
    return sorted(root.glob("*/pending.json"), key=lambda path: path.stat().st_mtime)


def pending_restore_session_status():
    markers = pending_restore_markers()
    if not markers:
        return None
    return {"pendingFile": str(markers[0]), "count": len(markers)}


def complete_pending_restore_if_needed(lock_timeout=5.0):
    """Finish one validated restore synchronously before opening the user DB.

    Restore is deliberately completed on the next manual app launch. This
    avoids a background copy of English Learning App.exe waiting on itself or
    on SQLite/WAL handles after the window has closed.
    """
    markers = pending_restore_markers()
    if not markers:
        return None
    if len(markers) != 1:
        raise RuntimeError("Có nhiều phiên khôi phục chưa hoàn tất; cần kiểm tra restore_pending")
    from restore_helper import apply_pending_restore
    return apply_pending_restore(
        markers[0], wait_pid=0, process_timeout=0.01, lock_timeout=max(0.01, float(lock_timeout))
    )


def new_restore_staging():
    _assert_no_pending_restore()
    return _restore_root() / uuid.uuid4().hex


def prepare_user_restore(archive_path, pending_dir=None):
    """Validate ZIP + safety snapshot and persist a helper-readable pending restore."""
    with USER_DATA_LOCK:
        base = _user_base_dir()
        base.mkdir(parents=True, exist_ok=True)
        pending = Path(pending_dir or new_restore_staging()).resolve()
        if pending.parent != _restore_root().resolve():
            raise ValueError("Thư mục staging restore không hợp lệ")
        _assert_no_pending_restore(except_dir=pending)
        pending.mkdir(parents=True, exist_ok=False) if not pending.exists() else None
        try:
            incoming = pending / "incoming.zip"
            source = Path(archive_path).resolve()
            if source != incoming.resolve():
                shutil.copy2(source, incoming)

            incoming_profile = pending / "incoming_profile"
            manifest = _extract_and_validate_backup(incoming, incoming_profile)

            safety_archive = pending / "safety-snapshot.zip"
            create_user_backup(safety_archive)
            safety_profile = pending / "safety_profile"
            _extract_and_validate_backup(safety_archive, safety_profile, check_disk=False)

            config = {
                "createdAt": time.time(),
                "incomingDb": str((incoming_profile / "user_data" / "learning.sqlite").resolve()),
                "incomingAudio": str((incoming_profile / "user_audio").resolve()),
                "safetyDb": str((safety_profile / "user_data" / "learning.sqlite").resolve()),
                "safetyAudio": str((safety_profile / "user_audio").resolve()),
                "safetyArchive": str(safety_archive.resolve()),
                "targetDb": str(Path(USER_DB).resolve()),
                "targetAudio": str(Path(USER_AUDIO).resolve()),
                "resultFile": str(_restore_result_file().resolve()),
                "schemaVersion": USER_DB_SCHEMA_VERSION,
                "restoredAppVersion": manifest.get("appVersion"),
                "backupFormatVersion": manifest.get("backupFormatVersion"),
            }
            partial = pending / "pending.json.partial"
            partial.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(partial, pending / "pending.json")
            state_partial = pending / "restore_state.json.partial"
            state_partial.write_text(
                json.dumps(
                    {"status": "prepared", "createdAt": time.time(), "updatedAt": time.time()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(state_partial, pending / "restore_state.json")
            return {
                "ok": True,
                "accepted": True,
                "closing": True,
                "pendingFile": str((pending / "pending.json").resolve()),
                "restoredAppVersion": manifest.get("appVersion"),
                "backupFormatVersion": manifest.get("backupFormatVersion"),
                "schemaVersion": manifest.get("userDbSchemaVersion"),
                "safetySnapshotCreated": True,
            }
        except Exception:
            shutil.rmtree(pending, ignore_errors=True)
            raise


def _restore_helper_command(pending_file):
    if getattr(sys, "frozen", False):
        return [sys.executable, "--restore-helper", "--pending", str(pending_file), "--wait-pid", str(os.getpid())]
    return [sys.executable, str(ROOT / "restore_helper.py"), "--pending", str(pending_file), "--wait-pid", str(os.getpid())]


def launch_pending_restore(pending_file):
    pending_file = Path(pending_file).resolve()
    if not pending_file.exists():
        raise ValueError("Pending restore không tồn tại")
    kwargs = {"cwd": str(ROOT)}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000
        kwargs["close_fds"] = True
    try:
        subprocess.Popen(_restore_helper_command(pending_file), **kwargs)
    except Exception:
        raise
    return {"ok": True, "accepted": True, "closing": True}


def open_user_data_folder(opener=None, platform_name=None):
    platform_name = os.name if platform_name is None else platform_name
    if platform_name != "nt":
        raise ValueError("Chức năng này chỉ hỗ trợ Windows")
    base = _user_base_dir()
    base.mkdir(parents=True, exist_ok=True)
    if opener is None:
        opener = os.startfile
    opener(str(base))
    return {"ok": True, "path": str(base)}


class Handler(BaseHTTPRequestHandler):
    server_version = f"EnglishLocal/{APP_VERSION}"

    def log_message(self, fmt, *args):
        pass

    def send_json(self,obj,status=200):
        raw=json.dumps(obj,ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(raw)

    def read_json(self):
        length=int(self.headers.get("Content-Length","0") or 0)
        if length>420*1024*1024: raise ValueError("Request quá lớn")
        return json.loads(self.rfile.read(length) or b"{}")

    def _receive_bulk_audio_zip(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        scope = (query.get("scope") or [""])[0]
        target = (query.get("target") or [""])[0]
        if scope not in {"my", "course"} or not target:
            raise ValueError("Đích Bulk Audio không hợp lệ")
        length = int(self.headers.get("Content-Length", "0") or 0)
        record = BULK_AUDIO_SESSIONS.from_uploaded_zip(_bulk_audio_stage_root(), scope, target, self.rfile, length)
        try:
            return self.send_json(preview_bulk_audio_session(record["token"]))
        except Exception:
            BULK_AUDIO_SESSIONS.cleanup(record["token"])
            raise

    def _receive_bulk_audio_file(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        token = (query.get("token") or [""])[0]
        name = urllib.parse.unquote((query.get("name") or [""])[0])
        length = int(self.headers.get("Content-Length", "0") or 0)
        return self.send_json(BULK_AUDIO_SESSIONS.add_stream(token, name, self.rfile, length))

    def send_file(self,path:Path,cache=False,download_name=None):
        if not path.exists() or not path.is_file(): return self.send_error(404)
        ctype,_=mimetypes.guess_type(str(path)); ctype=ctype or "application/octet-stream"; size=path.stat().st_size
        range_header=self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            try:
                spec=range_header.split("=",1)[1]; start_s,end_s=spec.split("-",1); start=int(start_s) if start_s else 0; end=int(end_s) if end_s else size-1; end=min(end,size-1)
                if start>end or start>=size: raise ValueError
                length=end-start+1; self.send_response(206); self.send_header("Content-Type",ctype); self.send_header("Content-Range",f"bytes {start}-{end}/{size}"); self.send_header("Accept-Ranges","bytes"); self.send_header("Content-Length",str(length)); self.send_header("Cache-Control","public, max-age=31536000" if cache else "no-cache");
                if download_name: self.send_header("Content-Disposition", f'attachment; filename="{download_name}"');
                self.end_headers()
                with path.open("rb") as f:
                    f.seek(start); remaining=length
                    while remaining:
                        chunk=f.read(min(65536,remaining))
                        if not chunk: break
                        self.wfile.write(chunk); remaining-=len(chunk)
                return
            except Exception:
                self.send_response(416); self.send_header("Content-Range",f"bytes */{size}"); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type",ctype); self.send_header("Content-Length",str(size)); self.send_header("Accept-Ranges","bytes"); self.send_header("Cache-Control","public, max-age=31536000" if cache else "no-cache");
        if download_name: self.send_header("Content-Disposition", f'attachment; filename="{download_name}"');
        self.end_headers()
        with path.open("rb") as f:
            while True:
                chunk=f.read(65536)
                if not chunk: break
                self.wfile.write(chunk)

    def _send_user_backup(self):
        base = _user_base_dir()
        base.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=".backup_download_", dir=base))
        try:
            output = work / _backup_filename()
            create_user_backup(output)
            return self.send_file(output, cache=False, download_name=output.name)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _receive_restore_zip(self):
        if RESTORE_SHUTDOWN_CALLBACK is None:
            raise RuntimeError("Restore chỉ khả dụng khi ứng dụng có thể tự đóng hoàn toàn")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in ("application/zip", "application/octet-stream"):
            raise ValueError("Hãy chọn trực tiếp file backup ZIP")
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValueError("Kích thước file backup không hợp lệ") from exc
        if length <= 0:
            raise ValueError("File backup trống")
        if length > MAX_BACKUP_ARCHIVE_BYTES:
            raise ValueError("File backup vượt giới hạn an toàn 16 GB")
        base = _user_base_dir()
        free = shutil.disk_usage(base).free
        if free < length + RESTORE_DISK_MARGIN_BYTES:
            raise ValueError("Không đủ dung lượng trống để nhận và kiểm tra file backup")

        pending = new_restore_staging()
        pending.mkdir(parents=True)
        incoming = pending / "incoming.zip"
        remaining = length
        try:
            with incoming.open("wb") as output:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("File backup tải lên bị thiếu dữ liệu")
                    output.write(chunk)
                    remaining -= len(chunk)
            prepared = prepare_user_restore(incoming, pending_dir=pending)
            # Do not spawn a background helper here. The validated restore is
            # completed synchronously on the next manual app launch, before any
            # user database connection/server is opened.
            RESTORE_SHUTDOWN_PENDING.set()
            response = {key: value for key, value in prepared.items() if key != "pendingFile"}
            self.send_json(response, 202)
            timer = threading.Timer(1.5, RESTORE_SHUTDOWN_CALLBACK)
            timer.daemon = True
            timer.start()
            return None
        except Exception:
            shutil.rmtree(pending, ignore_errors=True)
            raise

    def do_GET(self):
        with USER_DATA_LOCK:
            return self._do_GET()

    def _do_GET(self):
        parsed=urllib.parse.urlparse(self.path); path=parsed.path; qs=urllib.parse.parse_qs(parsed.query)
        try:
            if path=="/api/bootstrap": return self.send_json(get_bootstrap())
            if path=="/api/data/backup": return self._send_user_backup()
            if path=="/api/srs/info":
                item=(qs.get("item_key") or [None])[0]; col=(qs.get("collection_key") or [None])[0]; group=(qs.get("group_key") or [None])[0]
                return self.send_json(get_srs_info(item,col,group))
            if path=="/api/collection":
                kind=(qs.get("kind") or ["core"])[0]; ident=int((qs.get("id") or ["0"])[0]); d=get_collection_any(kind,ident); return self.send_json(d if d else {"error":"not found"},200 if d else 404)
            if path=="/api/search": return self.send_json({"items":search_content((qs.get("q") or [""])[0],(qs.get("filter") or ["all"])[0])})
            if path=="/api/saved": return self.send_json({"items":get_saved_items()})
            if path=="/api/my-islands": return self.send_json({"items":get_my_islands()})
            if path=="/api/my-island/missing-audio": return self.send_json({"items":missing_audio_my_island(int((qs.get("id") or ["0"])[0]))})
            if path=="/template/my-island.xlsx": return self.send_file(TEMPLATE_XLSX,cache=False,download_name="mau_import_my_island.xlsx")
            if path=="/api/session/daily":
                extra=(qs.get("extra") or [None])[0]; return self.send_json(daily_session(extra))
            if path.startswith("/audio/"):
                name=Path(path).name
                if not re.fullmatch(r"\d{6}\.mp3",name): return self.send_error(404)
                return self.send_file(AUDIO/name,cache=True)
            if path.startswith("/course-audio/"):
                rel=urllib.parse.unquote(path[len("/course-audio/"):]).replace("\\","/").lstrip("/")
                candidate=(COURSE_AUDIO/rel).resolve()
                if COURSE_AUDIO.resolve() not in candidate.parents: return self.send_error(403)
                return self.send_file(candidate,cache=True)
            if path.startswith("/user-audio/"):
                name=Path(path).name
                if name!=path.split("/")[-1] or not re.fullmatch(r"custom_\d{6}\.(mp3|wav|m4a|aac|ogg|webm)",name,re.I): return self.send_error(404)
                return self.send_file(USER_AUDIO/name,cache=False)
            if path in ("/",""): return self.send_file(WEB/"index.html")
            rel=path.lstrip("/"); candidate=(WEB/rel).resolve()
            if WEB.resolve() not in candidate.parents: return self.send_error(403)
            return self.send_file(candidate)
        except Exception as e:
            return self.send_json({"error":str(e)},400)

    def do_POST(self):
        parsed=urllib.parse.urlparse(self.path); path=parsed.path
        if path=="/api/data/restore":
            try:
                return self._receive_restore_zip()
            except Exception as e:
                return self.send_json({"error":str(e)},400)
        if path in {"/api/bulk-audio/zip", "/api/bulk-audio/file"}:
            try:
                with USER_DATA_LOCK:
                    return self._receive_bulk_audio_zip(parsed) if path.endswith("/zip") else self._receive_bulk_audio_file(parsed)
            except Exception as e:
                return self.send_json({"error":str(e)},400)
        if RESTORE_SHUTDOWN_PENDING.is_set():
            return self.send_json({"error":"Ứng dụng đang đóng để khôi phục dữ liệu"},503)
        with USER_DATA_LOCK:
            return self._do_POST()

    def _do_POST(self):
        parsed=urllib.parse.urlparse(self.path)
        try:
            data=self.read_json(); path=parsed.path
            if path=="/api/data/reset-progress": return self.send_json(reset_learning_progress())
            if path=="/api/data/delete-all": return self.send_json(delete_all_user_data())
            if path=="/api/data/restore-result/ack": return self.send_json(acknowledge_restore_result())
            if path=="/api/data/open-folder": return self.send_json(open_user_data_folder())
            if path=="/api/review": return self.send_json(apply_review(data.get("item_key"),int(data.get("rating",0)),data.get("source_mode","active_recall")))
            if path=="/api/srs/manage": return self.send_json(manage_srs(data.get("action"),data.get("item_key"),data.get("collection_key"),data.get("group_key")))
            if path=="/api/bookmark": return self.send_json(bookmark_item(data.get("item_key"),data.get("saved") if "saved" in data else None))
            if path=="/api/position": return self.send_json(save_position(data.get("collection_key"),data.get("index",0)))
            if path=="/api/active-source": return self.send_json(set_active_source(data.get("collection_key")))
            if path=="/api/setting": return self.send_json(update_setting(data.get("key"),data.get("value")))
            if path=="/api/my-island/create": return self.send_json(create_my_island(data.get("name"),data.get("description","")))
            if path=="/api/my-island/update": return self.send_json(update_my_island(data.get("id"),data.get("name"),data.get("description","")))
            if path=="/api/my-island/delete": return self.send_json(delete_my_island(data.get("id")))
            if path=="/api/my-island/add": return self.send_json(add_to_my_island(data.get("id"),data.get("item_key")))
            if path=="/api/my-island/remove": return self.send_json(remove_from_my_island(data.get("id"),data.get("item_key")))
            if path=="/api/my-island/reorder": return self.send_json(reorder_my_island(data.get("id"),data.get("item_keys")))
            if path=="/api/my-island/import-xlsx": return self.send_json(import_xlsx_to_island(data.get("id"),data.get("xlsx_data")))
            if path=="/api/bulk-audio/start": return self.send_json(start_bulk_audio_folder_session(data.get("scope"),data.get("target")))
            if path=="/api/bulk-audio/preview": return self.send_json(preview_bulk_audio_session(data.get("token")))
            if path=="/api/bulk-audio/confirm": return self.send_json(confirm_bulk_audio_session(data.get("token")))
            if path=="/api/bulk-audio/cancel": return self.send_json(cancel_bulk_audio_session(data.get("token")))
            if path=="/api/custom/create": return self.send_json(create_custom_sentence(data.get("en_us"),data.get("vi_vn",""),data.get("usage_note",""),data.get("literal_note",""),data.get("audio_data"),data.get("audio_name",""),data.get("audio_type",""),data.get("island_id"),data.get("audio_key"),data.get("audio_expected"),data.get("note","")))
            return self.send_error(404)
        except Exception as e:
            return self.send_json({"error":str(e)},400)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--port",type=int,default=DEFAULT_PORT); ap.add_argument("--no-browser",action="store_true"); args=ap.parse_args()
    if not CONTENT_DB.exists(): print(f"Missing database: {CONTENT_DB}"); sys.exit(1)
    if not AUDIO.exists(): print(f"Missing audio folder: {AUDIO}"); sys.exit(1)
    configure_restore_lifecycle(None)
    restore_result = complete_pending_restore_if_needed(lock_timeout=5.0)
    if restore_result and not restore_result.get("ok") and not restore_result.get("rollbackOk", True):
        print("Khôi phục và rollback đều thất bại; không mở app để tránh dùng profile không an toàn.")
        return
    cleanup_orphan_bulk_audio_staging()
    user_conn().close()
    httpd=ThreadingHTTPServer((HOST,args.port),Handler); url=f"http://{HOST}:{args.port}"
    configure_restore_lifecycle(httpd.shutdown)
    print("="*64); print(f" ENGLISH LOCAL APP V{APP_VERSION}"); print(f" Open: {url}"); print(" Learn List - Courses - Shadowing - Active Recall - FSRS - My Islands"); print(" Close app: press Ctrl+C in this window"); print("="*64)
    if not args.no_browser: threading.Thread(target=lambda:(time.sleep(1),webbrowser.open(url)),daemon=True).start()
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass
    finally: httpd.server_close()

if __name__=="__main__": main()
