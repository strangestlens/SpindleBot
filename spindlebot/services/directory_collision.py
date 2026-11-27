"""Refuse canonical album directories containing another recording's audio."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable

from spindlebot.core.errors import SpindleBotError
from spindlebot.core.identity import ContentId, audio_content_id
from spindlebot.disc import AUDIO_EXTENSIONS


class DirectoryCollision(SpindleBotError):
    """A canonical destination already contains audio outside the incoming album."""


class IdentityCache:
    """Reuse reads within one beet command, invalidating changed file metadata."""

    def __init__(self):
        self._values = {}

    def __call__(self, path: Path) -> ContentId:
        stat = path.stat()
        signature = (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
        previous = self._values.get(path)
        if previous is None or previous[0] != signature:
            previous = signature, audio_content_id(path)
            self._values[path] = previous
        return previous[1]


def _raise_walk_error(error):
    raise error


def check_album_destinations(
    sources: list[Path],
    destinations: list[Path],
    *,
    identity: Callable[[Path], ContentId] = audio_content_id,
    identities: set[ContentId] | None = None,
) -> set[ContentId]:
    """Check the actual filesystem before bytes enter any destination directory.

    An existing subset of the incoming album is safe (an interrupted move may
    already have put some tracks there). Any foreign identity refuses the whole
    album. Names and file hashes are not used as musical identity, so retagged
    copies of the same decoded audio remain the same album.
    """
    if not sources or len(sources) != len(destinations):
        raise DirectoryCollision(
            "cannot verify an album without its source/destination paths"
        )
    if identities is None:
        identities = {identity(p) for p in sources}
    checked = set()
    for destination in destinations:
        directory = destination.parent
        if directory in checked:
            continue
        checked.add(directory)
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise DirectoryCollision(
                f"album destination is not a directory: {directory}"
            )
        # os.walk with onerror refuses unreadable subdirectories rather than
        # silently treating an incomplete traversal as an empty safe directory.
        for root, directories, files in os.walk(directory, onerror=_raise_walk_error):
            for name in directories:
                if (Path(root) / name).is_symlink():
                    raise DirectoryCollision(
                        f"cannot verify album directory containing a directory symlink: {Path(root) / name}"
                    )
            for name in files:
                existing = Path(root) / name
                if (
                    name.startswith("._")
                    or existing.suffix.lower().lstrip(".") not in AUDIO_EXTENSIONS
                ):
                    continue
                if not stat.S_ISREG(existing.stat().st_mode):
                    continue
                if identity(existing) not in identities:
                    raise DirectoryCollision(
                        f"album directory collision: {directory} contains different audio "
                        f"({existing.name}); incoming files were left in place"
                    )
    return identities
