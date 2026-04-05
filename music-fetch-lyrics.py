#!/usr/bin/env python3
# music-fetch-lyrics.py — CLI entry point for the fetch-lyrics stage.
# Logic lives in spindlebot/pipeline/stages/fetch_lyrics.py.
#
# For interactive use, prefer: spindlebot fetch-lyrics <album_dir> [--dry-run] [--force]

import sys
from spindlebot.pipeline.stages.fetch_lyrics import fetch_lyrics
from spindlebot.config import load

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--dry-run", "-n", action="store_true")
    parser.add_argument("--force", "-f", action="store_true")
    args = parser.parse_args()

    cfg = load()
    result = fetch_lyrics(args.path, cfg, dry_run=args.dry_run, force=args.force)
    print(
        f"{'[dry-run] ' if args.dry_run else ''}"
        f"synced={result.synced} plain={result.plain} "
        f"skipped={result.skipped} missing={result.missing}"
    )
    if result.errors:
        print(f"errors: {result.errors}", file=sys.stderr)
