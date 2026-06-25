"""
Tests for the SpindleBot DB layer: connection pragmas + schema migration.
All tests run against a real tmpfile DB (WAL needs a file, not :memory:).
"""
from __future__ import annotations

import sqlite3

import pytest

from spindlebot.db import migrations
from spindlebot.db.connection import connect, open_db
from spindlebot.db.migrations import LATEST_VERSION, current_version, migrate


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def test_open_db_creates_schema_at_latest_version(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    assert current_version(conn) == LATEST_VERSION == 3
    assert {
        "location", "audio_content", "audio_presence", "location_scan",
        "album", "album_track", "sidecar_content", "sidecar_presence",
    } <= _tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(location)").fetchall()}
    assert "root_path" in cols


def test_open_db_creates_parent_dirs(tmp_path):
    conn = open_db(tmp_path / "nested" / "deeper" / "spindlebot.db")
    assert (tmp_path / "nested" / "deeper" / "spindlebot.db").exists()
    assert current_version(conn) == 3


def test_pragmas_applied(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_migrate_is_idempotent(tmp_path):
    db = tmp_path / "spindlebot.db"
    open_db(db).close()
    conn = open_db(db)  # second open must not re-run or error
    assert current_version(conn) == 3
    # re-invoking migrate directly is also a no-op
    assert migrate(conn) == 3


def test_migrate_from_fresh_connect(tmp_path):
    conn = connect(tmp_path / "spindlebot.db")
    assert current_version(conn) == 0       # not migrated yet
    assert migrate(conn) == 3
    assert {
        "location", "audio_content", "audio_presence", "location_scan",
        "album", "album_track", "sidecar_content", "sidecar_presence",
    } <= _tables(conn)


def test_foreign_keys_enforced(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    # audio_presence referencing non-existent audio_id/location_id must fail
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO audio_presence "
            "(audio_id, location_id, present, observed_utc) VALUES (999, 999, 1, 0)"
        )


def test_audio_content_identity_unique(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    conn.execute(
        "INSERT INTO audio_content (identity, identity_kind, first_seen_utc, last_seen_utc) "
        "VALUES ('abc', 'audio_md5', 0, 0)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO audio_content (identity, identity_kind, first_seen_utc, last_seen_utc) "
            "VALUES ('abc', 'audio_md5', 0, 0)"
        )


def test_v3_upgrades_existing_v2_db(tmp_path):
    """An existing v2 DB with data migrates forward to v3 without loss."""
    db = tmp_path / "spindlebot.db"
    conn = connect(db)
    for target, sql_file in [(1, "schema.sql"), (2, "schema_v2.sql")]:  # stop at v2
        sql = (migrations._SCHEMA_DIR / sql_file).read_text(encoding="utf-8")
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {target}")
    conn.execute(
        "INSERT INTO audio_content (identity, identity_kind, first_seen_utc, last_seen_utc) "
        "VALUES ('keep', 'audio_md5', 0, 0)"
    )
    conn.commit()
    assert current_version(conn) == 2

    assert migrate(conn) == 3
    assert "album" in _tables(conn) and "sidecar_content" in _tables(conn)
    # pre-existing data survives the upgrade
    assert conn.execute(
        "SELECT identity FROM audio_content"
    ).fetchone()[0] == "keep"


def test_sidecar_content_triple_unique(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    conn.execute(
        "INSERT INTO album (album_key, first_seen_utc, last_seen_utc) VALUES ('k', 0, 0)"
    )
    conn.execute(
        "INSERT INTO sidecar_content "
        "(parent_kind, parent_id, role, sha256, first_seen_utc, last_seen_utc) "
        "VALUES ('album', 1, 'cover', 'h', 0, 0)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sidecar_content "
            "(parent_kind, parent_id, role, sha256, first_seen_utc, last_seen_utc) "
            "VALUES ('album', 1, 'cover', 'h2', 0, 0)"
        )


def test_sidecar_presence_fk_enforced(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sidecar_presence "
            "(sidecar_id, location_id, present, observed_utc) VALUES (999, 999, 1, 0)"
        )
