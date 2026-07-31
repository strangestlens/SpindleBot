"""Tests for the collection audit service, the fixture provider, and the
library index."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from spindlebot.collections.base import get_provider
from spindlebot.collections.fixture import FixtureProvider
from spindlebot.core.collection import CollectionItem, LibraryAlbum
from spindlebot.core.enums import MediaKind
from spindlebot.core.errors import CollectionFetchError, UnknownProvider
from spindlebot.services import library_index
from spindlebot.services.collection_audit import filter_media, run_audit


def _cfg(tmp_path):
    return SimpleNamespace(
        core=SimpleNamespace(db_path=tmp_path / "spindlebot.db"),
        tools=SimpleNamespace(beet="/usr/bin/true"),
        collection=SimpleNamespace(
            source="fixture", account="", media=("cd",), index="beets",
            cache_dir=tmp_path / "cache", cache_ttl_hours=24.0,
        ),
        secrets=SimpleNamespace(discogs=SimpleNamespace(token="")),
    )


class _StubProvider:
    name = "stub"

    def __init__(self, items):
        self.items = items
        self.refresh_calls = []

    def fetch(self, account, *, refresh=False):
        self.refresh_calls.append(refresh)
        return self.items


def _item(artist, title, media=(MediaKind.CD,), **kw):
    return CollectionItem(
        source="stub", source_id=kw.pop("source_id", artist + title),
        artist=artist, title=title, media=frozenset(media), **kw,
    )


# ── media filter ──────────────────────────────────────────────────────────────

def test_filter_media_keeps_matching_media():
    items = [
        _item("A", "CD only"),
        _item("B", "Vinyl only", media=(MediaKind.VINYL,)),
        _item("C", "CD in a box", media=(MediaKind.CD, MediaKind.OTHER)),
    ]
    kept = filter_media(items, frozenset({MediaKind.CD}))
    assert [i.title for i in kept] == ["CD only", "CD in a box"]


def test_empty_media_filter_keeps_everything():
    items = [_item("A", "One"), _item("B", "Two", media=(MediaKind.VINYL,))]
    assert filter_media(items, frozenset()) == items


# ── audit ─────────────────────────────────────────────────────────────────────

def test_audit_buckets_and_counts(tmp_path):
    provider = _StubProvider([
        _item("Radiohead", "OK Computer"),
        _item("Beck", "Sea Change"),
        _item("Pink Floyd", "Vinyl Thing", media=(MediaKind.VINYL,)),
    ])
    library = [
        LibraryAlbum("Radiohead", "OK Computer"),
        LibraryAlbum("Beck", "Mutations"),
    ]
    report = run_audit(
        _cfg(tmp_path), account="x", provider=provider, library=library,
    )
    assert report.fetched == 3
    assert report.considered == 2          # the vinyl item was filtered out
    assert report.library_albums == 2
    assert [m.item.title for m in report.owned] == ["OK Computer"]
    assert [m.item.title for m in report.missing] == ["Sea Change"]


def test_audit_sorts_by_artist_then_title(tmp_path):
    provider = _StubProvider([
        _item("Zappa", "B"), _item("Alpha", "Z"), _item("Alpha", "A"),
    ])
    report = run_audit(_cfg(tmp_path), account="x", provider=provider, library=[])
    assert [(m.item.artist, m.item.title) for m in report.matches] == [
        ("Alpha", "A"), ("Alpha", "Z"), ("Zappa", "B"),
    ]


def test_strict_folds_uncertain_into_missing(tmp_path):
    provider = _StubProvider([_item("Portishead", "Dummy Sessions")])
    library = [LibraryAlbum("Portishead", "Dummy Session")]

    lenient = run_audit(_cfg(tmp_path), account="x", provider=provider, library=library)
    assert len(lenient.uncertain) == 1
    assert lenient.missing == []

    strict = run_audit(_cfg(tmp_path), account="x", provider=provider,
                       library=library, strict=True)
    assert strict.uncertain == []
    assert len(strict.missing) == 1
    # The evidence survives the reclassification.
    assert strict.missing[0].matched is not None


def test_refresh_is_passed_to_the_provider(tmp_path):
    provider = _StubProvider([])
    run_audit(_cfg(tmp_path), account="x", provider=provider, library=[], refresh=True)
    assert provider.refresh_calls == [True]


def test_audit_reads_nothing_it_should_not(tmp_path):
    """The audit is assistive: no DB file is created, no library bytes touched."""
    cfg = _cfg(tmp_path)
    run_audit(cfg, account="x", provider=_StubProvider([]), library=[])
    assert not cfg.core.db_path.exists()


# ── fixture provider ──────────────────────────────────────────────────────────

def test_fixture_provider_reads_a_hand_written_collection(tmp_path):
    path = tmp_path / "shelf.json"
    path.write_text(json.dumps({"items": [
        {"artist": "Slint", "title": "Spiderland", "year": 1991},
        {"id": "x9", "artist": "Codeine", "title": "Frigid Stars", "media": ["cd"]},
    ]}))
    items = FixtureProvider().fetch(str(path))
    assert [i.title for i in items] == ["Spiderland", "Frigid Stars"]
    assert items[0].source_id == "0"        # positional fallback
    assert items[1].source_id == "x9"
    assert items[0].media == frozenset({MediaKind.CD})   # defaults to CD
    assert items[0].year == 1991


def test_fixture_provider_accepts_a_bare_list(tmp_path):
    path = tmp_path / "shelf.json"
    path.write_text(json.dumps([{"artist": "Duster", "title": "Stratosphere"}]))
    assert len(FixtureProvider().fetch(str(path))) == 1


def test_fixture_provider_skips_rows_without_a_title(tmp_path):
    path = tmp_path / "shelf.json"
    path.write_text(json.dumps([{"artist": "Nobody"}, {"title": "Real"}]))
    assert [i.title for i in FixtureProvider().fetch(str(path))] == ["Real"]


@pytest.mark.parametrize("content,match", [
    (None, "not found"),
    ("{ not json", "not valid JSON"),
    ('{"items": "nope"}', "must be a list"),
])
def test_fixture_provider_errors(tmp_path, content, match):
    path = tmp_path / "shelf.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(CollectionFetchError, match=match):
        FixtureProvider().fetch(str(path))


# ── registry ──────────────────────────────────────────────────────────────────

def test_registry_resolves_known_providers(tmp_path):
    cfg = _cfg(tmp_path)
    assert get_provider("fixture", cfg).name == "fixture"
    assert get_provider("discogs", cfg).name == "discogs"


def test_registry_fails_loud_on_unknown_provider(tmp_path):
    with pytest.raises(UnknownProvider, match="unknown collection source"):
        get_provider("lastfm", _cfg(tmp_path))


# ── library index ─────────────────────────────────────────────────────────────

def test_parse_beets_output():
    sep = library_index.FIELD_SEP
    stdout = "\n".join([
        sep.join(["Radiohead", "OK Computer", "1997", "abc-123"]),
        sep.join(["Beck", "Sea Change", "", ""]),
        "",
        sep.join(["Broken", "row"]),                      # too few fields
        sep.join(["No album", "", "2000", ""]),           # no album title
    ])
    albums = library_index.parse_beets_output(stdout)
    assert [a.album for a in albums] == ["OK Computer", "Sea Change"]
    assert albums[0].year == 1997
    assert albums[0].mb_albumid == "abc-123"
    assert albums[1].year is None
    assert albums[1].mb_albumid is None


def test_separator_survives_a_title_containing_pipes():
    sep = library_index.FIELD_SEP
    stdout = sep.join(["A||B", "Album || With || Pipes", "2001", ""])
    albums = library_index.parse_beets_output(stdout)
    assert albums[0].album == "Album || With || Pipes"


def test_from_beets_raises_on_failure(tmp_path):
    def runner(argv, **kw):
        return SimpleNamespace(returncode=2, stdout="", stderr="no such database")
    with pytest.raises(RuntimeError, match="no such database"):
        library_index.from_beets(_cfg(tmp_path), runner=runner)


def test_from_beets_parses_success(tmp_path):
    sep = library_index.FIELD_SEP

    def runner(argv, **kw):
        assert "-a" in argv and "ls" in argv
        return SimpleNamespace(
            returncode=0, stdout=sep.join(["Slint", "Spiderland", "1991", ""]), stderr="",
        )
    albums = library_index.from_beets(_cfg(tmp_path), runner=runner)
    assert albums == [LibraryAlbum("Slint", "Spiderland", 1991, None)]


def test_load_rejects_an_unknown_index(tmp_path):
    with pytest.raises(ValueError, match="unknown library index"):
        library_index.load(_cfg(tmp_path), "spotify")
