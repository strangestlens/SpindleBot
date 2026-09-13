"""Frozen dataclasses mirroring DB rows. Pure data — no behaviour, no I/O."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from spindlebot.core.enums import (
    ActionKind,
    ConflictStatus,
    ContentKind,
    IdentityKind,
    LocationKind,
    NoteFormat,
    NoteStatus,
    NoteSubjectKind,
    RunKind,
    ScanStatus,
    SidecarParentKind,
    SidecarRole,
)


@dataclass(frozen=True)
class Location:
    id: int
    uuid: str
    name: str
    kind: LocationKind
    is_authoritative_audio: bool
    is_retention: bool
    enabled: bool
    last_seen_utc: int | None
    root_path: str | None = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Location":
        keys = row.keys()
        return Location(
            id=row["id"],
            uuid=row["uuid"],
            name=row["name"],
            kind=LocationKind(row["kind"]),
            is_authoritative_audio=bool(row["is_authoritative_audio"]),
            is_retention=bool(row["is_retention"]),
            enabled=bool(row["enabled"]),
            last_seen_utc=row["last_seen_utc"],
            root_path=row["root_path"] if "root_path" in keys else None,
        )


@dataclass(frozen=True)
class AudioContent:
    id: int
    identity: str
    identity_kind: IdentityKind
    artist: str | None
    album: str | None
    title: str | None
    disc_no: int | None
    track_no: int | None
    duration_s: int | None
    beets_item_id: int | None
    first_seen_utc: int
    last_seen_utc: int

    @staticmethod
    def from_row(row: sqlite3.Row) -> "AudioContent":
        return AudioContent(
            id=row["id"],
            identity=row["identity"],
            identity_kind=IdentityKind(row["identity_kind"]),
            artist=row["artist"],
            album=row["album"],
            title=row["title"],
            disc_no=row["disc_no"],
            track_no=row["track_no"],
            duration_s=row["duration_s"],
            beets_item_id=row["beets_item_id"],
            first_seen_utc=row["first_seen_utc"],
            last_seen_utc=row["last_seen_utc"],
        )


@dataclass(frozen=True)
class AudioPresence:
    audio_id: int
    location_id: int
    present: bool
    rel_path: str | None
    file_sha256: str | None
    byte_size: int | None
    observed_utc: int
    mtime: int | None = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "AudioPresence":
        keys = row.keys()
        return AudioPresence(
            audio_id=row["audio_id"],
            location_id=row["location_id"],
            present=bool(row["present"]),
            rel_path=row["rel_path"],
            file_sha256=row["file_sha256"],
            byte_size=row["byte_size"],
            observed_utc=row["observed_utc"],
            mtime=row["mtime"] if "mtime" in keys else None,
        )


@dataclass(frozen=True)
class Album:
    id: int
    album_key: str
    albumartist: str | None
    album: str | None
    mb_albumid: str | None
    first_seen_utc: int
    last_seen_utc: int

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Album":
        return Album(
            id=row["id"],
            album_key=row["album_key"],
            albumartist=row["albumartist"],
            album=row["album"],
            mb_albumid=row["mb_albumid"],
            first_seen_utc=row["first_seen_utc"],
            last_seen_utc=row["last_seen_utc"],
        )


@dataclass(frozen=True)
class SidecarContent:
    id: int
    parent_kind: SidecarParentKind
    parent_id: int
    role: SidecarRole
    sha256: str
    first_seen_utc: int
    last_seen_utc: int

    @staticmethod
    def from_row(row: sqlite3.Row) -> "SidecarContent":
        return SidecarContent(
            id=row["id"],
            parent_kind=SidecarParentKind(row["parent_kind"]),
            parent_id=row["parent_id"],
            role=SidecarRole(row["role"]),
            sha256=row["sha256"],
            first_seen_utc=row["first_seen_utc"],
            last_seen_utc=row["last_seen_utc"],
        )


@dataclass(frozen=True)
class SidecarPresence:
    sidecar_id: int
    location_id: int
    present: bool
    rel_path: str | None
    file_sha256: str | None
    byte_size: int | None
    observed_utc: int
    mtime: int | None = None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "SidecarPresence":
        keys = row.keys()
        return SidecarPresence(
            sidecar_id=row["sidecar_id"],
            location_id=row["location_id"],
            present=bool(row["present"]),
            rel_path=row["rel_path"],
            file_sha256=row["file_sha256"],
            byte_size=row["byte_size"],
            observed_utc=row["observed_utc"],
            mtime=row["mtime"] if "mtime" in keys else None,
        )


@dataclass(frozen=True)
class Run:
    id: int
    kind: RunKind
    location_id: int | None
    started_utc: int
    finished_utc: int | None
    status: ScanStatus
    note: str | None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Run":
        return Run(
            id=row["id"],
            kind=RunKind(row["kind"]),
            location_id=row["location_id"],
            started_utc=row["started_utc"],
            finished_utc=row["finished_utc"],
            status=ScanStatus(row["status"]),
            note=row["note"],
        )


@dataclass(frozen=True)
class PendingAction:
    id: int
    run_id: int
    action_kind: ActionKind
    content_kind: ContentKind
    content_id: int
    source_location_id: int | None
    dest_location_id: int | None
    rel_path: str | None
    reason: str | None
    acknowledged: bool
    acknowledged_utc: int | None
    executed_utc: int | None
    created_utc: int

    @staticmethod
    def from_row(row: sqlite3.Row) -> "PendingAction":
        return PendingAction(
            id=row["id"],
            run_id=row["run_id"],
            action_kind=ActionKind(row["action_kind"]),
            content_kind=ContentKind(row["content_kind"]),
            content_id=row["content_id"],
            source_location_id=row["source_location_id"],
            dest_location_id=row["dest_location_id"],
            rel_path=row["rel_path"],
            reason=row["reason"],
            acknowledged=bool(row["acknowledged"]),
            acknowledged_utc=row["acknowledged_utc"],
            executed_utc=row["executed_utc"],
            created_utc=row["created_utc"],
        )


@dataclass(frozen=True)
class LyricDoc:
    id: int
    audio_id: int
    head_version_id: int | None
    created_utc: int
    updated_utc: int

    @staticmethod
    def from_row(row: sqlite3.Row) -> "LyricDoc":
        return LyricDoc(
            id=row["id"],
            audio_id=row["audio_id"],
            head_version_id=row["head_version_id"],
            created_utc=row["created_utc"],
            updated_utc=row["updated_utc"],
        )


@dataclass(frozen=True)
class LyricVersion:
    id: int
    doc_id: int
    sha256: str
    vclock_json: str
    source: str | None
    authored_utc: int | None
    created_utc: int

    @staticmethod
    def from_row(row: sqlite3.Row) -> "LyricVersion":
        return LyricVersion(
            id=row["id"],
            doc_id=row["doc_id"],
            sha256=row["sha256"],
            vclock_json=row["vclock_json"],
            source=row["source"],
            authored_utc=row["authored_utc"],
            created_utc=row["created_utc"],
        )


@dataclass(frozen=True)
class LyricVersionPresence:
    doc_id: int
    location_id: int
    version_id: int
    observed_utc: int

    @staticmethod
    def from_row(row: sqlite3.Row) -> "LyricVersionPresence":
        return LyricVersionPresence(
            doc_id=row["doc_id"],
            location_id=row["location_id"],
            version_id=row["version_id"],
            observed_utc=row["observed_utc"],
        )


@dataclass(frozen=True)
class Conflict:
    id: int
    audio_id: int | None
    winner_version: int | None
    loser_version: int | None
    loser_kept_path: str | None
    status: ConflictStatus
    detected_utc: int
    resolved_utc: int | None

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Conflict":
        return Conflict(
            id=row["id"],
            audio_id=row["audio_id"],
            winner_version=row["winner_version"],
            loser_version=row["loser_version"],
            loser_kept_path=row["loser_kept_path"],
            status=ConflictStatus(row["status"]),
            detected_utc=row["detected_utc"],
            resolved_utc=row["resolved_utc"],
        )


@dataclass(frozen=True)
class NoteSubject:
    """What a note is about. Display fields are a snapshot, not a lookup —
    a note about an album that was never ripped still has to render."""
    id: int
    kind: NoteSubjectKind
    subject_key: str
    artist_name: str | None
    album_title: str | None
    track_title: str | None
    mbid: str | None
    created_utc: int

    @staticmethod
    def from_row(row: sqlite3.Row) -> "NoteSubject":
        return NoteSubject(
            id=row["id"],
            kind=NoteSubjectKind(row["kind"]),
            subject_key=row["subject_key"],
            artist_name=row["artist_name"],
            album_title=row["album_title"],
            track_title=row["track_title"],
            mbid=row["mbid"],
            created_utc=row["created_utc"],
        )

    @property
    def label(self) -> str:
        parts = [p for p in (self.artist_name, self.album_title, self.track_title) if p]
        return " — ".join(parts) if parts else self.subject_key


@dataclass(frozen=True)
class NoteSession:
    """One listening sitting. Notes from a single evening across several
    subjects belong to one of these; `note.session_id` is nullable, so a note
    written outside a sitting is still a first-class note."""
    id: int
    uuid: str
    occurred_utc: int
    title: str | None
    created_utc: int

    @staticmethod
    def from_row(row: sqlite3.Row) -> "NoteSession":
        return NoteSession(
            id=row["id"],
            uuid=row["uuid"],
            occurred_utc=row["occurred_utc"],
            title=row["title"],
            created_utc=row["created_utc"],
        )


@dataclass(frozen=True)
class Note:
    """A note's identity and lifecycle. The BODY lives in NoteRevision — a note
    is the stable thing an author keeps editing, not the text of any one draft."""
    id: int
    uuid: str
    subject_id: int
    session_id: int | None
    status: NoteStatus
    created_utc: int
    updated_utc: int

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Note":
        return Note(
            id=row["id"],
            uuid=row["uuid"],
            subject_id=row["subject_id"],
            session_id=row["session_id"],
            status=NoteStatus(row["status"]),
            created_utc=row["created_utc"],
            updated_utc=row["updated_utc"],
        )


@dataclass(frozen=True)
class NoteRevision:
    """One append-only draft of a note's body.

    `seq` is monotonic per note and the head is MAX(seq) — there is no
    head-pointer column, which keeps the note/revision foreign keys acyclic
    under `foreign_keys=ON`. `sha256` is the dedupe key today and the lineage
    key when notes sync across devices, the same primitive lyrics_sync reasons
    over."""
    id: int
    note_id: int
    seq: int
    body: str
    body_format: NoteFormat
    sha256: str
    author: str | None
    created_utc: int

    @staticmethod
    def from_row(row: sqlite3.Row) -> "NoteRevision":
        return NoteRevision(
            id=row["id"],
            note_id=row["note_id"],
            seq=row["seq"],
            body=row["body"],
            body_format=NoteFormat(row["body_format"]),
            sha256=row["sha256"],
            author=row["author"],
            created_utc=row["created_utc"],
        )
