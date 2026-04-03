"""
Staging directory scanner.

Finds importable items in the staging directory so the pipeline can process
whatever is sitting there — both traditional CD rips (signalled by an XLD
.log file) and digital downloads (Bandcamp, etc.) that arrive as a plain
directory of audio files with no .log trigger.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from spindlebot.disc import AUDIO_EXTENSIONS


class StagingItem(NamedTuple):
    """A single importable item found in the staging directory."""
    kind: str   # "log" → pass .log path to import script; "dir" → pass directory
    path: Path


def scan_staging(staging_dir: str | Path) -> list[StagingItem]:
    """
    Scan staging_dir and return an ordered list of items ready to import.

    Discovery rules:
    - Subdirectories that contain audio files are eligible.
    - If the subdir also contains a .log file (XLD rip that landed in a
      subdir), the .log file is used as the trigger (existing pipeline
      behaviour — import script uses dirname to find the album dir).
    - If there is no .log file, the directory itself is the trigger
      (digital download / Bandcamp mode).
    - Root-level .log files (XLD rips where files land directly in Staging)
      are collected after subdirectory scanning.
    - Subdirectories with no audio files are silently ignored (e.g. a folder
      of scans or downloads that haven't finished yet).
    - Hidden directories (name starts with ".") are skipped.

    Items are returned in a stable alphabetical order so repeated calls
    produce the same dispatch sequence.

    This function is purely advisory — it reads the filesystem but does not
    modify it.  It is safe to call at any time.
    """
    staging = Path(staging_dir)
    if not staging.is_dir():
        return []

    items: list[StagingItem] = []

    # ── Subdirectories ─────────────────────────────────────────────────────
    for entry in sorted(staging.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue

        audio_files = [
            f for f in entry.iterdir()
            if f.is_file() and f.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS
        ]
        if not audio_files:
            continue  # no audio → skip (might be art-only dir, incomplete download, etc.)

        log_files = sorted(entry.glob("*.log"))
        if log_files:
            # XLD rip that landed in a named subdir — use the log as trigger
            items.append(StagingItem(kind="log", path=log_files[0]))
        else:
            # Digital download — use the directory itself as trigger
            items.append(StagingItem(kind="dir", path=entry))

    # ── Root-level .log files ──────────────────────────────────────────────
    # XLD on macOS typically writes files directly into Staging/ with a .log
    # alongside them.  dirname(log) == Staging, so the import script imports
    # from the staging root — beets groups by album tags and handles it.
    for log in sorted(staging.glob("*.log")):
        items.append(StagingItem(kind="log", path=log))

    return items
