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
from pathlib import Path
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
    assert any("Fight Songs" in c for c in payload["candidates"])


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

def test_a_flags_value_is_never_read_as_a_positional(cfg, capsys, tmp_path):
    """`note import <file> --root-level 2` has ONE operand, not two.

    The hand-rolled parser has to know that `--root-level` consumed the `2`.
    Skip that and the value lands in the positional list, where the operand
    check rejects a perfectly valid command.
    """
    doc = tmp_path / "n.md"
    doc.write_text("# CDs\n\n## Old 97s\n\n### Fight Songs\n\nbody\n", encoding="utf-8")
    assert _run(cfg, "import", str(doc), "--root-level", "2", "--dry-run", "--json") == 0


def test_a_flag_belonging_to_another_subcommand_is_rejected(cfg, capsys):
    """`--session` is meaningful on `add`, not on `tag`. A shared flag table
    accepted it there and then silently ignored it."""
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs", "-m", "b", "--json")
    note_id = _json_out(capsys)["id"]
    assert _run(cfg, "tag", str(note_id), "todo", "--session", "3", "--json") == 1
    assert "--session" in _json_out(capsys)["error"]


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


# ── session dates render in local time ───────────────────────────────────────

@pytest.fixture
def fixed_tz(monkeypatch):
    """Pin the process timezone so "local" is assertable.

    `time.tzset` is POSIX-only, which is fine — this project is macOS, and CI
    runs Linux. The env var alone is not enough: Python caches the zone.
    """
    import time

    def _set(name: str):
        monkeypatch.setenv("TZ", name)
        time.tzset()
    yield _set
    time.tzset()  # monkeypatch has restored TZ; make Python notice


def test_session_dates_render_in_local_time(cfg, capsys, fixed_tz):
    """A listening sitting is a local-time event. 2026-09-15 04:23 UTC is
    2026-09-14 21:23 in Los Angeles — rendering it as the 15th puts an evening's
    listening on the wrong day, and disagrees with `--since`, which already
    reads a bare date as LOCAL midnight.
    """
    fixed_tz("America/Los_Angeles")
    evening_local = 1789435401  # 2026-09-14 21:23 PDT / 2026-09-15 04:23 UTC

    from spindlebot.db.connection import open_db
    from spindlebot.services import notes as svc
    conn = open_db(cfg.core.db_path)
    svc.start_session(conn, title="Sunday CDs", occurred_utc=evening_local,
                      now=evening_local)
    conn.commit()
    conn.close()
    capsys.readouterr()

    assert _run(cfg, "sessions") == 0
    out = capsys.readouterr().out
    assert "2026-09-14" in out
    assert "2026-09-15" not in out


def test_since_selects_the_session_its_own_date_display_shows(cfg, capsys, fixed_tz):
    """The consistency the UTC bug broke: a session shown as the 14th has to be
    the one `--since 2026-09-14` returns."""
    fixed_tz("America/Los_Angeles")
    evening_local = 1789435401

    from spindlebot.db.connection import open_db
    from spindlebot.services import notes as svc
    conn = open_db(cfg.core.db_path)
    svc.start_session(conn, title="Sunday CDs", occurred_utc=evening_local,
                      now=evening_local)
    conn.commit()
    conn.close()
    capsys.readouterr()

    _run(cfg, "sessions", "--since", "2026-09-14", "--json")
    assert _json_out(capsys)["count"] == 1
    _run(cfg, "sessions", "--since", "2026-09-15", "--json")
    assert _json_out(capsys)["count"] == 0


def test_session_json_reports_the_raw_epoch(cfg, capsys):
    """Machine output stays timezone-free; only the human rendering localizes."""
    _run(cfg, "session", "start", "--title", "x", "--json")
    assert isinstance(_json_out(capsys)["occurred_utc"], int)


# ── review round 3 (PR #74) ──────────────────────────────────────────────────

@pytest.mark.parametrize("args,typo", [
    (["list", "--artistt", "Nobody"], "--artistt"),
    (["export", "--artsit", "Nobody"], "--artsit"),
    (["add", "--artist", "Old 97s", "--albm", "Fight Songs", "-m", "x"], "--albm"),
])
def test_a_mistyped_option_is_an_error_not_a_silent_no_op(cfg, capsys, args, typo):
    """Ignoring unknown options is dangerous rather than lenient: `note list
    --artistt X` dropped the filter and listed the WHOLE corpus, and
    `note export --artsit X` exported everything. Both looked like success."""
    assert _run(cfg, *args, "--json") == 1
    assert typo in _json_out(capsys)["error"]


def test_a_bare_dash_is_stdin_not_an_unknown_option():
    """`-` is the read-stdin sentinel, and tightening the unknown-flag check to
    everything starting with `-` must not swallow it."""
    from spindlebot.cli import _note_positionals, _note_unknown_flags
    assert _note_unknown_flags(["--album", "B", "-"]) == []
    assert _note_positionals(["--album", "B", "-"]) == []


def test_a_short_unknown_option_is_caught_too():
    from spindlebot.cli import _note_unknown_flags
    assert _note_unknown_flags(["-x", "value"]) == ["-x"]


def test_a_value_that_looks_like_a_flag_is_not_flagged():
    """`--tag -weird` consumes its value; only unconsumed tokens are options."""
    from spindlebot.cli import _note_unknown_flags
    assert _note_unknown_flags(["--tag", "-weird"]) == []


@pytest.mark.parametrize("sub", ["import", "export"])
def test_an_out_of_range_root_level_is_refused_at_both_entry_points(cfg, capsys, sub, tmp_path):
    doc = tmp_path / "n.md"
    doc.write_text("# A\n\nbody\n", encoding="utf-8")
    args = [sub, str(doc)] if sub == "import" else [sub]
    assert _run(cfg, *args, "--root-level", "5", "--json") == 1
    assert "root level" in _json_out(capsys)["error"]


def test_history_prints_newest_prior_edit_first_and_oldest_last(cfg, capsys):
    """The order docs/notes.md documents, and the useful one — recent context
    nearer the top. It printed current, then 1, then 2."""
    _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs", "-m", "first",
         "--json")
    note_id = _json_out(capsys)["id"]
    _run(cfg, "edit", str(note_id), "-m", "second", "--json")
    _run(cfg, "edit", str(note_id), "-m", "third", "--json")
    capsys.readouterr()

    _run(cfg, "show", str(note_id), "--history")
    out = capsys.readouterr().out
    assert out.index("third") < out.index("second") < out.index("first")


# ── per-subcommand validation (review round 4, PR #74) ───────────────────────
# A shared flag table was not enough. Flags from other subcommands were accepted
# and silently ignored, stray operands were dropped, and a non-numeric id
# reached a bare int() and printed a traceback.

@pytest.mark.parametrize("args,expect", [
    (["list", "--new"], "--new"),
    (["export", "--tag", "todo"], "--tag"),
    (["list", "--dry-run"], "--dry-run"),
    (["sessions", "--artist", "X"], "--artist"),
])
def test_a_flag_from_another_subcommand_is_rejected(cfg, capsys, args, expect):
    assert _run(cfg, *args, "--json") == 1
    assert expect in _json_out(capsys)["error"]


@pytest.mark.parametrize("args", [
    ["list", "typo"], ["sessions", "extra"], ["export", "stray"],
    ["show", "1", "2"], ["untag", "1", "a", "b"],
])
def test_a_stray_operand_is_rejected(cfg, capsys, args):
    assert _run(cfg, *args, "--json") == 1
    assert "argument" in _json_out(capsys)["error"]


@pytest.mark.parametrize("args", [
    ["show", "nope"], ["rm", "nope"], ["restore", "nope"],
    ["edit", "nope", "-m", "x"], ["tag", "nope", "todo"], ["untag", "nope", "todo"],
])
def test_a_non_numeric_note_id_is_an_error_not_a_traceback(cfg, capsys, args):
    """`note show nope` raised an uncaught ValueError out of int()."""
    assert _run(cfg, *args, "--json") == 1
    assert "not a note id" in _json_out(capsys)["error"]


@pytest.mark.parametrize("args", [
    ["show"], ["edit"], ["rm"], ["restore"], ["tag", "1"], ["untag", "1"], ["import"],
])
def test_a_missing_operand_is_rejected(cfg, capsys, args):
    assert _run(cfg, *args, "--json") == 1


def test_session_only_accepts_start(cfg, capsys):
    assert _run(cfg, "session", "stop", "--json") == 1
    assert _run(cfg, "session", "start", "--json") == 0


@pytest.mark.parametrize("args", [
    ["list"], ["sessions"], ["export"], ["session", "start"],
    ["list", "--artist", "Old 97s", "--kind", "album", "--all"],
    ["export", "--root-level", "2", "--kind", "track"],
])
def test_valid_invocations_still_pass_validation(cfg, capsys, args):
    assert _run(cfg, *args, "--json") == 0


def test_mbid_picks_a_release_from_the_cli(cfg, capsys, monkeypatch):
    from spindlebot.services import library_index
    editions = [
        LibraryAlbum("Old 97's", "Fight Songs", 1999, "mb-original"),
        LibraryAlbum("Old 97's", "Fight Songs", 2019, "mb-deluxe"),
    ]
    monkeypatch.setattr(library_index, "load",
                        lambda cfg, index="auto": library_index.LibraryIndex(albums=editions))

    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-m", "x", "--json") == 1
    payload = _json_out(capsys)
    assert "--mbid" in payload["error"]
    assert any("--mbid mb-deluxe" in c for c in payload["candidates"]), \
        "the candidate list has to carry the value that resolves it"

    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "--mbid", "mb-deluxe", "-m", "x", "--json") == 0


# ── review round 5 (PR #74) ──────────────────────────────────────────────────

@pytest.mark.parametrize("args,flag", [
    (["add", "--artist", "--new", "-m", "x"], "--artist"),
    (["add", "--artist", "Old 97s", "--album", "--json"], "--album"),
    (["list", "--tag"], "--tag"),
    (["export", "--root-level"], "--root-level"),
])
def test_a_value_flag_does_not_swallow_the_next_option(cfg, capsys, args, flag):
    """`note add --artist --new -m x` took `--new` AS THE ARTIST NAME and would
    have filed a note under a subject called "--new"."""
    assert _run(cfg, *args, "--json") == 1
    error = _json_out(capsys)["error"]
    assert flag in error and "needs a value" in error


def test_a_bare_dash_is_a_legitimate_value():
    """`-F -` means read stdin; the option-looking-value check must allow it."""
    from spindlebot.cli import _note_missing_values
    assert _note_missing_values(["-F", "-"]) == []
    assert _note_missing_values(["--artist", "X"]) == []


# ── review round 5 (PR #74): user input never yields a traceback ─────────────
# Fixed as a CLASS, not as three more instances. Unvalidated input produced a
# traceback in every round of this review — `note show nope`, `note tag 999`,
# `--session nope`, `-F /nope` — because each was patched where it was found.
# `cmd_note` now converts every user-causable failure into fail(), and the
# specific checks below exist for the better message, not for the safety.

def test_a_missing_body_file_is_an_error_not_a_traceback(cfg, capsys):
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-F", "/nonexistent/body.md", "--json") == 1
    assert "file error" in _json_out(capsys)["error"]


def test_a_directory_as_the_body_file_is_an_error(cfg, capsys, tmp_path):
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-F", str(tmp_path), "--json") == 1
    assert "file error" in _json_out(capsys)["error"]


def test_a_non_utf8_body_file_is_an_error(cfg, capsys, tmp_path):
    binary = tmp_path / "body.bin"
    binary.write_bytes(b"\xff\xfe\x00\x01")
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-F", str(binary), "--json") == 1
    assert "UTF-8" in _json_out(capsys)["error"]


def test_a_readable_body_file_still_works(cfg, capsys, tmp_path):
    body = tmp_path / "body.md"
    body.write_text("from a file", encoding="utf-8")
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-F", str(body), "--json") == 0
    assert _json_out(capsys)["body"] == "from a file"


@pytest.mark.parametrize("sub", ["add", "list"])
def test_a_non_numeric_session_is_rejected(cfg, capsys, sub):
    args = [sub, "--session", "nope"]
    if sub == "add":
        args = ["add", "--artist", "Old 97s", "--album", "Fight Songs", "-m", "x",
                "--session", "nope"]
    assert _run(cfg, *args, "--json") == 1
    assert "wants a number" in _json_out(capsys)["error"]


def test_a_nonexistent_session_says_so_rather_than_leaking_a_foreign_key(cfg, capsys):
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-m", "x", "--session", "999", "--json") == 1
    error = _json_out(capsys)["error"]
    assert "no session 999" in error
    assert "FOREIGN KEY" not in error


def test_a_valid_session_still_attaches(cfg, capsys):
    _run(cfg, "session", "start", "--title", "S", "--json")
    session_id = _json_out(capsys)["id"]
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-m", "x", "--session", str(session_id), "--json") == 0
    assert _json_out(capsys)["session_id"] == session_id


def test_an_unknown_index_is_a_usage_error_even_with_new(cfg, capsys, monkeypatch):
    """`--new` skips a library that is UNAVAILABLE; it must not swallow a
    mistyped `--index`, which silently ignored the backend the user asked for."""
    from spindlebot.services import library_index
    monkeypatch.setattr(library_index, "load", lambda cfg, index="auto": (
        library_index.LibraryIndex(albums=list(LIBRARY)) if index in library_index.KNOWN_INDEXES
        else (_ for _ in ()).throw(ValueError(f"unknown library index {index!r}"))))
    assert _run(cfg, "add", "--artist", "Nobody", "--album", "Nothing",
                "--index", "typo", "--new", "-m", "x", "--json") == 1
    assert "unknown library index" in _json_out(capsys)["error"]


def test_a_missing_backend_is_still_skippable_with_new(cfg, capsys, monkeypatch):
    from spindlebot.services import library_index
    monkeypatch.setattr(library_index, "load", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("beets is not installed")))
    assert _run(cfg, "add", "--artist", "Nobody", "--album", "Nothing",
                "--new", "-m", "x", "--json") == 0


# ── review round 7 (PR #74): the error boundary covers the whole family ──────

def test_any_filesystem_error_on_a_body_path_is_handled(cfg, capsys, tmp_path):
    """Three OSError subclasses was not the family. `-F /some/file/child` raises
    ENOTDIR and escaped as a traceback."""
    regular = tmp_path / "afile"
    regular.write_text("x", encoding="utf-8")
    assert _run(cfg, "add", "--artist", "Old 97s", "--album", "Fight Songs",
                "-F", str(regular / "child"), "--json") == 1
    assert "file error" in _json_out(capsys)["error"]


def test_an_unwritable_export_path_is_handled(cfg, capsys, tmp_path):
    regular = tmp_path / "afile"
    regular.write_text("x", encoding="utf-8")
    assert _run(cfg, "export", "-o", str(regular / "child" / "out.md"), "--json") == 1


def test_an_unopenable_database_is_an_error_not_a_traceback(capsys):
    """`open_db` ran before the boundary, so an unwritable path or a failed
    migration bypassed it entirely."""
    from types import SimpleNamespace as NS
    broken = NS(core=NS(db_path=Path("/nonexistent-root/spindlebot.db")))
    assert cmd_note(broken, ["list", "--json"]) == 1
    assert "could not open" in _json_out(capsys)["error"]


def test_the_root_level_hint_never_suggests_an_invalid_level(cfg, capsys, tmp_path):
    """At the maximum root level the advice named a level `_check_root_level`
    rejects."""
    doc = tmp_path / "n.md"
    doc.write_text("#### Nobody At All\n\nbody\n", encoding="utf-8")
    assert _run(cfg, "import", str(doc), "--root-level", "4", "--json") == 1
    hint = _json_out(capsys)["hint"]
    assert "--root-level 5" not in hint
    assert hint, "still explains what went wrong"
