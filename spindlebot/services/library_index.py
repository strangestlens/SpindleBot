"""Read the digital library as a flat list of albums.

Two backends, and by default **both**. Each is only a partial view:

  beets  every album it has imported and still tracks locally.
  db     every album `spindlebot inventory` has seen at any location.

Neither is a superset of the other in practice. Measured on a real library:
beets held 112 albums, the SpindleBot DB held 177, and 67 albums existed only in
the DB — albums inventoried at a location beets no longer has a row for. Picking
either one alone silently inflates the missing list, which is the one failure
this feature cannot afford. So `auto` unions them: an album counts as owned if
*either* index knows it. A union can only ever reduce false "missing" reports.

`--index beets` / `--index db` remain, for when you want to ask about one.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from spindlebot.core.collection import LibraryAlbum

# ASCII unit separator: cannot occur in a tag, so no album title can split a row.
FIELD_SEP = "\x1f"
BEETS_FORMAT = FIELD_SEP.join(
    ("$albumartist", "$album", "$year", "$mb_albumid")
)

KNOWN_INDEXES = ("auto", "beets", "db")


@dataclass(frozen=True)
class LibraryIndex:
    """The library, plus where each part of it came from.

    `counts` and `errors` are reported to the user: when an audit says an album
    is missing, the first question is "which index was consulted?", and a silent
    zero-album source is exactly how this goes wrong.
    """
    albums: list[LibraryAlbum]
    counts: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.albums)


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
    try:
        proc = runner(
            [str(cfg.tools.beet), "ls", "-a", "-f", BEETS_FORMAT],
            capture_output=True, text=True,
        )
    except OSError as e:
        raise RuntimeError(f"could not run {cfg.tools.beet}: {e}") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"beet ls failed (exit {proc.returncode}): {(proc.stderr or '').strip()}"
        )
    return parse_beets_output(proc.stdout)


def from_db(cfg) -> list[LibraryAlbum]:
    """Every album the SpindleBot DB has inventoried.

    Refuses to create the database. `open_db` would happily bring an empty one
    into existence, and an empty index reports the entire collection as missing
    — a confidently wrong answer is worse than a loud failure.
    """
    from spindlebot.db.connection import open_db
    from spindlebot.db.repositories import album_repo

    if not cfg.core.db_path.exists():
        raise RuntimeError(
            f"no SpindleBot DB at {cfg.core.db_path} — run `spindlebot inventory` "
            "first, or use --index beets"
        )

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


def _dedupe(albums: list[LibraryAlbum]) -> list[LibraryAlbum]:
    """Collapse the SAME album reported by both indexes, keeping distinct releases.

    The point of this function is that beets and the SpindleBot DB both know an
    album and it should appear once. Keying on artist+title alone overshot: two
    genuinely different releases — an original and a reissue, same artist, same
    title, different `mb_albumid` — collapsed into whichever row came first.

    That silently disabled `note add`'s release disambiguation downstream, which
    can only refuse to guess between editions it can actually SEE. The audit has
    the same interest: reporting one row for two owned pressings understates the
    collection.

    So: group by artist+title, and within a group keep one row per distinct
    MBID. A row with no MBID is only kept when the group has no identified
    release at all — that is the beets-vs-DB overlap this exists to collapse.
    """
    groups: dict[tuple[str, str], dict[str, LibraryAlbum]] = {}
    for album in albums:
        key = (album.albumartist.casefold(), album.album.casefold())
        by_release = groups.setdefault(key, {})
        by_release.setdefault(album.mb_albumid or "", album)

    out: list[LibraryAlbum] = []
    for by_release in groups.values():
        identified = [a for mbid, a in by_release.items() if mbid]
        out.extend(identified or list(by_release.values()))
    return out


LOADERS = {"beets": from_beets, "db": from_db}


def load(cfg, index: str = "auto") -> LibraryIndex:
    """Load the library from one backend, or the union of both."""
    if index not in KNOWN_INDEXES:
        raise ValueError(
            f"unknown library index {index!r} (known: {', '.join(KNOWN_INDEXES)})"
        )

    if index != "auto":
        albums = LOADERS[index](cfg)
        return LibraryIndex(albums=albums, counts={index: len(albums)})

    collected: list[LibraryAlbum] = []
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    for name, loader in LOADERS.items():
        try:
            albums = loader(cfg)
        except (RuntimeError, OSError) as e:
            errors[name] = str(e)
            continue
        counts[name] = len(albums)
        collected.extend(albums)

    if not collected:
        detail = "; ".join(f"{k}: {v}" for k, v in errors.items()) or "both are empty"
        raise RuntimeError(
            f"no albums from any library index ({detail}). Refusing to report the "
            "whole collection as missing — run `beet import` or `spindlebot inventory`."
        )
    return LibraryIndex(albums=_dedupe(collected), counts=counts, errors=errors)
