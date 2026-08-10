"""
Disc-detection helpers for music-import.sh.

CLI usage (called by shell scripts):
    python -m spindlebot.disc check <album_dir>   # print WAIT:have:need if incomplete
    python -m spindlebot.disc count <album_dir>   # print integer disc count
"""
from __future__ import annotations

import sys
from pathlib import Path

from spindlebot.core.albums import album_key

# All audio file extensions that beets can import.
# Used by disc.py, pretag.py, staging.py, and music-import.sh.
AUDIO_EXTENSIONS: frozenset[str] = frozenset({
    "flac", "mp3", "m4a", "aac", "ogg", "opus",
    "wav", "aif", "aiff", "wv", "ape", "wma",
})


def find_audio_files(album_dir: str | Path) -> list[Path]:
    """Return a sorted list of audio files directly inside album_dir."""
    album_dir = Path(album_dir)
    return sorted(
        p for p in album_dir.iterdir()
        if p.is_file() and p.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS
    )


def _parse_disc_tags(f) -> tuple[int, int]:
    """
    Return (discnumber, disctotal) from a mutagen file object.

    Handles both Vorbis comment style (FLAC, OGG, Opus) and ID3 style
    (MP3, M4A) gracefully, defaulting to (1, 1) if tags are absent or
    unreadable.
    """
    if f is None or not f.tags:
        return 1, 1

    tags = f.tags

    # Vorbis comment style: dict-like, lowercase keys, list values.
    # mutagen.flac.FLAC, mutagen.oggvorbis.OggVorbis, mutagen.opus.Opus all use this.
    vorbis_dt = tags.get("disctotal") or tags.get("totaldiscs")
    vorbis_dn = tags.get("discnumber") or tags.get("disc")
    if vorbis_dt is not None or vorbis_dn is not None:
        try:
            dt = int(str((vorbis_dt or ["1"])[0]).strip())
            dn = int(str((vorbis_dn or ["1"])[0]).strip())
            return dn, dt
        except (ValueError, IndexError, TypeError):
            return 1, 1

    # ID3 style (MP3, M4A via mutagen.id3): TPOS frame = "discnum/disctotal"
    tpos = tags.get("TPOS")
    if tpos is not None:
        try:
            raw = str(tpos).strip()
            parts = raw.split("/")
            dn = int(parts[0]) if parts[0].strip().isdigit() else 1
            dt = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 1
            return dn, dt
        except (ValueError, IndexError):
            return 1, 1

    return 1, 1


def _coerce_files(album_dir_or_files: str | Path | list[Path]) -> list[Path]:
    """Accept a directory (path) or an explicit list of audio files.

    A directory is expanded via find_audio_files; a list is used as-is. This
    lets callers scope disc logic to ONE album's files even when the Import
    directory is a flat mix of several albums' rips.
    """
    if isinstance(album_dir_or_files, (str, Path)):
        return find_audio_files(album_dir_or_files)
    return list(album_dir_or_files)


def _read_disc_tags(album_dir_or_files: str | Path | list[Path]) -> tuple[set[int], set[int]]:
    """Return (disc_totals, disc_numbers) sets read from the given audio files.

    Accepts either a directory (all its audio files) or an explicit file list.
    """
    import mutagen  # local import so the module loads without mutagen installed

    disc_totals: set[int] = set()
    disc_numbers: set[int] = set()

    for path in _coerce_files(album_dir_or_files):
        try:
            f = mutagen.File(str(path))
            dn, dt = _parse_disc_tags(f)
            disc_totals.add(dt)
            disc_numbers.add(dn)
        except Exception:
            pass

    return disc_totals, disc_numbers


def _read_album_tags(path: Path) -> tuple[str | None, str | None, str | None]:
    """Best-effort (albumartist, album, mb_albumid) for grouping. Never raises."""
    import mutagen  # local import so the module loads without mutagen installed

    try:
        mf = mutagen.File(str(path), easy=True)
    except Exception:
        return None, None, None
    if mf is None:
        return None, None, None

    def first(key):
        val = mf.get(key)
        return val[0] if val else None

    albumartist = first("albumartist") or first("artist")
    album = first("album")
    mb_albumid = first("musicbrainz_albumid")
    return albumartist, album, mb_albumid


def normalize_album_title(title: str) -> str:
    """Casefolded, whitespace-collapsed album title for log↔tag matching."""
    return " ".join(title.split()).casefold()


def parse_xld_log(log_path: str | Path) -> tuple[str, str] | None:
    """Return (artist, album) from an XLD log header, or None if unreadable.

    XLD writes the rip identity as a bare ``Artist / Album`` line in the header
    block, before the ``Key : Value`` settings section::

        X Lossless Decoder version 20250302 (157.2)

        XLD extraction logfile from 2026-08-09 22:46:09 -0400

        Pink Floyd / Animals (2018 remix)

    Parsed from the BODY rather than the filename on purpose: XLD substitutes
    lookalike characters for "/" and ":" when building filenames, so the stem
    does not round-trip to the tag value.
    """
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    for line in text.splitlines()[:20]:
        line = line.strip()
        if not line or " / " not in line:
            continue
        if line.startswith(("X Lossless", "XLD ")):
            continue
        # The settings block below the header is "Key : Value"; the identity
        # line has no such separator.
        if " : " in line:
            continue
        artist, _, album = line.partition(" / ")
        artist, album = artist.strip(), album.strip()
        if artist and album:
            return artist, album
    return None


def group_by_album(album_dir: str | Path) -> dict[str, list[Path]]:
    """Group a directory's audio files by deterministic album_key.

    Uses the same album_key() grouping used across the DB layer (mb_albumid →
    albumartist+album). Files whose tags are wholly unreadable/absent fall into
    a single group keyed by the empty-tags album_key, so an untagged pile still
    imports as one unit rather than being silently dropped. Preserves sorted
    file order within each group.
    """
    groups: dict[str, list[Path]] = {}
    for path in find_audio_files(album_dir):
        albumartist, album, mb_albumid = _read_album_tags(path)
        key = album_key(albumartist, album, mb_albumid)
        groups.setdefault(key, []).append(path)
    return groups


def check_wait(album_dir_or_files: str | Path | list[Path]) -> str | None:
    """
    Return "WAIT:have:need" if discs are missing, None if import can proceed.

    Holds only when every audio file agrees on disctotal > 1 and we don't
    have all discs yet.  If empty or tags are unreadable, returns None (let
    beets attempt the import rather than blocking forever).

    Accepts a directory or an explicit file list; when a mixed Import holds
    several albums, callers must pass ONE album's files so disc totals aren't
    conflated across albums.
    """
    disc_totals, disc_numbers = _read_disc_tags(album_dir_or_files)
    if not disc_totals:
        return None
    if len(disc_totals) == 1:
        disctotal = disc_totals.pop()
        if disctotal > 1 and len(disc_numbers) < disctotal:
            return f"WAIT:{len(disc_numbers)}:{disctotal}"
    return None


def count_discs(album_dir_or_files: str | Path | list[Path]) -> int:
    """Return the number of unique disc numbers across the given audio files.

    Accepts a directory or an explicit file list; pass ONE album's files so
    the count reflects that album, not everything present in a mixed Import.
    """
    _, disc_numbers = _read_disc_tags(album_dir_or_files)
    return len(disc_numbers) if disc_numbers else 1


def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv)[1:]
    if len(args) < 2:
        print("Usage: python -m spindlebot.disc check|count <album_dir>", file=sys.stderr)
        return 1

    command, album_dir = args[0], args[1]

    if command == "check":
        result = check_wait(album_dir)
        if result:
            print(result)
        return 0

    if command == "count":
        print(count_discs(album_dir))
        return 0

    print(f"Unknown command: {command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
