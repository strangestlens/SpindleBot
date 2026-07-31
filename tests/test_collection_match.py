"""
Tests for spindlebot.core.collection_match.

The match table below is the contract. Most rows are real pairs taken from a
Discogs collection and the beets library it was audited against — the cases
that a whole-string similarity match got wrong are called out as such.
"""
from __future__ import annotations

import pytest

from spindlebot.core.collection import CollectionItem, LibraryAlbum, resolve_media
from spindlebot.core.collection_match import (
    MatchStatus,
    match_items,
    normalize_artist,
    normalize_title,
)
from spindlebot.core.enums import MediaKind


def _item(artist: str, title: str, **kw) -> CollectionItem:
    return CollectionItem(
        source="test", source_id=kw.pop("source_id", "1"),
        artist=artist, title=title,
        media=frozenset({MediaKind.CD}), **kw,
    )


def _album(artist: str, album: str, **kw) -> LibraryAlbum:
    return LibraryAlbum(albumartist=artist, album=album, **kw)


def _status(item: CollectionItem, library: list[LibraryAlbum]) -> MatchStatus:
    return match_items([item], library)[0].status


# ── normalization ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Ogle (2)", "ogle"),                    # Discogs disambiguation suffix
    ("Aurora (16)", "aurora"),
    ("James Taylor (2)", "james taylor"),
    ("The Mars Volta", "mars volta"),        # leading article
    ("Björk", "bjork"),                      # diacritics
    ("Simon & Garfunkel", "simon and garfunkel"),
    ("Sleater‐Kinney", "sleater kinney"),    # non-ASCII hyphen
    ("", ""),
    (None, ""),
])
def test_normalize_artist(raw, expected):
    assert normalize_artist(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Blind Man’s Zoo", "blind man s zoo"),  # typographic apostrophe
    ("Animals (2018 Remix)", "animals"),     # edition parenthetical is dropped
    ("Kid A (Deluxe Edition)", "kid a"),
    ("OK Computer [Remastered]", "ok computer"),
    ("The Fame", "the fame"),                # article kept in titles
])
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


def test_non_edition_parenthetical_is_preserved():
    """Blanket-stripping parentheses would destroy titles that need them."""
    assert normalize_title("Doolittle (I Bleed)") == "doolittle i bleed"


def test_non_latin_scripts_survive_normalization():
    """An ASCII-only fold would empty these out, losing the only thing a
    dual-script release can be matched on."""
    assert normalize_title("ハイパースペース(2020)") == "ハイパースペース 2020"
    assert normalize_artist("ベック") == "ベック"


def test_a_non_latin_library_title_matches_its_non_latin_alt():
    item = _item(
        "Aurora (16)",
        "ザ・ゴッズ・ウィー・キャン・タッチ = The Gods We Can Touch",
        title_alts=("ザ・ゴッズ・ウィー・キャン・タッチ", "The Gods We Can Touch"),
    )
    library = [_album("Aurora", "ザ・ゴッズ・ウィー・キャン・タッチ")]
    assert _status(item, library) is MatchStatus.OWNED


# ── the match table ───────────────────────────────────────────────────────────

# (discogs artist, discogs title, library artist, library album, expected)
REAL_CASES = [
    # Exact.
    ("Radiohead", "OK Computer", "Radiohead", "OK Computer", MatchStatus.OWNED),

    # Containment — every one of these scored below 0.79 on whole-string
    # similarity and was wrongly reported missing before matching became
    # artist-scoped. These are the regression guards for that.
    ("Pink Floyd", "Ummagumma",
     "Pink Floyd", "Ummagumma - Live Album", MatchStatus.OWNED),
    ("Massive Attack", "Mezzanine",
     "Massive Attack", "Mezzanine - Mezzanine Mad Professor", MatchStatus.OWNED),
    ("Lady Gaga", "The Fame",
     "Lady Gaga", "The Fame Monster - The Fame", MatchStatus.OWNED),
    ("Nirvana", "MTV Unplugged",
     "Nirvana", "MTV Unplugged in New York", MatchStatus.OWNED),
    ("James Taylor (2)", "James Taylor's Greatest Hits",
     "James Taylor", "Greatest Hits", MatchStatus.OWNED),

    # Genuinely absent: same artist, different record. The audit is worthless
    # if these drift into OWNED.
    ("Beck", "Sea Change", "Beck", "Mutations", MatchStatus.MISSING),
    ("Radiohead", "OK Computer", "Radiohead", "In Rainbows", MatchStatus.MISSING),
    ("Mastodon", "The Hunter", "Mastodon", "Leviathan", MatchStatus.MISSING),
    ("Tori Amos", "Under The Pink", "Tori Amos", "Winter", MatchStatus.MISSING),
    ("Nine Inch Nails", "The Fragile",
     "Nine Inch Nails", "The Downward Spiral", MatchStatus.MISSING),

    # Artist unknown to the library at all.
    ("Some Band", "Some Album", "Another Band", "Another Album", MatchStatus.MISSING),
]


@pytest.mark.parametrize("c_artist,c_title,l_artist,l_album,expected", REAL_CASES)
def test_match_table(c_artist, c_title, l_artist, l_album, expected):
    assert _status(_item(c_artist, c_title), [_album(l_artist, l_album)]) is expected


def test_various_artists_alias():
    """Discogs credits compilations to 'Various'; the library says 'Various Artists'."""
    item = _item(
        "Various", "O Brother, Where Art Thou?",
        artist_alts=("Various Artists",),
    )
    library = [_album(
        "Various Artists",
        "O Brother, Where Art Thou? Music From a Film by Joel Coen & Ethan Coen",
    )]
    assert _status(item, library) is MatchStatus.OWNED


def test_artist_name_variation_matches():
    """The sleeve name and Discogs' canonical name genuinely differ in the wild."""
    item = _item("Pulselovers", "Glass", artist_alts=("Pulse Lovers",))
    assert _status(item, [_album("Pulse Lovers", "Glass")]) is MatchStatus.OWNED


def test_reordered_multi_artist_credit():
    """Discogs and MusicBrainz order joint credits differently."""
    item = _item("Louis Armstrong, Jelly Roll Morton", "Birth Of Jazz")
    library = [_album(
        "Jelly Roll Morton & Louis Armstrong",
        "Louis Armstrong & Jelly Roll Morton: Birth of Jazz",
    )]
    assert _status(item, library) is MatchStatus.OWNED


def test_dual_script_title_alt_matches():
    item = _item(
        "Aurora (16)",
        "ザ・ゴッズ・ウィー・キャン・タッチ = The Gods We Can Touch",
        title_alts=("ザ・ゴッズ・ウィー・キャン・タッチ", "The Gods We Can Touch"),
    )
    assert _status(item, [_album("Aurora", "The Gods We Can Touch")]) is MatchStatus.OWNED


def test_mbid_wins_outright():
    """An id match short-circuits every string heuristic."""
    item = _item("Whoever", "Whatever", mb_release_id="abc-123")
    library = [_album("Totally Different", "Unrelated", mb_albumid="abc-123")]
    match = match_items([item], library)[0]
    assert match.status is MatchStatus.OWNED
    assert match.reason == "mbid"


def test_uncertain_bucket_for_near_titles():
    item = _item("Portishead", "Roseland NYC Live")
    library = [_album("Portishead", "Roseland NYC Live 1997")]
    match = match_items([item], library)[0]
    assert match.status is MatchStatus.OWNED  # containment covers this one

    item = _item("Portishead", "Dummy Sessions")
    match = match_items([item], [_album("Portishead", "Dummy Session")])[0]
    assert match.status is MatchStatus.UNCERTAIN
    assert match.matched is not None
    assert 0 < match.score < 1


def test_short_title_does_not_contain_match():
    """A 2-char title inside a longer one proves nothing."""
    item = _item("Prism Capture", "II")
    match = match_items([item], [_album("Prism Capture", "II Live At The Hall")])[0]
    assert match.status is not MatchStatus.OWNED


def test_empty_library_is_all_missing():
    items = [_item("A", "One"), _item("B", "Two")]
    assert all(m.status is MatchStatus.MISSING for m in match_items(items, []))


def test_input_order_preserved():
    items = [_item("Z", "Last", source_id="1"), _item("A", "First", source_id="2")]
    assert [m.item.source_id for m in match_items(items, [])] == ["1", "2"]


# ── media resolution ──────────────────────────────────────────────────────────

def test_resolve_media_accepts_known_names():
    assert resolve_media(["cd", "VINYL"]) == frozenset({MediaKind.CD, MediaKind.VINYL})
    assert resolve_media("cd") == frozenset({MediaKind.CD})


def test_resolve_media_fails_loud_on_unknown():
    with pytest.raises(ValueError, match="unknown medium 'minidisc'"):
        resolve_media(["cd", "minidisc"])
