"""
Location registry: turn configured locations into DB rows.

A location's uuid is derived deterministically from its name, so re-registering
is idempotent and stable across machines. The Pending (authoring) library is
always registered from core.pending_dir; explicit [[locations]] and (for
backward-compat) legacy [[destinations]] become retention locations.
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from spindlebot.core.models import Location
from spindlebot.db.repositories import location_repo

PENDING_LOCATION_NAME = "Pending"


def location_uuid(name: str) -> str:
    """Stable uuid for a location name (case/space-insensitive)."""
    return str(uuid5(NAMESPACE_URL, f"spindlebot:location:{name.strip().lower()}"))


def ensure_pending_location(conn, now: int, *, root_path=None) -> Location:
    """Ensure the local Pending (authoring) library location exists; return it."""
    return location_repo.upsert(
        conn,
        uuid=location_uuid(PENDING_LOCATION_NAME),
        name=PENDING_LOCATION_NAME,
        kind="library",
        root_path=(str(root_path) if root_path else None),
        is_authoritative_audio=True,
        is_retention=False,
        last_seen_utc=now,
    )


def register_from_config(conn, cfg, now: int) -> list[Location]:
    """Upsert location rows for the Pending area + every enabled configured location."""
    registered: list[Location] = []
    seen: set[str] = set()

    pending = ensure_pending_location(conn, now, root_path=cfg.core.pending_dir)
    registered.append(pending)
    seen.add(PENDING_LOCATION_NAME.strip().lower())

    for lc in cfg.locations:
        if not lc.enabled:
            continue
        key = lc.name.strip().lower()
        seen.add(key)
        registered.append(location_repo.upsert(
            conn,
            uuid=location_uuid(lc.name),
            name=lc.name,
            kind=lc.kind,
            root_path=lc.root_path or None,
            is_authoritative_audio=lc.is_authoritative_audio,
            is_retention=lc.is_retention,
        ))

    # Legacy [[destinations]] → retention locations, unless already covered above.
    for dest in cfg.destinations:
        if not dest.enabled or dest.name.strip().lower() in seen:
            continue
        registered.append(location_repo.upsert(
            conn,
            uuid=location_uuid(dest.name),
            name=dest.name,
            kind="rclone" if dest.type == "rclone" else "local_drive",
            root_path=dest.path or None,
            is_retention=True,
        ))

    return registered


def get_by_name(conn, name: str) -> Location | None:
    return location_repo.get_by_uuid(conn, location_uuid(name))
