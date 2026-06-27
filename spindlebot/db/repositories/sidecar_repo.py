"""Repository for `sidecar_content`. SQL only; the caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.enums import SidecarParentKind, SidecarRole
from spindlebot.core.models import SidecarContent


def upsert(
    conn: sqlite3.Connection,
    *,
    parent_kind: SidecarParentKind | str,
    parent_id: int,
    role: SidecarRole | str,
    sha256: str,
    now: int,
) -> SidecarContent:
    """Insert a sidecar by its (parent_kind, parent_id, role) triple, or refresh
    its content hash + last_seen_utc. first_seen_utc is preserved across updates.
    """
    conn.execute(
        """
        INSERT INTO sidecar_content
            (parent_kind, parent_id, role, sha256, first_seen_utc, last_seen_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(parent_kind, parent_id, role) DO UPDATE SET
            sha256 = excluded.sha256,
            last_seen_utc = excluded.last_seen_utc
        """,
        (str(SidecarParentKind(parent_kind)), parent_id, str(SidecarRole(role)),
         sha256, now, now),
    )
    found = get(conn, parent_kind=parent_kind, parent_id=parent_id, role=role)
    assert found is not None
    return found


def get(
    conn: sqlite3.Connection,
    *,
    parent_kind: SidecarParentKind | str,
    parent_id: int,
    role: SidecarRole | str,
) -> SidecarContent | None:
    row = conn.execute(
        "SELECT * FROM sidecar_content "
        "WHERE parent_kind = ? AND parent_id = ? AND role = ?",
        (str(SidecarParentKind(parent_kind)), parent_id, str(SidecarRole(role))),
    ).fetchone()
    return SidecarContent.from_row(row) if row else None


def get_by_id(conn: sqlite3.Connection, sidecar_id: int) -> SidecarContent | None:
    row = conn.execute(
        "SELECT * FROM sidecar_content WHERE id = ?", (sidecar_id,)
    ).fetchone()
    return SidecarContent.from_row(row) if row else None


def list_for_parent(
    conn: sqlite3.Connection,
    *,
    parent_kind: SidecarParentKind | str,
    parent_id: int,
) -> list[SidecarContent]:
    rows = conn.execute(
        "SELECT * FROM sidecar_content WHERE parent_kind = ? AND parent_id = ? "
        "ORDER BY role",
        (str(SidecarParentKind(parent_kind)), parent_id),
    ).fetchall()
    return [SidecarContent.from_row(r) for r in rows]


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM sidecar_content").fetchone()[0]
