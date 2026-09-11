"""
`spindlebot note` — the CLI contract.

Two clusters matter most. Body input, because the four modes are the whole
reason this is usable from a terminal at all and a silent mis-read loses what
was typed. And resolution failure, because the CLI is the layer that must
REFUSE rather than guess: a note filed under the wrong album is invisible.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from spindlebot.cli import cmd_note, read_note_body
from spindlebot.core.collection import LibraryAlbum

LIBRARY = [
    LibraryAlbum("Old 97's", "Fight Songs", 1999, "mb-fight"),
    LibraryAlbum("Old 97's", "Too Far to Care", 1997, None),
    LibraryAlbum("Loreena McKennitt", "An Ancient Muse", 2006, None),
]


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(core=SimpleNamespace(db_path=tmp_path / "spindlebot.db"))


@pytest.fixture(autouse=True)
def stub_library(monkeypatch):
    """The library index shells out to beets; the CLI contract is not about that."""
    from spindlebot.services import library_index
    monkeypatch.setattr(
        library_index, "load",
        lambda cfg, index="auto": library_index.LibraryIndex(albums=list(LIBRARY)),
    )


class _Tty(io.StringIO):
    def isatty(self):
        return True


def _pipe(text: str) -> io.StringIO:
    stream = io.StringIO(text)
    stream.isatty = lambda: False
    return stream


def _run(cfg, *args) -> int:
    return cmd_note(cfg, list(args))


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# ── body input ───────────────────────────────────────────────────────────────

def test_messages_join_as_paragraphs():
    """Repeatable -m is how multi-paragraph input gets in without fighting the
    shell over newlines."""
    assert read_note_body(["-m", "one", "-m", "two"]) == "one\n\ntwo"


def test_a_message_keeps_its_own_newlines():
    assert read_note_body(["-m", "line one\nline two"]) == "line one\nline two"


def test_file_input(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("from a file", encoding="utf-8")
    assert read_note_body(["-F", str(path)]) == "from a file"


def test_dash_reads_stdin():
    assert read_note_body(["-"], stdin=_pipe("piped in")) == "piped in"


def test_a_pipe_is_read_without_being_asked():
    """`... | spindlebot note add --album X` must just work."""
    assert read_note_body([], stdin=_pipe("piped in")) == "piped in"


def test_a_terminal_with_no_input_opens_the_editor():
    def fake_editor(path):
        path.write_text("typed in the editor", encoding="utf-8")
    body = read_note_body([], stdin=_Tty(), launch_editor=fake_editor)
    assert body == "typed in the editor"


def test_the_editor_buffer_is_seeded_for_an_edit():
    """`note edit` opens the existing text, so an edit is an edit."""
    seen = {}

    def fake_editor(path):
        seen["initial"] = path.read_text(encoding="utf-8")
    read_note_body([], initial="existing body", stdin=_Tty(), launch_editor=fake_editor)
    assert seen["initial"] == "existing body"


def test_the_editor_buffer_has_no_comment_header():
    """`#` is a markdown heading here, so a git-style commented header would make
    "strip comments" and "keep the author's headings" the same operation."""
    seen = {}

    def fake_editor(path):
        seen["initial"] = path.read_text(encoding="utf-8")
    read_note_body([], stdin=_Tty(), launch_editor=fake_editor)
    assert seen["initial"] == ""


def test_messages_win_over_a_pipe():
    assert read_note_body(["-m", "explicit"], stdin=_pipe("piped")) == "explicit"


# ── add ──────────────────────────────────────────────────────────────────────

def test_add_resolves_and_writes(cfg, capsys):
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-m", "a delightful record", "--json") == 0
    payload = _json_out(capsys)
    assert payload["subject"] == "Old 97's — Fight Songs"
    assert payload["kind"] == "album" and payload["revision"] == 1


def test_add_writes_a_track_note_under_its_album(cfg, capsys):
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "--track", "Murder", "-m", "surprise track", "--json") == 0
    assert _json_out(capsys)["kind"] == "track"


def test_add_refuses_an_unresolvable_subject_and_offers_candidates(cfg, capsys):
    """The CLI's job at this point is to refuse. A note filed under the wrong
    album is invisible — there is no error to notice later."""
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fite Songs",
                "-m", "x", "--json") == 1
    payload = _json_out(capsys)
    assert "Old 97's — Fight Songs" in payload["candidates"]


def test_add_refuses_a_track_without_an_album(cfg, capsys):
    assert _run(cfg, "add", "--artist", "Old 97s", "--track", "Murder", "-m", "x",
                "--json") == 1
    assert "requires --album" in _json_out(capsys)["error"]


def test_new_writes_about_something_the_library_lacks(cfg, capsys):
    """The corpus contains notes about records never owned."""
    assert _run(cfg, "add", "--artist", "Loreena McKennitt",
                "--album", "Morada Del Corazon", "--new", "-m", "look for this",
                "--json") == 0
    assert _json_out(capsys)["subject"].endswith("Morada Del Corazon")


def test_an_empty_body_writes_nothing(cfg, capsys):
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-m", "   ", "--json") == 1
    assert "empty note" in _json_out(capsys)["error"]


def test_add_accepts_tags(cfg, capsys):
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-m", "x", "--tag", "todo", "--tag", "surprise", "--json") == 0
    assert _json_out(capsys)["tags"] == ["surprise", "todo"]


def test_a_tag_value_is_not_mistaken_for_a_positional(cfg, capsys):
    """The hand-rolled parser has to know which tokens a flag consumed."""
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
         "-m", "x", "--tag", "todo", "--json")
    assert _json_out(capsys)["tags"] == ["todo"]


def test_new_still_works_when_the_library_cannot_be_read(cfg, capsys, monkeypatch):
    """--new says the library is not the authority for this note, so a beets
    failure must not block writing it."""
    from spindlebot.services import library_index
    monkeypatch.setattr(library_index, "load", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("beets is not installed")))
    assert _run(cfg, "add", "--artist", "Some Band", "--album", "Some Record",
                "--new", "-m", "x", "--json") == 0


def test_a_broken_library_blocks_a_normal_add(cfg, capsys, monkeypatch):
    """Without --new there is nothing to resolve against, and filing under an
    unverified subject is the failure this whole layer exists to prevent."""
    from spindlebot.services import library_index
    monkeypatch.setattr(library_index, "load", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("beets is not installed")))
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-m", "x", "--json") == 1
    assert "cannot read the library" in _json_out(capsys)["error"]


# ── list / show / edit ───────────────────────────────────────────────────────

def _seed(cfg):
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs", "-m", "album note")
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
         "--track", "Murder", "-m", "track note")
    _run(cfg, "add", "--artist", "Loreena McKennitt", "--album", "An Ancient Muse",
         "-m", "other note")


def test_list_filters_by_artist_and_reaches_its_tracks(cfg, capsys):
    _seed(cfg)
    capsys.readouterr()
    assert _run(cfg, "list", "--artist", "Old 97s", "--json") == 0
    payload = _json_out(capsys)
    assert payload["count"] == 2
    assert {n["kind"] for n in payload["notes"]} == {"album", "track"}


def test_list_narrows_by_kind(cfg, capsys):
    _seed(cfg)
    capsys.readouterr()
    _run(cfg, "list", "--artist", "Old 97s", "--kind", "track", "--json")
    assert _json_out(capsys)["count"] == 1


def test_an_unknown_kind_is_rejected(cfg, capsys):
    assert _run(cfg, "list", "--kind", "songwriter", "--json") == 1


def test_an_unparseable_since_is_rejected(cfg, capsys):
    assert _run(cfg, "list", "--since", "last tuesday", "--json") == 1
    assert "--since" in _json_out(capsys)["error"]


def test_list_on_an_empty_db_says_so(cfg, capsys):
    assert _run(cfg, "list") == 0
    assert "no notes" in capsys.readouterr().out


def test_show_renders_the_body(cfg, capsys):
    _seed(cfg)
    capsys.readouterr()
    _run(cfg, "list", "--album", "Fight Songs", "--kind", "album", "--json")
    note_id = _json_out(capsys)["notes"][0]["id"]
    assert _run(cfg, "show", str(note_id)) == 0
    assert "album note" in capsys.readouterr().out


def test_show_on_a_missing_note_fails(cfg, capsys):
    assert _run(cfg, "show", "999", "--json") == 1


def test_edit_appends_a_revision_and_history_keeps_both(cfg, capsys):
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
         "-m", "first", "--json")
    note_id = _json_out(capsys)["id"]

    assert _run(cfg, "edit", str(note_id), "-m", "second", "--json") == 0
    assert _json_out(capsys)["revision"] == 2

    _run(cfg, "show", str(note_id), "--history", "--json")
    bodies = [r["body"] for r in _json_out(capsys)["history"]]
    assert bodies == ["first", "second"]


def test_an_unchanged_edit_reports_no_change(cfg, capsys):
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
         "-m", "same", "--json")
    note_id = _json_out(capsys)["id"]
    _run(cfg, "edit", str(note_id), "-m", "same", "--json")
    payload = _json_out(capsys)
    assert payload["changed"] is False and payload["revision"] == 1


# ── rm / restore / tags ──────────────────────────────────────────────────────

def test_rm_is_soft_and_restore_brings_it_back(cfg, capsys):
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
         "-m", "body", "--json")
    note_id = _json_out(capsys)["id"]

    assert _run(cfg, "rm", str(note_id), "--json") == 0
    _run(cfg, "list", "--json")
    assert _json_out(capsys)["count"] == 0
    _run(cfg, "list", "--all", "--json")
    assert _json_out(capsys)["count"] == 1

    assert _run(cfg, "restore", str(note_id), "--json") == 0
    _run(cfg, "list", "--json")
    assert _json_out(capsys)["count"] == 1


def test_rm_tells_the_user_it_is_recoverable(cfg, capsys):
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs", "-m", "b", "--json")
    note_id = _json_out(capsys)["id"]
    _run(cfg, "rm", str(note_id))
    assert "note restore" in capsys.readouterr().out


def test_rm_on_a_missing_note_fails(cfg, capsys):
    assert _run(cfg, "rm", "999", "--json") == 1


def test_tag_and_untag(cfg, capsys):
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs", "-m", "b", "--json")
    note_id = _json_out(capsys)["id"]
    _run(cfg, "tag", str(note_id), "todo", "--json")
    assert _json_out(capsys)["tags"] == ["todo"]
    _run(cfg, "untag", str(note_id), "todo", "--json")
    assert _json_out(capsys)["tags"] == []


# ── sessions ─────────────────────────────────────────────────────────────────

def test_a_sitting_groups_notes_written_during_it(cfg, capsys):
    assert _run(cfg, "session", "start", "--title", "Sunday CDs", "--json") == 0
    session_id = _json_out(capsys)["id"]

    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
         "-m", "album", "--session", str(session_id), "--json")
    _run(cfg, "add", "--artist", "Loreena McKennitt", "--album", "An Ancient Muse",
         "-m", "unrelated", "--json")

    _run(cfg, "list", "--session", str(session_id), "--json")
    assert _json_out(capsys)["count"] == 1

    _run(cfg, "sessions", "--json")
    payload = _json_out(capsys)
    assert payload["sessions"][0]["title"] == "Sunday CDs"
    assert payload["sessions"][0]["notes"] == 1


def test_sessions_on_an_empty_db_says_so(cfg, capsys):
    assert _run(cfg, "sessions") == 0
    assert "no sessions" in capsys.readouterr().out


def test_an_unknown_subcommand_is_rejected(cfg, capsys):
    assert _run(cfg, "frobnicate") == 1


# ── argv parsing ─────────────────────────────────────────────────────────────

def test_a_flags_value_is_never_read_as_a_positional(cfg, capsys):
    """`note tag 1 todo --session 3` must attach one tag, not two.

    The hand-rolled parser has to know that `--session` consumed the `3`. Skip
    that and the value lands in the positional list, where `tag` reads
    everything after the id as a tag name.
    """
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs", "-m", "b", "--json")
    note_id = _json_out(capsys)["id"]
    _run(cfg, "tag", str(note_id), "todo", "--session", "3", "--json")
    assert _json_out(capsys)["tags"] == ["todo"]


def test_acceptance_is_gated_on_the_resolution_STATUS(cfg, capsys, monkeypatch):
    """Not on whether a subject happens to be attached.

    Today an AMBIGUOUS resolution never carries a subject, so the two tests are
    equivalent — which is exactly why this needs pinning. The moment the
    resolver offers a best guess alongside an ambiguous verdict, a
    `subject is None` check would accept it in silence.
    """
    from spindlebot import cli
    from spindlebot.core.notes import NoteSubjectRef
    from spindlebot.services.note_resolve import Resolution, ResolutionStatus

    monkeypatch.setattr(cli, "_note_resolve", lambda *a, **k: Resolution(
        ResolutionStatus.AMBIGUOUS,
        subject=NoteSubjectRef.for_album("Old 97's", "Fight Songs"),
        reason="a best guess that must not be taken",
    ))
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-m", "x", "--json") == 1
    assert "ambiguous" in _json_out(capsys)["error"]
