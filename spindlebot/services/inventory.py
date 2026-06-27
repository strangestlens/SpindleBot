"""
Inventory: scan a location's filesystem and record what audio it holds.

Read-only with respect to the files (it never moves or edits one) — it only
writes to the SpindleBot DB: upserting audio_content (by content identity),
grouping tracks into albums, recording sidecars (.lrc / cover.jpg / .nolrc),
and writing observed-present presence facts for the location.

Sidecars belong to identity, never to a path: a .lrc follows its track (matched
by stem within the same directory), while cover.jpg and the .nolrc marker follow
their album. Phase 0 covered the Pending location only; Phase 1 generalized this
to any registered location and added sidecar discovery.
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import mutagen

from spindlebot.core.albums import album_key
from spindlebot.core.enums import ScanStatus, SidecarParentKind, SidecarRole
from spindlebot.core.identity import audio_content_id, file_sha256
from spindlebot.core.models import Location
from spindlebot.db.repositories import (
    album_repo,
    audio_repo,
    presence_repo,
    scan_repo,
    sidecar_presence_repo,
    sidecar_repo,
)
from spindlebot.disc import AUDIO_EXTENSIONS
from spindlebot.services.volumes import ensure_marker
# Location registration lives in services.locations; re-exported here so existing
# imports (from spindlebot.services.inventory import ensure_pending_location) hold.
from spindlebot.services.locations import (  # noqa: F401
    ensure_pending_location,
    location_uuid,
)

PENDING_LOCATION_UUID = location_uuid("Pending")

# Album-level cover art names we recognize (case-insensitive). The pipeline
# writes cover.jpg; the others are accepted for foreign locations.
_COVER_NAMES = {"cover.jpg", "cover.jpeg", "cover.png"}
_NOLRC_NAME = ".nolrc"
_LRC_SUFFIX = ".lrc"


@dataclass
class InventoryResult:
    location: str
    scanned: int = 0
    new: int = 0
    updated: int = 0
    albums: int = 0
    # sidecars* count sidecar FILES observed, not distinct sidecar rows: when
    # several files map to one (parent, role) at a location — e.g. a per-disc
    # cover.jpg inside each multidisc folder — each file is counted, but they
    # upsert a single row (mirrors how `scanned` counts audio files).
    sidecars: int = 0
    sidecars_new: int = 0
    sidecars_updated: int = 0
    errors: int = 0
    error_paths: list[str] = field(default_factory=list)


def _iter_audio_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS:
            yield path


def _classify_sidecar(path: Path) -> SidecarRole | None:
    name = path.name.lower()
    if name == _NOLRC_NAME:
        return SidecarRole.NOLRC
    if name in _COVER_NAMES:
        return SidecarRole.COVER
    if path.suffix.lower() == _LRC_SUFFIX:
        return SidecarRole.LRC
    return None


def _iter_sidecar_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        role = _classify_sidecar(path)
        if role is not None:
            yield path, role


def _resolve_album_for_dir(directory: Path, dir_albums: dict[Path, set[int]]) -> int | None:
    """Album an album-level sidecar in `directory` belongs to, or None if unclear.

    Prefers an album whose tracks sit directly in the directory; falls back to a
    single album found one level down (the multidisc 'cover at album root, tracks
    in Disc N/ subfolders' layout). Ambiguous matches resolve to None — better to
    skip than mis-attach a shared cover.
    """
    direct = dir_albums.get(directory)
    if direct:
        return next(iter(direct)) if len(direct) == 1 else None
    sub: set[int] = set()
    for d, albums in dir_albums.items():
        if d.parent == directory:
            sub |= albums
    return next(iter(sub)) if len(sub) == 1 else None


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
        "albumartist": first("albumartist") or first("artist"),
        "album": first("album"),
        "title": first("title"),
        "disc_no": num("discnumber"),
        "track_no": num("tracknumber"),
        "duration_s": duration,
        "mb_albumid": first("musicbrainz_albumid"),
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
    """Scan `root` for audio + sidecars, upsert content + observed-present facts.

    Writes the location's marker file at `root`, records a `location_scan` row,
    groups tracks into albums, records sidecars (.lrc per track, cover.jpg /
    .nolrc per album), and (when `beets_db` is given) links each track's beets
    item id by path. Read-only with respect to the files themselves.
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
    status = ScanStatus.OK
    # Built during the audio pass, consumed by the sidecar pass.
    dir_albums: dict[Path, set[int]] = defaultdict(set)  # dir -> album ids of tracks in it
    stem_audio: dict[tuple[Path, str], int] = {}         # (dir, stem) -> audio id
    seen_albums: set[int] = set()
    try:
        for path in _iter_audio_files(root):
            result.scanned += 1
            try:
                tags = _read_tags(path)
                cid = audio_content_id(path)
                existed = audio_repo.get_by_identity(conn, cid.value) is not None
                beets_item_id = beets_index.get(os.fsencode(str(path))) if beets_index else None
                audio = audio_repo.upsert(
                    conn, cid, now=now, beets_item_id=beets_item_id,
                    artist=tags["artist"], album=tags["album"], title=tags["title"],
                    disc_no=tags["disc_no"], track_no=tags["track_no"],
                    duration_s=tags["duration_s"],
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
                stem_audio[(path.parent, path.stem.lower())] = audio.id
                if tags["album"]:
                    album = album_repo.upsert(
                        conn,
                        album_key=album_key(tags["albumartist"], tags["album"], tags["mb_albumid"]),
                        now=now, albumartist=tags["albumartist"], album=tags["album"],
                        mb_albumid=tags["mb_albumid"],
                    )
                    album_repo.link_track(conn, album.id, audio.id)
                    dir_albums[path.parent].add(album.id)
                    seen_albums.add(album.id)
                result.updated += 1 if existed else 0
                result.new += 0 if existed else 1
            except OSError as exc:  # file vanished/unreadable mid-scan: isolate, keep going
                result.errors += 1
                result.error_paths.append(f"{path}: {exc}")

        for path, role in _iter_sidecar_files(root):
            try:
                if role is SidecarRole.LRC:
                    audio_id = stem_audio.get((path.parent, path.stem.lower()))
                    if audio_id is None:
                        continue  # orphan .lrc with no matching track in its dir
                    parent_kind, parent_id = SidecarParentKind.TRACK, audio_id
                else:
                    album_id = _resolve_album_for_dir(path.parent, dir_albums)
                    if album_id is None:
                        continue  # no album to attach this cover/.nolrc to
                    parent_kind, parent_id = SidecarParentKind.ALBUM, album_id

                digest = file_sha256(path)
                existed = sidecar_repo.get(
                    conn, parent_kind=parent_kind, parent_id=parent_id, role=role
                ) is not None
                sidecar = sidecar_repo.upsert(
                    conn, parent_kind=parent_kind, parent_id=parent_id,
                    role=role, sha256=digest, now=now,
                )
                # presence PK is (sidecar_id, location_id): if several files at
                # this location map to one sidecar (per-disc cover.jpg), the last
                # one observed wins this row. Divergent per-copy paths are a
                # reconciler concern (same property audio_presence already has).
                sidecar_presence_repo.set_presence(
                    conn,
                    sidecar_id=sidecar.id,
                    location_id=location.id,
                    present=True,
                    observed_utc=now,
                    rel_path=str(path.relative_to(root)),
                    file_sha256=digest,
                    byte_size=path.stat().st_size,
                )
                result.sidecars += 1
                result.sidecars_updated += 1 if existed else 0
                result.sidecars_new += 0 if existed else 1
            except OSError as exc:
                result.errors += 1
                result.error_paths.append(f"{path}: {exc}")
    except BaseException:
        status = ScanStatus.INTERRUPTED
        raise
    finally:
        result.albums = len(seen_albums)
        scan_repo.finish_scan(conn, scan_id, files_seen=result.scanned, status=status, now=now)

    return result
