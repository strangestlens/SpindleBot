"""Repository for the `audio_presence` table. SQL only; caller owns the transaction."""
from __future__ import annotations

import sqlite3

from spindlebot.core.models import AudioPresence


def set_presence(
    conn: sqlite3.Connection,
    *,
    audio_id: int,
    location_id: int,
    present: bool,
    observed_utc: int,
    rel_path: str | None = None,
    file_sha256: str | None = None,
    byte_size: int | None = None,
    mtime: int | None = None,
) -> AudioPresence:
    """Record an observed presence fact for (audio_id, location_id). Upsert on PK."""
    conn.execute(
        """
        INSERT INTO audio_presence
            (audio_id, location_id, present, rel_path, file_sha256, byte_size,
             observed_utc, mtime)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(audio_id, location_id) DO UPDATE SET
            present = excluded.present,
            rel_path = excluded.rel_path,
            file_sha256 = excluded.file_sha256,
            byte_size = excluded.byte_size,
            observed_utc = excluded.observed_utc,
            -- Preserve a recorded mtime when the caller omits it (sync/prune
            -- write a new file_sha256 without an mtime); nulling it would force
            -- a needless full re-hash on the next inventory scan.
            mtime = COALESCE(excluded.mtime, audio_presence.mtime)
        """,
        (audio_id, location_id, int(present), rel_path, file_sha256, byte_size,
         observed_utc, mtime),
    )
    found = get(conn, audio_id, location_id)
    assert found is not None
    return found


def get(conn: sqlite3.Connection, audio_id: int, location_id: int) -> AudioPresence | None:
    row = conn.execute(
        "SELECT * FROM audio_presence WHERE audio_id = ? AND location_id = ?",
        (audio_id, location_id),
    ).fetchone()
    return AudioPresence.from_row(row) if row else None


def get_by_rel_path(
    conn: sqlite3.Connection, location_id: int, rel_path: str
) -> AudioPresence | None:
    """The live presence row for a given path at a location, if one is recorded.

    Keyed by (location_id, rel_path) rather than (audio_id, location_id) so a
    scanner can look a file up *before* hashing it (identity isn't known yet).
    That key is not unique — replaced content can leave a stale present=0 row at
    the same path — so this returns only the present, most-recently-observed row.
    """
    row = conn.execute(
        """
        SELECT * FROM audio_presence
        WHERE location_id = ? AND rel_path = ? AND present = 1
        ORDER BY observed_utc DESC
        LIMIT 1
        """,
        (location_id, rel_path),
    ).fetchone()
    return AudioPresence.from_row(row) if row else None


def find_present_by_rel_paths(
    conn: sqlite3.Connection, rel_paths: list[str]
) -> AudioPresence | None:
    """Most-recently-observed present presence row matching any of `rel_paths`,
    across *all* locations.

    Used to parent an orphan sidecar (a `.lrc` whose track was pruned from the
    scanned location) to the audio_content already recorded elsewhere: the caller
    passes the sidecar's canonical rel_path stem paired with each audio extension.
    Returns None when no such content exists in the DB — nothing to link against.

    Ambiguity guard: presence is keyed by (audio_id, location_id), not rel_path,
    so the same path can be present for more than one distinct audio_id (e.g. a
    track re-ripped to different decoded audio, present at that path on two
    locations). When the matches span multiple audio_ids there is no single
    correct parent, so this returns None rather than guessing.
    """
    if not rel_paths:
        return None
    placeholders = ",".join("?" * len(rel_paths))
    rows = conn.execute(
        f"""
        SELECT * FROM audio_presence
        WHERE present = 1 AND rel_path IN ({placeholders})
        ORDER BY observed_utc DESC
        """,
        rel_paths,
    ).fetchall()
    if not rows:
        return None
    presences = [AudioPresence.from_row(r) for r in rows]
    if len({p.audio_id for p in presences}) > 1:
        return None  # ambiguous: same path, multiple distinct contents
    return presences[0]


def list_for_location(
    conn: sqlite3.Connection, location_id: int, *, present: bool | None = None
) -> list[AudioPresence]:
    sql = "SELECT * FROM audio_presence WHERE location_id = ?"
    params: list = [location_id]
    if present is not None:
        sql += " AND present = ?"
        params.append(int(present))
    rows = conn.execute(sql + " ORDER BY audio_id", params).fetchall()
    return [AudioPresence.from_row(r) for r in rows]


def list_for_audio(conn: sqlite3.Connection, audio_id: int) -> list[AudioPresence]:
    """Every presence row for a content across all locations."""
    rows = conn.execute(
        "SELECT * FROM audio_presence WHERE audio_id = ? ORDER BY location_id",
        (audio_id,),
    ).fetchall()
    return [AudioPresence.from_row(r) for r in rows]


def count_retention_copies(conn: sqlite3.Connection, audio_id: int) -> int:
    """How many retention locations currently hold this content (the numcopies floor)."""
    return conn.execute(
        """
        SELECT COUNT(*)
        FROM audio_presence p
        JOIN location l ON l.id = p.location_id
        WHERE p.audio_id = ? AND p.present = 1 AND l.is_retention = 1
        """,
        (audio_id,),
    ).fetchone()[0]
