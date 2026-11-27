"""Repository for `pending_action`. SQL only; the caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.enums import ActionKind, ContentKind
from spindlebot.core.models import PendingAction
from spindlebot.core.paths import path_key


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
    expected_sha256: str | None = None,
    source_rel_path: str | None = None,
    dest_must_be_absent: bool = False,
) -> PendingAction:
    """Record a proposed (unacknowledged, unexecuted) action; return the row."""
    policy = conn.execute(
        "SELECT path_case_sensitive FROM location WHERE id = ?", (dest_location_id,),
    ).fetchone()
    sensitive = bool(policy[0]) if policy is not None and policy[0] is not None else None
    cur = conn.execute(
        """
        INSERT INTO pending_action
            (run_id, action_kind, content_kind, content_id,
             source_location_id, dest_location_id, rel_path, reason, created_utc,
             expected_sha256, source_rel_path, dest_must_be_absent, dest_path_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, str(ActionKind(action_kind)), str(ContentKind(content_kind)),
         content_id, source_location_id, dest_location_id, rel_path, reason, now,
         expected_sha256, source_rel_path, int(dest_must_be_absent),
         path_key(rel_path, case_sensitive=sensitive)),
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
    # The uncorrelated scalar subquery is evaluated once per UPDATE, under its
    # write transaction: each acknowledgement batch gets one monotonic ordinal.
    cur = conn.execute(
        f"UPDATE pending_action SET acknowledged = 1, acknowledged_utc = ?, "
        f"approval_order = (SELECT COALESCE(MAX(approval_order), 0) + 1 FROM pending_action) "
        f"WHERE acknowledged = 0 AND superseded_utc IS NULL AND id IN ({placeholders})",
        [now, *action_ids],
    )
    count = cur.rowcount
    _supersede_for_acknowledgement(conn, f"id IN ({placeholders})", action_ids, now)
    return count


def acknowledge_run(conn: sqlite3.Connection, run_id: int, now: int) -> int:
    """Acknowledge every outstanding action in a run (batch). Returns count."""
    cur = conn.execute(
        "UPDATE pending_action SET acknowledged = 1, acknowledged_utc = ?, "
        "approval_order = (SELECT COALESCE(MAX(approval_order), 0) + 1 FROM pending_action) "
        "WHERE run_id = ? AND acknowledged = 0 AND superseded_utc IS NULL",
        (now, run_id),
    )
    count = cur.rowcount
    _supersede_for_acknowledgement(conn, "run_id = ?", [run_id], now)
    return count


def _supersede_for_acknowledgement(
    conn: sqlite3.Connection, clause: str, params: list[int], now: int,
) -> None:
    # Scope normalization to the destinations explicitly acknowledged here.
    # clause is SQL assembled only by the two repository callers above.
    destinations = conn.execute(
        "SELECT DISTINCT dest_location_id FROM pending_action "
        "WHERE acknowledged = 1 AND action_kind = ? "
        "AND dest_location_id IS NOT NULL AND " + clause,
        [str(ActionKind.COPY), *params],
    ).fetchall()
    for row in destinations:
        supersede_weaker_copies(conn, now=now, dest_location_id=row[0])


def prepare_destination_keys(conn: sqlite3.Connection, *, dest_location_id: int) -> None:
    """Rebuild missing derived keys; original approved paths are untouched."""
    # Acquire the caller's write transaction before reading the policy, so a
    # concurrent policy change cannot be followed by keys made from old data.
    conn.execute(
        "UPDATE pending_action SET dest_path_key = dest_path_key "
        "WHERE dest_location_id = ? AND dest_path_key IS NULL AND rel_path IS NOT NULL",
        (dest_location_id,),
    )
    rows = conn.execute(
        "SELECT a.id, a.rel_path, l.path_case_sensitive FROM pending_action a "
        "JOIN location l ON l.id = a.dest_location_id "
        "WHERE a.dest_location_id = ? AND a.dest_path_key IS NULL AND a.rel_path IS NOT NULL",
        (dest_location_id,),
    ).fetchall()
    conn.executemany(
        "UPDATE pending_action SET dest_path_key = ? WHERE id = ?",
        [(path_key(row["rel_path"], case_sensitive=(bool(row["path_case_sensitive"])
                   if row["path_case_sensitive"] is not None else None)), row["id"])
         for row in rows],
    )


def supersede_weaker_copies(
    conn: sqlite3.Connection, *, now: int, dest_location_id: int | None = None,
    action_id: int | None = None,
) -> int:
    """Retire weaker COPY approvals protected by a later restoration approval.

    Original plan/approval values stay unchanged. The approval ordinal, not
    creation IDs or run timestamps, orders events; ties/unknowns favor safety.
    Completed restorations still protect
    their paths; purging replacement history cannot reactivate a retired row.
    Also called by the executor for mixed queues predating this lifecycle.
    """
    if action_id is not None and dest_location_id is None:
        row = conn.execute("SELECT dest_location_id FROM pending_action WHERE id = ?",
                           (action_id,)).fetchone()
        if row is None or row[0] is None:
            return 0
        dest_location_id = row[0]
    if dest_location_id is None:
        destinations = conn.execute(
            "SELECT DISTINCT dest_location_id FROM pending_action "
            "WHERE acknowledged = 1 AND executed_utc IS NULL AND superseded_utc IS NULL "
            "AND action_kind = ? AND dest_must_be_absent = 0 AND dest_location_id IS NOT NULL",
            (str(ActionKind.COPY),),
        ).fetchall()
        for row in destinations:
            prepare_destination_keys(conn, dest_location_id=row[0])
    else:
        prepare_destination_keys(conn, dest_location_id=dest_location_id)
    replacement = (
        "FROM pending_action AS newer WHERE newer.action_kind = ? "
        "AND newer.acknowledged = 1 AND newer.dest_must_be_absent = 1 "
        "AND newer.superseded_utc IS NULL "
        "AND newer.dest_location_id = older.dest_location_id "
        "AND newer.dest_path_key = older.dest_path_key "
        "AND (newer.approval_order IS NULL OR older.approval_order IS NULL "
        "OR newer.approval_order >= older.approval_order)"
    )
    sql = (
        "UPDATE pending_action AS older SET superseded_utc = ?, "
        "superseded_by_action_id = (SELECT newer.id " + replacement +
        " ORDER BY newer.approval_order DESC, newer.id DESC LIMIT 1) "
        "WHERE older.action_kind = ? AND older.acknowledged = 1 "
        "AND older.executed_utc IS NULL AND older.superseded_utc IS NULL "
        "AND older.dest_must_be_absent = 0 "
        "AND EXISTS (SELECT 1 " + replacement + ")"
    )
    copy = str(ActionKind.COPY)
    params: list = [now, copy, copy, copy]
    if dest_location_id is not None:
        sql += " AND older.dest_location_id = ?"
        params.append(dest_location_id)
    if action_id is not None:
        sql += " AND older.id = ?"
        params.append(action_id)
    return conn.execute(sql, params).rowcount


def has_potential_restoration(conn: sqlite3.Connection, action_id: int) -> bool:
    """Fail-closed check when mounted destination path semantics are unknown."""
    row = conn.execute(
        "SELECT 1 FROM pending_action older JOIN pending_action newer "
        "ON newer.dest_location_id = older.dest_location_id "
        "WHERE older.id = ? AND newer.action_kind = ? AND newer.acknowledged = 1 "
        "AND newer.dest_must_be_absent = 1 AND newer.superseded_utc IS NULL "
        "AND (newer.approval_order IS NULL OR older.approval_order IS NULL "
        "OR newer.approval_order >= older.approval_order) LIMIT 1",
        (action_id, str(ActionKind.COPY)),
    ).fetchone()
    return row is not None


def list_pending_execution(
    conn: sqlite3.Connection,
    *,
    action_kind: ActionKind | str | None = None,
    dest_location_id: int | None = None,
) -> list[PendingAction]:
    """Active acknowledged actions, oldest first — the executor's work queue.

    Executed and superseded rows remain history, excluded here. Optionally
    filtered to one action_kind and/or destination.
    """
    sql = ("SELECT * FROM pending_action "
           "WHERE acknowledged = 1 AND executed_utc IS NULL AND superseded_utc IS NULL")
    params: list = []
    if action_kind is not None:
        sql += " AND action_kind = ?"
        params.append(str(ActionKind(action_kind)))
    if dest_location_id is not None:
        sql += " AND dest_location_id = ?"
        params.append(dest_location_id)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [PendingAction.from_row(r) for r in rows]


def lock_copy_for_execution(conn: sqlite3.Connection, action_id: int) -> bool:
    """Recheck eligibility and hold the caller's write transaction through COPY.

    Checkpoints release the previous action's transaction. This no-op UPDATE
    reacquires it, so another approval cannot supersede the action between its
    eligibility check and the transfer. No plan or approval field changes.
    """
    cur = conn.execute(
        "UPDATE pending_action SET acknowledged = acknowledged "
        "WHERE id = ? AND action_kind = ? AND acknowledged = 1 "
        "AND executed_utc IS NULL AND superseded_utc IS NULL",
        (action_id, str(ActionKind.COPY)),
    )
    if cur.rowcount and not conn.in_transaction:
        raise sqlite3.ProgrammingError("COPY execution requires a caller-owned transaction")
    return bool(cur.rowcount)


def mark_executed(conn: sqlite3.Connection, action_id: int, now: int) -> None:
    conn.execute(
        "UPDATE pending_action SET executed_utc = ? WHERE id = ?",
        (now, action_id),
    )
