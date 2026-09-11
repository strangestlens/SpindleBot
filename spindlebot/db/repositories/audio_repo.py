"""Repository for the `audio_content` table. SQL only; caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.identity import ContentId
from spindlebot.core.models import AudioContent


def upsert(
    conn: sqlite3.Connection,
    content_id: ContentId,
    *,
    now: int,
    artist: str | None = None,
    album: str | None = None,
    title: str | None = None,
    disc_no: int | None = None,
    track_no: int | None = None,
    duration_s: int | None = None,
    beets_item_id: int | None = None,
) -> AudioContent:
    """Insert audio content by identity, or refresh advisory tags + last_seen_utc.

    first_seen_utc is preserved across updates; beets_item_id is only set, never
    cleared (a later scan without a beets id won't wipe an existing link).
    """
    conn.execute(
        """
        INSERT INTO audio_content
            (identity, identity_kind, artist, album, title,
             disc_no, track_no, duration_s, beets_item_id,
             first_seen_utc, last_seen_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(identity) DO UPDATE SET
            identity_kind = excluded.identity_kind,
            artist = excluded.artist,
            album = excluded.album,
            title = excluded.title,
            disc_no = excluded.disc_no,
            track_no = excluded.track_no,
            duration_s = excluded.duration_s,
            beets_item_id = COALESCE(excluded.beets_item_id, audio_content.beets_item_id),
            last_seen_utc = excluded.last_seen_utc
        """,
        (content_id.value, str(content_id.kind), artist, album, title,
         disc_no, track_no, duration_s, beets_item_id, now, now),
    )
    found = get_by_identity(conn, content_id.value)
    assert found is not None
    return found


def get_by_identity(conn: sqlite3.Connection, identity: str) -> AudioContent | None:
    row = conn.execute(
        "SELECT * FROM audio_content WHERE identity = ?", (identity,)
    ).fetchone()
    return AudioContent.from_row(row) if row else None


def get_by_id(conn: sqlite3.Connection, audio_id: int) -> AudioContent | None:
    row = conn.execute(
        "SELECT * FROM audio_content WHERE id = ?", (audio_id,)
    ).fetchone()
    return AudioContent.from_row(row) if row else None


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM audio_content").fetchone()[0]


def touch_last_seen(
    conn: sqlite3.Connection,
    *,
    audio_id: int,
    now: int,
    beets_item_id: int | None = None,
) -> None:
    """Refresh last_seen_utc without rewriting the advisory tags.

    For the incremental-rescan path: a file unchanged on (size, mtime) cannot
    have different tags, so re-reading them off disk is pure I/O for no new
    information — but the row is still being observed and must not go stale.
    beets_item_id keeps `upsert`'s COALESCE semantics: only ever set, never
    cleared by a scan that didn't find one.
    """
    conn.execute(
        """
        UPDATE audio_content
           SET last_seen_utc = ?,
               beets_item_id = COALESCE(?, beets_item_id)
         WHERE id = ?
        """,
        (now, beets_item_id, audio_id),
    )
