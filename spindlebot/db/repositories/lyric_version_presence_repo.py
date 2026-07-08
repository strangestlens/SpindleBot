"""Repository for `lyric_version_presence`. SQL only; the caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.models import LyricVersionPresence


def upsert(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    location_id: int,
    version_id: int,
    observed_utc: int,
) -> LyricVersionPresence:
    """Record the version a location currently holds for a doc. Upsert on the PK."""
    conn.execute(
        """
        INSERT INTO lyric_version_presence
            (doc_id, location_id, version_id, observed_utc)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(doc_id, location_id) DO UPDATE SET
            version_id = excluded.version_id,
            observed_utc = excluded.observed_utc
        """,
        (doc_id, location_id, version_id, observed_utc),
    )
    found = get(conn, doc_id, location_id)
    assert found is not None
    return found


def get(
    conn: sqlite3.Connection, doc_id: int, location_id: int
) -> LyricVersionPresence | None:
    row = conn.execute(
        "SELECT * FROM lyric_version_presence WHERE doc_id = ? AND location_id = ?",
        (doc_id, location_id),
    ).fetchone()
    return LyricVersionPresence.from_row(row) if row else None


def list_for_doc(conn: sqlite3.Connection, doc_id: int) -> list[LyricVersionPresence]:
    rows = conn.execute(
        "SELECT * FROM lyric_version_presence WHERE doc_id = ? ORDER BY location_id",
        (doc_id,),
    ).fetchall()
    return [LyricVersionPresence.from_row(r) for r in rows]
