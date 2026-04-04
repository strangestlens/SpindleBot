"""
Pretag and posttag stages for the import pipeline.

pretag:  normalizes FLAC tags before beet import
         - strips XLD non-standard duplicate fields
         - strips compilation tag
         - moves feat. credits from artist field into title
         - normalizes track artist to album artist
         - skips artist normalization for Various Artists compilations

posttag: cleans up tags after beet import
         - strips alias tags beets writes for compatibility
         - truncates DATE field to year-only
"""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path

import mutagen.flac

# XLD writes these non-standard fields; beets writes its own equivalents.
# Leaving both causes metadata readers to show duplicates.
XLD_JUNK_TAGS = {
    "year",         # beets uses 'date'
    "track",        # beets uses 'tracknumber'
    "trackc",       # beets uses 'tracktotal'
    "discc",        # beets uses 'disctotal'
    "totaldiscs",   # beets uses 'disctotal'
    "totaltracks",  # beets uses 'tracktotal'
}

# Beets writes these as compatibility aliases alongside its canonical fields.
BEETS_ALIAS_TAGS = {
    "year",    # duplicate of 'date'
    "track",   # duplicate of 'tracknumber'
    "trackc",  # duplicate of 'tracktotal'
    "discc",   # duplicate of 'disctotal'
}

_FEAT_RE = re.compile(r"\s*[\(\[]?feat\.?\s+([^\)\]\n]+?)[\)\]]?\s*$", re.IGNORECASE)


def pretag(album_dir: str | Path) -> bool:
    """
    Normalize tags in all FLACs under album_dir before beet import.
    Returns False if no FLAC files were found.
    """
    files = sorted(glob.glob(str(Path(album_dir) / "*.flac")))
    if not files:
        return False

    for path in files:
        flac = mutagen.flac.FLAC(path)
        tags = flac.tags

        artist = (tags.get("artist") or [""])[0]
        albumartist = (tags.get("albumartist") or [""])[0]
        title = (tags.get("title") or [""])[0]
        changed = False

        for junk in XLD_JUNK_TAGS:
            if junk in tags:
                del tags[junk]
                changed = True

        if "compilation" in tags:
            del tags["compilation"]
            changed = True

        is_va = albumartist.lower() in ("various artists", "various", "va")
        m = _FEAT_RE.search(artist)
        if not is_va:
            if m and albumartist:
                feat_str = m.group(1).strip()
                tags["title"] = [f"{title} (feat. {feat_str})"]
                tags["artist"] = [albumartist]
                changed = True
            elif albumartist and artist != albumartist:
                tags["artist"] = [albumartist]
                changed = True

        if changed:
            flac.save()

    return True


def posttag(paths: list[str]) -> int:
    """
    Strip beet alias tags and truncate DATE to year only.
    Returns the number of files modified.
    """
    fixed = 0
    for path in paths:
        path = path.strip()
        if not path.endswith(".flac") or not os.path.exists(path):
            continue
        flac = mutagen.flac.FLAC(path)
        changed = False

        for alias in BEETS_ALIAS_TAGS:
            if alias in flac.tags:
                del flac.tags[alias]
                changed = True

        date = (flac.tags.get("date") or [""])[0]
        if date and len(date) > 4:
            flac.tags["date"] = [date[:4]]
            changed = True

        if changed:
            flac.save()
            fixed += 1

    return fixed
