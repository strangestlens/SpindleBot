"""Match external collection items against the digital library. Pure.

The matcher is **artist-scoped**: it resolves the artist first, then compares
titles only within that artist's albums. This is not a performance choice — a
whole-string similarity over "artist title" does not work here. Album titles
diverge between a Discogs release and a MusicBrainz-tagged rip in ways that
sink a global score while an artist-scoped comparison sails through:

    Ummagumma            vs  Ummagumma - Live Album          (multi-disc suffix)
    Mezzanine            vs  Mezzanine - Mezzanine Mad Professor
    The Fame             vs  The Fame Monster - The Fame
    MTV Unplugged        vs  MTV Unplugged in New York       (differing release names)
    James Taylor's Greatest Hits  vs  Greatest Hits

Measured against a real 152-CD collection and a 112-album library, every one of
those scored below 0.79 on whole-string similarity — indistinguishable from
genuinely absent albums — and all were recovered by scoping to the artist and
allowing token-boundary containment.

Three outcomes, not two. `UNCERTAIN` exists because the whole value of the
audit is a *trustworthy* missing list: a normalization miss should send you to
review a row, not to the shelf to re-rip something you already own.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

from spindlebot.core.collection import CollectionItem, LibraryAlbum

# ── Thresholds ───────────────────────────────────────────────────────────────
# Calibrated against real data (see module docstring). The gap between the
# highest-scoring true negative (0.47) and the lowest true positive recovered by
# containment is wide, so these are not knife-edge values.
ARTIST_TOKEN_MIN = 0.60   # Jaccard over artist tokens — survives word reordering
ARTIST_SEQ_MIN = 0.88     # character similarity — survives "Pulselovers"/"Pulse Lovers"
TITLE_UNCERTAIN_MIN = 0.60  # below this, a title is a different album, not a near-miss
MIN_CONTAIN_CHARS = 3     # a 1-2 char title inside another proves nothing

# Parenthetical/bracketed content is dropped only when it reads as an edition
# marker. Blanket-stripping parentheses destroys titles where the parenthetical
# IS the title, and "remix" has to be gated this way too: "(2018 Remix)" is an
# edition, while "Mezzanine Mad Professor" is a genuinely different record.
EDITION_WORDS = frozenset({
    "anniversary", "bonus", "collectors", "definitive", "deluxe", "disc",
    "edition", "expanded", "explicit", "japan", "japanese", "limited", "mix",
    "mono", "reissue", "remaster", "remastered", "remix", "special", "stereo",
    "version",
})

_DISAMBIG = re.compile(r"\s*\(\d+\)\s*$")     # Discogs "Ogle (2)", "Aurora (16)"
_BRACKETED = re.compile(r"[(\[]([^)\]]*)[)\]]")
# Unicode-aware: keeps letters and digits in ANY script. An ASCII-only class
# would collapse a katakana or Cyrillic title to the empty string, which throws
# away the one thing a dual-script release has to match on.
_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)


class MatchStatus(StrEnum):
    OWNED = "owned"          # confidently in the library
    UNCERTAIN = "uncertain"  # plausible match a human should confirm
    MISSING = "missing"      # not in the library


@dataclass(frozen=True)
class ItemMatch:
    item: CollectionItem
    status: MatchStatus
    matched: LibraryAlbum | None
    reason: str
    score: float = 0.0


# ── Normalization ────────────────────────────────────────────────────────────

def _strip_editions(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        words = set(re.findall(r"[a-z]+", m.group(1).casefold()))
        return " " if words & EDITION_WORDS else m.group(0)
    return _BRACKETED.sub(repl, text)


def _strip_diacritics(text: str) -> str:
    """Fold accents off Latin letters, leaving other scripts intact.

    Dropping every combining mark would also strip Japanese dakuten — パ would
    decay to ハ, which is a different kana, not a decorated one. The mark is
    only removed when it decorates an ASCII base character.
    """
    out: list[str] = []
    for ch in unicodedata.normalize("NFKD", text):
        if unicodedata.combining(ch) and out and out[-1].isascii():
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def _fold(text: str) -> str:
    text = _strip_diacritics(text)
    return text.casefold().replace("&", " and ").replace("+", " and ")


def normalize_artist(name: str | None) -> str:
    """Fold an artist name to a comparable key.

    Drops the Discogs disambiguation suffix ("Ogle (2)"), a leading article,
    diacritics, and punctuation. The leading-article strip is artist-only —
    "The Fame" is an album title where "The" is load-bearing.
    """
    if not name:
        return ""
    text = _fold(_strip_editions(_DISAMBIG.sub("", name)))
    if text.startswith("the "):
        text = text[4:]
    return _NON_ALNUM.sub(" ", text).strip()


def normalize_title(title: str | None) -> str:
    """Fold an album title to a comparable key."""
    if not title:
        return ""
    return _NON_ALNUM.sub(" ", _fold(_strip_editions(title))).strip()


# ── Similarity primitives ────────────────────────────────────────────────────

def _jaccard(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _seq(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _contains(a: str, b: str) -> bool:
    """True when one normalized title sits inside the other at token boundaries.

    Substring-anywhere would match "her" inside "otherness"; padding both sides
    with spaces confines the test to whole tokens.
    """
    if not a or not b:
        return False
    if min(len(a), len(b)) < MIN_CONTAIN_CHARS:
        return False
    return f" {a} " in f" {b} " or f" {b} " in f" {a} "


# ── Matching ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _NormalizedAlbum:
    album: LibraryAlbum
    artist_key: str
    title_key: str


def _index_library(library: list[LibraryAlbum]) -> list[_NormalizedAlbum]:
    return [
        _NormalizedAlbum(
            album=a,
            artist_key=normalize_artist(a.albumartist),
            title_key=normalize_title(a.album),
        )
        for a in library
    ]


def _artist_candidates(
    item: CollectionItem, indexed: list[_NormalizedAlbum]
) -> list[_NormalizedAlbum]:
    keys = {normalize_artist(a) for a in item.all_artists} - {""}
    if not keys:
        return []
    exact = [n for n in indexed if n.artist_key in keys]
    if exact:
        return exact
    return [
        n for n in indexed
        if any(
            _jaccard(n.artist_key, k) >= ARTIST_TOKEN_MIN
            or _seq(n.artist_key, k) >= ARTIST_SEQ_MIN
            for k in keys
        )
    ]


def match_item(item: CollectionItem, indexed: list[_NormalizedAlbum]) -> ItemMatch:
    """Classify one collection item against a pre-normalized library."""
    if item.mb_release_id:
        for n in indexed:
            if n.album.mb_albumid and n.album.mb_albumid == item.mb_release_id:
                return ItemMatch(item, MatchStatus.OWNED, n.album, "mbid", 1.0)

    candidates = _artist_candidates(item, indexed)
    if not candidates:
        return ItemMatch(item, MatchStatus.MISSING, None, "no matching artist")

    title_keys = [normalize_title(t) for t in item.all_titles]
    title_keys = [t for t in title_keys if t]

    for n in candidates:
        if n.title_key in title_keys:
            return ItemMatch(item, MatchStatus.OWNED, n.album, "exact", 1.0)

    for n in candidates:
        if any(_contains(t, n.title_key) for t in title_keys):
            return ItemMatch(item, MatchStatus.OWNED, n.album, "contains", 1.0)

    best_score, best = 0.0, None
    for n in candidates:
        score = max(
            (max(_jaccard(t, n.title_key), _seq(t, n.title_key)) for t in title_keys),
            default=0.0,
        )
        if score > best_score:
            best_score, best = score, n

    if best is not None and best_score >= TITLE_UNCERTAIN_MIN:
        return ItemMatch(item, MatchStatus.UNCERTAIN, best.album, "similar title", best_score)
    return ItemMatch(
        item, MatchStatus.MISSING,
        best.album if best else None,
        "no matching title", best_score,
    )


def match_items(
    items: list[CollectionItem], library: list[LibraryAlbum]
) -> list[ItemMatch]:
    """Classify every collection item against the library. Input order preserved."""
    indexed = _index_library(library)
    return [match_item(item, indexed) for item in items]
