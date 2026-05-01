"""
Fetch-art stage — embed album art into all audio files in an album dir.

Sources (in preference order):
  1. Cover Art Archive — uses musicbrainz_albumid tag
  2. iTunes Search API fallback — uses albumartist + album name

Writes cover.jpg alongside audio files when art is found.

Can be invoked standalone:
    spindlebot fetch-art <album_dir> [--dry-run] [--force]
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
import mutagen.flac
import mutagen.id3
import mutagen.mp4

from spindlebot.disc import find_audio_files

CAA_API = "https://coverartarchive.org/release"
ITUNES_SEARCH_API = "https://itunes.apple.com/search"
_USER_AGENT = "SpindleBot/2.0"


@dataclass
class ArtResult:
    embedded: int = 0
    skipped: int = 0
    missing: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return self.embedded + self.skipped + self.missing + len(self.errors)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _detect_mime(img_data: bytes) -> str:
    """Detect JPEG vs PNG from magic bytes. Defaults to image/jpeg."""
    if img_data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def _has_art(audio_path: str) -> bool:
    """Return True if the file already has embedded album art."""
    try:
        f = mutagen.File(audio_path)
        if f is None:
            return False
        if hasattr(f, "pictures"):                        # FLAC
            return bool(f.pictures)
        if f.tags is None:
            return False
        if any(k.startswith("APIC") for k in f.tags):    # ID3
            return True
        if "covr" in f.tags:                              # MP4
            return True
        return False
    except Exception:
        return False


def _extract_embedded_art(audio_path: str) -> bytes | None:
    """Extract embedded art bytes from an audio file, if any."""
    try:
        f = mutagen.File(audio_path)
        if f is None:
            return None
        if hasattr(f, "pictures") and f.pictures:         # FLAC
            return f.pictures[0].data
        if f.tags is None:
            return None
        for key in f.tags:
            if key.startswith("APIC"):                    # ID3
                return f.tags[key].data
        if "covr" in f.tags:                              # MP4
            return bytes(f.tags["covr"][0])
        return None
    except Exception:
        return None


def _get_album_tags(audio_path: str) -> dict:
    """Read mbid, artist, album from an audio file."""
    try:
        f = mutagen.File(audio_path)
        if f is None or not f.tags:
            return {}
        tags = f.tags

        def _get(key: str) -> str | None:
            val = tags.get(key.lower()) or tags.get(key.upper())
            if val:
                return str(val[0])
            return None

        def _id3(frame: str) -> str | None:
            v = tags.get(frame)
            if v is None:
                return None
            return str(v.text[0]) if hasattr(v, "text") else str(v)

        mbid = _get("musicbrainz_albumid") or _id3("TXXX:MusicBrainz Album Id")
        artist = _get("albumartist") or _get("artist") or _id3("TPE2") or _id3("TPE1")
        album = _get("album") or _id3("TALB")

        return {"mbid": mbid, "artist": artist or "", "album": album or ""}
    except Exception:
        return {}


# ── Art fetching ──────────────────────────────────────────────────────────────


def _fetch_from_caa(mbid: str) -> bytes | None:
    """Fetch front cover image bytes from Cover Art Archive."""
    url = f"{CAA_API}/{mbid}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        img_url = next(
            (img["image"] for img in data.get("images", []) if img.get("front")),
            data["images"][0]["image"] if data.get("images") else None,
        )
        if not img_url:
            return None
        with urllib.request.urlopen(img_url, timeout=30) as r:
            return r.read()
    except Exception:
        return None


def _fetch_from_itunes(artist: str, album: str) -> bytes | None:
    """Fallback: fetch art from iTunes Search API."""
    try:
        q = urllib.parse.quote(f"{artist} {album}")
        url = f"{ITUNES_SEARCH_API}?term={q}&entity=album&limit=3"
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        for result in data.get("results", []):
            img_url = result.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
            if img_url:
                with urllib.request.urlopen(img_url, timeout=30) as r:
                    return r.read()
    except Exception:
        return None
    return None


# ── Art embedding ─────────────────────────────────────────────────────────────


def _embed_art_in_file(audio_path: str, img_data: bytes, mime: str) -> None:
    """Embed album art into a single audio file. Dispatches by extension."""
    ext = os.path.splitext(audio_path)[1].lower().lstrip(".")

    if ext == "flac":
        f = mutagen.flac.FLAC(audio_path)
        pic = mutagen.flac.Picture()
        pic.type = 3
        pic.mime = mime
        pic.data = img_data
        f.clear_pictures()
        f.add_picture(pic)
        f.save()

    elif ext == "mp3":
        try:
            tags = mutagen.id3.ID3(audio_path)
        except mutagen.id3.ID3NoHeaderError:
            tags = mutagen.id3.ID3()
        tags.delall("APIC")
        tags.add(mutagen.id3.APIC(
            encoding=3,
            mime=mime,
            type=3,
            desc="",
            data=img_data,
        ))
        tags.save(audio_path)

    elif ext in {"m4a", "aac", "alac"}:
        f = mutagen.mp4.MP4(audio_path)
        if f.tags is None:
            f.add_tags()
        fmt = (
            mutagen.mp4.MP4Cover.FORMAT_JPEG
            if mime == "image/jpeg"
            else mutagen.mp4.MP4Cover.FORMAT_PNG
        )
        f.tags["covr"] = [mutagen.mp4.MP4Cover(img_data, imageformat=fmt)]
        f.save()

    else:
        # Best-effort ID3 for unknown formats
        try:
            tags = mutagen.id3.ID3(audio_path)
            tags.delall("APIC")
            tags.add(mutagen.id3.APIC(
                encoding=3, mime=mime, type=3, desc="", data=img_data,
            ))
            tags.save(audio_path)
        except Exception:
            pass


# ── Per-album processing ──────────────────────────────────────────────────────


def _process_album(
    album_dir: Path,
    files: list[Path],
    *,
    dry_run: bool = False,
    force: bool = False,
) -> str:
    """
    Fetch and embed art for one album directory.
    Returns one of: "embedded", "skipped", "missing", "error".
    """
    cover_path = album_dir / "cover.jpg"
    if _has_art(str(files[0])) and not force:
        # Art is already embedded. Write the cover.jpg sidecar if it's missing —
        # beet import only moves audio files, so sidecars from the source dir are
        # left behind.
        if not cover_path.exists() and not dry_run:
            img_data = _extract_embedded_art(str(files[0]))
            if img_data:
                cover_path.write_bytes(img_data)
        return "skipped"

    tags = _get_album_tags(str(files[0]))
    mbid = tags.get("mbid")
    artist = tags.get("artist", "")
    album = tags.get("album", "")

    img_data: bytes | None = None

    if mbid:
        img_data = _fetch_from_caa(mbid)

    if not img_data and (artist or album):
        img_data = _fetch_from_itunes(artist, album)

    if not img_data:
        return "missing"

    if dry_run:
        return "embedded"

    mime = _detect_mime(img_data)

    try:
        for path in files:
            _embed_art_in_file(str(path), img_data, mime)
        (album_dir / "cover.jpg").write_bytes(img_data)
    except Exception:
        return "error"

    return "embedded"


# ── Public API ────────────────────────────────────────────────────────────────


def fetch_art(
    album_dir: str | Path,
    cfg,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> ArtResult:
    """Fetch and embed album art for all audio files in album_dir."""
    album_dir = Path(album_dir)
    result = ArtResult()

    files = find_audio_files(album_dir)
    if not files:
        return result

    outcome = _process_album(album_dir, files, dry_run=dry_run, force=force)

    if outcome == "embedded":
        result.embedded += 1
    elif outcome == "skipped":
        result.skipped += 1
    elif outcome == "missing":
        result.missing += 1
    elif outcome == "error":
        result.errors.append(album_dir.name)

    return result
