"""Refuse canonical album directories containing another recording's audio."""

from __future__ import annotations

from pathlib import Path

from spindlebot.core.errors import SpindleBotError
from spindlebot.core.identity import audio_content_id
from spindlebot.disc import find_audio_files


class DirectoryCollision(SpindleBotError):
    """A canonical destination already contains audio outside the incoming album."""


def check_album_destinations(sources: list[Path], destinations: list[Path]) -> None:
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
    identities = {audio_content_id(p) for p in sources}
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
        for existing in find_audio_files(directory):
            if audio_content_id(existing) not in identities:
                raise DirectoryCollision(
                    f"album directory collision: {directory} contains different audio "
                    f"({existing.name}); incoming files were left in place"
                )
