"""
Location <-> filesystem resolution via marker files.

Each location carries a marker file ``.spindlebot-location-<uuid>`` at its root.
We resolve a configured location to its current on-disk root by checking the
marker, so a drive that mounts at an unexpected path — or a *different* drive
sitting at the expected path — is identified correctly rather than blindly
trusted.

Safety rule (decision: a missing marker is NOT a wiped drive): a marker is only
ever written for a positively identified location, and its *absence* is never
treated as evidence about content. Resolution only refuses a root that carries a
*conflicting* marker.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from spindlebot.core.errors import MarkerMismatch
from spindlebot.core.models import Location

MARKER_PREFIX = ".spindlebot-location-"


def marker_path(root: str | Path, uuid: str) -> Path:
    return Path(root) / f"{MARKER_PREFIX}{uuid}"


def read_marker(root: str | Path) -> str | None:
    """Return the location uuid recorded by the marker at ``root``, or None."""
    root = Path(root)
    if not root.is_dir():
        return None
    for p in sorted(root.glob(f"{MARKER_PREFIX}*")):
        return p.name[len(MARKER_PREFIX):]
    return None


def write_marker(root: str | Path, *, uuid: str, name: str, now: int | None = None) -> Path:
    """Write/refresh the marker for a location at ``root``.

    Caller must have established that ``root`` belongs to this location (e.g. via
    resolve_root / ensure_marker) — this function does not check for conflicts.
    """
    now = int(time.time()) if now is None else now
    path = marker_path(root, uuid)
    path.write_text(
        json.dumps({"uuid": uuid, "name": name, "written_utc": now}),
        encoding="utf-8",
    )
    return path


def ensure_marker(root: str | Path, *, uuid: str, name: str, now: int | None = None) -> Path:
    """Ensure ``root`` carries this location's marker.

    Raises MarkerMismatch if ``root`` already carries a *different* location's
    marker. Idempotent when the marker is ours (it is refreshed).
    """
    existing = read_marker(root)
    if existing is not None and existing != uuid:
        raise MarkerMismatch(f"{root} carries marker for {existing!r}, expected {uuid!r}")
    return write_marker(root, uuid=uuid, name=name, now=now)


def resolve_root(location: Location) -> Path | None:
    """Return the location's current content root if present and identified.

    - present + our marker (or no marker)  -> the root path
    - present + a different marker         -> None (a foreign volume sits here)
    - not mounted / no root_path           -> None
    """
    if not location.root_path:
        return None
    root = Path(location.root_path)
    if not root.is_dir():
        return None
    existing = read_marker(root)
    if existing is not None and existing != location.uuid:
        return None
    return root
