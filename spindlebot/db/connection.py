"""
SQLite connection helpers for the SpindleBot database.

`open_db()` is the normal entry point: it connects, applies pragmas, and brings
the schema up to the latest version. `connect()` is the lower-level primitive
(no migration) used by tests and by migration code itself.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from spindlebot.db.migrations import migrate


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with SpindleBot's standard pragmas. No migration."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def open_db(db_path: str | Path) -> sqlite3.Connection:
    """Connect and migrate to the latest schema version."""
    conn = connect(db_path)
    migrate(conn)
    return conn
