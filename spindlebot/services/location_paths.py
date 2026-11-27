"""Read-only path-semantics probes and derived queue-key maintenance."""
from __future__ import annotations

import string
from pathlib import Path

from spindlebot.core.models import Location
from spindlebot.db.repositories import action_repo, location_repo

_SWAP_ASCII_CASE = str.maketrans(
    string.ascii_lowercase + string.ascii_uppercase,
    string.ascii_uppercase + string.ascii_lowercase,
)


def probe_case_sensitive(root: Path) -> bool | None:
    """Probe existing entries inside this root, without creating any files.

    Inventory's marker provides an ASCII name even on an otherwise empty
    location. An empty/unreadable unmarked root remains unknown; do not infer
    volume behavior from a mount point's name in its parent filesystem.
    """
    try:
        entries = list(root.iterdir())
        names = {p.name for p in entries}
        for entry in entries:
            alias_name = entry.name.translate(_SWAP_ASCII_CASE)
            if alias_name == entry.name:
                continue
            try:
                original = entry.lstat()
            except FileNotFoundError:
                continue
            if alias_name in names:
                # Distinct directory entries can be hard links to one inode.
                # Their simultaneous existence still proves sensitivity.
                return True
            try:
                alias = (root / alias_name).lstat()
            except FileNotFoundError:
                try:
                    current = entry.lstat()
                except FileNotFoundError:
                    continue
                if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                    return True
                continue
            if (alias.st_dev, alias.st_ino) == (original.st_dev, original.st_ino):
                # Recheck names so a newly created hard-link alias cannot be
                # mistaken for case-insensitive lookup.
                current_names = {p.name for p in root.iterdir()}
                if entry.name not in current_names:
                    return None
                return alias_name in current_names
            return True
    except OSError:
        return None
    return None


def refresh_case_policy(conn, *, location: Location, root: Path) -> bool | None:
    sensitive = probe_case_sensitive(root)
    location_repo.set_case_sensitive(conn, location.id, sensitive)
    action_repo.prepare_destination_keys(conn, dest_location_id=location.id)
    return sensitive
