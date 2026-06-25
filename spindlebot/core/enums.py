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
