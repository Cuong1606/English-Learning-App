#!/usr/bin/env python3
"""Test helper: keep a committed SQLite WAL connection open until released."""
import sqlite3
import sys
import time
from pathlib import Path


db, ready, release = map(Path, sys.argv[1:4])
con = sqlite3.connect(db, timeout=20)
try:
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "INSERT OR REPLACE INTO app_settings(key,value) VALUES('windows_lock_marker','held-in-wal')"
    )
    con.commit()
    ready.write_text("ready", encoding="utf-8")
    while not release.exists():
        time.sleep(0.05)
finally:
    con.close()
