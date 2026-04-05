"""
Fetch-lyrics stage — write .lrc sidecar files for all audio files in an album dir.

Sources (in preference order):
  1. lrclib.net — synced LRC with timestamps
  2. lrclib.net plain lyrics — wrapped in a minimal LRC envelope
  3. Embedded 'lyrics' tag on the file itself

Per-album .nolrc marker: if every file in the album comes up empty, a .nolrc
file is written alongside so future runs (and the sync gap-fill) can skip it
without making redundant API calls. The marker is removed if lyrics are found
on a later run.

Can be invoked standalone:
    spindlebot fetch-lyrics <album_dir> [--dry-run] [--force]
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import mutagen

from spindlebot.disc import find_audio_files

LRCLIB_API = "https://lrclib.net/api/get"


@dataclass
class LyricsResult:
    synced: int = 0
    plain: int = 0
    skipped: int = 0
    missing: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_found(self) -> int:
        return self.synced + self.plain

    @property
    def nolrc_written(self) -> bool:
        return self.missing > 0 and self.total_found == 0


# ── Tag reading ───────────────────────────────────────────────────────────────


def _get_tags(audio_path: str) -> dict:
    """
    Read artist, title, album, duration, and any embedded lyrics from an audio
    file. Uses mutagen.File() so all supported formats are handled uniformly.
    """
    try:
        f = mutagen.File(audio_path)
        if f is None or not f.tags:
            return {}

        tags = f.tags

        def _get(key: str) -> str | None:
            # VorbisComment: lowercase keys, list values
            val = tags.get(key.lower()) or tags.get(key.upper())
            if val:
                return str(val[0])
            # ID3: frame keys like TIT2, TPE1, TALB, USLT
            return None

        # ID3 fallback for common frames
        def _id3(frame: str) -> str | None:
            v = tags.get(frame)
            if v is None:
                return None
            return str(v.text[0]) if hasattr(v, "text") else str(v)

        artist = _get("artist") or _id3("TPE1")
        title = _get("title") or _id3("TIT2")
        album = _get("album") or _id3("TALB")
        duration = int(f.info.length) if f.info else 0

        # Embedded lyrics: VorbisComment 'lyrics'/'unsyncedlyrics', or ID3 USLT
        lyrics = _get("lyrics") or _get("unsyncedlyrics")
        if not lyrics:
            uslt = tags.get("USLT") or tags.get("USLT::")
            if uslt:
                lyrics = str(uslt.text) if hasattr(uslt, "text") else str(uslt)

        return {
            "artist": artist,
            "title": title,
            "album": album or "",
            "duration": duration,
            "lyrics": lyrics,
        }
    except Exception as exc:
        return {"_error": str(exc)}


# ── CJK stripping ─────────────────────────────────────────────────────────────


def _strip_cjk(text: str) -> str:
    """
    Remove CJK/Japanese characters and clean up leftover punctuation.
    Used to generate a fallback search term for Japanese-titled tracks
    whose lrclib entries may only have the romanised title.
    """
    cleaned = re.sub(r"[\u3000-\u9fff\uf900-\ufaff\ufe30-\ufe4f]+", "", text)
    cleaned = re.sub(r"^[\s/\-–—·・]+|[\s/\-–—·・]+$", "", cleaned)
    return cleaned.strip()


def _title_from_filename(path: str) -> str | None:
    name = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"^\d+[-.]?\d*[.\s]+", "", name).strip()
    return name or None


# ── lrclib API ────────────────────────────────────────────────────────────────


def _query_lrclib(
    artist: str,
    title: str,
    album: str,
    duration: int | None,
    request_delay: float,
) -> tuple[str | None, str | None]:
    """Return (synced_lrc, plain_lyrics) from lrclib, or (None, None) on miss."""
    params: dict = {"artist_name": artist, "track_name": title, "album_name": album}
    if duration:
        params["duration"] = duration

    url = LRCLIB_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "SpindleBot/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
            time.sleep(request_delay)
            return data.get("syncedLyrics"), data.get("plainLyrics")
    except urllib.error.HTTPError as exc:
        time.sleep(request_delay)
        if exc.code == 404:
            return None, None
        raise
    except Exception:
        time.sleep(request_delay)
        raise


def _fetch_from_lrclib(
    artist: str,
    title: str,
    album: str,
    duration: int,
    audio_path: str,
    request_delay: float,
) -> tuple[str | None, str | None]:
    """
    Try multiple title variants against lrclib to maximise match rate.
    Variants: original title → CJK-stripped → filename-derived.
    Each variant is tried with duration first, then without.
    """
    attempts = [title]

    stripped = _strip_cjk(title)
    if stripped and stripped != title:
        attempts.append(stripped)

    fn_title = _title_from_filename(audio_path)
    if fn_title and fn_title not in attempts:
        attempts.append(fn_title)

    last_exc = None
    for i, t in enumerate(attempts):
        if i > 0:
            time.sleep(request_delay)
        for dur in [duration, None]:
            try:
                synced, plain = _query_lrclib(artist, t, album, dur, request_delay)
                if synced or plain:
                    return synced, plain
            except Exception as exc:
                last_exc = exc

    if last_exc:
        raise last_exc

    return None, None


def _plain_to_lrc(text: str) -> str:
    """Wrap plain-text lyrics in a minimal LRC envelope with timestamp 00:00.00."""
    lines = text.strip().splitlines()
    return "\n".join(
        f"[00:00.00] {line}" if line.strip() else "" for line in lines
    )


# ── Per-file processing ───────────────────────────────────────────────────────


def _process_file(
    audio_path: str,
    request_delay: float,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> str:
    """
    Fetch and write lyrics for a single audio file.
    Returns one of: "synced", "plain", "skipped", "missing", "error".
    """
    lrc_path = os.path.splitext(audio_path)[0] + ".lrc"

    if os.path.exists(lrc_path) and not force:
        return "skipped"

    tags = _get_tags(audio_path)
    if "_error" in tags:
        return "error"

    artist = tags.get("artist")
    title = tags.get("title")
    if not artist or not title:
        return "skipped"

    album = tags.get("album", "")
    duration = tags.get("duration", 0)

    try:
        synced, plain = _fetch_from_lrclib(
            artist, title, album, duration, audio_path, request_delay
        )
    except Exception:
        return "error"

    if synced:
        lrc_content = synced
        outcome = "synced"
    elif plain:
        lrc_content = _plain_to_lrc(plain)
        outcome = "plain"
    elif tags.get("lyrics"):
        lrc_content = _plain_to_lrc(tags["lyrics"])
        outcome = "plain"
    else:
        return "missing"

    if not dry_run:
        try:
            with open(lrc_path, "w", encoding="utf-8") as fh:
                fh.write(lrc_content + "\n")
        except OSError:
            return "error"

    return outcome


# ── Public API ────────────────────────────────────────────────────────────────


def fetch_lyrics(
    album_dir: str | Path,
    cfg,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> LyricsResult:
    """
    Fetch .lrc sidecar files for all audio files in album_dir.

    cfg is a SpindleBotConfig. request_delay comes from cfg.lyrics.request_delay_seconds.
    """
    album_dir = Path(album_dir)
    result = LyricsResult()
    request_delay = cfg.lyrics.request_delay_seconds

    files = find_audio_files(album_dir)
    if not files:
        return result

    nolrc_marker = album_dir / ".nolrc"

    for path in files:
        outcome = _process_file(
            str(path),
            request_delay,
            dry_run=dry_run,
            force=force,
        )
        if outcome == "synced":
            result.synced += 1
        elif outcome == "plain":
            result.plain += 1
        elif outcome == "skipped":
            result.skipped += 1
        elif outcome == "missing":
            result.missing += 1
        elif outcome == "error":
            result.errors.append(path.name)

    # Manage the .nolrc marker: write if everything came up empty,
    # remove if we found lyrics this time.
    if not dry_run:
        if result.nolrc_written:
            try:
                nolrc_marker.touch()
            except OSError:
                pass
        elif result.total_found > 0 and nolrc_marker.exists():
            nolrc_marker.unlink(missing_ok=True)

    return result
