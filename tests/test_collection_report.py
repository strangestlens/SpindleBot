"""Tests for the static HTML collection-audit report."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from spindlebot.core.collection import CollectionItem, LibraryAlbum
from spindlebot.core.collection_match import ItemMatch, MatchStatus
from spindlebot.core.enums import MediaKind
from spindlebot.services.collection_audit import AuditReport
from spindlebot.services.library_index import LibraryIndex
from spindlebot.services.collection_report import render_html


def _item(artist="Slint", title="Spiderland", **kw) -> CollectionItem:
    return CollectionItem(
        source=kw.pop("source", "discogs"),
        source_id=kw.pop("source_id", "1"),
        artist=artist, title=title,
        media=kw.pop("media", frozenset({MediaKind.CD})),
        **kw,
    )


def _report(*matches, **kw) -> AuditReport:
    return AuditReport(
        source=kw.pop("source", "discogs"),
        account=kw.pop("account", "someone"),
        media=kw.pop("media", frozenset({MediaKind.CD})),
        fetched=kw.pop("fetched", len(matches)),
        considered=kw.pop("considered", len(matches)),
        library_albums=kw.pop("library_albums", 0),
        matches=list(matches),
        library_sources=kw.pop("library_sources", {}),
        library_errors=kw.pop("library_errors", {}),
    )


def _match(item, status=MatchStatus.MISSING, matched=None, score=0.0) -> ItemMatch:
    return ItemMatch(item, status, matched, "reason", score)


# ── structure ─────────────────────────────────────────────────────────────────

def test_renders_a_complete_document():
    page = render_html(_report(_match(_item())))
    assert page.startswith("<!DOCTYPE html>")
    assert page.rstrip().endswith("</html>")
    assert "<title>Collection Audit — someone</title>" in page


def test_is_self_contained():
    """It must open from disk with no server and no asset fetches beyond covers."""
    page = render_html(_report(_match(_item())))
    assert "<script src=" not in page
    assert "<link " not in page
    assert "@import" not in page


def test_counts_every_bucket():
    page = render_html(_report(
        _match(_item(title="A"), MatchStatus.MISSING),
        _match(_item(title="B"), MatchStatus.MISSING),
        _match(_item(title="C"), MatchStatus.OWNED),
        library_albums=42,
    ))
    assert '<div class="n" data-count="missing">2</div>' in page
    assert '<div class="n" data-count="owned">1</div>' in page
    assert '<div class="n" data-count="uncertain">0</div>' in page
    assert '<div class="n">42</div><div class="l">in library</div>' in page


def test_every_item_becomes_a_card_tagged_with_its_status():
    page = render_html(_report(
        _match(_item(title="Gone"), MatchStatus.MISSING),
        _match(_item(title="Have"), MatchStatus.OWNED),
    ))
    assert page.count('class="card"') == 2
    assert 'data-status="missing"' in page
    assert 'data-status="owned"' in page


def test_missing_is_the_default_tab():
    page = render_html(_report(_match(_item())))
    tab = re.search(r'<button data-status="missing"[^>]*>', page).group(0)
    assert "active" in tab


def test_uncertain_card_shows_what_it_nearly_matched():
    page = render_html(_report(_match(
        _item(artist="Portishead", title="Dummy Sessions"),
        MatchStatus.UNCERTAIN,
        LibraryAlbum("Portishead", "Dummy Session"),
        0.94,
    )))
    assert "≈ Portishead — Dummy Session (0.94)" in page


def test_metadata_line_names_the_indexes_consulted():
    """Same reasoning as the CLI: a wrongly-missing album usually means the
    index didn't know about it, so never hide which one answered."""
    page = render_html(_report(
        _match(_item()), library_sources={"beets": 112, "db": 177},
    ))
    assert "beets 112, db 177" in page


def test_metadata_line_survives_an_absent_breakdown():
    page = render_html(_report(_match(_item())))
    assert "Collection Audit" in page


def test_a_pinned_timestamp_is_honoured():
    """`generated_utc` pins the stamp so a report is reproducible."""
    import time
    page = render_html(_report(_match(_item())), generated_utc=1_000_000_000.0)
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(1_000_000_000.0)) in page


def test_epoch_zero_is_a_timestamp_not_a_missing_value():
    """0.0 is falsy but perfectly valid; only None means "use now"."""
    import time
    page = render_html(_report(_match(_item())), generated_utc=0.0)
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(0.0)) in page


def test_metadata_line_reports_the_filter():
    page = render_html(_report(
        _match(_item()), fetched=212, considered=152,
        media=frozenset({MediaKind.CD, MediaKind.VINYL}),
    ))
    assert "152 of" in page and "212" in page
    assert "cd/vinyl" in page


# ── item detail ───────────────────────────────────────────────────────────────

def test_thumbnail_is_used_when_present():
    page = render_html(_report(_match(
        _item(thumb_url="https://img.example.test/a.jpg")
    )))
    assert '<img class="thumb" src="https://img.example.test/a.jpg"' in page
    assert 'loading="lazy"' in page


def test_placeholder_when_no_thumbnail():
    page = render_html(_report(_match(_item())))
    assert 'class="thumb placeholder"' in page


def test_year_media_and_catno_render():
    page = render_html(_report(_match(
        _item(year=1991, catno="TG105CD", media=frozenset({MediaKind.CD}))
    )))
    assert "1991 · cd · TG105CD" in page


def test_search_haystack_is_lowercased():
    page = render_html(_report(_match(_item(artist="Slint", title="Spiderland"))))
    assert 'data-search="slint spiderland' in page


# ── escaping and URL safety ───────────────────────────────────────────────────

def test_text_is_escaped():
    page = render_html(_report(_match(
        _item(artist="<script>alert(1)</script>", title="A & B \"quoted\"")
    )))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "A &amp; B" in page


def test_account_is_escaped_in_the_title():
    page = render_html(_report(_match(_item()), account='"><script>x</script>'))
    assert "<script>x</script>" not in page
    assert "&lt;script&gt;x&lt;/script&gt;" in page


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
])
def test_unsafe_urls_are_dropped(url):
    page = render_html(_report(_match(_item(url=url, thumb_url=url))))
    assert url not in page
    assert 'class="thumb placeholder"' in page


def test_safe_link_is_kept_with_noopener():
    page = render_html(_report(_match(
        _item(url="https://www.discogs.com/release/1")
    )))
    assert 'href="https://www.discogs.com/release/1"' in page
    assert 'rel="noopener noreferrer"' in page


# ── CLI integration ───────────────────────────────────────────────────────────

def _cfg(tmp_path):
    return SimpleNamespace(
        core=SimpleNamespace(db_path=tmp_path / "spindlebot.db"),
        tools=SimpleNamespace(beet="/usr/bin/true"),
        collection=SimpleNamespace(
            source="fixture", account="", media=("cd",), index="beets",
            cache_dir=tmp_path / "cache", cache_ttl_hours=24.0,
            ignore_path=tmp_path / "collection-ignore.json",
        ),
        secrets=SimpleNamespace(discogs=SimpleNamespace(token="")),
    )


@pytest.fixture
def shelf(tmp_path):
    import json
    path = tmp_path / "shelf.json"
    path.write_text(json.dumps([
        {"artist": "Radiohead", "title": "OK Computer"},
        {"artist": "Beck", "title": "Sea Change"},
    ]))
    return path


@pytest.fixture(autouse=True)
def stub_library(monkeypatch):
    monkeypatch.setattr(
        "spindlebot.services.library_index.load",
        lambda cfg, index="auto": LibraryIndex(
            albums=[LibraryAlbum("Radiohead", "OK Computer")], counts={"beets": 1}
        ),
    )


def test_cli_writes_the_report(tmp_path, shelf, capsys):
    from spindlebot.cli import cmd_collection_audit

    out_path = tmp_path / "nested" / "report.html"
    rc = cmd_collection_audit(
        _cfg(tmp_path), ["--handle", str(shelf), "--html", str(out_path)]
    )
    assert rc == 0
    assert out_path.exists()
    page = out_path.read_text(encoding="utf-8")
    assert "Sea Change" in page
    assert str(out_path) in capsys.readouterr().out


def test_cli_reports_the_path_in_json(tmp_path, shelf, capsys):
    import json

    from spindlebot.cli import cmd_collection_audit

    out_path = tmp_path / "report.html"
    cmd_collection_audit(
        _cfg(tmp_path), ["--handle", str(shelf), "--html", str(out_path), "--json"]
    )
    assert json.loads(capsys.readouterr().out)["html"] == str(out_path)


def test_cli_json_html_is_null_when_not_requested(tmp_path, shelf, capsys):
    import json

    from spindlebot.cli import cmd_collection_audit

    cmd_collection_audit(_cfg(tmp_path), ["--handle", str(shelf), "--json"])
    assert json.loads(capsys.readouterr().out)["html"] is None


def test_cli_surfaces_an_unwritable_path(tmp_path, shelf, capsys):
    from spindlebot.cli import cmd_collection_audit

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    rc = cmd_collection_audit(
        _cfg(tmp_path), ["--handle", str(shelf), "--html", str(blocker / "r.html")]
    )
    assert rc == 1
    assert "could not write" in capsys.readouterr().err
