"""Discogs collection provider.

Two halves, deliberately separable:

  `to_items()`     pure — Discogs payload → CollectionItem. Every Discogs quirk
                   lives here and is tested against a recorded fixture.
  `DiscogsClient`  impure — paging, rate limiting, caching, HTTP.

Auth is optional. A public collection reads with no credentials at all; a
personal access token raises the rate limit from 25 to 60 requests/minute and
is required for a private collection.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

from spindlebot.core.collection import CollectionItem
from spindlebot.core.enums import MediaKind
from spindlebot.core.errors import CollectionFetchError

API_ROOT = "https://api.discogs.com"
DEFAULT_USER_AGENT = "SpindleBot/0.3 +https://github.com/strangestlens/music-pipeline"
PER_PAGE = 100          # documented maximum; larger values are accepted but unsupported
ALL_FOLDER = 0          # Discogs' synthetic "All" folder
UNAUTH_RPM = 25
AUTH_RPM = 60
RATE_MARGIN = 1.1       # stay comfortably inside a moving-average window
MAX_RETRIES = 3
RETRY_FALLBACK_SECONDS = 60.0   # no/unparseable Retry-After: one rate-limit window
MAX_RETRY_SECONDS = 300.0       # never honour an absurd Retry-After

# Discogs format name → medium. Names not listed map to OTHER, which covers the
# container pseudo-formats ("Box Set", "All Media") that always accompany the
# real medium on a release — every format entry is inspected, so a 3×CD box set
# still reads as a CD.
FORMAT_MEDIA: dict[str, MediaKind] = {
    "CD": MediaKind.CD,
    "CDr": MediaKind.CD,
    "CD-ROM": MediaKind.CD,
    "HDCD": MediaKind.CD,
    "SACD": MediaKind.CD,
    "SHM-CD": MediaKind.CD,
    "Blu-spec CD": MediaKind.CD,
    "Mini-CD": MediaKind.CD,
    "Vinyl": MediaKind.VINYL,
    "Acetate": MediaKind.VINYL,
    "Flexi-disc": MediaKind.VINYL,
    "Shellac": MediaKind.VINYL,
    "Lathe Cut": MediaKind.VINYL,
    "Cassette": MediaKind.CASSETTE,
    "8-Track Cartridge": MediaKind.CASSETTE,
    "Reel-To-Reel": MediaKind.CASSETTE,
    "DAT": MediaKind.CASSETTE,
    "Microcassette": MediaKind.CASSETTE,
    "File": MediaKind.DIGITAL,
    "Digital Media": MediaKind.DIGITAL,
}

_TIGHT_PUNCT = re.compile(r"\s+([,;])")
_SAFE_HANDLE = re.compile(r"[^A-Za-z0-9._-]")

# Discogs writes dual-script titles as "ネイティブ = Latin". Either side may be
# what the library was tagged with, so both become candidates.
_SCRIPT_SPLIT = " = "


# ── Pure transformer ─────────────────────────────────────────────────────────

def _artist_display(artists: list[dict]) -> str:
    """Rebuild the credited artist string from Discogs' per-artist join tokens.

    `join` is the separator that follows an artist ("&", ",", "Feat."), and
    `anv` is the name as printed on the release, which is what a rip is usually
    tagged with when it differs from Discogs' canonical name.
    """
    parts: list[str] = []
    last = len(artists) - 1
    for i, a in enumerate(artists):
        parts.append((a.get("anv") or a.get("name") or "").strip())
        join = (a.get("join") or "").strip()
        if join and i < last:
            parts.append(join)
    return _TIGHT_PUNCT.sub(r"\1", " ".join(p for p in parts if p)).strip()


def _artist_alts(artists: list[dict], display: str) -> tuple[str, ...]:
    """Every other plausible spelling of the credited artist."""
    alts: list[str] = []
    canonical = " ".join((a.get("name") or "").strip() for a in artists if a.get("name"))
    alts.append(canonical)
    if artists:
        first = artists[0]
        alts.append((first.get("anv") or "").strip())
        alts.append((first.get("name") or "").strip())
    if display.casefold().startswith("various"):
        # Discogs credits compilations to "Various"; beets/MusicBrainz use the
        # full "Various Artists".
        alts.append("Various Artists")
    seen = {display}
    out: list[str] = []
    for alt in alts:
        if alt and alt not in seen:
            seen.add(alt)
            out.append(alt)
    return tuple(out)


def _title_alts(title: str) -> tuple[str, ...]:
    if _SCRIPT_SPLIT not in title:
        return ()
    return tuple(
        part.strip() for part in title.split(_SCRIPT_SPLIT) if part.strip()
    )


def _media(formats: list[dict]) -> frozenset[MediaKind]:
    return frozenset(
        FORMAT_MEDIA.get((f.get("name") or "").strip(), MediaKind.OTHER)
        for f in formats
    )


def _catno(labels: list[dict]) -> str | None:
    for label in labels:
        catno = (label.get("catno") or "").strip()
        if catno and catno.casefold() != "none":
            return catno
    return None


def to_item(release: dict) -> CollectionItem | None:
    """Map one collection release to a CollectionItem, or None if unusable."""
    info = release.get("basic_information") or {}
    release_id = release.get("id") or info.get("id")
    title = (info.get("title") or "").strip()
    if release_id is None or not title:
        return None

    artists = info.get("artists") or []
    display = _artist_display(artists)
    year = info.get("year") or None  # Discogs writes 0 for "unknown"
    thumb = (info.get("thumb") or "").strip() or None

    return CollectionItem(
        source="discogs",
        source_id=str(release_id),
        artist=display,
        title=title,
        media=_media(info.get("formats") or []),
        year=int(year) if year else None,
        catno=_catno(info.get("labels") or []),
        artist_alts=_artist_alts(artists, display),
        title_alts=_title_alts(title),
        url=f"https://www.discogs.com/release/{release_id}",
        thumb_url=thumb,
    )


def to_items(releases: list[dict]) -> list[CollectionItem]:
    """Map a list of collection releases, dropping any that lack id or title."""
    items = [to_item(r) for r in releases]
    return [i for i in items if i is not None]


# ── HTTP ─────────────────────────────────────────────────────────────────────

# (url, headers) -> (status, headers, body). Injected so tests never touch
# the network.
Fetcher = Callable[[str, dict], tuple[int, dict, bytes]]


def _urlopen_fetch(url: str, headers: dict) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except urllib.error.URLError as e:
        raise CollectionFetchError(f"Discogs request failed: {e.reason}") from e


@dataclass
class DiscogsClient:
    """Paged, rate-limited, cached reader for a Discogs collection."""

    token: str = ""
    user_agent: str = DEFAULT_USER_AGENT
    cache_dir: Path | None = None
    cache_ttl_hours: float = 24.0
    fetcher: Fetcher = _urlopen_fetch
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        rpm = AUTH_RPM if self.token else UNAUTH_RPM
        self._min_interval = (60.0 / rpm) * RATE_MARGIN
        self._last_request = 0.0

    # ── cache ────────────────────────────────────────────────────────────────

    def cache_path(self, account: str) -> Path | None:
        if self.cache_dir is None:
            return None
        safe = _SAFE_HANDLE.sub("_", account)
        return self.cache_dir / f"discogs-{safe}.json"

    def _read_cache(self, account: str, *, ignore_ttl: bool = False) -> list[dict] | None:
        path = self.cache_path(account)
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not ignore_ttl:
            age_hours = (self.now() - float(payload.get("fetched_utc", 0))) / 3600.0
            if age_hours > self.cache_ttl_hours:
                return None
        releases = payload.get("releases")
        return releases if isinstance(releases, list) else None

    def _write_cache(self, account: str, releases: list[dict]) -> None:
        path = self.cache_path(account)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "source": "discogs",
            "account": account,
            "fetched_utc": int(self.now()),
            "releases": releases,
        }))

    # ── requests ─────────────────────────────────────────────────────────────

    def _throttle(self) -> None:
        elapsed = self.now() - self._last_request
        if self._last_request and elapsed < self._min_interval:
            self.sleep(self._min_interval - elapsed)

    def _headers(self) -> dict:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Discogs token={self.token}"
        return headers

    def _retry_delay(self, headers: dict) -> float:
        """How long to wait after a 429.

        RFC 9110 allows Retry-After to be either delta-seconds or an HTTP-date,
        and a server is free to send neither. Every branch falls back rather
        than raising — a malformed header must not abort a fetch mid-page. The
        upper clamp stops a bogus far-future date from hanging the run.
        """
        raw = (headers.get("Retry-After") or headers.get("retry-after") or "").strip()
        if not raw:
            return RETRY_FALLBACK_SECONDS
        try:
            return max(0.0, min(float(raw), MAX_RETRY_SECONDS))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return RETRY_FALLBACK_SECONDS
        if when is None:
            return RETRY_FALLBACK_SECONDS
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        delta = (when - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, min(delta, MAX_RETRY_SECONDS))

    def _get(self, url: str) -> dict:
        for attempt in range(MAX_RETRIES):
            self._throttle()
            status, headers, body = self.fetcher(url, self._headers())
            self._last_request = self.now()

            if status == 200:
                try:
                    return json.loads(body)
                except json.JSONDecodeError as e:
                    raise CollectionFetchError(
                        f"Discogs returned malformed JSON for {url}"
                    ) from e
            if status == 429:
                if attempt == MAX_RETRIES - 1:
                    break
                self.sleep(self._retry_delay(headers))
                continue
            if status == 404:
                raise CollectionFetchError(
                    "Discogs user not found — check the handle "
                    "(a private collection also reads as missing without a token)"
                )
            if status in (401, 403):
                raise CollectionFetchError(
                    "Discogs refused the request — a private collection needs a "
                    "personal access token in secrets.toml ([discogs] token)"
                )
            raise CollectionFetchError(f"Discogs returned HTTP {status} for {url}")
        raise CollectionFetchError(
            "Discogs rate limit exceeded after retries — try again shortly, or "
            "add a token to raise the limit from 25 to 60 requests/minute"
        )

    def fetch_raw(
        self, account: str, *, refresh: bool = False, cached_only: bool = False
    ) -> list[dict]:
        """Every release in the account's collection, as raw Discogs payloads.

        `cached_only` never touches the network — callers that merely want nice
        labels shouldn't be able to stall on HTTP. It deliberately ignores the
        TTL: a release's artist and title don't go stale, so a day-old cache is
        a perfectly good answer, and the alternative is no answer at all.
        """
        if cached_only:
            return self._read_cache(account, ignore_ttl=True) or []
        if not refresh:
            cached = self._read_cache(account)
            if cached is not None:
                return cached

        releases: list[dict] = []
        page = 1
        while True:
            url = (
                f"{API_ROOT}/users/{account}/collection/folders/{ALL_FOLDER}"
                f"/releases?per_page={PER_PAGE}&page={page}"
            )
            payload = self._get(url)
            releases.extend(payload.get("releases") or [])
            pages = int((payload.get("pagination") or {}).get("pages") or 1)
            if page >= pages:
                break
            page += 1

        self._write_cache(account, releases)
        return releases


@dataclass
class DiscogsProvider:
    """CollectionProvider over a DiscogsClient."""

    client: DiscogsClient
    name: str = "discogs"

    @classmethod
    def from_config(cls, cfg) -> DiscogsProvider:
        return cls(client=DiscogsClient(
            token=cfg.secrets.discogs.token,
            cache_dir=cfg.collection.cache_dir,
            cache_ttl_hours=cfg.collection.cache_ttl_hours,
        ))

    def fetch(
        self, account: str, *, refresh: bool = False, cached_only: bool = False
    ) -> list[CollectionItem]:
        return to_items(
            self.client.fetch_raw(account, refresh=refresh, cached_only=cached_only)
        )
