"""
Note repositories: subject upsert, append-only revisions, filters, tags.

The behaviours pinned here are the ones that decide whether authored text is
preserved or quietly lost — no-op edits, display fields surviving a thinner
second write, and soft deletes leaving history intact.
"""
from __future__ import annotations

import pytest

from spindlebot.core.enums import NoteStatus, NoteSubjectKind
from spindlebot.core.notes import NoteSubjectRef
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import note_repo, note_subject_repo


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


def _subject(conn, **over) -> int:
    ref = over.pop("ref", None) or NoteSubjectRef.for_album("Old 97's", "Fight Songs")
    return note_subject_repo.upsert(conn, ref, now=over.pop("now", 100)).id


def _note(conn, body="a delightful record", **over) -> int:
    subject_id = over.pop("subject_id", None) or _subject(conn)
    note, _ = note_repo.create(conn, subject_id=subject_id, body=body, now=over.pop("now", 100), **over)
    return note.id


# ── subjects ─────────────────────────────────────────────────────────────────

def test_upsert_is_get_or_create(conn):
    ref = NoteSubjectRef.for_album("Old 97's", "Fight Songs")
    first = note_subject_repo.upsert(conn, ref, now=100)
    second = note_subject_repo.upsert(conn, ref, now=200)
    assert first.id == second.id
    assert second.created_utc == 100, "created_utc is preserved across re-upsert"


def test_upsert_never_clears_a_display_field(conn):
    """A later note added with less context must not blank what an earlier one
    supplied — otherwise a subject degrades to a bare uuid over time."""
    note_subject_repo.upsert(
        conn, NoteSubjectRef.for_album("Old 97's", "Fight Songs"), now=100
    )
    thinner = NoteSubjectRef(
        kind=NoteSubjectKind.ALBUM,
        subject_key=NoteSubjectRef.for_album("Old 97's", "Fight Songs").subject_key,
        album_title="Fight Songs",
    )
    after = note_subject_repo.upsert(conn, thinner, now=200)
    assert after.artist_name == "Old 97's"


def test_upsert_fills_in_a_field_that_was_missing(conn):
    key = NoteSubjectRef.for_album("Old 97's", "Fight Songs").subject_key
    note_subject_repo.upsert(
        conn,
        NoteSubjectRef(kind=NoteSubjectKind.ALBUM, subject_key=key, album_title="Fight Songs"),
        now=100,
    )
    after = note_subject_repo.upsert(
        conn, NoteSubjectRef.for_album("Old 97's", "Fight Songs"), now=200
    )
    assert after.artist_name == "Old 97's"


def test_same_album_written_two_ways_is_one_subject(conn):
    """The whole point of the identity keys, seen from the DB side."""
    a = note_subject_repo.upsert(conn, NoteSubjectRef.for_album("Old 97's", "Fight Songs"), now=1)
    b = note_subject_repo.upsert(conn, NoteSubjectRef.for_album("old 97's", "fight songs"), now=2)
    assert a.id == b.id


def test_list_subjects_filters_by_kind(conn):
    note_subject_repo.upsert(conn, NoteSubjectRef.for_artist("Old 97's"), now=1)
    note_subject_repo.upsert(conn, NoteSubjectRef.for_album("Old 97's", "Fight Songs"), now=1)
    artists = note_subject_repo.list_all(conn, NoteSubjectKind.ARTIST)
    assert [s.kind for s in artists] == [NoteSubjectKind.ARTIST]
    assert len(note_subject_repo.list_all(conn)) == 2


# ── create + revisions ───────────────────────────────────────────────────────

def test_create_makes_a_note_and_its_first_revision(conn):
    subject_id = _subject(conn)
    note, revision = note_repo.create(
        conn, subject_id=subject_id, body="a delightful record", now=100
    )
    assert note.status is NoteStatus.ACTIVE
    assert revision.seq == 1
    assert note_repo.head(conn, note.id).body == "a delightful record"


def test_create_assigns_a_device_stable_uuid(conn):
    """The join key for a future cross-device sync, where autoincrement ids collide."""
    a, b = _note(conn), _note(conn)
    assert note_repo.get(conn, a).uuid != note_repo.get(conn, b).uuid
    assert note_repo.get_by_uuid(conn, note_repo.get(conn, a).uuid).id == a


def test_editing_appends_rather_than_overwrites(conn):
    note_id = _note(conn, "first thoughts")
    revision, changed = note_repo.append_revision(
        conn, note_id=note_id, body="second thoughts", now=200
    )
    assert changed and revision.seq == 2
    assert note_repo.head(conn, note_id).body == "second thoughts"
    assert [r.body for r in note_repo.list_revisions(conn, note_id)] == [
        "first thoughts", "second thoughts",
    ], "the earlier draft is still there"


def test_a_no_op_edit_does_not_grow_the_chain(conn):
    """Opening a note in $EDITOR and closing it unchanged must not append."""
    note_id = _note(conn, "a delightful record")
    revision, changed = note_repo.append_revision(
        conn, note_id=note_id, body="a delightful record", now=200
    )
    assert not changed and revision.seq == 1
    assert len(note_repo.list_revisions(conn, note_id)) == 1


def test_an_editor_rewriting_line_endings_is_not_an_edit(conn):
    """Canonicalization is what makes this a no-op instead of a phantom revision."""
    note_id = _note(conn, "one\n\ntwo")
    _, changed = note_repo.append_revision(
        conn, note_id=note_id, body="one\r\n\r\ntwo  \r\n", now=200
    )
    assert not changed


def test_a_no_op_edit_leaves_updated_utc_alone(conn):
    note_id = _note(conn, "same", now=100)
    note_repo.append_revision(conn, note_id=note_id, body="same", now=999)
    assert note_repo.get(conn, note_id).updated_utc == 100


def test_editing_touches_updated_utc(conn):
    note_id = _note(conn, "first", now=100)
    note_repo.append_revision(conn, note_id=note_id, body="second", now=200)
    assert note_repo.get(conn, note_id).updated_utc == 200


def test_stored_body_is_canonical_not_raw(conn):
    """The stored text and its hash are canonicalized together — storing raw
    bytes while hashing canonical ones would make the no-op check disagree with
    what a reader sees."""
    note_id = _note(conn, "  padded  \r\n\r\n")
    assert note_repo.head(conn, note_id).body == "  padded"


# ── status ───────────────────────────────────────────────────────────────────

def test_soft_delete_hides_the_note_but_keeps_its_history(conn):
    note_id = _note(conn, "first")
    note_repo.append_revision(conn, note_id=note_id, body="second", now=200)
    note_repo.set_status(conn, note_id, NoteStatus.DELETED, now=300)

    assert note_repo.list_notes(conn) == []
    assert len(note_repo.list_notes(conn, status=None)) == 1
    assert len(note_repo.list_revisions(conn, note_id)) == 2, "the writing survives"


def test_soft_delete_is_reversible(conn):
    note_id = _note(conn)
    note_repo.set_status(conn, note_id, NoteStatus.DELETED, now=200)
    note_repo.set_status(conn, note_id, NoteStatus.ACTIVE, now=300)
    assert [n.id for n in note_repo.list_notes(conn)] == [note_id]


# ── filters ──────────────────────────────────────────────────────────────────

def test_list_notes_is_newest_first(conn):
    subject_id = _subject(conn)
    old = _note(conn, "old", subject_id=subject_id, now=100)
    new = _note(conn, "new", subject_id=subject_id, now=200)
    assert [n.id for n in note_repo.list_notes(conn)] == [new, old]


def test_empty_subject_ids_means_nothing_matched_not_everything(conn):
    """A failed lookup must never widen to the whole corpus. `None` means
    "don't filter"; `[]` means "no subjects matched"."""
    _note(conn)
    assert note_repo.list_notes(conn, subject_ids=[]) == []
    assert len(note_repo.list_notes(conn, subject_ids=None)) == 1


def test_list_notes_filters_by_subject(conn):
    album = _subject(conn, ref=NoteSubjectRef.for_album("Old 97's", "Fight Songs"))
    artist = _subject(conn, ref=NoteSubjectRef.for_artist("Loreena McKennitt"))
    on_album = _note(conn, subject_id=album)
    _note(conn, subject_id=artist)
    assert [n.id for n in note_repo.list_notes(conn, subject_ids=[album])] == [on_album]


def test_list_notes_filters_by_session_and_since(conn):
    session = note_repo.create_session(conn, occurred_utc=500, now=500, title="Sunday CDs")
    subject_id = _subject(conn)
    in_session = _note(conn, subject_id=subject_id, session_id=session.id, now=500)
    _note(conn, subject_id=subject_id, now=100)
    assert [n.id for n in note_repo.list_notes(conn, session_id=session.id)] == [in_session]
    assert [n.id for n in note_repo.list_notes(conn, since_utc=400)] == [in_session]


def test_list_notes_filters_by_tag(conn):
    subject_id = _subject(conn)
    tagged = _note(conn, subject_id=subject_id)
    _note(conn, subject_id=subject_id)
    note_repo.add_tags(conn, tagged, ["todo"])
    assert [n.id for n in note_repo.list_notes(conn, tag="todo")] == [tagged]


# ── sessions ─────────────────────────────────────────────────────────────────

def test_sessions_read_backwards(conn):
    """A listening log is read most-recent-first."""
    early = note_repo.create_session(conn, occurred_utc=100, now=100)
    late = note_repo.create_session(conn, occurred_utc=900, now=900)
    assert [s.id for s in note_repo.list_sessions(conn)] == [late.id, early.id]
    assert [s.id for s in note_repo.list_sessions(conn, since_utc=500)] == [late.id]


def test_a_note_needs_no_session(conn):
    assert note_repo.get(conn, _note(conn)).session_id is None


# ── tags ─────────────────────────────────────────────────────────────────────

def test_add_tags_is_idempotent_and_drops_blanks(conn):
    note_id = _note(conn)
    note_repo.add_tags(conn, note_id, ["todo", "surprise"])
    note_repo.add_tags(conn, note_id, ["todo", "  ", ""])
    assert note_repo.list_tags(conn, note_id) == ["surprise", "todo"]


def test_tags_are_trimmed(conn):
    note_id = _note(conn)
    note_repo.add_tags(conn, note_id, ["  todo  "])
    assert note_repo.list_tags(conn, note_id) == ["todo"]


def test_remove_tag(conn):
    note_id = _note(conn)
    note_repo.add_tags(conn, note_id, ["todo", "surprise"])
    note_repo.remove_tag(conn, note_id, "todo")
    assert note_repo.list_tags(conn, note_id) == ["surprise"]


def test_all_tags_counts_only_live_notes(conn):
    subject_id = _subject(conn)
    a, b = _note(conn, subject_id=subject_id), _note(conn, subject_id=subject_id)
    note_repo.add_tags(conn, a, ["todo"])
    note_repo.add_tags(conn, b, ["todo"])
    assert note_repo.all_tags(conn) == [("todo", 2)]
    note_repo.set_status(conn, b, NoteStatus.DELETED, now=300)
    assert note_repo.all_tags(conn) == [("todo", 1)]
