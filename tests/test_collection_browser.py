"""collection-browser: the local web UI for the collection audit.

Drives the Flask app through its test client — no socket, no browser.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")

from spindlebot.core.collection import LibraryAlbum  # noqa: E402
from spindlebot.core.enums import MediaKind  # noqa: E402
from spindlebot.services.collection_ignore import IgnoreStore  # noqa: E402
from spindlebot.services.library_index import LibraryIndex  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def shelf(tmp_path):
    path = tmp_path / "shelf.json"
    path.write_text(json.dumps([
        {"id": "1", "artist": "Beck", "title": "Sea Change"},
        {"id": "2", "artist": "Mastodon", "title": "The Hunter"},
        {"id": "3", "artist": "Radiohead", "title": "OK Computer"},
    ]))
    return path


@pytest.fixture
def browser(monkeypatch, tmp_path, shelf):
    """Load the extension-less script and point it at a fixture collection."""
    monkeypatch.setattr(
        "spindlebot.services.library_index.load",
        lambda cfg, index="auto": LibraryIndex(
            albums=[LibraryAlbum("Radiohead", "OK Computer")], counts={"beets": 1},
        ),
    )

    loader = importlib.machinery.SourceFileLoader(
        "collection_browser_script", str(ROOT / "collection-browser")
    )
    spec = importlib.util.spec_from_loader("collection_browser_script", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    cfg = SimpleNamespace(
        core=SimpleNamespace(db_path=tmp_path / "spindlebot.db"),
        tools=SimpleNamespace(beet="/usr/bin/true"),
        collection=SimpleNamespace(
            source="fixture", account=str(shelf), media=("cd",), index="auto",
            cache_dir=tmp_path / "cache", cache_ttl_hours=24.0,
            ignore_path=tmp_path / "collection-ignore.json",
        ),
        secrets=SimpleNamespace(discogs=SimpleNamespace(token="")),
    )
    mod.state.update(
        cfg=cfg, account=str(shelf), source="fixture", index="auto",
        media=frozenset({MediaKind.CD}),
    )
    mod.state["report"] = mod._audit()
    mod.app.config["TESTING"] = True
    mod.client = mod.app.test_client()
    return mod


def _post(browser, path, payload, **kw):
    return browser.client.post(
        path, data=json.dumps(payload), content_type="application/json", **kw
    )


# ── page ──────────────────────────────────────────────────────────────────────

def test_index_renders_the_interactive_report(browser):
    res = browser.client.get("/")
    assert res.status_code == 200
    page = res.get_data(as_text=True)
    assert "Collection Audit" in page
    assert 'data-act="ignore"' in page          # the affordance the export lacks
    assert 'id="toast-undo"' in page


def test_owned_cards_have_no_ignore_button(browser):
    page = browser.client.get("/").get_data(as_text=True)
    owned = [
        block for block in page.split('<div class="card"')
        if 'data-status="owned"' in block
    ]
    assert owned, "expected an owned card in the fixture"
    assert all('class="act"' not in block for block in owned)


def test_exported_report_stays_inert(browser):
    """The static export must not ship dead buttons."""
    from spindlebot.services.collection_report import render_html
    page = render_html(browser.state["report"])
    assert 'data-act="ignore"' not in page
    assert "toast" not in page
    assert "/ignore" not in page


# ── ignore / unignore ─────────────────────────────────────────────────────────

def test_ignore_then_unignore_round_trips(browser):
    store_path = browser.state["cfg"].collection.ignore_path

    res = _post(browser, "/ignore", {"key": "fixture:1"})
    body = res.get_json()
    assert res.status_code == 200 and body["ok"] is True
    assert body["counts"]["missing"] == 1
    assert body["counts"]["ignored"] == 1
    assert "fixture:1" in IgnoreStore.load(store_path)

    res = _post(browser, "/unignore", {"key": "fixture:1"})
    body = res.get_json()
    assert body["ok"] is True
    assert body["counts"]["missing"] == 2
    assert body["counts"]["ignored"] == 0
    assert "fixture:1" not in IgnoreStore.load(store_path)


def test_ignore_stores_artist_and_title(browser):
    _post(browser, "/ignore", {"key": "fixture:1"})
    entry = IgnoreStore.load(browser.state["cfg"].collection.ignore_path).items["fixture:1"]
    assert entry.artist == "Beck"
    assert entry.title == "Sea Change"


def test_ignore_accepts_a_reason(browser):
    _post(browser, "/ignore", {"key": "fixture:1", "reason": "disc is cracked"})
    entry = IgnoreStore.load(browser.state["cfg"].collection.ignore_path).items["fixture:1"]
    assert entry.reason == "disc is cracked"


def test_ignore_accepts_a_bare_id(browser):
    res = _post(browser, "/ignore", {"id": "2"})
    assert res.get_json()["key"] == "fixture:2"


def test_unignoring_something_never_ignored_is_harmless(browser):
    res = _post(browser, "/unignore", {"key": "fixture:9"})
    assert res.status_code == 200
    assert res.get_json()["ok"] is True


def test_ignoring_reflects_in_the_next_page_load(browser):
    _post(browser, "/ignore", {"key": "fixture:1"})
    page = browser.client.get("/").get_data(as_text=True)
    assert 'data-act="undo"' in page
    # The verdict underneath survives, which is what makes undo free.
    assert 'data-verdict="missing"' in page


def test_missing_id_is_a_400(browser):
    res = _post(browser, "/ignore", {})
    assert res.status_code == 400
    assert res.get_json()["ok"] is False


def test_cross_origin_post_is_refused(browser):
    """Any page in the browser can POST to a localhost port; a drive-by
    ignore-list edit is still a write."""
    res = _post(browser, "/ignore", {"key": "fixture:1"},
                headers={"Origin": "https://evil.test"})
    assert res.status_code == 403
    assert "fixture:1" not in IgnoreStore.load(
        browser.state["cfg"].collection.ignore_path
    )


def test_same_origin_post_is_allowed(browser):
    res = _post(browser, "/ignore", {"key": "fixture:1"},
                headers={"Origin": "http://localhost"})
    assert res.status_code == 200


def test_store_write_failure_surfaces_as_500(browser, monkeypatch):
    from spindlebot.services.collection_ignore import IgnoreStoreError

    def boom(self):
        raise IgnoreStoreError("disk is full")

    monkeypatch.setattr(IgnoreStore, "save", boom)
    res = _post(browser, "/ignore", {"key": "fixture:1"})
    assert res.status_code == 500
    assert "disk is full" in res.get_json()["error"]


# ── json ──────────────────────────────────────────────────────────────────────

def test_audit_json_shape(browser):
    payload = browser.client.get("/audit.json").get_json()
    assert payload["source"] == "fixture"
    assert payload["counts"]["missing"] == 2
    assert payload["counts"]["owned"] == 1
    assert {i["title"] for i in payload["items"]} == {
        "Sea Change", "The Hunter", "OK Computer",
    }


def test_audit_json_marks_ignored_items(browser):
    _post(browser, "/ignore", {"key": "fixture:1"})
    payload = browser.client.get("/audit.json").get_json()
    ignored = [i for i in payload["items"] if i["ignored"]]
    assert [i["title"] for i in ignored] == ["Sea Change"]
    # Status is untouched — ignoring is an overlay, not a reclassification.
    assert ignored[0]["status"] == "missing"
