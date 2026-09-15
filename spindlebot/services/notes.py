"""Listening notes: create, edit, read, retire. Orchestration over repos + core.

No print, no Flask, no argv — the CLI is one client of this and the
collection-browser will be another, the same arrangement `collection_ignore`
already uses.

The caller owns the transaction and commits, like every other service here.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import time

from spindlebot.core.enums import NoteStatus, NoteSubjectKind
from spindlebot.core.models import Note, NoteRevision, NoteSession, NoteSubject
from spindlebot.core.notes import NoteSubjectRef, artist_key, text_key
from spindlebot.db.repositories import note_repo, note_subject_repo


def _now() -> int:
    return int(time())


@dataclass(frozen=True)
class NoteView:
    """A note as anything outside this module wants it: the row, its subject,
    its current body, and the tags — assembled once so no caller has to know
    that the body lives in a separate table."""
    note: Note
    subject: NoteSubject
    body: str
    revision: int
    tags: tuple[str, ...] = ()
    session: NoteSession | None = None

    @property
    def id(self) -> int:
        return self.note.id

    @property
    def label(self) -> str:
        return self.subject.label


def _subject_row(conn, subject: NoteSubjectRef, now: int) -> NoteSubject:
    """Get-or-create the subject, ADOPTING one the same work was filed under
    before its identity sharpened.

    `album_key` prefers a MusicBrainz id, so a record's key changes the day it is
    ripped. Without this, a note written while the record was still a wishlist
    entry would be orphaned on a name-derived subject the moment the real one
    resolves — the note would still exist but would stop showing up under the
    album it is about.
    """
    if note_subject_repo.get(conn, subject.kind, subject.subject_key) is None:
        for previous_key in subject.alt_keys:
            legacy = note_subject_repo.get(conn, subject.kind, previous_key)
            if legacy is not None:
                note_subject_repo.rekey(conn, legacy.id, subject.subject_key)
                break
    return note_subject_repo.upsert(conn, subject, now)


def _view(conn, note: Note) -> NoteView:
    head = note_repo.head(conn, note.id)
    subject = note_subject_repo.get_by_id(conn, note.subject_id)
    assert head is not None and subject is not None
    return NoteView(
        note=note,
        subject=subject,
        body=head.body,
        revision=head.seq,
        tags=tuple(note_repo.list_tags(conn, note.id)),
        session=(
            note_repo.get_session(conn, note.session_id)
            if note.session_id is not None else None
        ),
    )


# ── writing ──────────────────────────────────────────────────────────────────

def add_note(
    conn,
    *,
    subject: NoteSubjectRef,
    body: str,
    now: int | None = None,
    session_id: int | None = None,
    tags: list[str] | None = None,
    author: str | None = None,
) -> NoteView:
    """Write a new note. The subject is get-or-created, so a second note about
    an album joins the first rather than forking a parallel subject."""
    now = now if now is not None else _now()
    subject_row = _subject_row(conn, subject, now)
    note, _ = note_repo.create(
        conn, subject_id=subject_row.id, body=body, now=now,
        session_id=session_id, author=author,
    )
    if tags:
        note_repo.add_tags(conn, note.id, tags)
    return _view(conn, note)


def edit_note(
    conn, *, note_id: int, body: str, now: int | None = None, author: str | None = None
) -> tuple[NoteView, bool]:
    """Append a revision. Returns (view, changed); an unchanged body writes nothing."""
    now = now if now is not None else _now()
    _require(conn, note_id)
    _, changed = note_repo.append_revision(
        conn, note_id=note_id, body=body, now=now, author=author
    )
    return _view(conn, note_repo.get(conn, note_id)), changed


def delete_note(conn, note_id: int, now: int | None = None) -> NoteView:
    """Retire a note. SOFT: the revisions stay, so this is undoable."""
    return _set_status(conn, note_id, NoteStatus.DELETED, now)


def restore_note(conn, note_id: int, now: int | None = None) -> NoteView:
    return _set_status(conn, note_id, NoteStatus.ACTIVE, now)


def _set_status(conn, note_id: int, status: NoteStatus, now: int | None) -> NoteView:
    _require(conn, note_id)
    note_repo.set_status(conn, note_id, status, now if now is not None else _now())
    return _view(conn, note_repo.get(conn, note_id))


def tag_note(conn, note_id: int, tags: list[str]) -> NoteView:
    """Attach tags. Checks the note exists FIRST.

    `note_tag.note_id` has a foreign key, so inserting against a missing note
    raised sqlite3.IntegrityError — which the CLI does not catch, so
    `note tag 999 todo` printed a traceback instead of an error.
    """
    note = _require(conn, note_id)
    note_repo.add_tags(conn, note_id, tags)
    return _view(conn, note)


def untag_note(conn, note_id: int, tag: str) -> NoteView:
    note = _require(conn, note_id)
    note_repo.remove_tag(conn, note_id, tag)
    return _view(conn, note)


def _require(conn, note_id: int) -> Note:
    note = note_repo.get(conn, note_id)
    if note is None:
        raise LookupError(f"no note {note_id}")
    return note


# ── reading ──────────────────────────────────────────────────────────────────

def get_note(conn, note_id: int) -> NoteView | None:
    note = note_repo.get(conn, note_id)
    return _view(conn, note) if note else None


def history(conn, note_id: int) -> list[NoteRevision]:
    """Every draft, oldest first. Nothing is ever overwritten, so this is the
    whole of what was written."""
    return note_repo.list_revisions(conn, note_id)


def subject_ids_for(
    conn,
    *,
    artist: str | None = None,
    album: str | None = None,
    track: str | None = None,
) -> list[int]:
    """Subjects a filter reaches — INCLUDING everything beneath them.

    `--artist "Old 97's"` means every note about that band: the artist-level
    notes, its albums, and its tracks. Filtering to artist-KIND subjects only
    would answer a question nobody asks and hide most of what was written.

    Matching goes through the identity keys (`artist_key` / `text_key`), not raw
    string equality, so a filter can never disagree with the key it filters on —
    the same reason resolution groups by `artist_key`.
    """
    want_artist = artist_key(artist) if artist else None
    want_album = text_key(album) if album else None
    want_track = text_key(track) if track else None

    out = []
    for subject in note_subject_repo.list_all(conn):
        if want_artist and (
            not subject.artist_name or artist_key(subject.artist_name) != want_artist
        ):
            continue
        if want_album and (
            not subject.album_title or text_key(subject.album_title) != want_album
        ):
            continue
        if want_track and (
            not subject.track_title or text_key(subject.track_title) != want_track
        ):
            continue
        out.append(subject.id)
    return out


def list_notes(
    conn,
    *,
    artist: str | None = None,
    album: str | None = None,
    track: str | None = None,
    kind: NoteSubjectKind | None = None,
    tag: str | None = None,
    session_id: int | None = None,
    since_utc: int | None = None,
    include_deleted: bool = False,
) -> list[NoteView]:
    """Notes matching every filter given, newest first."""
    subject_ids = None
    if artist or album or track:
        subject_ids = subject_ids_for(conn, artist=artist, album=album, track=track)

    notes = note_repo.list_notes(
        conn,
        subject_ids=subject_ids,
        session_id=session_id,
        tag=tag,
        since_utc=since_utc,
        status=None if include_deleted else NoteStatus.ACTIVE,
    )
    views = [_view(conn, n) for n in notes]
    if kind is not None:
        views = [v for v in views if v.subject.kind is kind]
    return views


# ── sessions ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SessionView:
    session: NoteSession
    note_count: int


def start_session(
    conn,
    *,
    title: str | None = None,
    occurred_utc: int | None = None,
    now: int | None = None,
) -> NoteSession:
    """Open a listening sitting. `occurred_utc` defaults to now, so a session
    started while listening needs no date argument."""
    now = now if now is not None else _now()
    return note_repo.create_session(
        conn, occurred_utc=occurred_utc if occurred_utc is not None else now,
        now=now, title=title,
    )


def list_sessions(conn, *, since_utc: int | None = None) -> list[SessionView]:
    """The listening log, most recent sitting first."""
    return [
        SessionView(
            session=s,
            note_count=len(note_repo.list_notes(conn, session_id=s.id)),
        )
        for s in note_repo.list_sessions(conn, since_utc=since_utc)
    ]
