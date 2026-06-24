"""Repository for the `location_scan` table. SQL only; caller owns the transaction."""
from __future__ import annotations

import sqlite3


def start_scan(conn: sqlite3.Connection, location_id: int, now: int) -> int:
    """Open a running scan row for a location; return its id."""
    cur = conn.execute(
        "INSERT INTO location_scan (location_id, started_utc, status) "
        "VALUES (?, ?, 'running')",
        (location_id, now),
    )
    return int(cur.lastrowid)


def finish_scan(
    conn: sqlite3.Connection, scan_id: int, *, files_seen: int, status: str, now: int
) -> None:
    conn.execute(
        "UPDATE location_scan SET finished_utc = ?, files_seen = ?, status = ? WHERE id = ?",
        (now, files_seen, status, scan_id),
    )


def latest_scan(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM location_scan WHERE location_id = ? "
        "ORDER BY started_utc DESC, id DESC LIMIT 1",
        (location_id,),
    ).fetchone()
