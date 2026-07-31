"""Read the digital library as a flat list of albums.

Two backends behind one signature. **beets is the default**, because the audit
asks "have I ever ripped this?" — a question about the whole library regardless
of which location holds the bytes today. The SpindleBot `album` table answers a
narrower question: what `inventory` has scanned. It becomes the better source
once inventory covers every location, so it is available as `--index db`.
"""
from __future__ import annotations

import subprocess

from spindlebot.core.collection import LibraryAlbum

# ASCII unit separator: cannot occur in a tag, so no album title can split a row.
FIELD_SEP = "\x1f"
BEETS_FORMAT = FIELD_SEP.join(
    ("$albumartist", "$album", "$year", "$mb_albumid")
)


def _year(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) if raw.isdigit() and raw != "0" else None


def parse_beets_output(stdout: str) -> list[LibraryAlbum]:
    """Parse `beet ls -a -f ...` output into albums. Pure."""
    albums = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(FIELD_SEP)
        if len(parts) < 4:
            continue
        artist, album, year, mbid = (p.strip() for p in parts[:4])
        if not album:
            continue
        albums.append(LibraryAlbum(
            albumartist=artist,
            album=album,
            year=_year(year),
            mb_albumid=mbid or None,
        ))
    return albums


def from_beets(cfg, *, runner=subprocess.run) -> list[LibraryAlbum]:
    """Every album in the beets library."""
    proc = runner(
        [str(cfg.tools.beet), "ls", "-a", "-f", BEETS_FORMAT],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"beet ls failed (exit {proc.returncode}): {(proc.stderr or '').strip()}"
        )
    return parse_beets_output(proc.stdout)


def from_db(cfg) -> list[LibraryAlbum]:
    """Every album the SpindleBot DB has inventoried."""
    from spindlebot.db.connection import open_db
    from spindlebot.db.repositories import album_repo

    conn = open_db(cfg.core.db_path)
    try:
        return [
            LibraryAlbum(
                albumartist=a.albumartist or "",
                album=a.album or "",
                mb_albumid=a.mb_albumid,
            )
            for a in album_repo.list_all(conn)
            if a.album
        ]
    finally:
        conn.close()


def load(cfg, index: str = "beets") -> list[LibraryAlbum]:
    if index == "beets":
        return from_beets(cfg)
    if index == "db":
        return from_db(cfg)
    raise ValueError(f"unknown library index {index!r} (known: beets, db)")
