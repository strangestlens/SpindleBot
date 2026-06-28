"""Repository for the `run` table. SQL only; the caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.enums import RunKind, ScanStatus
from spindlebot.core.models import Run


def start_run(
    conn: sqlite3.Connection,
    kind: RunKind | str,
    *,
    now: int,
    location_id: int | None = None,
    note: str | None = None,
) -> int:
    """Open a running run row; return its id."""
    cur = conn.execute(
        "INSERT INTO run (kind, location_id, started_utc, status, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (str(RunKind(kind)), location_id, now, str(ScanStatus.RUNNING), note),
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: ScanStatus | str,
    now: int,
    note: str | None = None,
) -> None:
    conn.execute(
        "UPDATE run SET finished_utc = ?, status = ?, "
        "note = COALESCE(?, note) WHERE id = ?",
        (now, str(ScanStatus(status)), note, run_id),
    )


def get(conn: sqlite3.Connection, run_id: int) -> Run | None:
    row = conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    return Run.from_row(row) if row else None


def latest(conn: sqlite3.Connection, kind: RunKind | str | None = None) -> Run | None:
    if kind is None:
        row = conn.execute(
            "SELECT * FROM run ORDER BY started_utc DESC, id DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM run WHERE kind = ? ORDER BY started_utc DESC, id DESC LIMIT 1",
            (str(RunKind(kind)),),
        ).fetchone()
    return Run.from_row(row) if row else None
