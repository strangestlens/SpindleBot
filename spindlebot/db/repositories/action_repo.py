"""Repository for `pending_action`. SQL only; the caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.enums import ActionKind, ContentKind
from spindlebot.core.models import PendingAction


def add(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    action_kind: ActionKind | str,
    content_kind: ContentKind | str,
    content_id: int,
    now: int,
    source_location_id: int | None = None,
    dest_location_id: int | None = None,
    rel_path: str | None = None,
    reason: str | None = None,
) -> PendingAction:
    """Record a proposed (unacknowledged, unexecuted) action; return the row."""
    cur = conn.execute(
        """
        INSERT INTO pending_action
            (run_id, action_kind, content_kind, content_id,
             source_location_id, dest_location_id, rel_path, reason, created_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, str(ActionKind(action_kind)), str(ContentKind(content_kind)),
         content_id, source_location_id, dest_location_id, rel_path, reason, now),
    )
    found = get(conn, int(cur.lastrowid))
    assert found is not None
    return found


def get(conn: sqlite3.Connection, action_id: int) -> PendingAction | None:
    row = conn.execute(
        "SELECT * FROM pending_action WHERE id = ?", (action_id,)
    ).fetchone()
    return PendingAction.from_row(row) if row else None


def list_for_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    acknowledged: bool | None = None,
    executed: bool | None = None,
) -> list[PendingAction]:
    sql = "SELECT * FROM pending_action WHERE run_id = ?"
    params: list = [run_id]
    if acknowledged is not None:
        sql += " AND acknowledged = ?"
        params.append(int(acknowledged))
    if executed is not None:
        sql += " AND executed_utc IS NOT NULL" if executed else " AND executed_utc IS NULL"
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [PendingAction.from_row(r) for r in rows]


def acknowledge(conn: sqlite3.Connection, action_ids: list[int], now: int) -> int:
    """Acknowledge specific actions (idempotent). Returns rows newly acknowledged."""
    if not action_ids:
        return 0
    placeholders = ",".join("?" for _ in action_ids)
    cur = conn.execute(
        f"UPDATE pending_action SET acknowledged = 1, acknowledged_utc = ? "
        f"WHERE acknowledged = 0 AND id IN ({placeholders})",
        [now, *action_ids],
    )
    return cur.rowcount


def acknowledge_run(conn: sqlite3.Connection, run_id: int, now: int) -> int:
    """Acknowledge every outstanding action in a run (batch). Returns count."""
    cur = conn.execute(
        "UPDATE pending_action SET acknowledged = 1, acknowledged_utc = ? "
        "WHERE run_id = ? AND acknowledged = 0",
        (now, run_id),
    )
    return cur.rowcount


def list_pending_execution(
    conn: sqlite3.Connection,
    *,
    action_kind: ActionKind | str | None = None,
    dest_location_id: int | None = None,
) -> list[PendingAction]:
    """Acknowledged-but-not-yet-executed actions, oldest first — the executor's
    work queue. Optionally filtered to one action_kind and/or destination."""
    sql = ("SELECT * FROM pending_action "
           "WHERE acknowledged = 1 AND executed_utc IS NULL")
    params: list = []
    if action_kind is not None:
        sql += " AND action_kind = ?"
        params.append(str(ActionKind(action_kind)))
    if dest_location_id is not None:
        sql += " AND dest_location_id = ?"
        params.append(dest_location_id)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [PendingAction.from_row(r) for r in rows]


def mark_executed(conn: sqlite3.Connection, action_id: int, now: int) -> None:
    conn.execute(
        "UPDATE pending_action SET executed_utc = ? WHERE id = ?",
        (now, action_id),
    )
