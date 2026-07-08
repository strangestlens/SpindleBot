"""Repository for `album` + `album_track`. SQL only; the caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.models import Album


def upsert(
    conn: sqlite3.Connection,
    *,
    album_key: str,
    now: int,
    albumartist: str | None = None,
    album: str | None = None,
    mb_albumid: str | None = None,
) -> Album:
    """Insert an album by album_key, or refresh advisory tags + last_seen_utc.

    first_seen_utc is preserved across updates; mb_albumid is only set, never
    cleared (a later scan without an mb id won't wipe an existing one).
    """
    conn.execute(
        """
        INSERT INTO album
            (album_key, albumartist, album, mb_albumid, first_seen_utc, last_seen_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(album_key) DO UPDATE SET
            albumartist = excluded.albumartist,
            album = excluded.album,
            mb_albumid = COALESCE(excluded.mb_albumid, album.mb_albumid),
            last_seen_utc = excluded.last_seen_utc
        """,
        (album_key, albumartist, album, mb_albumid, now, now),
    )
    found = get_by_key(conn, album_key)
    assert found is not None
    return found


def link_track(conn: sqlite3.Connection, album_id: int, audio_id: int) -> None:
    """Associate an audio track with an album. Idempotent."""
    conn.execute(
        "INSERT OR IGNORE INTO album_track (album_id, audio_id) VALUES (?, ?)",
        (album_id, audio_id),
    )


def get_by_key(conn: sqlite3.Connection, album_key: str) -> Album | None:
    row = conn.execute(
        "SELECT * FROM album WHERE album_key = ?", (album_key,)
    ).fetchone()
    return Album.from_row(row) if row else None


def get_by_id(conn: sqlite3.Connection, album_id: int) -> Album | None:
    row = conn.execute("SELECT * FROM album WHERE id = ?", (album_id,)).fetchone()
    return Album.from_row(row) if row else None


def list_track_ids(conn: sqlite3.Connection, album_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT audio_id FROM album_track WHERE album_id = ? ORDER BY audio_id",
        (album_id,),
    ).fetchall()
    return [r[0] for r in rows]


def list_present_track_paths(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """(rel_path, album_id) for every present track across all locations.

    Lets inventory resolve an album-level orphan sidecar (cover.jpg / bare .nolrc
    whose album's audio was pruned from the scanned location) to the album already
    recorded from a prior inventory elsewhere, by matching the sidecar's directory
    against the directory of that album's tracks.
    """
    rows = conn.execute(
        """
        SELECT p.rel_path, at.album_id
        FROM audio_presence p
        JOIN album_track at ON at.audio_id = p.audio_id
        WHERE p.present = 1 AND p.rel_path IS NOT NULL
        """
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM album").fetchone()[0]
