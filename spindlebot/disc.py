"""
Disc-detection helpers for music-import.sh.

CLI usage (called by shell scripts):
    python -m spindlebot.disc check <album_dir>   # print WAIT:have:need if incomplete
    python -m spindlebot.disc count <album_dir>   # print integer disc count
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path


def _read_disc_tags(album_dir: str) -> tuple[set[int], set[int]]:
    """Return (disc_totals, disc_numbers) sets read from all FLACs in album_dir."""
    import mutagen.flac  # local import so the module loads without mutagen installed

    disc_totals: set[int] = set()
    disc_numbers: set[int] = set()

    for path in glob.glob(str(Path(album_dir) / "*.flac")):
        try:
            f = mutagen.flac.FLAC(path)
            dt = int((f.tags.get("disctotal") or f.tags.get("totaldiscs") or ["1"])[0])
            dn = int((f.tags.get("discnumber") or f.tags.get("disc") or ["1"])[0])
            disc_totals.add(dt)
            disc_numbers.add(dn)
        except Exception:
            pass

    return disc_totals, disc_numbers


def check_wait(album_dir: str) -> str | None:
    """
    Return "WAIT:have:need" if discs are missing, None if import can proceed.

    Holds only when every FLAC agrees on disctotal > 1 and we don't have all discs yet.
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
    """Return the number of unique disc numbers found across FLACs in album_dir."""
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
