#!/usr/bin/env python3
# music-pretag.py — CLI entry point for pretag/posttag.
# Logic lives in spindlebot/pipeline/stages/pretag.py.

import sys
from spindlebot.pipeline.stages.pretag import pretag, posttag

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
