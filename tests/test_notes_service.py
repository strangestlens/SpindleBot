"""
The notes service: what the CLI and any future UI actually call.

The filter semantics are the substance here. "Show me my notes on Old 97's"
must return the album and track notes too — a filter that answered with only
artist-KIND notes would hide most of what was written and look like data loss.
"""
from __future__ import annotations

import pytest

from spindlebot.core.enums import NoteStatus, NoteSubjectKind
from spindlebot.core.notes import NoteSubjectRef
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import note_subject_repo
from spindlebot.services import notes as svc

ARTIST = NoteSubjectRef.for_artist("Old 97's")
ALBUM = NoteSubjectRef.for_album("Old 97's", "Fight Songs")
TRACK = NoteSubjectRef.for_track("Old 97's", "Fight Songs", "Murder")
OTHER = NoteSubjectRef.for_album("Loreena McKennitt", "An Ancient Muse")


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


# ── writing ──────────────────────────────────────────────────────────────────

def test_add_note_returns_an_assembled_view(conn):
    view = svc.add_note(conn, subject=ALBUM, body="a delightful record", now=100)
    assert view.body == "a delightful record"
    assert view.revision == 1
    assert view.label == "Old 97's — Fight Songs"
    assert view.subject.kind is NoteSubjectKind.ALBUM


def test_a_second_note_joins_the_existing_subject(conn):
    a = svc.add_note(conn, subject=ALBUM, body="one", now=100)
    b = svc.add_note(conn, subject=ALBUM, body="two", now=200)
    assert a.subject.id == b.subject.id
    assert a.id != b.id


def test_add_note_with_tags(conn):
    view = svc.add_note(conn, subject=ALBUM, body="b", now=100, tags=["todo", "surprise"])
    assert view.tags == ("surprise", "todo")


def test_edit_appends_and_reports_the_change(conn):
    note = svc.add_note(conn, subject=ALBUM, body="first", now=100)
    view, changed = svc.edit_note(conn, note_id=note.id, body="second", now=200)
    assert changed and view.body == "second" and view.revision == 2
    assert [r.body for r in svc.history(conn, note.id)] == ["first", "second"]


def test_an_unchanged_edit_writes_nothing(conn):
    note = svc.add_note(conn, subject=ALBUM, body="same", now=100)
    view, changed = svc.edit_note(conn, note_id=note.id, body="same", now=200)
    assert not changed and view.revision == 1


def test_editing_a_missing_note_is_a_lookup_error(conn):
    with pytest.raises(LookupError):
        svc.edit_note(conn, note_id=999, body="x", now=100)


# ── retiring ─────────────────────────────────────────────────────────────────

def test_delete_is_soft_and_reversible(conn):
    note = svc.add_note(conn, subject=ALBUM, body="body", now=100)
    svc.edit_note(conn, note_id=note.id, body="edited", now=150)

    svc.delete_note(conn, note.id, now=200)
    assert svc.list_notes(conn) == []
    assert len(svc.list_notes(conn, include_deleted=True)) == 1
    assert len(svc.history(conn, note.id)) == 2, "the writing survives a delete"

    svc.restore_note(conn, note.id, now=300)
    assert [v.id for v in svc.list_notes(conn)] == [note.id]
    assert svc.get_note(conn, note.id).note.status is NoteStatus.ACTIVE


# ── filters ──────────────────────────────────────────────────────────────────

def _populate(conn):
    return {
        "artist": svc.add_note(conn, subject=ARTIST, body="artist note", now=100).id,
        "album": svc.add_note(conn, subject=ALBUM, body="album note", now=200).id,
        "track": svc.add_note(conn, subject=TRACK, body="track note", now=300).id,
        "other": svc.add_note(conn, subject=OTHER, body="other note", now=400).id,
    }


def test_filtering_by_artist_reaches_its_albums_and_tracks(conn):
    """The question is "what have I said about this band", not "which notes are
    filed at artist level"."""
    ids = _populate(conn)
    got = {v.id for v in svc.list_notes(conn, artist="Old 97's")}
    assert got == {ids["artist"], ids["album"], ids["track"]}
    assert ids["other"] not in got


def test_filtering_by_artist_folds_spelling(conn):
    ids = _populate(conn)
    assert {v.id for v in svc.list_notes(conn, artist="old 97s")} == {
        ids["artist"], ids["album"], ids["track"]
    }


def test_filtering_by_album_reaches_its_tracks(conn):
    ids = _populate(conn)
    got = {v.id for v in svc.list_notes(conn, album="Fight Songs")}
    assert got == {ids["album"], ids["track"]}


def test_filtering_by_album_folds_case_and_spacing(conn):
    ids = _populate(conn)
    assert {v.id for v in svc.list_notes(conn, album="fight  songs")} == {
        ids["album"], ids["track"]
    }


def test_kind_narrows_to_one_level(conn):
    ids = _populate(conn)
    got = svc.list_notes(conn, artist="Old 97's", kind=NoteSubjectKind.ALBUM)
    assert [v.id for v in got] == [ids["album"]]


def test_an_unknown_artist_filter_returns_nothing_not_everything(conn):
    """The empty-subject-ids trap, from the service side: a failed lookup must
    never widen to the whole corpus."""
    _populate(conn)
    assert svc.list_notes(conn, artist="Nobody At All") == []


def test_notes_are_newest_first(conn):
    ids = _populate(conn)
    assert [v.id for v in svc.list_notes(conn, artist="Old 97's")] == [
        ids["track"], ids["album"], ids["artist"]
    ]


def test_filtering_by_tag(conn):
    ids = _populate(conn)
    svc.tag_note(conn, ids["album"], ["todo"])
    assert [v.id for v in svc.list_notes(conn, tag="todo")] == [ids["album"]]


def test_untagging(conn):
    ids = _populate(conn)
    svc.tag_note(conn, ids["album"], ["todo"])
    view = svc.untag_note(conn, ids["album"], "todo")
    assert view.tags == ()
    assert svc.list_notes(conn, tag="todo") == []


def test_filtering_by_since(conn):
    ids = _populate(conn)
    assert {v.id for v in svc.list_notes(conn, since_utc=250)} == {
        ids["track"], ids["other"]
    }


# ── sessions ─────────────────────────────────────────────────────────────────

def test_notes_group_into_a_listening_sitting(conn):
    """One evening, several subjects, one event — the shape the corpus is
    already written in."""
    session = svc.start_session(conn, title="Sunday CDs", occurred_utc=500, now=500)
    svc.add_note(conn, subject=ALBUM, body="album", now=500, session_id=session.id)
    svc.add_note(conn, subject=TRACK, body="track", now=501, session_id=session.id)
    svc.add_note(conn, subject=OTHER, body="unrelated", now=900)

    assert len(svc.list_notes(conn, session_id=session.id)) == 2
    assert [(s.session.title, s.note_count) for s in svc.list_sessions(conn)] == [
        ("Sunday CDs", 2)
    ]


def test_a_session_defaults_to_now(conn):
    session = svc.start_session(conn, title="ad hoc", now=777)
    assert session.occurred_utc == 777


def test_sessions_read_backwards(conn):
    early = svc.start_session(conn, title="early", occurred_utc=100, now=100)
    late = svc.start_session(conn, title="late", occurred_utc=900, now=900)
    assert [s.session.id for s in svc.list_sessions(conn)] == [late.id, early.id]
    assert [s.session.id for s in svc.list_sessions(conn, since_utc=500)] == [late.id]


def test_a_note_carries_its_session_in_the_view(conn):
    session = svc.start_session(conn, title="Sunday CDs", occurred_utc=500, now=500)
    view = svc.add_note(conn, subject=ALBUM, body="b", now=500, session_id=session.id)
    assert view.session.title == "Sunday CDs"
    assert svc.add_note(conn, subject=OTHER, body="b", now=600).session is None


# ── review round 3 (PR #74) ──────────────────────────────────────────────────

def test_a_note_written_before_the_record_was_owned_stays_with_the_work(conn):
    """`album_key` prefers a MusicBrainz id, so a record's key CHANGES the day
    it is ripped. Without adoption, the wishlist note ("look for this") is
    orphaned on a name-derived subject the moment the real one resolves — it
    still exists, but stops appearing under the album it is about. The real
    corpus contains exactly such a note."""
    wishlist = NoteSubjectRef.for_album("Loreena McKennitt", "Morada Del Corazon")
    before = svc.add_note(conn, subject=wishlist, body="Look for this record.", now=100)

    owned = NoteSubjectRef.for_album("Loreena McKennitt", "Morada Del Corazon", "mb-morada")
    after = svc.add_note(conn, subject=owned, body="Finally heard it.", now=200)

    assert before.subject.id == after.subject.id, "one subject, not a fork"
    assert len(svc.list_notes(conn, album="Morada Del Corazon")) == 2
    assert svc.get_note(conn, before.id).subject.subject_key == owned.subject_key


def test_adoption_does_not_fire_when_the_mbid_subject_already_exists(conn):
    """Two subjects genuinely exist; re-keying would collide on
    UNIQUE(kind, subject_key)."""
    owned = NoteSubjectRef.for_album("Loreena McKennitt", "An Ancient Muse", "mb-muse")
    named = NoteSubjectRef.for_album("Loreena McKennitt", "An Ancient Muse")
    a = svc.add_note(conn, subject=owned, body="owned", now=100)
    b = svc.add_note(conn, subject=named, body="named", now=200)
    c = svc.add_note(conn, subject=owned, body="owned again", now=300)
    assert a.subject.id == c.subject.id
    assert b.subject.id != a.subject.id


def test_adoption_never_runs_backwards(conn):
    """A name-keyed ref cannot guess an MBID it has never seen, so it must not
    hijack an existing MBID-backed subject."""
    owned = NoteSubjectRef.for_album("Old 97's", "Fight Songs", "mb-fight")
    named = NoteSubjectRef.for_album("Old 97's", "Fight Songs")
    a = svc.add_note(conn, subject=owned, body="owned", now=100)
    b = svc.add_note(conn, subject=named, body="named", now=200)
    assert a.subject.id != b.subject.id


def test_tagging_a_missing_note_raises_lookup_error_not_an_integrity_error(conn):
    """`note_tag.note_id` has a foreign key, so inserting first raised
    sqlite3.IntegrityError — which the CLI does not catch, so `note tag 999
    todo` printed a traceback instead of an error."""
    with pytest.raises(LookupError):
        svc.tag_note(conn, 999, ["todo"])
    with pytest.raises(LookupError):
        svc.untag_note(conn, 999, "todo")


# ── review round 6 (PR #74): adoption must survive spelling drift ────────────

def test_adoption_survives_a_spelling_difference(conn):
    """A wishlist note typed "Old 97s" while the library was empty, then the
    album is ripped and the library spells it "Old 97's" with an MBID.

    `alt_keys` recomputes through `album_key`, which only lowercases and strips —
    so the two keys differed and the note forked. The FIFTH instance of this
    normalization trap on this branch, inside the code written to prevent a fork.
    Matching is on canonicalized display fields now.
    """
    typed = NoteSubjectRef.for_album("Old 97s", "Fight Songs")
    before = svc.add_note(conn, subject=typed, body="Look for this.", now=100)

    owned = NoteSubjectRef.for_album("Old 97's", "Fight Songs", "mb-fight")
    after = svc.add_note(conn, subject=owned, body="Heard it.", now=200)

    assert before.subject.id == after.subject.id
    assert len(svc.list_notes(conn, album="Fight Songs")) == 2


def test_adoption_never_merges_two_identified_releases(conn):
    """Only an UNIDENTIFIED subject is adoptable, and only by one with an mbid —
    otherwise an original and a reissue would collapse into each other."""
    original = svc.add_note(conn, subject=NoteSubjectRef.for_album(
        "Old 97's", "Fight Songs", "mb-original"), body="the original", now=100)
    reissue = svc.add_note(conn, subject=NoteSubjectRef.for_album(
        "Old 97's", "Fight Songs", "mb-deluxe"), body="the reissue", now=200)
    assert original.subject.id != reissue.subject.id


def test_a_nameless_subject_is_not_adopted_by_an_unrelated_one(conn):
    """Canonicalized display matching must not collapse different albums."""
    a = svc.add_note(conn, subject=NoteSubjectRef.for_album(
        "Old 97's", "Fight Songs"), body="one", now=100)
    b = svc.add_note(conn, subject=NoteSubjectRef.for_album(
        "Old 97's", "Too Far to Care", "mb-tftc"), body="two", now=200)
    assert a.subject.id != b.subject.id


def test_adoption_is_idempotent(conn):
    owned = NoteSubjectRef.for_album("Old 97's", "Fight Songs", "mb-fight")
    svc.add_note(conn, subject=NoteSubjectRef.for_album("Old 97s", "Fight Songs"),
                 body="wishlist", now=100)
    first = svc.adopt_subject(conn, owned, now=200)
    second = svc.adopt_subject(conn, owned, now=300)
    assert first.id == second.id


def test_a_punctuation_only_name_is_a_real_filter_not_an_absent_one(conn):
    """`--album '!!!'` normalizes to an empty key. Testing truthiness skipped
    the predicate and returned every unrelated note — and such a name is
    creatable with --new, so it has to work."""
    svc.add_note(conn, subject=ALBUM, body="unrelated", now=100)
    assert svc.list_notes(conn, album="!!!") == []

    odd = NoteSubjectRef.for_album("Old 97's", "!!!")
    wanted = svc.add_note(conn, subject=odd, body="the odd one", now=200)
    assert [v.id for v in svc.list_notes(conn, album="!!!")] == [wanted.id]


def test_an_album_only_wishlist_is_adopted_when_the_artist_arrives(conn):
    """`note add --album X --new` records artist_name=None. Requiring
    artist_key(None) to equal the real artist key could never match, so the note
    was stranded the moment the record resolved — and unlike a spelling drift,
    `note list --artist` could not find it either."""
    before = svc.add_note(conn, subject=NoteSubjectRef.for_album(
        None, "Morada Del Corazon"), body="Look for this.", now=100)
    after = svc.add_note(conn, subject=NoteSubjectRef.for_album(
        "Loreena McKennitt", "Morada Del Corazon", "mb-morada"), body="Heard it.", now=200)

    assert before.subject.id == after.subject.id
    assert len(svc.list_notes(conn, artist="Loreena McKennitt")) == 2


def test_adoption_refuses_when_two_unidentified_subjects_could_match(conn):
    """A blank field means "not yet known", which can describe several works —
    so the fallback only adopts when exactly one candidate matches. An EXACT
    name-key hit still wins, being the stronger signal."""
    svc.add_note(conn, subject=NoteSubjectRef.for_album(None, "Fight Songs"),
                 body="no artist", now=100)
    svc.add_note(conn, subject=NoteSubjectRef.for_album("Old 97s", "Fight Songs"),
                 body="other spelling", now=200)
    incoming = NoteSubjectRef.for_album("Old 97's", "Fight Songs", "mb-fight")
    assert svc.find_adoptable_subject(conn, incoming) is None


def test_a_subject_with_every_field_blank_adopts_nothing(conn):
    """Otherwise it would match every work there is."""
    blank = NoteSubjectRef(kind=NoteSubjectKind.ALBUM, subject_key="blank-key")
    note_subject_repo.upsert(conn, blank, 100)
    incoming = NoteSubjectRef.for_album("Old 97's", "Fight Songs", "mb-fight")
    assert svc.find_adoptable_subject(conn, incoming) is None
