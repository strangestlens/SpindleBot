"""
`note import` / `note export` — the escape hatch in both directions.

Notes are the only un-regenerable data in this database, so the guarantees that
matter are: an import never silently drops part of a document, re-running one
never duplicates, and anything exported can be read back in unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from spindlebot.cli import cmd_note
from spindlebot.core.collection import LibraryAlbum

FIXTURE = Path(__file__).parent / "fixtures" / "listening_notes_sample.md"

LIBRARY = [
    LibraryAlbum("Old 97s", "Fight Songs", 1999, "mb-fight"),
    LibraryAlbum("Afro Celt Sound System", "Volume 1 Sound Magic", 1996, None),
    LibraryAlbum("Loreena McKennitt", "An Ancient Muse", 2006, "mb-muse"),
]


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(core=SimpleNamespace(db_path=tmp_path / "spindlebot.db"))


@pytest.fixture(autouse=True)
def stub_library(monkeypatch):
    from spindlebot.services import library_index
    monkeypatch.setattr(
        library_index, "load",
        lambda cfg, index="auto": library_index.LibraryIndex(albums=list(LIBRARY)),
    )


def _run(cfg, *args) -> int:
    return cmd_note(cfg, list(args))


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _statuses(payload) -> list[str]:
    return [r["status"] for r in payload["rows"]]


# ── import ───────────────────────────────────────────────────────────────────

def test_dry_run_writes_nothing(cfg, capsys):
    assert _run(cfg, "import", str(FIXTURE), "--root-level", "2",
                "--dry-run", "--json") == 0
    assert all(s == "ready" for s in _statuses(_json_out(capsys)))

    _run(cfg, "list", "--json")
    assert _json_out(capsys)["count"] == 0


def test_the_real_corpus_imports(cfg, capsys):
    assert _run(cfg, "import", str(FIXTURE), "--root-level", "2", "--json") == 0
    payload = _json_out(capsys)
    assert set(_statuses(payload)) == {"imported"}
    assert len(payload["rows"]) == 7

    _run(cfg, "list", "--json")
    assert _json_out(capsys)["count"] == 7


def test_a_grouping_heading_is_reported_not_silently_dropped(cfg, capsys):
    """`# CDs` names no artist. The author wrote it, so the import says so."""
    _run(cfg, "import", str(FIXTURE), "--root-level", "2", "--dry-run", "--json")
    assert [h["text"] for h in _json_out(capsys)["skipped_headings"]] == ["CDs"]


def test_one_import_becomes_one_listening_session(cfg, capsys):
    """Provenance survives: a day's writing stays one event."""
    _run(cfg, "import", str(FIXTURE), "--root-level", "2", "--json")
    _run(cfg, "sessions", "--json")
    sessions = _json_out(capsys)["sessions"]
    assert len(sessions) == 1 and sessions[0]["notes"] == 7


def test_re_importing_the_same_file_duplicates_nothing(cfg, capsys):
    """Idempotent by body hash per subject — an accidental second run is safe."""
    _run(cfg, "import", str(FIXTURE), "--root-level", "2", "--json")
    assert _run(cfg, "import", str(FIXTURE), "--root-level", "2", "--json") == 0
    assert set(_statuses(_json_out(capsys))) == {"duplicate"}

    _run(cfg, "list", "--json")
    assert _json_out(capsys)["count"] == 7


def test_an_unresolved_note_blocks_the_WHOLE_import(cfg, capsys, tmp_path):
    """Filing 2 of 3 notes and silently dropping one is the worst available
    outcome; the caller has to make a decision instead."""
    doc = tmp_path / "mixed.md"
    doc.write_text(
        "# Old 97s\n\n## Fight Songs\n\nknown album\n\n"
        "# Nobody At All\n\n## Some Record\n\nunknown\n",
        encoding="utf-8",
    )
    assert _run(cfg, "import", str(doc), "--json") == 1
    payload = _json_out(capsys)
    assert payload["blocked"] is True
    assert "unresolved" in _statuses(payload)

    _run(cfg, "list", "--json")
    assert _json_out(capsys)["count"] == 0, "nothing was written"


def test_skip_unresolved_imports_the_rest(cfg, capsys, tmp_path):
    doc = tmp_path / "mixed.md"
    doc.write_text(
        "# Old 97s\n\n## Fight Songs\n\nknown album\n\n"
        "# Nobody At All\n\n## Some Record\n\nunknown\n",
        encoding="utf-8",
    )
    assert _run(cfg, "import", str(doc), "--skip-unresolved", "--json") == 0
    assert sorted(_statuses(_json_out(capsys))) == ["imported", "unresolved"]
    _run(cfg, "list", "--json")
    assert _json_out(capsys)["count"] == 1


def test_new_resolves_everything_the_library_lacks(cfg, capsys, tmp_path):
    doc = tmp_path / "wishlist.md"
    doc.write_text("# Nobody At All\n\n## Some Record\n\nlook for this\n",
                   encoding="utf-8")
    assert _run(cfg, "import", str(doc), "--new", "--json") == 0
    assert _statuses(_json_out(capsys)) == ["imported"]


def test_the_wrong_root_level_says_so_instead_of_importing_nonsense(cfg, capsys):
    """At level 1 the fixture's `# CDs` is the only artist and nothing nests
    under it correctly — better to refuse with a hint than to file junk."""
    assert _run(cfg, "import", str(FIXTURE), "--json") == 1
    payload = _json_out(capsys)
    assert payload["blocked"] is True
    assert "--root-level 2" in payload["hint"]


def test_a_missing_file_fails(cfg, capsys):
    assert _run(cfg, "import", "/nonexistent/notes.md", "--json") == 1


def test_a_bad_root_level_is_rejected(cfg, capsys):
    assert _run(cfg, "import", str(FIXTURE), "--root-level", "deep", "--json") == 1


# ── export ───────────────────────────────────────────────────────────────────

def test_export_round_trips_through_import(cfg, capsys, tmp_path):
    """THE contract. If this holds, the writing is never trapped in SQLite."""
    _run(cfg, "import", str(FIXTURE), "--root-level", "2", "--json")
    capsys.readouterr()

    _run(cfg, "export")
    exported = capsys.readouterr().out
    doc = tmp_path / "exported.md"
    doc.write_text(exported, encoding="utf-8")

    # A fresh database, imported from the export alone.
    fresh = SimpleNamespace(core=SimpleNamespace(db_path=tmp_path / "fresh.db"))
    assert _run(fresh, "import", str(doc), "--json") == 0
    assert set(_statuses(_json_out(capsys))) == {"imported"}

    _run(fresh, "export")
    assert capsys.readouterr().out == exported


def test_export_to_a_file(cfg, capsys, tmp_path):
    _run(cfg, "import", str(FIXTURE), "--root-level", "2", "--json")
    out = tmp_path / "notes.md"
    assert _run(cfg, "export", "-o", str(out)) == 0
    assert "Fight Songs" in out.read_text(encoding="utf-8")


def test_export_groups_by_subject_rather_than_by_recency(cfg, capsys):
    """An exported document is meant to be read as a document."""
    _run(cfg, "add", "--artist", "Loreena McKennitt", "--album", "An Ancient Muse",
         "-m", "written first")
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
         "-m", "written second")
    capsys.readouterr()
    _run(cfg, "export")
    text = capsys.readouterr().out
    assert text.index("Loreena") < text.index("Old 97s")


def test_export_honours_filters(cfg, capsys):
    _run(cfg, "import", str(FIXTURE), "--root-level", "2", "--json")
    capsys.readouterr()
    _run(cfg, "export", "--artist", "Old 97s")
    text = capsys.readouterr().out
    assert "Fight Songs" in text and "Loreena" not in text


def test_exporting_nothing_is_empty_not_an_error(cfg, capsys):
    assert _run(cfg, "export") == 0
    assert capsys.readouterr().out == ""


def test_export_root_level_shifts_the_headings(cfg, capsys):
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs", "-m", "b")
    capsys.readouterr()
    _run(cfg, "export", "--root-level", "2")
    assert "## Old 97s" in capsys.readouterr().out
