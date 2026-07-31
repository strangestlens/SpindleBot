"""Tests for the `spindlebot collection-audit` CLI command."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from spindlebot.cli import cmd_collection_audit
from spindlebot.core.collection import LibraryAlbum
from spindlebot.services.library_index import LibraryIndex
from spindlebot.core.enums import MediaKind


def _cfg(tmp_path, account="", media=("cd",)):
    return SimpleNamespace(
        core=SimpleNamespace(db_path=tmp_path / "spindlebot.db"),
        tools=SimpleNamespace(beet="/usr/bin/true"),
        collection=SimpleNamespace(
            source="fixture", account=account, media=media, index="auto",
            cache_dir=tmp_path / "cache", cache_ttl_hours=24.0,
            ignore_path=tmp_path / "collection-ignore.json",
        ),
        secrets=SimpleNamespace(discogs=SimpleNamespace(token="")),
    )


@pytest.fixture
def collection_file(tmp_path):
    """A hand-written collection: one owned, one missing, one vinyl-only."""
    path = tmp_path / "shelf.json"
    path.write_text(json.dumps([
        {"artist": "Radiohead", "title": "OK Computer", "year": 1997},
        {"artist": "Beck", "title": "Sea Change", "year": 2002},
        {"artist": "Slint", "title": "Spiderland", "media": ["vinyl"]},
    ]))
    return path


def _index(*albums, counts=None, errors=None) -> LibraryIndex:
    return LibraryIndex(
        albums=list(albums),
        counts=counts or {"beets": len(albums)},
        errors=errors or {},
    )


@pytest.fixture(autouse=True)
def stub_library(monkeypatch):
    monkeypatch.setattr(
        "spindlebot.services.library_index.load",
        lambda cfg, index="auto": _index(
            LibraryAlbum("Radiohead", "OK Computer"),
            LibraryAlbum("Beck", "Mutations"),
        ),
    )


def test_reports_missing_albums(tmp_path, collection_file, capsys):
    rc = cmd_collection_audit(_cfg(tmp_path), ["--handle", str(collection_file)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "MISSING (1)" in out
    assert "Beck — Sea Change (2002)" in out
    assert "1 owned · 0 uncertain · 1 missing" in out
    # The vinyl item was filtered out by the default cd-only media set.
    assert "Spiderland" not in out


def test_media_flag_widens_the_filter(tmp_path, collection_file, capsys):
    rc = cmd_collection_audit(
        _cfg(tmp_path), ["--handle", str(collection_file), "--media", "cd,vinyl"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Spiderland" in out


def test_owned_hidden_unless_all_requested(tmp_path, collection_file, capsys):
    cmd_collection_audit(_cfg(tmp_path), ["--handle", str(collection_file)])
    assert "OWNED" not in capsys.readouterr().out

    cmd_collection_audit(_cfg(tmp_path), ["--handle", str(collection_file), "--all"])
    out = capsys.readouterr().out
    assert "OWNED (1)" in out
    assert "Radiohead — OK Computer" in out


def test_json_output(tmp_path, collection_file, capsys):
    rc = cmd_collection_audit(
        _cfg(tmp_path), ["--handle", str(collection_file), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["counts"] == {
        "owned": 1, "uncertain": 0, "missing": 1, "ignored": 0,
    }
    assert payload["fetched"] == 3
    assert payload["considered"] == 2
    assert payload["media"] == ["cd"]
    missing = [i for i in payload["items"] if i["status"] == "missing"]
    assert missing[0]["title"] == "Sea Change"
    assert missing[0]["key"].startswith("fixture:")


def test_shows_which_index_answered(tmp_path, collection_file, monkeypatch, capsys):
    """A wrongly-missing album is almost always an index that didn't know it,
    so the breakdown is never hidden."""
    monkeypatch.setattr(
        "spindlebot.services.library_index.load",
        lambda cfg, index="auto": _index(
            LibraryAlbum("Radiohead", "OK Computer"),
            counts={"beets": 112, "db": 177},
        ),
    )
    cmd_collection_audit(_cfg(tmp_path), ["--handle", str(collection_file)])
    assert "library (beets 112, db 177) — 1 unique album(s)" in capsys.readouterr().out


def test_warns_when_an_index_is_unavailable(tmp_path, collection_file, monkeypatch, capsys):
    monkeypatch.setattr(
        "spindlebot.services.library_index.load",
        lambda cfg, index="auto": _index(
            LibraryAlbum("Radiohead", "OK Computer"),
            counts={"beets": 1},
            errors={"db": "no SpindleBot DB — run `spindlebot inventory`"},
        ),
    )
    cmd_collection_audit(_cfg(tmp_path), ["--handle", str(collection_file)])
    assert "db index unavailable" in capsys.readouterr().err


def test_json_carries_the_index_breakdown(tmp_path, collection_file, capsys):
    cmd_collection_audit(_cfg(tmp_path), ["--handle", str(collection_file), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["library_sources"] == {"beets": 2}
    assert payload["library_errors"] == {}


def test_config_supplies_the_account(tmp_path, collection_file, capsys):
    cfg = _cfg(tmp_path, account=str(collection_file))
    rc = cmd_collection_audit(cfg, [])
    assert rc == 0
    assert "MISSING (1)" in capsys.readouterr().out


def test_handle_overrides_config(tmp_path, collection_file, capsys):
    other = tmp_path / "other.json"
    other.write_text(json.dumps([{"artist": "Duster", "title": "Stratosphere"}]))
    cfg = _cfg(tmp_path, account=str(collection_file))
    cmd_collection_audit(cfg, ["--handle", str(other)])
    out = capsys.readouterr().out
    assert "Stratosphere" in out
    assert "Sea Change" not in out


def test_missing_account_is_a_clear_error(tmp_path, capsys):
    rc = cmd_collection_audit(_cfg(tmp_path), [])
    assert rc == 1
    assert "--handle" in capsys.readouterr().err


def test_unknown_medium_is_a_clear_error(tmp_path, collection_file, capsys):
    rc = cmd_collection_audit(
        _cfg(tmp_path), ["--handle", str(collection_file), "--media", "minidisc"]
    )
    assert rc == 1
    assert "unknown medium" in capsys.readouterr().err


def test_unknown_source_is_a_clear_error(tmp_path, capsys):
    rc = cmd_collection_audit(_cfg(tmp_path), ["--handle", "x", "--source", "lastfm"])
    assert rc == 1
    assert "unknown collection source" in capsys.readouterr().err


def test_fetch_failure_reports_as_json_when_asked(tmp_path, capsys):
    rc = cmd_collection_audit(
        _cfg(tmp_path), ["--handle", "/nope/missing.json", "--json"]
    )
    assert rc == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_strict_flag_moves_uncertain_into_missing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "shelf.json"
    path.write_text(json.dumps([{"artist": "Portishead", "title": "Dummy Sessions"}]))
    monkeypatch.setattr(
        "spindlebot.services.library_index.load",
        lambda cfg, index="auto": _index(LibraryAlbum("Portishead", "Dummy Session")),
    )

    cmd_collection_audit(_cfg(tmp_path), ["--handle", str(path)])
    assert "UNCERTAIN (1)" in capsys.readouterr().out

    cmd_collection_audit(_cfg(tmp_path), ["--handle", str(path), "--strict"])
    out = capsys.readouterr().out
    assert "UNCERTAIN" not in out
    assert "MISSING (1)" in out


def test_item_shape_carries_ui_fields(tmp_path, capsys):
    """Phase 2's static HTML report renders straight off --json."""
    path = tmp_path / "shelf.json"
    path.write_text(json.dumps([{
        "artist": "Slint", "title": "Spiderland", "year": 1991,
        "url": "https://example.test/r/1", "thumb_url": "https://example.test/t/1.jpg",
    }]))
    cmd_collection_audit(_cfg(tmp_path), ["--handle", str(path), "--json"])
    item = json.loads(capsys.readouterr().out)["items"][0]
    assert item["url"] == "https://example.test/r/1"
    assert item["thumb_url"] == "https://example.test/t/1.jpg"
    assert item["media"] == [MediaKind.CD.value]
    assert item["reason"]
