#!/usr/bin/env python3
# music-pretag.py — run before beet import
#
# pretag (per-file, before import):
#   - Strips XLD non-standard duplicate fields (FLAC only)
#   - Strips compilation tag
#   - Moves feat. credits from artist field into title
#   - Forces track artist = album artist
#   - Fixes Bandcamp ALBUM encoding artifacts (ASCII apostrophe for accented chars)
#     while deliberately preserving the Bandcamp COMMENT tag
#
# posttag (per-file path on stdin, after import):
#   - Strips beets alias duplicate fields
#   - Truncates DATE to year-only

import sys
import re
import os

import mutagen
import mutagen.flac

from spindlebot.disc import find_audio_files, AUDIO_EXTENSIONS  # noqa: F401 (re-exported)

# XLD writes these non-standard fields; beets writes its own equivalents.
# Leaving both causes metadata readers to show duplicates (e.g. "2007\\2007-08-21").
# These only exist in XLD-generated FLAC files; the cleanup is a harmless no-op elsewhere.
XLD_JUNK_TAGS = {
    "year",        # beets uses "date"
    "track",       # beets uses "tracknumber"
    "trackc",      # beets uses "tracktotal"
    "discc",       # beets uses "disctotal"
    "totaldiscs",  # beets uses "disctotal"
    "totaltracks", # beets uses "tracktotal"
}

# Beets writes these as compatibility aliases alongside its canonical fields,
# causing metadata readers to show duplicates (e.g. "2016\\2016-11-18", "1\\1").
BEETS_ALIAS_TAGS = {
    "year",    # duplicate of "date"
    "track",   # duplicate of "tracknumber"
    "trackc",  # duplicate of "tracktotal"
    "discc",   # duplicate of "disctotal"
}


# ── Bandcamp encoding fix ──────────────────────────────────────────────────────

def _fix_bandcamp_album_encoding(tags, album_dir: str) -> bool:
    """
    Fix the Bandcamp ALBUM tag encoding artifact where accented characters are
    exported as an ASCII apostrophe followed by the base character
    (e.g. "Aqu'aticos" instead of "Aquáticos").

    Detects the issue by comparing the ALBUM tag (which may be ASCII-mangled)
    against the album directory name (which Bandcamp names with correct Unicode).
    Fixes only when:
      - The file has a Bandcamp COMMENT tag (fingerprint for Bandcamp downloads)
      - The current ALBUM tag is pure ASCII
      - The directory name contains non-ASCII characters
      - The ASCII-folded directory name loosely matches the ALBUM tag

    Deliberately does NOT remove the COMMENT tag — that stays.

    Returns True if the album tag was changed.
    """
    import unicodedata

    comment = (tags.get("comment") or [""])[0]
    if "bandcamp.com" not in comment.lower():
        return False

    album_tag = (tags.get("album") or [""])[0]
    if not album_tag:
        return False

    dir_name = os.path.basename(album_dir.rstrip("/\\"))
    if not dir_name or dir_name == album_tag:
        return False

    # Only act if the ALBUM tag is pure ASCII but the directory name has Unicode.
    if not album_tag.isascii() or dir_name.isascii():
        return False

    # Sanity check: ASCII-fold both strings and compare loosely.
    # This guards against using an unrelated directory name as the album title.
    def _ascii_fold(s: str) -> str:
        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()

    # Strip "Artist - " prefix from directory name if present (Bandcamp naming convention)
    dir_album_part = dir_name
    if " - " in dir_name:
        parts = dir_name.split(" - ", 2)
        if len(parts) >= 2:
            dir_album_part = parts[-1]

    if _ascii_fold(dir_album_part) == _ascii_fold(album_tag.replace("'", "")):
        tags["album"] = [dir_album_part]
        print(f"  bandcamp: fixed album encoding '{album_tag}' → '{dir_album_part}'")
        return True

    return False


# ── FLAC helpers ──────────────────────────────────────────────────────────────

def _pretag_flac(path: str, album_dir: str) -> bool:
    """Run pretag on a single FLAC file. Returns True if the file was modified."""
    flac = mutagen.flac.FLAC(path)
    tags = flac.tags
    if tags is None:
        return False

    artist      = (tags.get("artist")      or [""])[0]
    albumartist = (tags.get("albumartist") or [""])[0]
    title       = (tags.get("title")       or [""])[0]
    changed     = False

    # 1. Strip XLD non-standard duplicate fields
    for junk in XLD_JUNK_TAGS:
        if junk in tags:
            del tags[junk]
            changed = True

    # 2. Strip compilation tag
    if "compilation" in tags:
        del tags["compilation"]
        changed = True

    # 3. Fix Bandcamp ALBUM encoding (leaves COMMENT tag intact)
    if _fix_bandcamp_album_encoding(tags, album_dir):
        changed = True

    # 4. Move feat. from artist into title; normalize artist → albumartist
    is_va = albumartist.lower() in ("various artists", "various", "va")
    m = re.search(r"\s*[\(\[]?feat\.?\s+([^\)\]\n]+?)[\)\]]?\s*$", artist, re.IGNORECASE)
    if not is_va:
        if m and albumartist:
            feat_str = m.group(1).strip()
            tags["title"]  = [f"{title} (feat. {feat_str})"]
            tags["artist"] = [albumartist]
            print(f"  fixed: '{title}' | artist '{artist}' → title '{title} (feat. {feat_str})', artist '{albumartist}'")
            changed = True
        elif albumartist and artist != albumartist:
            tags["artist"] = [albumartist]
            print(f"  normalized: artist '{artist}' → '{albumartist}'")
            changed = True

    if changed:
        flac.save()
    return changed


def _pretag_other(path: str, album_dir: str) -> bool:
    """
    Run pretag on a non-FLAC audio file (MP3, M4A, OGG, etc.) using
    mutagen's easy interface for consistent cross-format tag access.

    Skips XLD junk-tag cleanup (those tags don't exist outside FLAC).
    Returns True if the file was modified.
    """
    f = mutagen.File(path, easy=True)
    if f is None or f.tags is None:
        return False

    tags    = f.tags
    artist      = (tags.get("artist")      or [""])[0]
    albumartist = (tags.get("albumartist") or [""])[0]
    title       = (tags.get("title")       or [""])[0]
    changed     = False

    # Fix Bandcamp ALBUM encoding (leaves COMMENT intact)
    if _fix_bandcamp_album_encoding(tags, album_dir):
        changed = True

    # Move feat. from artist into title; normalize artist → albumartist
    is_va = albumartist.lower() in ("various artists", "various", "va")
    m = re.search(r"\s*[\(\[]?feat\.?\s+([^\)\]\n]+?)[\)\]]?\s*$", artist, re.IGNORECASE)
    if not is_va:
        if m and albumartist:
            feat_str = m.group(1).strip()
            tags["title"]  = [f"{title} (feat. {feat_str})"]
            tags["artist"] = [albumartist]
            print(f"  fixed: '{title}' | artist '{artist}' → title '{title} (feat. {feat_str})', artist '{albumartist}'")
            changed = True
        elif albumartist and artist != albumartist:
            tags["artist"] = [albumartist]
            print(f"  normalized: artist '{artist}' → '{albumartist}'")
            changed = True

    if changed:
        f.save()
    return changed


# ── Public API ────────────────────────────────────────────────────────────────

def pretag(album_dir: str) -> bool:
    """
    Pre-import tag cleanup for all audio files in album_dir.
    Returns True if any files were found, False if the directory had no audio.
    """
    files = find_audio_files(album_dir)
    if not files:
        print(f"No audio files found in {album_dir}", file=sys.stderr)
        return False

    for path in files:
        p = str(path)
        try:
            if p.lower().endswith(".flac"):
                _pretag_flac(p, album_dir)
            else:
                _pretag_other(p, album_dir)
        except Exception as exc:
            print(f"  warning: could not pretag {os.path.basename(p)}: {exc}", file=sys.stderr)

    print(f"pretag done: {len(files)} files in {album_dir}")
    return True


def posttag(paths: list[str]) -> None:
    """
    Post-import fixes: strip beets alias tags and truncate DATE to year only.
    Reads a list of absolute file paths (may be any supported audio format).
    Silently skips paths that don't exist or aren't readable.
    """
    fixed = 0
    for raw_path in paths:
        path = raw_path.strip()
        if not path:
            continue
        if not os.path.isfile(path):
            continue

        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext not in AUDIO_EXTENSIONS:
            continue

        try:
            if path.lower().endswith(".flac"):
                _posttag_flac(path)
                fixed += 1
            else:
                _posttag_other(path)
                fixed += 1
        except Exception as exc:
            print(f"  warning: could not posttag {os.path.basename(path)}: {exc}", file=sys.stderr)

    print(f"posttag done: {fixed} files updated")


def _posttag_flac(path: str) -> None:
    flac = mutagen.flac.FLAC(path)
    if not flac.tags:
        return
    changed = False

    for alias in BEETS_ALIAS_TAGS:
        if alias in flac.tags:
            del flac.tags[alias]
            changed = True

    date = (flac.tags.get("date") or [""])[0]
    if date and len(date) > 4:
        flac.tags["date"] = [date[:4]]
        changed = True
        print(f"  date truncated: {date} → {date[:4]} ({os.path.basename(path)})")

    if changed:
        flac.save()


def _posttag_other(path: str) -> None:
    f = mutagen.File(path, easy=True)
    if f is None or f.tags is None:
        return
    changed = False

    # Easy interface doesn't expose beets alias tags for non-FLAC formats,
    # so this is primarily about date truncation.
    date = (f.tags.get("date") or [""])[0]
    if date and len(date) > 4:
        f.tags["date"] = [date[:4]]
        changed = True
        print(f"  date truncated: {date} → {date[:4]} ({os.path.basename(path)})")

    if changed:
        f.save()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: music-pretag.py <album_dir>", file=sys.stderr)
        print("       music-pretag.py --post  (reads file paths from stdin)", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--post":
        paths = sys.stdin.read().splitlines()
        posttag(paths)
    else:
        success = pretag(sys.argv[1])
        sys.exit(0 if success else 1)
