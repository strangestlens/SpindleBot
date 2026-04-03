"""
Disc-detection helpers for music-import.sh.

CLI usage (called by shell scripts):
    python -m spindlebot.disc check <album_dir>   # print WAIT:have:need if incomplete
    python -m spindlebot.disc count <album_dir>   # print integer disc count
"""
from __future__ import annotations

import sys
from pathlib import Path

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


def _read_disc_tags(album_dir: str) -> tuple[set[int], set[int]]:
    """Return (disc_totals, disc_numbers) sets read from all audio files in album_dir."""
    import mutagen  # local import so the module loads without mutagen installed

    disc_totals: set[int] = set()
    disc_numbers: set[int] = set()

    for path in find_audio_files(album_dir):
        try:
            f = mutagen.File(str(path))
            dn, dt = _parse_disc_tags(f)
            disc_totals.add(dt)
            disc_numbers.add(dn)
        except Exception:
            pass

    return disc_totals, disc_numbers


def check_wait(album_dir: str) -> str | None:
    """
    Return "WAIT:have:need" if discs are missing, None if import can proceed.

    Holds only when every audio file agrees on disctotal > 1 and we don't
    have all discs yet.  If the directory is empty or tags are unreadable,
    returns None (let beets attempt the import rather than blocking forever).
    """
    disc_totals, disc_numbers = _read_disc_tags(album_dir)
    if not disc_totals:
        return None
    if len(disc_totals) == 1:
        disctotal = disc_totals.pop()
        if disctotal > 1 and len(disc_numbers) < disctotal:
            return f"WAIT:{len(disc_numbers)}:{disctotal}"
    return None


def count_discs(album_dir: str) -> int:
    """Return the number of unique disc numbers found across audio files in album_dir."""
    _, disc_numbers = _read_disc_tags(album_dir)
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
