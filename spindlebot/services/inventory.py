"""
Inventory: scan a location's filesystem and record what audio it holds.

Read-only with respect to the audio (it never moves or edits a file) — it only
writes to the SpindleBot DB: upserting audio_content (by content identity) and
recording an observed-present audio_presence fact for the location.

Phase 0 covers the local authoring/Pending location only. Phase 1 generalizes
this to any registered location (off-site drives, DAPs) and adds sidecars.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import mutagen

from spindlebot.core.identity import audio_content_id, file_sha256
from spindlebot.core.models import Location
from spindlebot.db.repositories import audio_repo, presence_repo, scan_repo
from spindlebot.disc import AUDIO_EXTENSIONS
from spindlebot.services.volumes import ensure_marker
# Location registration lives in services.locations; re-exported here so existing
# imports (from spindlebot.services.inventory import ensure_pending_location) hold.
from spindlebot.services.locations import (  # noqa: F401
    ensure_pending_location,
    location_uuid,
)

PENDING_LOCATION_UUID = location_uuid("Pending")


@dataclass
class InventoryResult:
    location: str
    scanned: int = 0
    new: int = 0
    updated: int = 0
    errors: int = 0
    error_paths: list[str] = field(default_factory=list)


def _iter_audio_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS:
            yield path


def _read_tags(path: Path) -> dict:
    """Best-effort advisory tags. Never raises; missing/unreadable → empty/None."""
    try:
        mf = mutagen.File(str(path), easy=True)
    except Exception:
        return {}
    if mf is None:
        return {}

    def first(key):
        val = mf.get(key)
        return val[0] if val else None

    def num(key):
        raw = first(key)
        if not raw:
            return None
        try:
            return int(str(raw).split("/")[0])
        except ValueError:
            return None

    duration = None
    try:
        if mf.info and mf.info.length:
            duration = int(mf.info.length)
    except Exception:
        duration = None

    return {
        "artist": first("artist") or first("albumartist"),
        "album": first("album"),
        "title": first("title"),
        "disc_no": num("discnumber"),
        "track_no": num("tracknumber"),
        "duration_s": duration,
    }


def _load_beets_index(beets_db: str | Path | None) -> dict[bytes, int]:
    """Map beets items.path (filesystem bytes) -> items.id, read-only. {} on any error."""
    if not beets_db or not Path(beets_db).exists():
        return {}
    index: dict[bytes, int] = {}
    try:
        bconn = sqlite3.connect(f"file:{Path(beets_db)}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        for item_id, path in bconn.execute("SELECT id, path FROM items"):
            if isinstance(path, str):
                path = os.fsencode(path)
            if path:
                index[path] = item_id
    except sqlite3.Error:
        return {}
    finally:
        bconn.close()
    return index


def inventory_location(
    conn,
    *,
    location: Location,
    root: str | Path,
    now: int | None = None,
    beets_db: str | Path | None = None,
) -> InventoryResult:
    """Scan `root` for audio, upsert content + observed-present presence facts.

    Writes the location's marker file at `root`, records a `location_scan` row,
    and (when `beets_db` is given) links each track's beets item id by path.
    Read-only with respect to the audio files themselves.
    """
    now = int(time.time()) if now is None else now
    result = InventoryResult(location=location.name)
    root = Path(root)
    if not root.is_dir():
        return result

    # Positively identify the location on disk before recording anything.
    ensure_marker(root, uuid=location.uuid, name=location.name, now=now)

    beets_index = _load_beets_index(beets_db)
    scan_id = scan_repo.start_scan(conn, location.id, now)
    status = "ok"
    try:
        for path in _iter_audio_files(root):
            result.scanned += 1
            try:
                cid = audio_content_id(path)
                existed = audio_repo.get_by_identity(conn, cid.value) is not None
                beets_item_id = beets_index.get(os.fsencode(str(path))) if beets_index else None
                audio = audio_repo.upsert(
                    conn, cid, now=now, beets_item_id=beets_item_id, **_read_tags(path)
                )
                presence_repo.set_presence(
                    conn,
                    audio_id=audio.id,
                    location_id=location.id,
                    present=True,
                    observed_utc=now,
                    rel_path=str(path.relative_to(root)),
                    file_sha256=file_sha256(path),
                    byte_size=path.stat().st_size,
                )
                result.updated += 1 if existed else 0
                result.new += 0 if existed else 1
            except OSError as exc:  # file vanished/unreadable mid-scan: isolate, keep going
                result.errors += 1
                result.error_paths.append(f"{path}: {exc}")
    except BaseException:
        status = "interrupted"
        raise
    finally:
        scan_repo.finish_scan(conn, scan_id, files_seen=result.scanned, status=status, now=now)

    return result
