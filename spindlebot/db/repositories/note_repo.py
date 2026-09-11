"""Repository for `note`, `note_revision`, `note_tag`, `note_session`.

SQL only; the caller owns the transaction and commits.
"""
from __future__ import annotations

import sqlite3
from uuid import uuid4

from spindlebot.core.enums import NoteFormat, NoteStatus
from spindlebot.core.models import Note, NoteRevision, NoteSession
from spindlebot.core.notes import body_sha256, canonicalize_body


# ── sessions ─────────────────────────────────────────────────────────────────

def create_session(
    conn: sqlite3.Connection,
    *,
    occurred_utc: int,
    now: int,
    title: str | None = None,
) -> NoteSession:
    cur = conn.execute(
        "INSERT INTO note_session (uuid, occurred_utc, title, created_utc) "
        "VALUES (?, ?, ?, ?)",
        (str(uuid4()), occurred_utc, title, now),
    )
    found = get_session(conn, int(cur.lastrowid))
    assert found is not None
    return found


def get_session(conn: sqlite3.Connection, session_id: int) -> NoteSession | None:
    row = conn.execute(
        "SELECT * FROM note_session WHERE id = ?", (session_id,)
    ).fetchone()
    return NoteSession.from_row(row) if row else None


def list_sessions(
    conn: sqlite3.Connection, *, since_utc: int | None = None
) -> list[NoteSession]:
    """Most recent sitting first — a listening log reads backwards."""
    sql = "SELECT * FROM note_session"
    params: list[object] = []
    if since_utc is not None:
        sql += " WHERE occurred_utc >= ?"
        params.append(since_utc)
    sql += " ORDER BY occurred_utc DESC, id DESC"
    return [NoteSession.from_row(r) for r in conn.execute(sql, params).fetchall()]


# ── notes ────────────────────────────────────────────────────────────────────

def create(
    conn: sqlite3.Connection,
    *,
    subject_id: int,
    body: str,
    now: int,
    session_id: int | None = None,
    author: str | None = None,
    body_format: NoteFormat = NoteFormat.MARKDOWN,
) -> tuple[Note, NoteRevision]:
    """Create a note and its first revision together.

    A note with no revision has no body, which is not a state any reader should
    have to handle — so the pair is created in one call, inside the caller's
    transaction.
    """
    cur = conn.execute(
        "INSERT INTO note (uuid, subject_id, session_id, status, created_utc, updated_utc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid4()), subject_id, session_id, str(NoteStatus.ACTIVE), now, now),
    )
    note_id = int(cur.lastrowid)
    revision = _insert_revision(
        conn, note_id=note_id, seq=1, body=body, now=now,
        author=author, body_format=body_format,
    )
    found = get(conn, note_id)
    assert found is not None
    return found, revision


def append_revision(
    conn: sqlite3.Connection,
    *,
    note_id: int,
    body: str,
    now: int,
    author: str | None = None,
    body_format: NoteFormat = NoteFormat.MARKDOWN,
) -> tuple[NoteRevision, bool]:
    """Append the next revision. Returns (revision, changed).

    A body whose canonicalized hash matches the current head is a NO-OP: the
    head is returned with changed=False and nothing is written. Opening a note
    in $EDITOR and closing it unchanged must not grow the chain, and neither
    must an editor that rewrote line endings on the way out.
    """
    head_revision = head(conn, note_id)
    assert head_revision is not None, f"note {note_id} has no revisions"
    if head_revision.sha256 == body_sha256(body):
        return head_revision, False

    revision = _insert_revision(
        conn, note_id=note_id, seq=head_revision.seq + 1, body=body, now=now,
        author=author, body_format=body_format,
    )
    conn.execute("UPDATE note SET updated_utc = ? WHERE id = ?", (now, note_id))
    return revision, True


def _insert_revision(
    conn: sqlite3.Connection,
    *,
    note_id: int,
    seq: int,
    body: str,
    now: int,
    author: str | None,
    body_format: NoteFormat,
) -> NoteRevision:
    """Insert one revision, storing the CANONICAL body.

    The stored text and its hash are canonicalized together — storing raw bytes
    while hashing canonical ones would make the no-op check above disagree with
    what a reader actually sees.
    """
    canonical = canonicalize_body(body)
    cur = conn.execute(
        "INSERT INTO note_revision "
        "(note_id, seq, body, body_format, sha256, author, created_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (note_id, seq, canonical, str(body_format), body_sha256(canonical), author, now),
    )
    row = conn.execute(
        "SELECT * FROM note_revision WHERE id = ?", (int(cur.lastrowid),)
    ).fetchone()
    return NoteRevision.from_row(row)


def head(conn: sqlite3.Connection, note_id: int) -> NoteRevision | None:
    """The current body: MAX(seq). There is no head-pointer column."""
    row = conn.execute(
        "SELECT * FROM note_revision WHERE note_id = ? ORDER BY seq DESC LIMIT 1",
        (note_id,),
    ).fetchone()
    return NoteRevision.from_row(row) if row else None


def list_revisions(conn: sqlite3.Connection, note_id: int) -> list[NoteRevision]:
    """Full history, oldest first."""
    rows = conn.execute(
        "SELECT * FROM note_revision WHERE note_id = ? ORDER BY seq", (note_id,)
    ).fetchall()
    return [NoteRevision.from_row(r) for r in rows]


def get(conn: sqlite3.Connection, note_id: int) -> Note | None:
    row = conn.execute("SELECT * FROM note WHERE id = ?", (note_id,)).fetchone()
    return Note.from_row(row) if row else None


def get_by_uuid(conn: sqlite3.Connection, uuid: str) -> Note | None:
    row = conn.execute("SELECT * FROM note WHERE uuid = ?", (uuid,)).fetchone()
    return Note.from_row(row) if row else None


def set_status(
    conn: sqlite3.Connection, note_id: int, status: NoteStatus, now: int
) -> None:
    """Soft delete and its undo. Revisions are untouched either way."""
    conn.execute(
        "UPDATE note SET status = ?, updated_utc = ? WHERE id = ?",
        (str(status), now, note_id),
    )


def list_notes(
    conn: sqlite3.Connection,
    *,
    subject_ids: list[int] | None = None,
    session_id: int | None = None,
    tag: str | None = None,
    since_utc: int | None = None,
    status: NoteStatus | None = NoteStatus.ACTIVE,
) -> list[Note]:
    """Filtered notes, most recently written first.

    `status=None` includes soft-deleted notes; the default hides them. An EMPTY
    `subject_ids` list means "no subjects matched" and returns nothing — it is
    not the same as None ("don't filter by subject"), and conflating the two
    would turn a failed lookup into the whole corpus.
    """
    if subject_ids is not None and not subject_ids:
        return []

    sql = "SELECT n.* FROM note n"
    where: list[str] = []
    params: list[object] = []

    if tag is not None:
        sql += " JOIN note_tag t ON t.note_id = n.id"
        where.append("t.tag = ?")
        params.append(tag)
    if subject_ids is not None:
        where.append(f"n.subject_id IN ({','.join('?' * len(subject_ids))})")
        params.extend(subject_ids)
    if session_id is not None:
        where.append("n.session_id = ?")
        params.append(session_id)
    if since_utc is not None:
        where.append("n.created_utc >= ?")
        params.append(since_utc)
    if status is not None:
        where.append("n.status = ?")
        params.append(str(status))

    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY n.created_utc DESC, n.id DESC"
    return [Note.from_row(r) for r in conn.execute(sql, params).fetchall()]


# ── tags ─────────────────────────────────────────────────────────────────────

def add_tags(conn: sqlite3.Connection, note_id: int, tags: list[str]) -> None:
    """Attach tags. Idempotent; blank tags are dropped rather than stored."""
    conn.executemany(
        "INSERT OR IGNORE INTO note_tag (note_id, tag) VALUES (?, ?)",
        [(note_id, t.strip()) for t in tags if t.strip()],
    )


def remove_tag(conn: sqlite3.Connection, note_id: int, tag: str) -> None:
    conn.execute(
        "DELETE FROM note_tag WHERE note_id = ? AND tag = ?", (note_id, tag)
    )


def list_tags(conn: sqlite3.Connection, note_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT tag FROM note_tag WHERE note_id = ? ORDER BY tag", (note_id,)
    ).fetchall()
    return [r[0] for r in rows]


def all_tags(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Every tag in use with its note count, most used first."""
    rows = conn.execute(
        "SELECT t.tag, COUNT(*) AS n FROM note_tag t "
        "JOIN note n ON n.id = t.note_id AND n.status = ? "
        "GROUP BY t.tag ORDER BY n DESC, t.tag",
        (str(NoteStatus.ACTIVE),),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]
