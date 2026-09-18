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


def test_export_round_trips_with_an_artist_less_album(cfg, capsys, tmp_path):
    """The one gapped shape a supported path can actually produce: `note add
    --album X --new` with no artist, or `## X` in an imported file with no
    artist heading above it.

    Heading nesting cannot express an empty parent level once one is in scope,
    so a note that empties a level must never FOLLOW one that fills it. The
    grouped sort guarantees that — None sorts as "", putting such a note first
    within its prefix — which is why the ordering is load-bearing rather than
    cosmetic. Raised in review on PR #72.
    """
    _run(cfg, "add", "--album", "Nameless Record", "--new", "-m", "album with no artist")
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs", "-m", "album note")
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
         "--track", "Murder", "-m", "track note")
    capsys.readouterr()

    _run(cfg, "export")
    exported = capsys.readouterr().out
    assert "# None" not in exported
    assert "album with no artist" in exported

    doc = tmp_path / "gapped.md"
    doc.write_text(exported, encoding="utf-8")
    fresh = SimpleNamespace(core=SimpleNamespace(db_path=tmp_path / "fresh.db"))
    assert _run(fresh, "import", str(doc), "--new", "--json") == 0
    assert set(_statuses(_json_out(capsys))) == {"imported"}

    _run(fresh, "export")
    assert capsys.readouterr().out == exported, "byte-identical through a gapped corpus"


def test_a_track_subject_with_no_album_is_unreachable(cfg, capsys, tmp_path):
    """Why export never has to cope with the worst gapped shape.

    `render`/`parse` handle a parentless `### C` perfectly well, but resolution
    refuses it at BOTH entry points, so no such subject can be created. The
    guarantee lives there rather than in a downstream warning.
    """
    assert _run(cfg, "add", "--artist", "Old 97s", "--track", "Murder",
                "-m", "x", "--json") == 1
    assert "requires --album" in _json_out(capsys)["error"]

    doc = tmp_path / "parentless.md"
    doc.write_text("### Wandering Track\n\nbody\n", encoding="utf-8")
    assert _run(cfg, "import", str(doc), "--new", "--json") == 1
    payload = _json_out(capsys)
    assert payload["blocked"] is True
    assert "requires --album" in payload["rows"][0]["detail"]

    _run(cfg, "list", "--json")
    assert _json_out(capsys)["count"] == 0


def test_the_grouped_sort_leaves_nothing_unrepresentable(cfg, capsys):
    """Stated directly against the detector, so a future change to the export
    sort fails here rather than quietly corrupting a document."""
    from spindlebot.core.note_markdown import ParsedNote, unrepresentable
    from spindlebot.db.connection import open_db
    from spindlebot.services import notes as svc

    _run(cfg, "import", str(FIXTURE), "--root-level", "2", "--json")
    _run(cfg, "add", "--album", "Nameless Record", "--new", "-m", "no artist")
    _run(cfg, "add", "--artist", "Old 97s", "--new", "-m", "artist level")

    conn = open_db(cfg.core.db_path)
    views = svc.list_notes(conn)
    conn.close()
    views.sort(key=lambda v: (
        (v.subject.artist_name or "").casefold(),
        (v.subject.album_title or "").casefold(),
        (v.subject.track_title or "").casefold(),
        v.note.created_utc,
    ))
    parsed = [
        ParsedNote(kind=v.subject.kind, body=v.body, artist=v.subject.artist_name,
                   album=v.subject.album_title, track=v.subject.track_title)
        for v in views
    ]
    assert unrepresentable(parsed) == ()


def test_re_importing_after_the_album_is_ripped_does_not_duplicate(cfg, capsys):
    """Idempotence has to survive a subject's identity sharpening.

    `add_note` re-keys a name-derived subject onto its MusicBrainz-backed key the
    first time the album resolves with an id. Looking up only the NEW key found
    nothing, so the row was marked READY and a second copy of a body already
    present was inserted — the failure landing exactly when a wishlist note
    graduates to an owned one, which is the real corpus's situation.
    """
    from spindlebot.core.notes import NoteSubjectRef
    from spindlebot.db.connection import open_db
    from spindlebot.services import notes as svc

    conn = open_db(cfg.core.db_path)
    svc.add_note(conn, subject=NoteSubjectRef.for_album(
        "Loreena McKennitt", "Morada Del Corazon"), body="Look for this record.")
    conn.commit()
    conn.close()

    doc = FIXTURE.parent / "_morada.md"
    doc.write_text("# Loreena McKennitt\n\n## Morada Del Corazon\n\nLook for this record.\n",
                   encoding="utf-8")
    try:
        # The library now knows it, with an MBID.
        from spindlebot.services import library_index
        owned = [LibraryAlbum("Loreena McKennitt", "Morada Del Corazon", 2001, "mb-morada")]
        library_index.load = lambda cfg, index="auto": library_index.LibraryIndex(albums=owned)

        assert _run(cfg, "import", str(doc), "--json") == 0
        assert _statuses(_json_out(capsys)) == ["duplicate"]
        _run(cfg, "list", "--json")
        assert _json_out(capsys)["count"] == 1
    finally:
        doc.unlink(missing_ok=True)


# ── the lossless export (review round 4, PR #74) ─────────────────────────────
# Markdown carries prose for humans and nothing else. Tags are an OPEN set, so
# no delimiter is safe inside one and any marker chosen can also open a line of
# someone's actual writing — encoding them in the markdown was a mistake. Every
# un-regenerable thing lives here instead.

def _export_json(cfg, capsys, *extra) -> dict:
    _run(cfg, "export", "--json", *extra)
    return json.loads(capsys.readouterr().out)


def test_json_export_carries_everything_markdown_cannot(cfg, capsys):
    _run(cfg, "session", "start", "--title", "Sunday CDs", "--json")
    session_id = _json_out(capsys)["id"]
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs", "-m", "first",
         "--tag", "todo", "--tag", "pressing, original", "--session", str(session_id),
         "--json")
    note_id = _json_out(capsys)["id"]
    _run(cfg, "edit", str(note_id), "-m", "second", "--json")
    capsys.readouterr()

    payload = _export_json(cfg, capsys)
    assert payload["schema"] == "spindlebot.notes/1"
    note = payload["notes"][0]

    assert note["tags"] == ["pressing, original", "todo"], "a comma in a tag is a non-issue here"
    assert note["uuid"] and note["subject_key"] and note["mbid"] == "mb-fight"
    assert note["session"]["title"] == "Sunday CDs"
    assert [r["seq"] for r in note["revisions"]] == [1, 2]
    assert [r["body"] for r in note["revisions"]] == ["first", "second"]
    assert all(r["sha256"] for r in note["revisions"])


def test_markdown_export_carries_prose_only(cfg, capsys):
    """And says nothing about tags — no marker, nothing to collide with."""
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs", "-m", "body",
         "--tag", "todo", "--json")
    capsys.readouterr()
    _run(cfg, "export")
    text = capsys.readouterr().out
    assert "body" in text
    assert "todo" not in text and "<!--" not in text


def test_a_body_that_looks_like_the_old_tags_marker_is_just_prose(cfg, capsys):
    """The marker has no meaning now, so nothing can eat a line of writing."""
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
         "-m", "<!-- tags: not-metadata -->", "--json")
    capsys.readouterr()
    _run(cfg, "export")
    exported = capsys.readouterr().out
    assert "not-metadata" in exported


def test_json_export_honours_filters(cfg, capsys):
    _run(cfg, "import", str(FIXTURE), "--root-level", "2", "--json")
    capsys.readouterr()
    payload = _export_json(cfg, capsys, "--artist", "Old 97s")
    assert payload["count"] == 3
    assert {n["artist"] for n in payload["notes"]} == {"Old 97s"}, \
        "one spelling, the library's"


def test_json_export_of_nothing_is_still_valid_json(cfg, capsys):
    payload = _export_json(cfg, capsys)
    assert payload["count"] == 0 and payload["notes"] == []


def test_writing_to_a_file_keeps_stdout_clean(cfg, capsys, tmp_path):
    """stdout is the data channel; a status line must not end up in the file or
    in a caller's pipe."""
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs", "-m", "b", "--json")
    out = tmp_path / "notes.json"
    capsys.readouterr()
    assert _run(cfg, "export", "--json", "-o", str(out)) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "1 note(s)" in captured.err
    assert json.loads(out.read_text(encoding="utf-8"))["count"] == 1
