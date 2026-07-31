"""Closed enumerations for SpindleBot domain values.

StrEnum members compare/serialize as their string value, so they store in SQLite
as plain TEXT and stay backward-compatible with existing string comparisons.
"""
from __future__ import annotations

from enum import StrEnum


class LocationKind(StrEnum):
    LIBRARY = "library"          # local authoring/Pending area; not retention
    LOCAL_DRIVE = "local_drive"  # a mounted disk / DAP / SD card
    RCLONE = "rclone"            # an rclone remote (B2, S3, SFTP, ...)


class IdentityKind(StrEnum):
    AUDIO_MD5 = "audio_md5"      # decoded-audio MD5 (survives re-tagging)
    FILE_SHA256 = "file_sha256"  # whole-file hash fallback when audio MD5 is absent


class ScanStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    INTERRUPTED = "interrupted"
    ERROR = "error"


class SidecarRole(StrEnum):
    LRC = "lrc"      # synchronized lyrics (.lrc), paired to a single track
    COVER = "cover"  # album cover art (cover.jpg)
    NOLRC = "nolrc"  # marker: this album intentionally has no lyrics (.nolrc)


class SidecarParentKind(StrEnum):
    TRACK = "track"  # parent_id -> audio_content.id
    ALBUM = "album"  # parent_id -> album.id


class RunKind(StrEnum):
    IMPORT = "import"
    SYNC = "sync"
    INVENTORY = "inventory"
    RECONCILE = "reconcile"


class ActionKind(StrEnum):
    COPY = "copy"                        # copy content to a location that lacks it
    DELETE = "delete"                    # remove a copy (never below min_copies)
    UPDATE_PRESENCE = "update_presence"  # record an observed absence (non-destructive)
    RESOLVE_CONFLICT = "resolve_conflict"  # a divergence a human must adjudicate


class ContentKind(StrEnum):
    AUDIO = "audio"      # references audio_content.id
    SIDECAR = "sidecar"  # references sidecar_content.id


class ConflictStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class MediaKind(StrEnum):
    """Physical/digital medium an external collection item was released on.

    Deliberately coarse: the collection audit only needs to answer "is this the
    kind of thing I'd rip?", not to reproduce a discography database. Container
    formats (Box Set, All Media) map to OTHER — they always appear alongside the
    real medium, and every format entry on a release is inspected.
    """
    CD = "cd"
    VINYL = "vinyl"
    CASSETTE = "cassette"
    DIGITAL = "digital"
    OTHER = "other"
