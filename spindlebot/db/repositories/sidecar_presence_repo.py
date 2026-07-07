"""Repository for `sidecar_presence`. SQL only; the caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.models import SidecarPresence


def set_presence(
    conn: sqlite3.Connection,
    *,
    sidecar_id: int,
    location_id: int,
    present: bool,
    observed_utc: int,
    rel_path: str | None = None,
    file_sha256: str | None = None,
    byte_size: int | None = None,
    mtime: int | None = None,
) -> SidecarPresence:
    """Record an observed presence fact for (sidecar_id, location_id). Upsert on PK."""
    conn.execute(
        """
        INSERT INTO sidecar_presence
            (sidecar_id, location_id, present, rel_path, file_sha256, byte_size,
             observed_utc, mtime)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sidecar_id, location_id) DO UPDATE SET
            present = excluded.present,
            rel_path = excluded.rel_path,
            file_sha256 = excluded.file_sha256,
            byte_size = excluded.byte_size,
            observed_utc = excluded.observed_utc,
            mtime = excluded.mtime
        """,
        (sidecar_id, location_id, int(present), rel_path, file_sha256, byte_size,
         observed_utc, mtime),
    )
    found = get(conn, sidecar_id, location_id)
    assert found is not None
    return found


def get(conn: sqlite3.Connection, sidecar_id: int, location_id: int) -> SidecarPresence | None:
    row = conn.execute(
        "SELECT * FROM sidecar_presence WHERE sidecar_id = ? AND location_id = ?",
        (sidecar_id, location_id),
    ).fetchone()
    return SidecarPresence.from_row(row) if row else None


def get_by_rel_path(
    conn: sqlite3.Connection, location_id: int, rel_path: str
) -> SidecarPresence | None:
    """The presence row for a given path at a location, if one is recorded.

    Keyed by (location_id, rel_path) so a scanner can look a sidecar up *before*
    hashing it (its parent sidecar_id isn't known yet).
    """
    row = conn.execute(
        "SELECT * FROM sidecar_presence WHERE location_id = ? AND rel_path = ?",
        (location_id, rel_path),
    ).fetchone()
    return SidecarPresence.from_row(row) if row else None


def list_for_location(
    conn: sqlite3.Connection, location_id: int, *, present: bool | None = None
) -> list[SidecarPresence]:
    sql = "SELECT * FROM sidecar_presence WHERE location_id = ?"
    params: list = [location_id]
    if present is not None:
        sql += " AND present = ?"
        params.append(int(present))
    rows = conn.execute(sql + " ORDER BY sidecar_id", params).fetchall()
    return [SidecarPresence.from_row(r) for r in rows]


def list_for_sidecar(conn: sqlite3.Connection, sidecar_id: int) -> list[SidecarPresence]:
    """Every presence row for a sidecar across all locations."""
    rows = conn.execute(
        "SELECT * FROM sidecar_presence WHERE sidecar_id = ? ORDER BY location_id",
        (sidecar_id,),
    ).fetchall()
    return [SidecarPresence.from_row(r) for r in rows]
