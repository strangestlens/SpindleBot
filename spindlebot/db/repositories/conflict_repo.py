"""Repository for the `conflict` table. SQL only; the caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.enums import ConflictStatus
from spindlebot.core.models import Conflict


def open_conflict(
    conn: sqlite3.Connection,
    *,
    audio_id: int | None,
    now: int,
    winner_version: int | None = None,
    loser_version: int | None = None,
    loser_kept_path: str | None = None,
) -> Conflict:
    """Record a new open conflict; return it. Never auto-deletes the loser."""
    cur = conn.execute(
        "INSERT INTO conflict "
        "(audio_id, winner_version, loser_version, loser_kept_path, status, detected_utc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (audio_id, winner_version, loser_version, loser_kept_path,
         str(ConflictStatus.OPEN), now),
    )
    found = get(conn, int(cur.lastrowid))
    assert found is not None
    return found


def resolve(conn: sqlite3.Connection, conflict_id: int, now: int) -> None:
    conn.execute(
        "UPDATE conflict SET status = ?, resolved_utc = ? WHERE id = ?",
        (str(ConflictStatus.RESOLVED), now, conflict_id),
    )


def get(conn: sqlite3.Connection, conflict_id: int) -> Conflict | None:
    row = conn.execute(
        "SELECT * FROM conflict WHERE id = ?", (conflict_id,)
    ).fetchone()
    return Conflict.from_row(row) if row else None


def list_open(conn: sqlite3.Connection) -> list[Conflict]:
    rows = conn.execute(
        "SELECT * FROM conflict WHERE status = ? ORDER BY detected_utc, id",
        (str(ConflictStatus.OPEN),),
    ).fetchall()
    return [Conflict.from_row(r) for r in rows]


def find_open_for_audio(conn: sqlite3.Connection, audio_id: int) -> Conflict | None:
    row = conn.execute(
        "SELECT * FROM conflict WHERE audio_id = ? AND status = ? "
        "ORDER BY detected_utc DESC, id DESC LIMIT 1",
        (audio_id, str(ConflictStatus.OPEN)),
    ).fetchone()
    return Conflict.from_row(row) if row else None
