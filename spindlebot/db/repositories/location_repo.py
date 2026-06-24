"""Repository for the `location` table. SQL only; the caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.models import Location


def upsert(
    conn: sqlite3.Connection,
    *,
    uuid: str,
    name: str,
    kind: str,
    is_authoritative_audio: bool = False,
    is_retention: bool = False,
    enabled: bool = True,
    last_seen_utc: int | None = None,
) -> Location:
    """Insert a location, or update it in place (matched on uuid). Returns the row.

    last_seen_utc is only advanced, never cleared, on update.
    """
    conn.execute(
        """
        INSERT INTO location
            (uuid, name, kind, is_authoritative_audio, is_retention, enabled, last_seen_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uuid) DO UPDATE SET
            name = excluded.name,
            kind = excluded.kind,
            is_authoritative_audio = excluded.is_authoritative_audio,
            is_retention = excluded.is_retention,
            enabled = excluded.enabled,
            last_seen_utc = COALESCE(excluded.last_seen_utc, location.last_seen_utc)
        """,
        (uuid, name, kind, int(is_authoritative_audio), int(is_retention),
         int(enabled), last_seen_utc),
    )
    found = get_by_uuid(conn, uuid)
    assert found is not None  # just inserted/updated
    return found


def get_by_uuid(conn: sqlite3.Connection, uuid: str) -> Location | None:
    row = conn.execute("SELECT * FROM location WHERE uuid = ?", (uuid,)).fetchone()
    return Location.from_row(row) if row else None


def get_by_id(conn: sqlite3.Connection, location_id: int) -> Location | None:
    row = conn.execute("SELECT * FROM location WHERE id = ?", (location_id,)).fetchone()
    return Location.from_row(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[Location]:
    rows = conn.execute("SELECT * FROM location ORDER BY name").fetchall()
    return [Location.from_row(r) for r in rows]
