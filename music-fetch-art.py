#!/usr/bin/env python3
# music-fetch-art.py — shim; logic lives in spindlebot.pipeline.stages.fetch_art
import sys
from spindlebot.config import load
from spindlebot.pipeline.stages.fetch_art import fetch_art

cfg = load()
for album_dir in (sys.argv[1:] or [line.strip() for line in sys.stdin if line.strip()]):
    result = fetch_art(album_dir, cfg)
    print(f"embedded={result.embedded} skipped={result.skipped} missing={result.missing}")
    if result.errors:
        print(f"errors: {result.errors}", file=sys.stderr)
