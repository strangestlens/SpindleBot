"""
Row models for listening notes: field mapping and fail-loud enum validation.

The closed sets are stored as TEXT, so the only thing standing between a typo'd
row and a silently wrong note is validation on read. These tests pin that.
"""
from __future__ import annotations

import sqlite3

import pytest

from spindlebot.core.enums import NoteFormat, NoteStatus, NoteSubjectKind
from spindlebot.core.models import Note, NoteRevision, NoteSession, NoteSubject


def _row(**cols) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    names = ", ".join(f'? AS "{k}"' for k in cols)
    return conn.execute(f"SELECT {names}", tuple(cols.values())).fetchone()


def _subject_row(**over):
    return _row(**{
        "id": 1, "kind": "album", "subject_key": "key-1",
        "artist_name": "Old 97's", "album_title": "Fight Songs",
        "track_title": None, "mbid": None, "created_utc": 100, **over,
    })


def _note_row(**over):
    return _row(**{
        "id": 7, "uuid": "u-7", "subject_id": 1, "session_id": None,
        "status": "active", "created_utc": 100, "updated_utc": 200, **over,
    })


def _revision_row(**over):
    return _row(**{
        "id": 3, "note_id": 7, "seq": 1, "body": "a delightful record",
        "body_format": "markdown", "sha256": "abc", "author": None,
        "created_utc": 100, **over,
    })


def test_note_subject_from_row():
    s = NoteSubject.from_row(_subject_row())
    assert s.kind is NoteSubjectKind.ALBUM
    assert s.subject_key == "key-1"
    assert s.label == "Old 97's — Fight Songs"


def test_note_subject_label_falls_back_to_the_key():
    s = NoteSubject.from_row(_subject_row(artist_name=None, album_title=None))
    assert s.label == "key-1"


def test_note_from_row():
    n = Note.from_row(_note_row(session_id=4))
    assert n.status is NoteStatus.ACTIVE
    assert n.session_id == 4


def test_note_session_id_is_optional():
    """A note written outside a listening sitting is still a first-class note."""
    assert Note.from_row(_note_row()).session_id is None


def test_note_session_from_row():
    s = NoteSession.from_row(_row(
        id=4, uuid="u-4", occurred_utc=900, title="Sunday CDs", created_utc=901,
    ))
    assert s.title == "Sunday CDs"
    assert s.occurred_utc == 900


def test_note_revision_from_row():
    r = NoteRevision.from_row(_revision_row())
    assert r.body_format is NoteFormat.MARKDOWN
    assert r.seq == 1


@pytest.mark.parametrize("model,row,bad", [
    (NoteSubject, _subject_row, {"kind": "songwriter"}),
    (Note, _note_row, {"status": "archived"}),
    (NoteRevision, _revision_row, {"body_format": "html"}),
])
def test_unknown_enum_value_in_a_row_fails_loud(model, row, bad):
    """Never coerce, never default. An unrecognized closed-set value means the
    row was written by something that does not share this contract."""
    with pytest.raises(ValueError):
        model.from_row(row(**bad))
