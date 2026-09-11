"""
Markdown <-> notes. The contract is `parse(render(x)) == x`, because that round
trip is what makes `note export` a real escape hatch rather than a lossy
pretty-printer — notes are the only un-regenerable data in this system.

The failure mode these tests exist to prevent is silent: prose attributed to the
wrong subject, or dropped entirely, with no error anywhere.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from spindlebot.core.enums import NoteSubjectKind
from spindlebot.core.note_markdown import ParsedNote, parse, render

FIXTURE = Path(__file__).parent / "fixtures" / "listening_notes_sample.md"


def _note(kind, body, **over) -> ParsedNote:
    return ParsedNote(kind=kind, body=body, **over)


# ── parse: levels ────────────────────────────────────────────────────────────

def test_heading_levels_map_to_subject_levels():
    doc = parse("# A\n\nartist note\n\n## B\n\nalbum note\n\n### C\n\ntrack note\n")
    assert [(n.kind, n.label) for n in doc.notes] == [
        (NoteSubjectKind.ARTIST, "A"),
        (NoteSubjectKind.ALBUM, "A — B"),
        (NoteSubjectKind.TRACK, "A — B — C"),
    ]


def test_root_level_shifts_the_window():
    """The real corpus opens with a `# CDs` grouping heading that names no
    artist; root_level=2 reads `##` as the artist level instead."""
    doc = parse("# CDs\n\n## A\n\n### B\n\nbody\n", root_level=2)
    assert [n.label for n in doc.notes] == ["A — B"]
    assert [(s.text, s.reason) for s in doc.skipped] == [
        ("CDs", "above the subject levels")
    ]


def test_a_grouping_heading_ends_the_previous_subject():
    """`# Vinyl` after a CDs section must not leave the next album attached to
    the previous artist."""
    doc = parse("## A\n\n### B\n\nbody\n\n# Vinyl\n\n### C\n\nother\n", root_level=2)
    labels = [n.label for n in doc.notes]
    assert labels == ["A — B", "C"], "C is not under artist A"


def test_a_heading_with_no_prose_produces_no_note():
    """Structure, not content: `# Artist` above an album is not an empty note."""
    doc = parse("# A\n\n## B\n\nonly the album has prose\n")
    assert [n.kind for n in doc.notes] == [NoteSubjectKind.ALBUM]


def test_an_album_heading_with_no_artist_is_still_a_note():
    """The parser records what is there; finding the artist is resolution's job."""
    doc = parse("## B\n\nbody\n")
    assert doc.notes[0].artist is None
    assert doc.notes[0].kind is NoteSubjectKind.ALBUM


# ── parse: body handling ─────────────────────────────────────────────────────

def test_all_prose_under_a_heading_is_one_note():
    """Splitting on blank lines would guess where a thought ends, and a wrong
    guess shreds one paragraph into fragments."""
    doc = parse("# A\n\nfirst paragraph\n\nsecond paragraph\n")
    assert len(doc.notes) == 1
    assert doc.notes[0].body == "first paragraph\n\nsecond paragraph"


def test_two_headings_for_one_subject_are_two_notes():
    doc = parse("# A\n\none\n\n# A\n\ntwo\n")
    assert [n.body for n in doc.notes] == ["one", "two"]


def test_prose_before_any_heading_is_dropped_not_misattributed():
    """Better to lose an untitled preamble than to file it under whatever
    subject happens to come next."""
    doc = parse("loose preamble\n\n# A\n\nbody\n")
    assert [n.body for n in doc.notes] == ["body"]


def test_a_heading_deeper_than_track_stays_in_the_body():
    """Not a subject, but the author wrote it — it must not vanish."""
    doc = parse("# A\n\n## B\n\n### C\n\nbody\n\n#### Deeper\n\nmore\n")
    assert len(doc.notes) == 1
    assert "#### Deeper" in doc.notes[0].body and "more" in doc.notes[0].body
    assert doc.skipped[0].reason == "deeper than track level"


def test_bodies_are_canonicalized():
    doc = parse("# A\n\nbody  \r\n\r\n")
    assert doc.notes[0].body == "body"


def test_line_numbers_are_recorded_for_the_dry_run_table():
    doc = parse("# A\n\nbody\n\n## B\n\nmore\n")
    assert [n.line_no for n in doc.notes] == [1, 5]


def test_line_numbers_do_not_affect_equality():
    """The round-trip contract is about content; a rendered document has no
    line numbers to reproduce."""
    assert _note(NoteSubjectKind.ARTIST, "b", artist="A", line_no=1) == _note(
        NoteSubjectKind.ARTIST, "b", artist="A", line_no=99
    )


# ── render ───────────────────────────────────────────────────────────────────

def test_render_does_not_repeat_an_unchanged_parent_heading():
    notes = [
        _note(NoteSubjectKind.ALBUM, "one", artist="A", album="B"),
        _note(NoteSubjectKind.ALBUM, "two", artist="A", album="C"),
    ]
    assert render(notes).count("# A") == 1


def test_render_repeats_the_deepest_heading_for_a_repeated_subject():
    """Otherwise two notes merge into one body on the way back in."""
    notes = [
        _note(NoteSubjectKind.ALBUM, "one", artist="A", album="B"),
        _note(NoteSubjectKind.ALBUM, "two", artist="A", album="B"),
    ]
    assert parse(render(notes)).notes == tuple(notes)


def test_render_pops_back_out_for_an_artist_note_after_an_album_note():
    """The case a naive "emit levels that changed" loop emits nothing for: the
    artist is unchanged, but we are currently inside its album."""
    notes = [
        _note(NoteSubjectKind.ALBUM, "album body", artist="A", album="B"),
        _note(NoteSubjectKind.ARTIST, "artist body", artist="A"),
    ]
    assert parse(render(notes)).notes == tuple(notes)


def test_render_of_nothing_is_empty():
    assert render([]) == ""


# ── round trip ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("notes", [
    pytest.param([_note(NoteSubjectKind.ARTIST, "body", artist="A")], id="artist-only"),
    pytest.param([_note(NoteSubjectKind.TRACK, "b", artist="A", album="B", track="C")],
                 id="track"),
    pytest.param([
        _note(NoteSubjectKind.ARTIST, "one", artist="A"),
        _note(NoteSubjectKind.ALBUM, "two", artist="A", album="B"),
        _note(NoteSubjectKind.TRACK, "three", artist="A", album="B", track="C"),
        _note(NoteSubjectKind.TRACK, "four", artist="A", album="B", track="D"),
        _note(NoteSubjectKind.ALBUM, "five", artist="A", album="E"),
        _note(NoteSubjectKind.ARTIST, "six", artist="F"),
    ], id="full-descent-and-pops"),
    pytest.param([_note(NoteSubjectKind.ALBUM, "one\n\ntwo\n\nthree", artist="A", album="B")],
                 id="multi-paragraph"),
    pytest.param([_note(NoteSubjectKind.ALBUM, "body", artist="Old 97's", album="Fight Songs")],
                 id="punctuation-in-heading"),
    pytest.param([_note(NoteSubjectKind.ALBUM, "ハイパースペース", artist="ベック", album="X")],
                 id="non-latin"),
])
def test_round_trip(notes):
    assert parse(render(notes)).notes == tuple(notes)


def test_round_trip_at_a_shifted_root_level():
    notes = [_note(NoteSubjectKind.TRACK, "b", artist="A", album="B", track="C")]
    assert parse(render(notes, root_level=2), root_level=2).notes == tuple(notes)


def test_a_body_line_that_looks_like_a_heading_survives_the_round_trip():
    """Without escaping, `# Not a heading` inside a note would be silently
    promoted to a subject on re-import, shredding the note it came from."""
    notes = [_note(NoteSubjectKind.ALBUM, "intro\n\n# Not a heading\n\nrest",
                   artist="A", album="B")]
    assert parse(render(notes)).notes == tuple(notes)


def test_escaping_is_reversible_for_a_literal_backslash_heading():
    notes = [_note(NoteSubjectKind.ALBUM, "\\# already escaped", artist="A", album="B")]
    assert parse(render(notes)).notes == tuple(notes)


# ── the real corpus ──────────────────────────────────────────────────────────

def test_the_sample_corpus_parses_into_the_expected_subjects():
    """Shaped from the notes this feature exists to hold."""
    doc = parse(FIXTURE.read_text(encoding="utf-8"), root_level=2)
    assert [(n.kind, n.label) for n in doc.notes] == [
        (NoteSubjectKind.ALBUM, "Old 97s — Fight Songs"),
        (NoteSubjectKind.TRACK, "Old 97s — Fight Songs — What We Talk About"),
        (NoteSubjectKind.TRACK, "Old 97s — Fight Songs — Crash on the Barrelhead"),
        (NoteSubjectKind.ALBUM, "Afro Celt Sound System — Volume 1 Sound Magic"),
        (NoteSubjectKind.ARTIST, "Loreena McKennitt"),
        (NoteSubjectKind.ALBUM, "Loreena McKennitt — An Ancient Muse"),
        (NoteSubjectKind.TRACK, "Loreena McKennitt — An Ancient Muse — Kecharitomene"),
    ]
    assert [s.text for s in doc.skipped] == ["CDs"]


def test_the_sample_corpus_round_trips():
    doc = parse(FIXTURE.read_text(encoding="utf-8"), root_level=2)
    assert parse(render(doc.notes, root_level=2), root_level=2).notes == doc.notes
