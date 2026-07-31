"""
Tests for spindlebot.collections.discogs.

The transformer runs against `tests/fixtures/discogs_collection_page1.json`, a
recorded slice of a real Discogs collection chosen to cover every quirk the
matcher depends on. The client is exercised with an injected fetcher — no test
touches the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from spindlebot.collections.discogs import (
    AUTH_RPM,
    UNAUTH_RPM,
    DiscogsClient,
    DiscogsProvider,
    to_items,
)
from spindlebot.core.enums import MediaKind
from spindlebot.core.errors import CollectionFetchError

FIXTURE = Path(__file__).parent / "fixtures" / "discogs_collection_page1.json"


@pytest.fixture
def releases() -> list[dict]:
    return json.loads(FIXTURE.read_text())["releases"]


@pytest.fixture
def items(releases):
    return {i.title: i for i in to_items(releases)}


# ── transformer ───────────────────────────────────────────────────────────────

def test_every_release_maps(releases):
    assert len(to_items(releases)) == len(releases)


def test_release_identity_and_url(items):
    item = items["OK Computer"]
    assert item.source == "discogs"
    assert item.source_id.isdigit()
    assert item.key == f"discogs:{item.source_id}"
    assert item.url == f"https://www.discogs.com/release/{item.source_id}"


def test_disambiguation_suffix_is_carried_through_verbatim(items):
    """The transformer preserves the raw name; normalization strips the suffix."""
    assert items["James Taylor's Greatest Hits"].artist == "James Taylor (2)"


def test_various_gets_the_full_name_as_an_alt(items):
    item = items["O Brother, Where Art Thou?"]
    assert item.artist == "Various"
    assert "Various Artists" in item.artist_alts


def test_multi_artist_join_is_rebuilt(items):
    item = items["Birth Of Jazz"]
    assert item.artist == "Louis Armstrong, Jelly Roll Morton"
    # The first artist alone is an alt — libraries often credit only the lead.
    assert "Louis Armstrong" in item.artist_alts


def test_dual_script_title_splits_into_alts(items):
    item = next(i for t, i in items.items() if " = " in t)
    assert "The Gods We Can Touch (Japan Special Edition)" in item.title_alts
    assert any("ゴッズ" in alt for alt in item.title_alts)


def test_media_scans_every_format_entry(items):
    """A CD+DVD release is still a CD — container entries never mask the medium."""
    assert MediaKind.CD in items["The Curse"].media
    assert MediaKind.OTHER in items["The Curse"].media  # DVD + All Media


def test_cdr_counts_as_cd(items):
    item = items["Imperial Dunes"]
    assert item.media == frozenset({MediaKind.CD})


def test_vinyl_only_release_is_not_a_cd(items):
    item = items["My Life In A Hole In The Ground"]
    assert item.media == frozenset({MediaKind.VINYL})
    assert MediaKind.CD not in item.media


def test_unknown_year_becomes_none():
    items_ = to_items([{
        "id": 1,
        "basic_information": {"title": "T", "year": 0, "artists": [{"name": "A"}]},
    }])
    assert items_[0].year is None


def test_placeholder_catno_is_dropped():
    items_ = to_items([{
        "id": 1,
        "basic_information": {
            "title": "T", "artists": [{"name": "A"}],
            "labels": [{"name": "L", "catno": "none"}],
        },
    }])
    assert items_[0].catno is None


def test_unusable_rows_are_skipped():
    assert to_items([{"id": 1, "basic_information": {"title": ""}}]) == []
    assert to_items([{"basic_information": {"title": "No id"}}]) == []


# ── client ────────────────────────────────────────────────────────────────────

def _fetcher(pages: list[dict], calls: list[str] | None = None):
    def fetch(url, headers):
        if calls is not None:
            calls.append(url)
        page = int(url.split("&page=")[1].split("&")[0])
        return 200, {}, json.dumps(pages[page - 1]).encode()
    return fetch


def _page(page: int, pages: int, releases: list[dict]) -> dict:
    return {"pagination": {"page": page, "pages": pages}, "releases": releases}


def test_client_walks_every_page():
    calls: list[str] = []
    pages = [
        _page(1, 3, [{"id": 1}]),
        _page(2, 3, [{"id": 2}]),
        _page(3, 3, [{"id": 3}]),
    ]
    client = DiscogsClient(fetcher=_fetcher(pages, calls), sleep=lambda _: None)
    assert [r["id"] for r in client.fetch_raw("someone")] == [1, 2, 3]
    assert len(calls) == 3
    assert "users/someone/collection/folders/0/releases" in calls[0]


def test_client_sends_token_only_when_present():
    seen: list[dict] = []

    def fetch(url, headers):
        seen.append(headers)
        return 200, {}, json.dumps(_page(1, 1, [])).encode()

    DiscogsClient(fetcher=fetch, sleep=lambda _: None).fetch_raw("x")
    assert "Authorization" not in seen[0]
    assert seen[0]["User-Agent"]

    DiscogsClient(token="abc", fetcher=fetch, sleep=lambda _: None).fetch_raw("x")
    assert seen[1]["Authorization"] == "Discogs token=abc"


def test_token_raises_the_rate_limit():
    unauth = DiscogsClient(fetcher=_fetcher([_page(1, 1, [])]))
    auth = DiscogsClient(token="t", fetcher=_fetcher([_page(1, 1, [])]))
    assert unauth._min_interval > auth._min_interval
    assert unauth._min_interval >= 60.0 / UNAUTH_RPM
    assert auth._min_interval >= 60.0 / AUTH_RPM


def test_client_throttles_between_pages():
    slept: list[float] = []
    clock = iter([float(i) * 0.01 for i in range(200)])
    pages = [_page(1, 2, [{"id": 1}]), _page(2, 2, [{"id": 2}])]
    client = DiscogsClient(
        fetcher=_fetcher(pages), sleep=slept.append, now=lambda: next(clock),
    )
    client.fetch_raw("someone")
    assert slept and all(s > 0 for s in slept)


def test_client_retries_on_rate_limit_then_succeeds():
    slept: list[float] = []
    responses = [
        (429, {"Retry-After": "7"}, b""),
        (200, {}, json.dumps(_page(1, 1, [{"id": 1}])).encode()),
    ]
    client = DiscogsClient(
        fetcher=lambda url, headers: responses.pop(0), sleep=slept.append,
    )
    assert client.fetch_raw("someone") == [{"id": 1}]
    assert 7.0 in slept


def test_client_gives_up_after_repeated_rate_limits():
    client = DiscogsClient(
        fetcher=lambda url, headers: (429, {}, b""), sleep=lambda _: None,
    )
    with pytest.raises(CollectionFetchError, match="rate limit"):
        client.fetch_raw("someone")


@pytest.mark.parametrize("status,expected", [
    (404, "not found"),
    (401, "personal access token"),
    (403, "personal access token"),
    (500, "HTTP 500"),
])
def test_client_surfaces_http_errors(status, expected):
    client = DiscogsClient(
        fetcher=lambda url, headers: (status, {}, b""), sleep=lambda _: None,
    )
    with pytest.raises(CollectionFetchError, match=expected):
        client.fetch_raw("someone")


# ── cache ─────────────────────────────────────────────────────────────────────

def test_cache_avoids_a_second_fetch(tmp_path):
    calls: list[str] = []
    pages = [_page(1, 1, [{"id": 1}])]
    client = DiscogsClient(
        fetcher=_fetcher(pages, calls), sleep=lambda _: None, cache_dir=tmp_path,
    )
    assert client.fetch_raw("someone") == [{"id": 1}]
    assert client.fetch_raw("someone") == [{"id": 1}]
    assert len(calls) == 1


def test_refresh_bypasses_the_cache(tmp_path):
    calls: list[str] = []
    client = DiscogsClient(
        fetcher=_fetcher([_page(1, 1, [{"id": 1}])], calls),
        sleep=lambda _: None, cache_dir=tmp_path,
    )
    client.fetch_raw("someone")
    client.fetch_raw("someone", refresh=True)
    assert len(calls) == 2


def test_expired_cache_is_refetched(tmp_path):
    calls: list[str] = []
    clock = [1000.0]
    client = DiscogsClient(
        fetcher=_fetcher([_page(1, 1, [{"id": 1}])], calls),
        sleep=lambda _: None, cache_dir=tmp_path, cache_ttl_hours=1.0,
        now=lambda: clock[0],
    )
    client.fetch_raw("someone")
    clock[0] += 3600 * 2
    client.fetch_raw("someone")
    assert len(calls) == 2


def test_corrupt_cache_is_ignored(tmp_path):
    calls: list[str] = []
    client = DiscogsClient(
        fetcher=_fetcher([_page(1, 1, [{"id": 1}])], calls),
        sleep=lambda _: None, cache_dir=tmp_path,
    )
    path = client.cache_path("someone")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    assert client.fetch_raw("someone") == [{"id": 1}]
    assert len(calls) == 1


def test_cache_filename_cannot_escape_the_cache_dir(tmp_path):
    client = DiscogsClient(cache_dir=tmp_path)
    path = client.cache_path("../../etc/passwd")
    assert path.parent == tmp_path


# ── provider ──────────────────────────────────────────────────────────────────

def test_provider_returns_items(releases):
    pages = [_page(1, 1, releases)]
    provider = DiscogsProvider(
        client=DiscogsClient(fetcher=_fetcher(pages), sleep=lambda _: None)
    )
    fetched = provider.fetch("someone")
    assert provider.name == "discogs"
    assert len(fetched) == len(releases)
    assert all(i.source == "discogs" for i in fetched)
