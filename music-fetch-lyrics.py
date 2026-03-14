#!/opt/homebrew/bin/python3
"""
music-fetch-lyrics.py — fetch synced LRC lyrics from lrclib.net
Falls back to embedded 'lyrics' tag if lrclib has no synced match.
Writes .lrc files alongside FLAC files.

Usage:
  music-fetch-lyrics.py /path/to/album/dir [--dry-run] [--force]
  music-fetch-lyrics.py /path/to/library --recurse [--dry-run] [--force]
"""

import sys
import os
import time
import argparse
import urllib.request
import urllib.parse
import urllib.error
import json
import mutagen.flac

LRCLIB_API = "https://lrclib.net/api/get"
REQUEST_DELAY = 0.3  # seconds between API calls


def get_tags(flac_path):
    try:
        f = mutagen.flac.FLAC(flac_path)
        tags = f.tags
        if not tags:
            return {}
        def get(key):
            val = tags.get(key.upper()) or tags.get(key.lower())
            return val[0] if val else None
        return {
            "artist": get("artist"),
            "title": get("title"),
            "album": get("album"),
            "duration": int(f.info.length),
            "lyrics": get("lyrics") or get("unsyncedlyrics"),
        }
    except Exception as e:
        print(f"  [warn] Could not read tags: {e}")
        return {}


def strip_cjk(text):
    """Remove CJK/Japanese characters and clean up leftover punctuation/whitespace."""
    import re
    # Remove CJK Unified Ideographs, Hiragana, Katakana, CJK symbols/punctuation
    cleaned = re.sub(r'[\u3000-\u9fff\uf900-\ufaff\ufe30-\ufe4f]+', '', text)
    # Remove stray separators left behind (/, ·, ・, —, etc.) at start/end
    cleaned = re.sub(r'^[\s/\-–—·・]+|[\s/\-–—·・]+$', '', cleaned)
    return cleaned.strip()


def _query_lrclib(artist, title, album, duration=None):
    params = {
        "artist_name": artist,
        "track_name": title,
        "album_name": album,
    }
    if duration is not None:
        params["duration"] = duration
    url = LRCLIB_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "music-fetch-lyrics/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)
        return data.get("syncedLyrics"), data.get("plainLyrics")


def title_from_filename(flac_path):
    """Extract a clean title from the filename, e.g. '03. Song Title.flac' → 'Song Title'."""
    import re
    name = os.path.splitext(os.path.basename(flac_path))[0]
    # Strip leading track/disc numbers like "01. " or "1-02. "
    name = re.sub(r'^\d+[-.]?\d*[.\s]+', '', name).strip()
    return name or None


def fetch_lrclib(artist, title, album, duration, flac_path=None):
    attempts = [title]

    # Add CJK-stripped variant if title contains Japanese characters
    cleaned = strip_cjk(title)
    if cleaned and cleaned != title:
        attempts.append(cleaned)

    # Add filename-derived title as final fallback
    if flac_path:
        fn_title = title_from_filename(flac_path)
        if fn_title and fn_title not in attempts:
            attempts.append(fn_title)

    for i, t in enumerate(attempts):
        if i > 0:
            time.sleep(REQUEST_DELAY)
        # Try with duration first, then without (Japanese imports may have different lengths)
        for dur in [duration, None]:
            try:
                synced, plain = _query_lrclib(artist, t, album, dur)
                if synced or plain:
                    if t != title:
                        print(f" [fallback→{t!r}]", end="")
                    return synced, plain
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    print(f"  [warn] lrclib HTTP {e.code} for {t!r}")
                    return None, None
            except Exception as e:
                print(f"  [warn] lrclib error for {t!r}: {e}")
                return None, None
            time.sleep(REQUEST_DELAY)

    return None, None


def plain_to_lrc(plain_lyrics):
    """Wrap plain text lyrics in a minimal LRC envelope."""
    lines = plain_lyrics.strip().splitlines()
    return "\n".join(f"[00:00.00] {line}" if line.strip() else "" for line in lines)


def process_file(flac_path, dry_run=False, force=False):
    lrc_path = os.path.splitext(flac_path)[0] + ".lrc"

    if os.path.exists(lrc_path) and not force:
        print(f"  [skip] .lrc already exists: {os.path.basename(lrc_path)}")
        return "skipped"

    tags = get_tags(flac_path)
    if not tags.get("artist") or not tags.get("title"):
        print(f"  [skip] Missing artist/title tags: {os.path.basename(flac_path)}")
        return "skipped"

    artist = tags["artist"]
    title = tags["title"]
    album = tags.get("album", "")
    duration = tags.get("duration", 0)

    print(f"  🔍 {artist} - {title}", end="", flush=True)

    synced, plain = fetch_lrclib(artist, title, album, duration, flac_path=flac_path)
    time.sleep(REQUEST_DELAY)

    if synced:
        lrc_content = synced
        source = "synced (lrclib)"
    elif plain:
        lrc_content = plain_to_lrc(plain)
        source = "plain (lrclib)"
    elif tags.get("lyrics"):
        lrc_content = plain_to_lrc(tags["lyrics"])
        source = "embedded tag"
    else:
        print(f" → no lyrics found")
        return "missing"

    print(f" → ✓ {source}")

    if not dry_run:
        try:
            os.makedirs(os.path.dirname(lrc_path), exist_ok=True)
            with open(lrc_path, "w", encoding="utf-8") as f:
                f.write(lrc_content + "\n")
        except OSError as e:
            print(f" → [warn] could not write LRC: {e}")
            return "missing"

    return "synced" if synced else "plain"


def find_flac_files(path, recurse=False):
    if os.path.isfile(path) and path.endswith(".flac"):
        return [path]
    files = []
    if recurse:
        for root, dirs, names in os.walk(path):
            dirs.sort()
            for name in sorted(names):
                if name.endswith(".flac"):
                    files.append(os.path.join(root, name))
    else:
        files = sorted(
            os.path.join(path, f) for f in os.listdir(path) if f.endswith(".flac")
        )
    return files


MISS_LOG = os.path.expanduser("~/.config/beets/lyrics-missing.log")


def main():
    parser = argparse.ArgumentParser(description="Fetch LRC lyrics for FLAC files")
    parser.add_argument("path", help="Album directory, library root, or single FLAC file")
    parser.add_argument("--recurse", "-r", action="store_true", help="Recurse into subdirectories")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Don't write files, just show what would happen")
    parser.add_argument("--force", "-f", action="store_true", help="Overwrite existing .lrc files")
    args = parser.parse_args()

    files = find_flac_files(args.path, recurse=args.recurse)
    if not files:
        print(f"No FLAC files found in: {args.path}")
        sys.exit(1)

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Processing {len(files)} file(s) in: {args.path}\n")

    counts = {"synced": 0, "plain": 0, "skipped": 0, "missing": 0}
    missing = []

    for flac in files:
        result = process_file(flac, dry_run=args.dry_run, force=args.force)
        counts[result] += 1
        if result == "missing":
            tags = get_tags(flac)
            missing.append({
                "file": flac,
                "artist": tags.get("artist", "?"),
                "title": tags.get("title", "?"),
                "album": tags.get("album", "?"),
            })

    print(f"\n{'─'*50}")
    print(f"  ✅ Synced LRC:     {counts['synced']}")
    print(f"  📄 Plain/fallback: {counts['plain']}")
    print(f"  ⏭  Skipped:        {counts['skipped']}")
    print(f"  ❌ No lyrics:      {counts['missing']}")

    if missing and not args.dry_run:
        os.makedirs(os.path.dirname(MISS_LOG), exist_ok=True)
        with open(MISS_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n# Run: {time.strftime('%Y-%m-%d %H:%M:%S')} — {args.path}\n")
            for m in missing:
                f.write(f"{m['artist']}\t{m['album']}\t{m['title']}\t{m['file']}\n")
        print(f"\n  📝 Missing logged to: {MISS_LOG}")

    # If every track came up empty, stamp a .nolrc marker so the sync script
    # skips this album on future runs. Remove it if lyrics were found.
    if not args.dry_run and not args.recurse:
        all_missing = counts["missing"] > 0 and counts["synced"] == 0 and counts["plain"] == 0
        marker = os.path.join(args.path, ".nolrc")
        if all_missing:
            try:
                open(marker, "w").close()
            except OSError:
                pass
        elif os.path.exists(marker):
            os.remove(marker)


if __name__ == "__main__":
    main()
