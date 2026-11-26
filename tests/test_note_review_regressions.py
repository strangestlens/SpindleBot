"""Review regressions for the notes workflow, using temporary databases only."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from spindlebot.cli import cmd_note, read_note_body
from spindlebot.core.collection import LibraryAlbum
from spindlebot.core.enums import ImportRowStatus, NoteSubjectKind
from spindlebot.core.note_markdown import ParsedNote
from spindlebot.core.notes import NoteSubjectRef
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import note_subject_repo
from spindlebot.services import notes
from spindlebot.services.note_import import apply_import, plan_import

LIBRARY = [LibraryAlbum("The Beatles", "Revolver", 1966, "mb-revolver")]


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from spindlebot.services import library_index
    monkeypatch.setattr(library_index, "load", lambda *a, **kw:
                        library_index.LibraryIndex(albums=LIBRARY))
    return SimpleNamespace(core=SimpleNamespace(db_path=tmp_path / "cli.db"))


@pytest.mark.parametrize("kind", [NoteSubjectKind.ALBUM, NoteSubjectKind.TRACK])
@pytest.mark.parametrize("legacy_artist", ["Beatles", ""])
def test_adopts_article_variant_or_unknown_artist(conn, kind, legacy_artist):
    factory = NoteSubjectRef.for_album if kind == NoteSubjectKind.ALBUM else NoteSubjectRef.for_track
    args = ("Revolver",) if kind == NoteSubjectKind.ALBUM else ("Revolver", "Taxman")
    legacy = notes.add_note(conn, subject=factory(legacy_artist, *args), body="wishlist")
    identified = notes.add_note(conn, subject=factory("The Beatles", *args, "mb-revolver"), body="owned")
    assert identified.subject.id == legacy.subject.id
    assert len(note_subject_repo.list_all(conn)) == 1


def test_article_adoption_prefers_exact_artist(conn):
    exact = notes.add_note(conn, subject=NoteSubjectRef.for_album("The Band", "Album"), body="a")
    other = notes.add_note(conn, subject=NoteSubjectRef.for_album("Band", "Album"), body="b")
    identified = notes.add_note(conn, subject=NoteSubjectRef.for_album("The Band", "Album", "mb"), body="c")
    assert identified.subject.id == exact.subject.id
    assert identified.subject.id != other.subject.id


def test_article_adoption_refuses_multiple_loose_candidates(conn):
    for artist in ("Band", "The Band"):
        notes.add_note(conn, subject=NoteSubjectRef.for_album(artist, "Album"), body=artist)
    assert notes.find_adoptable_subject(conn, NoteSubjectRef.for_album("Band (3)", "Album", "mb")) is None


@pytest.mark.parametrize("field", ["artist", "album", "track"])
@pytest.mark.parametrize("value", ["", "!!!"])
def test_empty_normalized_filter_does_not_include_missing_fields(conn, field, value):
    notes.add_note(conn, subject=NoteSubjectRef.for_artist("Other"), body="unrelated")
    notes.add_note(conn, subject=NoteSubjectRef.for_album(None, "Other"), body="missing artist")
    factory = {"artist": lambda: NoteSubjectRef.for_artist(value),
               "album": lambda: NoteSubjectRef.for_album("Artist", value),
               "track": lambda: NoteSubjectRef.for_track("Artist", "Album", value)}[field]
    matching = notes.add_note(conn, subject=factory(), body="match")
    assert [v.id for v in notes.list_notes(conn, **{field: value})] == [matching.id]


def test_article_insensitive_filter_reaches_canonical_subjects(conn):
    matching = notes.add_note(conn, subject=NoteSubjectRef.for_album("The Beatles", "Revolver"), body="a")
    notes.add_note(conn, subject=NoteSubjectRef.for_artist("Other"), body="b")
    assert [v.id for v in notes.list_notes(conn, artist="Beatles")] == [matching.id]


def test_exact_artist_filter_keeps_distinct_article_identity(conn):
    exact = notes.add_note(conn, subject=NoteSubjectRef.for_artist("Band"), body="a")
    notes.add_note(conn, subject=NoteSubjectRef.for_artist("The Band"), body="b")
    assert [v.id for v in notes.list_notes(conn, artist="Band")] == [exact.id]


def test_loose_artist_filter_refuses_multiple_identities(conn):
    for artist in ("Band", "The Band"):
        notes.add_note(conn, subject=NoteSubjectRef.for_artist(artist), body=artist)
    assert notes.list_notes(conn, artist="Band (3)") == []


def test_duplicate_rows_in_one_document_are_planned_and_applied_once(conn):
    parsed = ParsedNote(kind=NoteSubjectKind.ALBUM, artist="Beatles", album="Revolver", body="same")
    plan = plan_import(conn, LIBRARY, [parsed, parsed])
    assert [r.status for r in plan.rows] == [ImportRowStatus.READY, ImportRowStatus.DUPLICATE]
    assert notes.list_notes(conn) == [], "planning stays read-only"
    applied = apply_import(conn, plan)
    assert len(notes.list_notes(conn)) == 1
    assert applied.rows[1].note_id == applied.rows[0].note_id


def test_message_aliases_preserve_argument_order():
    assert read_note_body(["--message", "first", "-m", "second", "--message", "third"]) == "first\n\nsecond\n\nthird"


def test_option_looking_text_is_consumed_as_text_not_another_message():
    assert read_note_body(["-m", "--message", "--message", "second"]) == "--message\n\nsecond"


@pytest.mark.parametrize("body", ["- an observation", "-urgent"])
def test_dash_prefixed_text_values_are_accepted(cfg, capsys, body):
    assert cmd_note(cfg, ["add", "--artist", "Beatles", "-m", body, "--tag", "-urgent", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["body"] == body and payload["tags"] == ["-urgent"]


def test_database_operational_errors_become_cli_errors(cfg, monkeypatch, capsys):
    original = notes.start_session

    def fail(*a, **kw):
        original(*a, **kw)
        raise sqlite3.OperationalError("readonly database")
    monkeypatch.setattr(notes, "start_session", fail)
    assert cmd_note(cfg, ["session", "start", "--json"]) == 1
    assert "readonly database" in json.loads(capsys.readouterr().out)["error"]
    conn = open_db(cfg.core.db_path)
    try:
        assert notes.list_sessions(conn) == [], "the failed command rolls back"
    finally:
        conn.close()


@pytest.mark.parametrize("mbid", [None, "missing-mbid"])
def test_ambiguous_or_explicit_id_failure_does_not_suggest_new(cfg, monkeypatch, capsys, mbid):
    from spindlebot import cli
    from spindlebot.services.note_resolve import Resolution, ResolutionStatus
    status = ResolutionStatus.UNMATCHED if mbid else ResolutionStatus.AMBIGUOUS
    monkeypatch.setattr(cli, "_note_resolve", lambda *a, **kw: Resolution(status, reason="choose release"))
    args = ["add", "--artist", "Beatles", "--album", "Revolver", "-m", "body", "--json"]
    if mbid:
        args += ["--mbid", mbid]
    assert cmd_note(cfg, args) == 1
    assert all("--new" not in hint for hint in json.loads(capsys.readouterr().out)["candidates"])
