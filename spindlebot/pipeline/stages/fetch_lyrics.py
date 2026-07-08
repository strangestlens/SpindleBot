"""
Fetch-lyrics stage — write .lrc sidecar files for all audio files in an album dir.

Sources (in preference order):
  1. lrclib.net — synced LRC with timestamps
  2. lrclib.net plain lyrics — wrapped in a minimal LRC envelope
  3. Embedded 'lyrics' tag on the file itself

Per-track terminal state: every track ends in one of two terminal states, or
stays incomplete (to be retried). A `<track>.lrc` means lyrics were found; a
`<track>.nolrc` sibling means lrclib definitively has no lyrics for it (a
successful response with no synced/plain lyrics, including a 404). A *transient*
failure (connection error, timeout, 5xx, or any other exception) writes NOTHING
— the track stays incomplete so a later run retries it.

This miss/error distinction is what makes completion reliable: a transient
network blip must not be swept into a permanent "no lyrics" marker.

Per-album .nolrc marker: retained for backward compatibility and written only
when EVERY track reached a definitive-miss terminal state (no finds AND no
transient errors). The marker is removed if lyrics are found on a later run.

`album_lyrics_complete(album_dir)` is a pure predicate: an album is complete when
every audio file has a terminal sidecar (`.lrc` or `<base>.nolrc`), or the
album-level `.nolrc` is present (which implies every track missed).

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
        # Album-level marker: only when every track reached a definitive-miss
        # terminal state. A transient error (self.errors) means at least one
        # track is still incomplete and retryable, so the album is NOT all-miss
        # and the blanket marker must not be written.
        return self.missing > 0 and self.total_found == 0 and not self.errors


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

        # Sort/romanised fields — written by beet after mbsync, or set manually.
        # artist_sort is standard (VorbisComment: artistsort; ID3: TSOP).
        # title_english is a SpindleBot flex field for non-Latin-script releases
        # where the primary title is in e.g. Japanese but lrclib indexes by
        # English title. Written via `beet modify title_english=...` + mutagen.
        artist_sort = _get("artistsort") or _id3("TSOP") or ""
        title_english = _get("title_english") or ""

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
            "artist_sort": artist_sort,
            "title_english": title_english,
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
    *,
    artist_sort: str = "",
    title_english: str = "",
) -> tuple[str | None, str | None]:
    """
    Try multiple (artist, title, album) combinations against lrclib to maximise
    match rate.

    Primary attempts use the canonical artist + title variants:
      original title → CJK-stripped → filename-derived

    If all primary attempts miss and sort/English fields are available, try the
    romanised combo — useful for non-Latin releases where lrclib indexes by
    English title (e.g. Beck - Hyperspace Japanese pressing).  The romanised
    attempts use a CJK-stripped album name (typically "" for Japanese albums)
    because lrclib stores English album names.
    """
    primary_artist = artist
    title_variants = [title]

    stripped = _strip_cjk(title)
    if stripped and stripped != title:
        title_variants.append(stripped)

    fn_title = _title_from_filename(audio_path)
    if fn_title and fn_title not in title_variants:
        title_variants.append(fn_title)

    # Collect (artist, title, album) triples — primary album for primary attempts
    attempts: list[tuple[str, str, str]] = [
        (primary_artist, t, album) for t in title_variants
    ]

    # English fallback: artist_sort + title_english.
    # lrclib indexes by English names; non-Latin album names cause mismatches,
    # so the fallback attempts use an empty album string.
    fallback_artist = artist_sort if artist_sort and artist_sort != artist else ""
    if title_english and title_english != title:
        if fallback_artist:
            attempts.append((fallback_artist, title_english, ""))
        attempts.append((primary_artist, title_english, ""))
    elif fallback_artist:
        # Sort artist with CJK-stripped album (covers partial-CJK titles)
        fallback_album = _strip_cjk(album)
        for t in title_variants:
            attempts.append((fallback_artist, t, fallback_album))

    last_exc = None
    for i, (a, t, alb) in enumerate(attempts):
        if i > 0:
            time.sleep(request_delay)
        for dur in [duration, None]:
            try:
                synced, plain = _query_lrclib(a, t, alb, dur, request_delay)
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

    Terminal states: "synced"/"plain" write `<base>.lrc`; "missing" (a definitive
    lrclib miss) writes `<base>.nolrc`. "error" is transient — it writes nothing so
    the track stays incomplete and is retried on a later run.

    Both terminal sidecars short-circuit re-query when present (unless `force`): an
    existing `.lrc` returns "skipped"; an existing `.nolrc` returns "missing"
    without hitting lrclib, so a definitive miss is never re-queried.
    """
    base = os.path.splitext(audio_path)[0]
    lrc_path = base + ".lrc"
    nolrc_path = base + ".nolrc"

    if not force:
        if os.path.exists(lrc_path):
            return "skipped"
        if os.path.exists(nolrc_path):
            # Definitive miss recorded on a prior run — terminal, don't re-query.
            return "missing"

    tags = _get_tags(audio_path)
    if "_error" in tags:
        return "error"

    artist = tags.get("artist")
    title = tags.get("title")
    if not artist or not title:
        return "skipped"

    album = tags.get("album", "")
    duration = tags.get("duration", 0)
    artist_sort = tags.get("artist_sort", "")
    title_english = tags.get("title_english", "")

    try:
        synced, plain = _fetch_from_lrclib(
            artist, title, album, duration, audio_path, request_delay,
            artist_sort=artist_sort,
            title_english=title_english,
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
        # Definitive miss: a successful lrclib response (incl. 404) with no
        # lyrics. Write the per-track terminal marker so this track is complete
        # and never re-queried. Transient failures never reach here — they raise
        # and are caught above as "error", writing nothing.
        if not dry_run:
            try:
                with open(nolrc_path, "w", encoding="utf-8") as fh:
                    fh.write("")
            except OSError:
                return "error"
        return "missing"

    if not dry_run:
        # Clear any stale per-track miss marker from a prior run BEFORE writing
        # the .lrc, so a partial failure can never leave the track carrying both
        # .lrc and .nolrc (contradictory terminal state). "Already gone" is the
        # normal case; any other removal failure aborts as a transient error,
        # leaving the prior .nolrc intact (consistent, retried next run).
        try:
            os.remove(nolrc_path)
        except FileNotFoundError:
            pass
        except OSError:
            return "error"
        try:
            with open(lrc_path, "w", encoding="utf-8") as fh:
                fh.write(lrc_content + "\n")
        except OSError:
            return "error"

    return outcome


# ── Public API ────────────────────────────────────────────────────────────────


def album_lyrics_complete(album_dir: str | Path) -> bool:
    """Return True iff every audio file in `album_dir` is in a terminal lyric state.

    A track is terminal when a `<base>.lrc` (found) OR a `<base>.nolrc`
    (definitive miss) sits beside it. An album-level `.nolrc` short-circuits to
    complete — it is only ever written when every track missed.

    Pure: reads the directory listing only, writes nothing. An album with no
    audio files is trivially complete.
    """
    album_dir = Path(album_dir)
    if (album_dir / ".nolrc").exists():
        return True
    for audio in find_audio_files(album_dir):
        # os.path.splitext (not Path.with_suffix) — a stem like "01. Title"
        # contains dots that with_suffix would mangle. Matches _process_file.
        base = os.path.splitext(str(audio))[0]
        if os.path.exists(base + ".lrc") or os.path.exists(base + ".nolrc"):
            continue
        return False
    return True


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
