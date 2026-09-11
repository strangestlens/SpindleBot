"""
The listening-note identity contract: what makes two notes land on one subject.

These keys are exact-equality identifiers with no downstream fuzzy comparison,
so every folding rule below is load-bearing. A change that makes any of these
assertions flip does not just alter a score — it detaches existing notes from
their subject, or silently merges two subjects into one.
"""
from __future__ import annotations

import pytest

from spindlebot.core.albums import album_key
from spindlebot.core.collection_match import normalize_track_title
from spindlebot.core.enums import NoteFormat, NoteStatus, NoteSubjectKind
from spindlebot.core.notes import (
    NoteSubjectRef,
    artist_key,
    body_sha256,
    canonicalize_body,
    track_key,
)

# ── enums ────────────────────────────────────────────────────────────────────

def test_note_enums_store_as_plain_text():
    assert NoteSubjectKind.ALBUM == "album"
    assert NoteStatus.DELETED == "deleted"
    assert NoteFormat.MARKDOWN == "markdown"
    assert NoteSubjectKind("track") is NoteSubjectKind.TRACK


def test_unknown_enum_value_fails_loud():
    with pytest.raises(ValueError):
        NoteSubjectKind("songwriter")


# ── artist_key ───────────────────────────────────────────────────────────────

def test_artist_key_is_deterministic():
    assert artist_key("Loreena McKennitt") == artist_key("Loreena McKennitt")


@pytest.mark.parametrize("a,b,why", [
    ("Old 97's", "Old 97s", "intra-word apostrophe: the corpus spells it both ways"),
    ("The Beatles", "Beatles", "leading article is not part of an artist's identity"),
    ("Björk", "Bjork", "Latin diacritics fold"),
    ("AFRO CELT SOUND SYSTEM", "Afro Celt Sound System", "case folds"),
    ("Belle & Sebastian", "Belle and Sebastian", "ampersand folds to 'and'"),
    ("Godspeed You! Black Emperor", "Godspeed You Black Emperor", "stray punctuation"),
])
def test_artist_key_folds_spelling_variants(a, b, why):
    assert artist_key(a) == artist_key(b), why


def test_artist_key_separates_different_artists():
    assert artist_key("Old 97's") != artist_key("Afro Celt Sound System")


def test_artist_key_prefers_musicbrainz_id_over_name():
    """The MBID wins outright: two spellings under one id are one artist."""
    mbid = "aa7a2827-f74b-473c-bd35-6f9b4c4e7f3a"
    assert artist_key("Loreena McKennitt", mbid) == artist_key("L. McKennitt", mbid)
    assert artist_key("x", mbid) == artist_key("x", mbid.upper())


def test_artist_key_tolerates_empty_name():
    """A subject with no name is degenerate, not an exception."""
    assert artist_key(None) == artist_key("")


# ── track_key ────────────────────────────────────────────────────────────────

def test_track_key_is_scoped_to_its_album():
    """The same song title on two albums is two subjects, by design."""
    a = album_key("Old 97's", "Fight Songs")
    b = album_key("Old 97's", "Hitchhike to Rhome")
    assert track_key(a, "Murder") != track_key(b, "Murder")


def test_track_key_ignores_position():
    """Position is not an argument at all.

    `runner._fix_multidisc()` exists because MusicBrainz reports disctotal=2 for
    single-disc albums and numbering is patched AFTER import. A position-keyed
    note would detach from its track during that ordinary repair.
    """
    a = album_key("Loreena McKennitt", "An Ancient Muse")
    assert track_key(a, "Kecharitomene") == track_key(a, "Kecharitomene")


def test_track_key_folds_typing_variants():
    a = album_key("Old 97's", "Fight Songs")
    assert track_key(a, "Don't Cry") == track_key(a, "Dont Cry")
    assert track_key(a, "MURDER") == track_key(a, "murder")


def test_track_key_keeps_edition_markers_distinct():
    """Two real tracks on one album, not one track named two ways.

    `normalize_title` strips edition markers so a single RELEASE can be matched
    across catalogues. At track level that folding would merge two notes onto
    one subject, so `normalize_track_title` deliberately does not do it.
    """
    a = album_key("Massive Attack", "Mezzanine")
    assert track_key(a, "Angel") != track_key(a, "Angel (2018 Remix)")


def test_normalize_track_title_preserves_non_latin_scripts():
    """Mirrors the collection-matcher rule: an ASCII-only fold empties a
    katakana title, which throws away the only thing it can match on."""
    assert normalize_track_title("ハイパースペース") == "ハイパースペース"


# ── body canonicalization ────────────────────────────────────────────────────

def test_canonicalize_body_normalizes_line_endings_and_trailing_space():
    assert canonicalize_body("a\r\nb  \n") == "a\nb"


def test_canonicalize_body_preserves_interior_blank_lines():
    """Blank lines are the author's paragraph breaks, not noise."""
    assert canonicalize_body("one\n\ntwo") == "one\n\ntwo"


def test_canonicalize_body_strips_surrounding_blank_lines():
    assert canonicalize_body("\n\nbody\n\n") == "body"


def test_body_sha256_makes_a_no_op_edit_detectable():
    """Opening a note in $EDITOR and closing it unchanged must not append a
    revision, even if the editor rewrote line endings or added a newline."""
    original = "Surprise track: What We Talk About.\n\nTango through line."
    round_tripped = original.replace("\n", "\r\n") + "\r\n"
    assert body_sha256(original) == body_sha256(round_tripped)


def test_body_sha256_detects_a_real_edit():
    assert body_sha256("a delightful record") != body_sha256("a delightful record!")


# ── NoteSubjectRef ───────────────────────────────────────────────────────────

def test_album_subject_key_is_the_library_album_key():
    """Not a parallel key: note_subject.subject_key for an album IS
    album.album_key, which is UNIQUE, so note -> library album is a plain join."""
    ref = NoteSubjectRef.for_album("Old 97's", "Fight Songs")
    assert ref.subject_key == album_key("Old 97's", "Fight Songs")
    assert ref.kind is NoteSubjectKind.ALBUM


def test_track_subject_ref_nests_under_its_album_key():
    ref = NoteSubjectRef.for_track("Old 97's", "Fight Songs", "Murder")
    assert ref.subject_key == track_key(album_key("Old 97's", "Fight Songs"), "Murder")
    assert ref.kind is NoteSubjectKind.TRACK


def test_artist_subject_ref_carries_its_mbid():
    ref = NoteSubjectRef.for_artist("Loreena McKennitt", "abc-123")
    assert ref.kind is NoteSubjectKind.ARTIST
    assert ref.mbid == "abc-123"
    assert ref.subject_key == artist_key("Loreena McKennitt", "abc-123")


def test_subject_ref_label_is_self_describing_without_the_library():
    """A note about an album that was never ripped still has to render as
    something a human recognizes — the display fields are a snapshot, not a
    lookup."""
    assert NoteSubjectRef.for_artist("Old 97's").label == "Old 97's"
    assert (
        NoteSubjectRef.for_album("Old 97's", "Fight Songs").label
        == "Old 97's — Fight Songs"
    )
    assert (
        NoteSubjectRef.for_track("Old 97's", "Fight Songs", "Murder").label
        == "Old 97's — Fight Songs — Murder"
    )


def test_subject_ref_label_falls_back_to_the_key():
    ref = NoteSubjectRef(kind=NoteSubjectKind.ARTIST, subject_key="uuid-here")
    assert ref.label == "uuid-here"
