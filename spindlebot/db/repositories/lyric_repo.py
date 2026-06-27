"""Repository for `lyric_doc` + `lyric_version`. SQL only; caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.models import LyricDoc, LyricVersion


def ensure_doc(conn: sqlite3.Connection, audio_id: int, now: int) -> LyricDoc:
    """Get-or-create the lyric doc for a track; created_utc preserved on re-call."""
    conn.execute(
        "INSERT INTO lyric_doc (audio_id, created_utc, updated_utc) VALUES (?, ?, ?) "
        "ON CONFLICT(audio_id) DO NOTHING",
        (audio_id, now, now),
    )
    found = get_doc(conn, audio_id)
    assert found is not None
    return found


def add_version(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    sha256: str,
    vclock_json: str,
    now: int,
    source: str | None = None,
    authored_utc: int | None = None,
) -> LyricVersion:
    """Append an immutable version to a doc; touch the doc's updated_utc."""
    cur = conn.execute(
        "INSERT INTO lyric_version "
        "(doc_id, sha256, vclock_json, source, authored_utc, created_utc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (doc_id, sha256, vclock_json, source, authored_utc, now),
    )
    conn.execute("UPDATE lyric_doc SET updated_utc = ? WHERE id = ?", (now, doc_id))
    found = get_version(conn, int(cur.lastrowid))
    assert found is not None
    return found


def set_head(conn: sqlite3.Connection, doc_id: int, version_id: int, now: int) -> None:
    conn.execute(
        "UPDATE lyric_doc SET head_version_id = ?, updated_utc = ? WHERE id = ?",
        (version_id, now, doc_id),
    )


def get_doc(conn: sqlite3.Connection, audio_id: int) -> LyricDoc | None:
    row = conn.execute(
        "SELECT * FROM lyric_doc WHERE audio_id = ?", (audio_id,)
    ).fetchone()
    return LyricDoc.from_row(row) if row else None


def get_doc_by_id(conn: sqlite3.Connection, doc_id: int) -> LyricDoc | None:
    row = conn.execute("SELECT * FROM lyric_doc WHERE id = ?", (doc_id,)).fetchone()
    return LyricDoc.from_row(row) if row else None


def get_version(conn: sqlite3.Connection, version_id: int) -> LyricVersion | None:
    row = conn.execute(
        "SELECT * FROM lyric_version WHERE id = ?", (version_id,)
    ).fetchone()
    return LyricVersion.from_row(row) if row else None


def list_versions(conn: sqlite3.Connection, doc_id: int) -> list[LyricVersion]:
    rows = conn.execute(
        "SELECT * FROM lyric_version WHERE doc_id = ? ORDER BY id", (doc_id,)
    ).fetchall()
    return [LyricVersion.from_row(r) for r in rows]


def head_version(conn: sqlite3.Connection, doc_id: int) -> LyricVersion | None:
    doc = get_doc_by_id(conn, doc_id)
    if doc is None or doc.head_version_id is None:
        return None
    return get_version(conn, doc.head_version_id)
