"""Frozen dataclasses mirroring DB rows. Pure data — no behaviour, no I/O."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Location:
    id: int
    uuid: str
    name: str
    kind: str
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
            kind=row["kind"],
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
    identity_kind: str
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
            identity_kind=row["identity_kind"],
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

    @staticmethod
    def from_row(row: sqlite3.Row) -> "AudioPresence":
        return AudioPresence(
            audio_id=row["audio_id"],
            location_id=row["location_id"],
            present=bool(row["present"]),
            rel_path=row["rel_path"],
            file_sha256=row["file_sha256"],
            byte_size=row["byte_size"],
            observed_utc=row["observed_utc"],
        )
