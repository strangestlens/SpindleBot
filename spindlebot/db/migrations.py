"""
Schema migrations, keyed to PRAGMA user_version.

Each phase appends a (target_version, sql_filename) step; `migrate()` applies
every step whose target is above the database's current user_version, in order,
inside a transaction. Forward-only — there are no down-migrations.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_DIR = Path(__file__).parent

# Ordered, append-only. Later phases add (3, "..."), (4, "...") here.
MIGRATIONS: list[tuple[int, str]] = [
    (1, "schema.sql"),
    (2, "schema_v2.sql"),
    (3, "schema_v3.sql"),
    (4, "schema_v4.sql"),
    (5, "schema_v5.sql"),
    (6, "schema_v6.sql"),
    (7, "schema_v7.sql"),
]

LATEST_VERSION = MIGRATIONS[-1][0]


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """Bring `conn` up to LATEST_VERSION; return the resulting version."""
    version = current_version(conn)
    for target, sql_file in MIGRATIONS:
        if version >= target:
            continue
        sql = (_SCHEMA_DIR / sql_file).read_text(encoding="utf-8")
        with conn:  # one transaction per step
            conn.executescript(sql)
            # PRAGMA can't be parameterized; target is a trusted int literal.
            conn.execute(f"PRAGMA user_version = {target}")
        version = target
    return version
