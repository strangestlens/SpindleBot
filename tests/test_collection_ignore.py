"""Tests for the collection-audit ignore list — store, audit overlay, and CLI.

The recurring theme: ignoring must be reversible and must never destroy the
underlying verdict. The mistake this feature invites is ignoring the wrong row.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from spindlebot.cli import cmd_collection_audit, cmd_collection_ignore
from spindlebot.core.collection import CollectionItem, LibraryAlbum
from spindlebot.core.collection_match import MatchStatus
from spindlebot.core.enums import MediaKind
from spindlebot.services.collection_audit import run_audit
from spindlebot.services.collection_ignore import (
    IgnoreStore,
    IgnoreStoreError,
    resolve_key,
)
from spindlebot.services.library_index import LibraryIndex


def _cfg(tmp_path, account=""):
    return SimpleNamespace(
        core=SimpleNamespace(db_path=tmp_path / "spindlebot.db"),
        tools=SimpleNamespace(beet="/usr/bin/true"),
        collection=SimpleNamespace(
            source="discogs", account=account, media=("cd",), index="auto",
            cache_dir=tmp_path / "cache", cache_ttl_hours=24.0,
            ignore_path=tmp_path / "collection-ignore.json",
        ),
        secrets=SimpleNamespace(discogs=SimpleNamespace(token="")),
    )


def _item(artist, title, source_id, media=(MediaKind.CD,)):
    return CollectionItem(
        source="discogs", source_id=source_id, artist=artist, title=title,
        media=frozenset(media),
    )


class _StubProvider:
    name = "discogs"

    def __init__(self, items):
        self.items = items

    def fetch(self, account, *, refresh=False):
        return self.items


# ── store ─────────────────────────────────────────────────────────────────────

def test_missing_file_is_an_empty_store(tmp_path):
    store = IgnoreStore.load(tmp_path / "nope.json")
    assert len(store) == 0
    assert "discogs:1" not in store


def test_add_then_reload_round_trips(tmp_path):
    path = tmp_path / "ignore.json"
    store = IgnoreStore.load(path)
    store.add("discogs:1", artist="Beck", title="Hyperspace", reason="katakana copy")
    store.save()

    reloaded = IgnoreStore.load(path)
    assert "discogs:1" in reloaded
    entry = reloaded.items["discogs:1"]
    assert entry.artist == "Beck"
    assert entry.reason == "katakana copy"
    assert entry.ignored_utc > 0


def test_remove_returns_the_entry_and_forgets_it(tmp_path):
    store = IgnoreStore.load(tmp_path / "ignore.json")
    store.add("discogs:1", artist="Beck", title="Hyperspace")
    removed = store.remove("discogs:1")
    assert removed is not None
    assert removed.artist == "Beck"
    assert "discogs:1" not in store


def test_remove_of_something_never_ignored_is_not_an_error(tmp_path):
    store = IgnoreStore.load(tmp_path / "ignore.json")
    assert store.remove("discogs:999") is None


def test_re_adding_keeps_the_original_timestamp(tmp_path):
    """When you first decided is the interesting fact, not when you last said so."""
    store = IgnoreStore.load(tmp_path / "ignore.json")
    store.add("discogs:1", artist="Beck", title="Hyperspace", now=1000)
    store.add("discogs:1", reason="second thoughts", now=2000)
    entry = store.items["discogs:1"]
    assert entry.ignored_utc == 1000
    assert entry.reason == "second thoughts"
    assert entry.artist == "Beck"      # earlier detail is not clobbered by a bare re-add


def test_clear_empties_the_store(tmp_path):
    store = IgnoreStore.load(tmp_path / "ignore.json")
    store.add("discogs:1")
    store.add("discogs:2")
    assert store.clear() == 2
    assert len(store) == 0


def test_listing_is_newest_first(tmp_path):
    store = IgnoreStore.load(tmp_path / "ignore.json")
    store.add("discogs:1", title="Older", now=1000)
    store.add("discogs:2", title="Newer", now=2000)
    assert [i.title for i in store.listing()] == ["Newer", "Older"]


def test_label_falls_back_to_the_key(tmp_path):
    store = IgnoreStore.load(tmp_path / "ignore.json")
    assert store.add("discogs:1").label == "discogs:1"
    assert store.add("discogs:2", artist="Beck", title="Odelay").label == "Beck — Odelay"


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "ignore.json"
    store = IgnoreStore.load(path)
    store.add("discogs:1")
    store.save()
    assert path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_corrupt_store_fails_loud(tmp_path):
    """Silently starting empty would un-ignore everything without saying so."""
    path = tmp_path / "ignore.json"
    path.write_text("{ not json")
    with pytest.raises(IgnoreStoreError, match="not valid JSON"):
        IgnoreStore.load(path)


def test_malformed_store_fails_loud(tmp_path):
    path = tmp_path / "ignore.json"
    path.write_text(json.dumps({"ignored": "nope"}))
    with pytest.raises(IgnoreStoreError, match="malformed"):
        IgnoreStore.load(path)


@pytest.mark.parametrize("token,expected", [
    ("26936627", "discogs:26936627"),        # bare id, as copied from a URL
    ("discogs:26936627", "discogs:26936627"),
    ("  26936627  ", "discogs:26936627"),
    ("fixture:9", "fixture:9"),              # explicit source wins
])
def test_resolve_key(token, expected):
    assert resolve_key(token, source="discogs") == expected


def test_resolve_key_rejects_empty():
    with pytest.raises(ValueError):
        resolve_key("   ", source="discogs")


# ── audit overlay ─────────────────────────────────────────────────────────────

def test_ignored_item_leaves_the_missing_bucket(tmp_path):
    cfg = _cfg(tmp_path)
    provider = _StubProvider([
        _item("Beck", "Sea Change", "1"),
        _item("Mastodon", "The Hunter", "2"),
    ])
    store = IgnoreStore.load(cfg.collection.ignore_path)
    store.add("discogs:1")

    report = run_audit(cfg, account="x", provider=provider, library=[], ignore=store)
    assert [m.item.title for m in report.missing] == ["The Hunter"]
    assert [m.item.title for m in report.ignored] == ["Sea Change"]


def test_ignoring_preserves_the_underlying_verdict(tmp_path):
    """Un-ignoring must restore the original answer with nothing recomputed."""
    cfg = _cfg(tmp_path)
    store = IgnoreStore.load(cfg.collection.ignore_path)
    store.add("discogs:1")
    report = run_audit(
        cfg, account="x", provider=_StubProvider([_item("Beck", "Sea Change", "1")]),
        library=[], ignore=store,
    )
    entry = report.ignored[0]
    assert entry.status is MatchStatus.MISSING
    assert entry.ignored is True


def test_an_owned_album_is_never_reported_as_ignored(tmp_path):
    """Ignore it, then rip it: the rip wins and the stale entry stops mattering."""
    cfg = _cfg(tmp_path)
    store = IgnoreStore.load(cfg.collection.ignore_path)
    store.add("discogs:1")
    report = run_audit(
        cfg, account="x", provider=_StubProvider([_item("Beck", "Odelay", "1")]),
        library=[LibraryAlbum("Beck", "Odelay")], ignore=store,
    )
    assert [m.item.title for m in report.owned] == ["Odelay"]
    assert report.ignored == []


def test_audit_loads_the_store_from_config_when_not_injected(tmp_path):
    cfg = _cfg(tmp_path)
    store = IgnoreStore.load(cfg.collection.ignore_path)
    store.add("discogs:1")
    store.save()

    report = run_audit(
        cfg, account="x", provider=_StubProvider([_item("Beck", "Sea Change", "1")]),
        library=[],
    )
    assert len(report.ignored) == 1
    assert report.missing == []


# ── CLI ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def shelf(tmp_path):
    path = tmp_path / "shelf.json"
    path.write_text(json.dumps([
        {"id": "1", "artist": "Beck", "title": "Sea Change"},
        {"id": "2", "artist": "Mastodon", "title": "The Hunter"},
    ]))
    return path


@pytest.fixture(autouse=True)
def stub_library(monkeypatch):
    monkeypatch.setattr(
        "spindlebot.services.library_index.load",
        lambda cfg, index="auto": LibraryIndex(albums=[], counts={"beets": 0}),
    )


def test_cli_ignore_then_list_then_remove(tmp_path, capsys):
    cfg = _cfg(tmp_path)

    assert cmd_collection_ignore(cfg, ["26936627"]) == 0
    assert "discogs:26936627" in capsys.readouterr().out

    assert cmd_collection_ignore(cfg, ["--list"]) == 0
    out = capsys.readouterr().out
    assert "Ignored (1)" in out
    assert "--remove" in out          # the way back is on screen

    assert cmd_collection_ignore(cfg, ["--remove", "26936627"]) == 0
    assert "Un-ignored 1 item(s)" in capsys.readouterr().out

    cmd_collection_ignore(cfg, ["--list"])
    assert "Nothing ignored." in capsys.readouterr().out


def test_cli_unignore_is_an_accepted_spelling(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    cmd_collection_ignore(cfg, ["1"])
    capsys.readouterr()
    assert cmd_collection_ignore(cfg, ["--unignore", "1"]) == 0
    assert "Un-ignored 1 item(s)" in capsys.readouterr().out


def test_cli_ignores_several_at_once(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    cmd_collection_ignore(cfg, ["1", "2", "3", "--reason", "damaged"])
    capsys.readouterr()
    cmd_collection_ignore(cfg, ["--list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 3
    assert {i["key"] for i in payload["ignored"]} == {
        "discogs:1", "discogs:2", "discogs:3",
    }
    assert all(i["reason"] == "damaged" for i in payload["ignored"])


def test_cli_removing_something_not_ignored_says_so(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    assert cmd_collection_ignore(cfg, ["--remove", "404"]) == 0
    assert "was not ignored" in capsys.readouterr().out


def test_cli_bare_invocation_lists(tmp_path, capsys):
    assert cmd_collection_ignore(_cfg(tmp_path), []) == 0
    assert "Nothing ignored." in capsys.readouterr().out


def test_cli_clear_requires_confirmation(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    cmd_collection_ignore(cfg, ["1", "2"])
    capsys.readouterr()

    assert cmd_collection_ignore(cfg, ["--clear"]) == 1
    assert "--yes" in capsys.readouterr().err
    assert len(IgnoreStore.load(cfg.collection.ignore_path)) == 2

    assert cmd_collection_ignore(cfg, ["--clear", "--yes"]) == 0
    assert "Cleared 2" in capsys.readouterr().out
    assert len(IgnoreStore.load(cfg.collection.ignore_path)) == 0


def test_cli_enriches_from_the_collection_when_it_can(tmp_path, shelf, capsys):
    """`--list` a month later has to be readable, so store artist/title too."""
    cfg = _cfg(tmp_path, account=str(shelf))
    cfg.collection.source = "fixture"
    cmd_collection_ignore(cfg, ["1"])
    assert "Beck — Sea Change" in capsys.readouterr().out


def test_cli_ignore_works_even_if_the_collection_cannot_be_read(tmp_path, capsys):
    """A cache miss or a dead network must never block an ignore."""
    cfg = _cfg(tmp_path, account="/nope/missing.json")
    cfg.collection.source = "fixture"
    assert cmd_collection_ignore(cfg, ["1"]) == 0
    stored = IgnoreStore.load(cfg.collection.ignore_path).items
    assert "fixture:1" in stored
    # Nothing to enrich from, so it lands bare rather than not at all.
    assert stored["fixture:1"].artist == ""


def test_cli_corrupt_store_is_reported_not_silently_reset(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    cfg.collection.ignore_path.write_text("{ not json")
    assert cmd_collection_ignore(cfg, ["--list"]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_cli_json_error_shape(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    cfg.collection.ignore_path.write_text("{ not json")
    assert cmd_collection_ignore(cfg, ["--list", "--json"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


# ── audit CLI integration ─────────────────────────────────────────────────────

def test_audit_hides_ignored_and_counts_them(tmp_path, shelf, capsys):
    cfg = _cfg(tmp_path, account=str(shelf))
    cfg.collection.source = "fixture"
    cmd_collection_ignore(cfg, ["1"])
    capsys.readouterr()

    cmd_collection_audit(cfg, [])
    out = capsys.readouterr().out
    assert "MISSING (1)" in out
    assert "Sea Change" not in out
    assert "1 ignored" in out


def test_audit_show_ignored_brings_them_back(tmp_path, shelf, capsys):
    cfg = _cfg(tmp_path, account=str(shelf))
    cfg.collection.source = "fixture"
    cmd_collection_ignore(cfg, ["1"])
    capsys.readouterr()

    cmd_collection_audit(cfg, ["--show-ignored"])
    out = capsys.readouterr().out
    assert "IGNORED (1)" in out
    assert "Sea Change" in out


def test_audit_prints_ids_so_the_list_is_actionable(tmp_path, shelf, capsys):
    cfg = _cfg(tmp_path, account=str(shelf))
    cfg.collection.source = "fixture"
    cmd_collection_audit(cfg, [])
    out = capsys.readouterr().out
    assert "1  Beck — Sea Change" in out
    assert "collection-ignore" in out      # tells you what to do with the id
