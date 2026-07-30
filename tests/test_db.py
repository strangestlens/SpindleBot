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
    assert current_version(conn) == LATEST_VERSION == 7
    assert {
        "location", "audio_content", "audio_presence", "location_scan",
        "album", "album_track", "sidecar_content", "sidecar_presence",
        "run", "pending_action", "lyric_doc", "lyric_version", "conflict",
        "lyric_version_presence",
    } <= _tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(location)").fetchall()}
    assert "root_path" in cols


def test_open_db_creates_parent_dirs(tmp_path):
    conn = open_db(tmp_path / "nested" / "deeper" / "spindlebot.db")
    assert (tmp_path / "nested" / "deeper" / "spindlebot.db").exists()
    assert current_version(conn) == 7


def test_pragmas_applied(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_migrate_is_idempotent(tmp_path):
    db = tmp_path / "spindlebot.db"
    open_db(db).close()
    conn = open_db(db)  # second open must not re-run or error
    assert current_version(conn) == 7
    # re-invoking migrate directly is also a no-op
    assert migrate(conn) == 7


def test_migrate_from_fresh_connect(tmp_path):
    conn = connect(tmp_path / "spindlebot.db")
    assert current_version(conn) == 0       # not migrated yet
    assert migrate(conn) == 7
    assert {
        "location", "audio_content", "audio_presence", "location_scan",
        "album", "album_track", "sidecar_content", "sidecar_presence",
        "run", "pending_action", "lyric_doc", "lyric_version", "conflict",
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

    assert migrate(conn) == 7
    assert "album" in _tables(conn) and "sidecar_content" in _tables(conn)
    # pre-existing data survives the upgrade
    assert conn.execute(
        "SELECT identity FROM audio_content"
    ).fetchone()[0] == "keep"


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_v6_adds_mtime_to_presence_tables(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    assert current_version(conn) == 7
    assert "mtime" in _cols(conn, "audio_presence")
    assert "mtime" in _cols(conn, "sidecar_presence")


def test_v6_upgrades_existing_v5_db(tmp_path):
    """An existing v5 DB with presence data migrates forward to v6 without loss.

    Pre-v6 rows have no mtime, so the column is added NULL for them.
    """
    db = tmp_path / "spindlebot.db"
    conn = connect(db)
    for target, sql_file in migrations.MIGRATIONS:
        if target > 5:
            break
        sql = (migrations._SCHEMA_DIR / sql_file).read_text(encoding="utf-8")
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {target}")
    conn.execute(
        "INSERT INTO location (uuid, name, kind) VALUES ('u', 'L', 'library')"
    )
    conn.execute(
        "INSERT INTO audio_content (identity, identity_kind, first_seen_utc, last_seen_utc) "
        "VALUES ('keep', 'audio_md5', 0, 0)"
    )
    conn.execute(
        "INSERT INTO audio_presence "
        "(audio_id, location_id, present, rel_path, file_sha256, byte_size, observed_utc) "
        "VALUES (1, 1, 1, 'a.flac', 'deadbeef', 10, 0)"
    )
    conn.commit()
    assert current_version(conn) == 5

    assert migrate(conn) == 7
    assert "mtime" in _cols(conn, "audio_presence")
    # pre-existing presence survives; its mtime is NULL (unknown until re-scanned)
    row = conn.execute(
        "SELECT file_sha256, mtime FROM audio_presence"
    ).fetchone()
    assert row["file_sha256"] == "deadbeef"
    assert row["mtime"] is None


def test_v6_migration_is_idempotent(tmp_path):
    db = tmp_path / "spindlebot.db"
    open_db(db).close()
    conn = open_db(db)
    assert migrate(conn) == 7
    # exactly one mtime column on each table (no duplicate ALTER)
    assert sum(1 for c in _cols(conn, "audio_presence") if c == "mtime") == 1
    assert sum(1 for c in _cols(conn, "sidecar_presence") if c == "mtime") == 1


def test_v7_adds_lyric_version_presence(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    assert current_version(conn) == 7
    assert "lyric_version_presence" in _tables(conn)
    assert _cols(conn, "lyric_version_presence") == {
        "doc_id", "location_id", "version_id", "observed_utc",
    }


def test_v7_upgrades_existing_v6_db(tmp_path):
    """An existing v6 DB with data migrates forward to v7 without loss."""
    db = tmp_path / "spindlebot.db"
    conn = connect(db)
    for target, sql_file in migrations.MIGRATIONS:
        if target > 6:
            break
        sql = (migrations._SCHEMA_DIR / sql_file).read_text(encoding="utf-8")
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {target}")
    conn.execute(
        "INSERT INTO audio_content (identity, identity_kind, first_seen_utc, last_seen_utc) "
        "VALUES ('keep', 'audio_md5', 0, 0)"
    )
    conn.commit()
    assert current_version(conn) == 6

    assert migrate(conn) == 7
    assert "lyric_version_presence" in _tables(conn)
    # pre-existing data survives the upgrade
    assert conn.execute("SELECT identity FROM audio_content").fetchone()[0] == "keep"


def test_v7_migration_is_idempotent(tmp_path):
    db = tmp_path / "spindlebot.db"
    open_db(db).close()
    conn = open_db(db)  # second open must not re-run or error
    assert migrate(conn) == 7
    assert "lyric_version_presence" in _tables(conn)


def test_v7_lyric_version_presence_fk_and_pk(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    # FK: doc/location/version must exist
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lyric_version_presence "
            "(doc_id, location_id, version_id, observed_utc) VALUES (99, 99, 99, 0)"
        )
    # a deleted doc cascades its version-presence rows away
    conn.execute("INSERT INTO location (uuid, name, kind) VALUES ('u', 'L', 'library')")
    conn.execute(
        "INSERT INTO audio_content (identity, identity_kind, first_seen_utc, last_seen_utc) "
        "VALUES ('a', 'audio_md5', 0, 0)"
    )
    conn.execute("INSERT INTO lyric_doc (audio_id, created_utc, updated_utc) VALUES (1, 0, 0)")
    conn.execute(
        "INSERT INTO lyric_version (doc_id, sha256, vclock_json, created_utc) "
        "VALUES (1, 'h', '{}', 0)"
    )
    conn.execute(
        "INSERT INTO lyric_version_presence "
        "(doc_id, location_id, version_id, observed_utc) VALUES (1, 1, 1, 0)"
    )
    conn.execute("DELETE FROM lyric_doc WHERE id = 1")
    assert conn.execute(
        "SELECT COUNT(*) FROM lyric_version_presence"
    ).fetchone()[0] == 0


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


def test_pending_action_requires_a_run(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pending_action "
            "(run_id, action_kind, content_kind, content_id, created_utc) "
            "VALUES (999, 'copy', 'audio', 1, 0)"
        )


def test_pending_action_cascades_with_its_run(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    conn.execute("INSERT INTO run (kind, started_utc) VALUES ('reconcile', 0)")
    run_id = conn.execute("SELECT id FROM run").fetchone()[0]
    # content_id is polymorphic → no FK on it, so an arbitrary id is accepted
    conn.execute(
        "INSERT INTO pending_action "
        "(run_id, action_kind, content_kind, content_id, created_utc) "
        "VALUES (?, 'copy', 'audio', 4242, 0)",
        (run_id,),
    )
    conn.execute("DELETE FROM run WHERE id = ?", (run_id,))
    assert conn.execute("SELECT COUNT(*) FROM pending_action").fetchone()[0] == 0
