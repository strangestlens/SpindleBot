"""
Pretag and posttag stages for the import pipeline.

pretag:  normalizes tags before beet import
         - strips XLD non-standard duplicate fields (VorbisComment formats only —
           XLD rips to whatever format the user configures; those files carry
           the junk tags; digital downloads in other formats don't)
         - strips compilation tag
         - fixes Bandcamp ALBUM encoding artifacts (ASCII apostrophe for Unicode)
           while deliberately preserving the Bandcamp COMMENT tag
         - moves feat. credits from artist field into title
         - normalizes track artist to album artist
         - skips artist normalization for Various Artists compilations

posttag: cleans up tags after beet import
         - strips VorbisComment alias tags beets writes for compatibility
           (FLAC/OGG/Opus only — ID3/MP4 formats use their own tag frames)
         - truncates DATE field to year-only (all formats)
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

import mutagen
import mutagen.flac

from spindlebot.disc import AUDIO_EXTENSIONS, find_audio_files

# XLD writes these non-standard VorbisComment fields; beets writes its own
# equivalents. Leaving both causes metadata readers to show duplicates.
# Only present in files ripped by XLD (FLAC by default, but format is
# user-configurable — the cleanup is a no-op for formats that don't carry them).
XLD_JUNK_TAGS = {
    "year",         # beets uses "date"
    "track",        # beets uses "tracknumber"
    "trackc",       # beets uses "tracktotal"
    "discc",        # beets uses "disctotal"
    "totaldiscs",   # beets uses "disctotal"
    "totaltracks",  # beets uses "tracktotal"
}

# Beets writes these VorbisComment alias fields alongside its canonical fields.
# Relevant for FLAC, OGG, and Opus; ID3/MP4 formats use their own tag frames.
BEETS_ALIAS_TAGS = {
    "year",    # duplicate of "date"
    "track",   # duplicate of "tracknumber"
    "trackc",  # duplicate of "tracktotal"
    "discc",   # duplicate of "disctotal"
}

# Matches "feat." credits at the end of an artist field, with optional
# surrounding parens or brackets. Captures the featured artist(s).
# Examples: "Band feat. Guest", "Band (feat. Guest)", "Band [feat. Guest]"
_FEAT_RE = re.compile(r"\s*[\(\[]?feat\.?\s+([^\)\]\n]+?)[\)\]]?\s*$", re.IGNORECASE)

# Matches trailing performer credit annotations that appear in the artist field
# on some releases (especially Japanese pressings): "(Backing Vocals: Name)"
# The colon distinguishes credits from legitimate parenthetical name fragments.
# Examples: "Beck (Backing Vocals: Roger Manning)", "Artist (Chorus: Name)"
_CREDIT_RE = re.compile(
    r"\s*\([^)]*(?:Backing\s+Vocals?|Lead\s+Vocals?|Chorus|Additional\s+Vocals?)[^)]*:[^)]+\)\s*$",
    re.IGNORECASE,
)

# Formats that use VorbisComment tags (lowercase string keys, list values).
_VORBIS_EXTENSIONS = {"flac", "ogg", "opus", "oga", "spx"}


# ── Bandcamp encoding fix ──────────────────────────────────────────────────────


def _fix_bandcamp_album_encoding(tags, album_dir: str) -> bool:
    """
    Fix Bandcamp's ALBUM tag encoding artifact where accented characters are
    represented as an ASCII apostrophe + base character
    (e.g. "Aqu'aticos" instead of "Aquáticos").

    Only acts when the file has a Bandcamp COMMENT tag, the ALBUM tag is pure
    ASCII, and the album directory name contains non-ASCII characters. The
    COMMENT tag is deliberately left intact.

    Returns True if the album tag was changed.
    """
    comment = (tags.get("comment") or [""])[0]
    if "bandcamp.com" not in comment.lower():
        return False

    album_tag = (tags.get("album") or [""])[0]
    if not album_tag:
        return False

    dir_name = os.path.basename(album_dir.rstrip("/\\"))
    if not dir_name or dir_name == album_tag:
        return False

    if not album_tag.isascii() or dir_name.isascii():
        return False

    def _ascii_fold(s: str) -> str:
        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()

    # Strip "Artist - " prefix Bandcamp uses in directory names
    dir_album_part = dir_name
    if " - " in dir_name:
        dir_album_part = dir_name.split(" - ", 2)[-1]

    if _ascii_fold(dir_album_part) == _ascii_fold(album_tag.replace("'", "")):
        tags["album"] = [dir_album_part]
        return True

    return False


# ── Artist/feat. normalization (format-agnostic) ──────────────────────────────


def _clean_artist_credits(value: str) -> str:
    """Strip feat. and performer credit annotations from an artist string.

    Returns the base artist name, e.g.:
      "Beck feat. Chris Martin (Backing Vocals: ...)" → "Beck"
      "Beck (Backing Vocals: Roger Manning)"         → "Beck"
    """
    m = _FEAT_RE.search(value)
    if m:
        return value[: m.start()].strip()
    m = _CREDIT_RE.search(value)
    if m:
        return value[: m.start()].strip()
    return value


def _normalize_artists(tags, changed: bool) -> bool:
    """
    Apply feat. and albumartist normalization to a tag dict.
    Works with both VorbisComment-style and easy-interface tag dicts.
    Returns True if any tag was modified.
    """
    artist = (tags.get("artist") or [""])[0]
    albumartist = (tags.get("albumartist") or [""])[0]
    title = (tags.get("title") or [""])[0]

    is_va = albumartist.lower() in ("various artists", "various", "va")

    clean_aa = _clean_artist_credits(albumartist) if albumartist else ""
    m = _FEAT_RE.search(artist)

    if not is_va:
        if m:
            feat_str = m.group(1).strip()
            tags["title"] = [f"{title} (feat. {feat_str})"]
            # Use clean albumartist if available, else strip from artist itself
            base = clean_aa if clean_aa else artist[: m.start()].strip()
            tags["artist"] = [base]
            changed = True
        elif albumartist and artist != clean_aa:
            tags["artist"] = [clean_aa]
            changed = True

        # Clean albumartist itself if it had credits embedded
        if albumartist and clean_aa != albumartist:
            tags["albumartist"] = [clean_aa]
            changed = True

    return changed


# ── Per-file pretag helpers ───────────────────────────────────────────────────


def _pretag_vorbis(path: str, album_dir: str) -> bool:
    """
    Pretag a VorbisComment file (FLAC, OGG, Opus, …).
    Returns True if the file was modified.
    """
    f = mutagen.flac.FLAC(path) if path.lower().endswith(".flac") else mutagen.File(path)
    if f is None or not f.tags:
        return False

    tags = f.tags
    changed = False

    # Strip XLD junk tags (no-op on files that don't carry them)
    for junk in XLD_JUNK_TAGS:
        if junk in tags:
            del tags[junk]
            changed = True

    if "compilation" in tags:
        del tags["compilation"]
        changed = True

    if _fix_bandcamp_album_encoding(tags, album_dir):
        changed = True

    changed = _normalize_artists(tags, changed)

    if changed:
        f.save()
    return changed


def _pretag_easy(path: str, album_dir: str) -> bool:
    """
    Pretag a non-VorbisComment file (MP3, M4A, AAC, …) using mutagen's easy
    interface, which normalises tag keys to lowercase across all ID3/MP4 formats.

    Skips XLD junk-tag cleanup — those tags only appear in VorbisComment files.
    Returns True if the file was modified.
    """
    f = mutagen.File(path, easy=True)
    if f is None or f.tags is None:
        return False

    tags = f.tags
    changed = False

    if _fix_bandcamp_album_encoding(tags, album_dir):
        changed = True

    changed = _normalize_artists(tags, changed)

    if changed:
        f.save()
    return changed


# ── Per-file posttag helpers ──────────────────────────────────────────────────


def _posttag_vorbis(path: str) -> bool:
    """Strip beet alias tags and truncate DATE for a VorbisComment file."""
    f = mutagen.flac.FLAC(path) if path.lower().endswith(".flac") else mutagen.File(path)
    if f is None or not f.tags:
        return False

    changed = False
    for alias in BEETS_ALIAS_TAGS:
        if alias in f.tags:
            del f.tags[alias]
            changed = True

    date = (f.tags.get("date") or [""])[0]
    if date and len(date) > 4:
        f.tags["date"] = [date[:4]]
        changed = True

    if changed:
        f.save()
    return changed


def _posttag_easy(path: str) -> bool:
    """Truncate DATE for a non-VorbisComment file via mutagen's easy interface."""
    f = mutagen.File(path, easy=True)
    if f is None or f.tags is None:
        return False

    date = (f.tags.get("date") or [""])[0]
    if date and len(date) > 4:
        f.tags["date"] = [date[:4]]
        f.save()
        return True

    return False


# ── Public API ────────────────────────────────────────────────────────────────


def pretag(album_dir: str | Path) -> bool:
    """
    Normalize tags in all audio files under album_dir before beet import.
    Returns False if no audio files were found.
    """
    files = find_audio_files(album_dir)
    if not files:
        return False

    album_dir_str = str(album_dir)
    for path in files:
        p = str(path)
        try:
            if path.suffix.lower().lstrip(".") in _VORBIS_EXTENSIONS:
                _pretag_vorbis(p, album_dir_str)
            else:
                _pretag_easy(p, album_dir_str)
        except Exception as exc:
            import sys
            print(f"  warning: could not pretag {path.name}: {exc}", file=sys.stderr)

    return True


def posttag(paths: list[str]) -> int:
    """
    Strip beet alias tags and truncate DATE to year only.
    Returns the number of files modified.
    """
    fixed = 0
    for raw in paths:
        path = raw.strip()
        if not path or not os.path.isfile(path):
            continue
        ext = path.lower().rsplit(".", 1)[-1]
        if ext not in AUDIO_EXTENSIONS:
            continue
        try:
            if ext in _VORBIS_EXTENSIONS:
                if _posttag_vorbis(path):
                    fixed += 1
            else:
                if _posttag_easy(path):
                    fixed += 1
        except Exception as exc:
            import sys
            print(f"  warning: could not posttag {os.path.basename(path)}: {exc}", file=sys.stderr)

    return fixed
