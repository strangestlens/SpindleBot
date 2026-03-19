#!/usr/bin/env python3
# music-pretag.py — run before beet import
# - Removes compilation tag
# - Moves feat. credits from artist field into title
# - Forces track artist = album artist

import sys, re, glob, os
import mutagen.flac

# XLD writes these non-standard fields; beets writes its own equivalents.
# Leaving both causes metadata readers to show duplicates (e.g. "2007\\2007-08-21").
XLD_JUNK_TAGS = {
    'year',        # beets uses 'date'
    'track',       # beets uses 'tracknumber'
    'trackc',      # beets uses 'tracktotal'
    'discc',       # beets uses 'disctotal'
    'totaldiscs',  # beets uses 'disctotal'
    'totaltracks', # beets uses 'tracktotal'
}

def pretag(album_dir):
    files = sorted(glob.glob(os.path.join(album_dir, '*.flac')))
    if not files:
        print(f"No FLAC files found in {album_dir}", file=sys.stderr)
        return False

    for path in files:
        flac = mutagen.flac.FLAC(path)
        tags = flac.tags

        artist     = (tags.get('artist') or [''])[0]
        albumartist= (tags.get('albumartist') or [''])[0]
        title      = (tags.get('title') or [''])[0]

        changed = False

        # 1. Strip XLD non-standard duplicate fields
        for junk in XLD_JUNK_TAGS:
            if junk in tags:
                del tags[junk]
                changed = True

        # 2. Strip compilation tag
        if 'compilation' in tags:
            del tags['compilation']
            changed = True

        # 3. Move feat. from artist into title
        # Skip artist normalization for Various Artists compilations — each track
        # has its own artist and flattening them to albumartist would lose that.
        is_va = albumartist.lower() in ('various artists', 'various', 'va')
        m = re.search(r'\s*[\(\[]?feat\.?\s+([^\)\]\n]+?)[\)\]]?\s*$', artist, re.IGNORECASE)
        if not is_va:
            if m and albumartist:
                feat_str = m.group(1).strip()
                new_title = f"{title} (feat. {feat_str})"
                tags['title'] = [new_title]
                tags['artist'] = [albumartist]
                print(f"  fixed: '{title}' | artist '{artist}' → title '{new_title}', artist '{albumartist}'")
                changed = True
            elif albumartist and artist != albumartist:
                # artist differs from albumartist but no feat. pattern — still normalize
                tags['artist'] = [albumartist]
                print(f"  normalized: artist '{artist}' → '{albumartist}'")
                changed = True

        if changed:
            flac.save()

    print(f"pretag done: {len(files)} files in {album_dir}")
    return True

# Beets writes these as compatibility aliases alongside its canonical fields,
# causing metadata readers to show duplicates (e.g. "2016\\2016-11-18", "1\\1").
BEETS_ALIAS_TAGS = {
    'year',    # duplicate of 'date'
    'track',   # duplicate of 'tracknumber'
    'trackc',  # duplicate of 'tracktotal'
    'discc',   # duplicate of 'disctotal'
}

def posttag(paths):
    """Post-import fixes: strip beets alias tags and truncate DATE to year only."""
    fixed = 0
    for path in paths:
        path = path.strip()
        if not path.endswith('.flac') or not os.path.exists(path):
            continue
        flac = mutagen.flac.FLAC(path)
        changed = False

        # Strip alias tags beets writes for compatibility
        for alias in BEETS_ALIAS_TAGS:
            if alias in flac.tags:
                del flac.tags[alias]
                changed = True

        # Truncate DATE to year only
        date = (flac.tags.get('date') or [''])[0]
        if date and len(date) > 4:
            flac.tags['date'] = [date[:4]]
            changed = True
            print(f"  date truncated: {date} → {date[:4]} ({os.path.basename(path)})")

        if changed:
            flac.save()
            fixed += 1
    print(f"posttag done: {fixed} files updated")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: music-pretag.py <album_dir>", file=sys.stderr)
        print("       music-pretag.py --post  (reads file paths from stdin)", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == '--post':
        paths = sys.stdin.read().splitlines()
        posttag(paths)
    else:
        success = pretag(sys.argv[1])
        sys.exit(0 if success else 1)
