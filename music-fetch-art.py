#!/usr/bin/env python3
# music-fetch-art.py — embed album art into FLAC files that are missing it
# Reads album dirs from stdin (one per line), checks for embedded art,
# fetches from Cover Art Archive using musicbrainz_albumid tag if missing.

import sys, os, glob, urllib.request, json
import mutagen.flac

def fetch_caa(mbid):
    """Fetch front cover image bytes from Cover Art Archive."""
    url = f"https://coverartarchive.org/release/{mbid}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "music-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        img_url = next(
            (img["image"] for img in data.get("images", []) if img.get("front")),
            data["images"][0]["image"] if data.get("images") else None
        )
        if not img_url:
            return None
        with urllib.request.urlopen(img_url, timeout=30) as r:
            return r.read()
    except Exception as e:
        print(f"  CAA fetch failed ({mbid}): {e}", file=sys.stderr)
        return None

def fetch_itunes(artist, album):
    """Fallback: fetch art from iTunes Store."""
    try:
        q = urllib.request.quote(f"{artist} {album}")
        url = f"https://itunes.apple.com/search?term={q}&entity=album&limit=3"
        req = urllib.request.Request(url, headers={"User-Agent": "music-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        for result in data.get("results", []):
            img_url = result.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
            if img_url:
                with urllib.request.urlopen(img_url, timeout=30) as r:
                    return r.read()
    except Exception as e:
        print(f"  iTunes fetch failed: {e}", file=sys.stderr)
    return None

def embed_art(album_dir):
    files = get_flac_files(album_dir)
    if not files:
        return

    # Check if any file already has art
    first = mutagen.flac.FLAC(files[0])
    if first.pictures:
        return  # already has art

    # Get MusicBrainz album ID from tags
    mbid = (first.tags.get("musicbrainz_albumid") or [""])[0]
    if not mbid:
        print(f"  no mbid for {album_dir}, skipping")
        return

    # Get artist/album from tags for iTunes fallback
    artist = (first.tags.get("albumartist") or first.tags.get("artist") or [""])[0]
    album = (first.tags.get("album") or [""])[0]

    img_data = None
    if mbid:
        print(f"  fetching art for {os.path.basename(album_dir)} (CAA)")
        img_data = fetch_caa(mbid)
    if not img_data:
        print(f"  trying iTunes for {os.path.basename(album_dir)}")
        img_data = fetch_itunes(artist, album)
    if not img_data:
        print(f"  no art found for {os.path.basename(album_dir)}")
        return

    pic = mutagen.flac.Picture()
    pic.type = 3  # Front cover
    pic.mime = "image/jpeg"
    pic.data = img_data

    for path in files:
        f = mutagen.flac.FLAC(path)
        f.clear_pictures()
        f.add_picture(pic)
        f.save()

    # Also save cover.jpg alongside
    cover_path = os.path.join(album_dir, "cover.jpg")
    with open(cover_path, "wb") as f:
        f.write(img_data)

    print(f"  embedded art in {len(files)} files: {os.path.basename(album_dir)}")

def find_album_dirs(root):
    """Walk root and yield leaf directories that contain FLAC files."""
    for dirpath, dirnames, filenames in os.walk(root):
        if any(f.endswith(".flac") for f in filenames):
            yield dirpath

def get_flac_files(album_dir):
    """Get FLAC files using os.listdir to avoid glob misinterpreting [Disk N] in paths."""
    return sorted(
        os.path.join(album_dir, f)
        for f in os.listdir(album_dir)
        if f.endswith(".flac")
    )

if __name__ == "__main__":
    if len(sys.argv) > 1:
        roots = sys.argv[1:]
    else:
        roots = [line.strip() for line in sys.stdin if line.strip()]

    for root in roots:
        for album_dir in find_album_dirs(root):
            embed_art(album_dir)
