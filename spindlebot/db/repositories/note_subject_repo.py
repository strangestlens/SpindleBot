"""Repository for `note_subject`. SQL only; the caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.enums import NoteSubjectKind
from spindlebot.core.models import NoteSubject
from spindlebot.core.notes import NoteSubjectRef


def upsert(conn: sqlite3.Connection, ref: NoteSubjectRef, now: int) -> NoteSubject:
    """Get-or-create the subject for `ref`, refreshing its display fields.

    created_utc is preserved across calls, and the display columns are only
    ever SET, never cleared: a later note added with less context (an album
    title but no artist) must not blank the name an earlier note supplied.

    NULLIF is load-bearing, not belt-and-braces. COALESCE alone treats only NULL
    as missing, and an EMPTY string is reachable in practice: `beet ls` emits a
    blank `$albumartist` for an untagged album, `parse_beets_output` keeps it,
    and resolution passes it straight through — so a single such album would
    blank a good artist name that an earlier note had supplied.
    """
    conn.execute(
        """
        INSERT INTO note_subject
            (kind, subject_key, artist_name, album_title, track_title, mbid, created_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind, subject_key) DO UPDATE SET
            artist_name = COALESCE(NULLIF(excluded.artist_name, ''), note_subject.artist_name),
            album_title = COALESCE(NULLIF(excluded.album_title, ''), note_subject.album_title),
            track_title = COALESCE(NULLIF(excluded.track_title, ''), note_subject.track_title),
            mbid        = COALESCE(NULLIF(excluded.mbid, ''), note_subject.mbid)
        """,
        (
            str(ref.kind), ref.subject_key, ref.artist_name,
            ref.album_title, ref.track_title, ref.mbid, now,
        ),
    )
    found = get(conn, ref.kind, ref.subject_key)
    assert found is not None
    return found


def get(
    conn: sqlite3.Connection, kind: NoteSubjectKind, subject_key: str
) -> NoteSubject | None:
    row = conn.execute(
        "SELECT * FROM note_subject WHERE kind = ? AND subject_key = ?",
        (str(kind), subject_key),
    ).fetchone()
    return NoteSubject.from_row(row) if row else None


def get_by_id(conn: sqlite3.Connection, subject_id: int) -> NoteSubject | None:
    row = conn.execute(
        "SELECT * FROM note_subject WHERE id = ?", (subject_id,)
    ).fetchone()
    return NoteSubject.from_row(row) if row else None


def list_all(
    conn: sqlite3.Connection, kind: NoteSubjectKind | None = None
) -> list[NoteSubject]:
    """Every subject, ordered for display. Optionally one kind."""
    sql = "SELECT * FROM note_subject"
    params: list[object] = []
    if kind is not None:
        sql += " WHERE kind = ?"
        params.append(str(kind))
    sql += (" ORDER BY artist_name COLLATE NOCASE, album_title COLLATE NOCASE, "
            "track_title COLLATE NOCASE")
    return [NoteSubject.from_row(r) for r in conn.execute(sql, params).fetchall()]


def rekey(conn: sqlite3.Connection, subject_id: int, subject_key: str) -> NoteSubject:
    """Re-point an existing subject at a new key, keeping its notes.

    Used when a work's identity sharpens — a record noted before it was ripped is
    keyed by name, and gains a MusicBrainz-backed key once it is in the library.
    Re-keying the subject keeps the earlier notes attached to the work instead of
    leaving them stranded on a subject nothing resolves to any more.

    The caller must have established that `subject_key` is free; UNIQUE(kind,
    subject_key) fails loudly rather than silently merging two subjects.
    """
    conn.execute(
        "UPDATE note_subject SET subject_key = ? WHERE id = ?", (subject_key, subject_id)
    )
    found = get_by_id(conn, subject_id)
    assert found is not None
    return found
