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
from typing import Callable

import mutagen

from spindlebot.core.albums import album_key
from spindlebot.core.enums import (
    IdentityKind,
    ScanStatus,
    SidecarParentKind,
    SidecarRole,
)
from spindlebot.core.identity import ContentId, audio_content_id, file_sha256
from spindlebot.core.models import Location
from spindlebot.core.progress import ProgressCallback, emit
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
_NOLRC_SUFFIX = ".nolrc"
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


def _classify_sidecar(path: Path) -> SidecarRole | None:
    name = path.name.lower()
    # `.nolrc` with no stem is the album-level miss marker; `<track>.nolrc`
    # (e.g. "01. Song.nolrc") is a per-track terminal miss marker. Both map to
    # the NOLRC role; parenting (album vs track) is decided in the walk from the
    # presence/absence of a stem.
    if name == _NOLRC_NAME or path.suffix.lower() == _NOLRC_SUFFIX:
        return SidecarRole.NOLRC
    if name in _COVER_NAMES:
        return SidecarRole.COVER
    if path.suffix.lower() == _LRC_SUFFIX:
        return SidecarRole.LRC
    return None


def _is_track_nolrc(path: Path) -> bool:
    """A per-track `<base>.nolrc` (has a stem), vs the bare album-level `.nolrc`."""
    return path.name.lower() != _NOLRC_NAME and path.suffix.lower() == _NOLRC_SUFFIX


def _walk_tree(root: Path) -> tuple[list[Path], list[tuple[Path, SidecarRole]], int]:
    """One pass over the tree → (audio paths, (sidecar, role) pairs, total audio bytes).

    A single walk (audio + sidecars together) so the tree is stat'd once and the
    totals are known up front for progress reporting. Audio bytes dominate scan
    time (hashing), so that's the byte total we surface; sidecars are negligible.
    """
    audio: list[Path] = []
    sidecars: list[tuple[Path, SidecarRole]] = []
    total_audio_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        # AppleDouble companion files (._Track01.flac, ._cover.jpg) that macOS
        # writes onto exFAT/FAT volumes — e.g. DAP cards. They carry a real
        # file's extension but are resource-fork metadata, never audio or a
        # sidecar. Skip before classification so they never reach the DB.
        if path.name.startswith("._"):
            continue
        if path.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS:
            audio.append(path)
            try:
                total_audio_bytes += path.stat().st_size
            except OSError:
                pass
            continue
        role = _classify_sidecar(path)
        if role is not None:
            sidecars.append((path, role))
    return audio, sidecars, total_audio_bytes


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


_EMPTY_TAGS = {
    "artist": None, "albumartist": None, "album": None, "title": None,
    "disc_no": None, "track_no": None, "duration_s": None, "mb_albumid": None,
}


def _read_tags(path: Path) -> dict:
    """Best-effort advisory tags. Never raises; missing/unreadable → all keys None.

    Callers index by key (tags["artist"]), so the returned dict must always carry
    the full key set — an unreadable file yields every value None, never a dict
    that's missing keys.
    """
    try:
        mf = mutagen.File(str(path), easy=True)
    except Exception:
        return dict(_EMPTY_TAGS)
    if mf is None:
        return dict(_EMPTY_TAGS)

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


def _identify_or_reuse(
    conn,
    path: Path,
    *,
    location_id: int,
    rel_path: str,
    byte_size: int,
    mtime: int,
    rehash: bool,
) -> tuple[ContentId, str]:
    """Return (identity, file_sha256) for `path`, reusing the DB when unchanged.

    On a repeat scan a file whose recorded presence matches on
    (rel_path, byte_size, mtime) is treated as unchanged: its stored identity and
    per-copy sha256 are reused, avoiding both hashes. Any mismatch — new file,
    changed size/mtime, missing mtime (pre-v6 row), or `rehash=True` — falls back
    to hashing the bytes.
    """
    if not rehash:
        prior = presence_repo.get_by_rel_path(conn, location_id, rel_path)
        if (
            prior is not None
            and prior.file_sha256 is not None
            and prior.byte_size == byte_size
            and prior.mtime is not None
            and prior.mtime == mtime
        ):
            audio = audio_repo.get_by_id(conn, prior.audio_id)
            if audio is not None:
                cid = ContentId(IdentityKind(audio.identity_kind), audio.identity)
                return cid, prior.file_sha256
    return audio_content_id(path), file_sha256(path)


def _sidecar_digest_or_reuse(
    conn,
    path: Path,
    *,
    location_id: int,
    rel_path: str,
    byte_size: int,
    mtime: int,
    rehash: bool,
) -> str:
    """Return file_sha256 for a sidecar, reusing the DB copy when unchanged.

    Same (rel_path, byte_size, mtime) skip rule as audio; a sidecar has no
    identity hash, only the per-copy integrity sha256.
    """
    if not rehash:
        prior = sidecar_presence_repo.get_by_rel_path(conn, location_id, rel_path)
        if (
            prior is not None
            and prior.file_sha256 is not None
            and prior.byte_size == byte_size
            and prior.mtime is not None
            and prior.mtime == mtime
        ):
            return prior.file_sha256
    return file_sha256(path)


def inventory_location(
    conn,
    *,
    location: Location,
    root: str | Path,
    now: int | None = None,
    beets_db: str | Path | None = None,
    progress: ProgressCallback | None = None,
    checkpoint: Callable[[], None] | None = None,
    commit_every: int = 200,
    rehash: bool = False,
) -> InventoryResult:
    """Scan `root` for audio + sidecars, upsert content + observed-present facts.

    Writes the location's marker file at `root`, records a `location_scan` row,
    groups tracks into albums, records sidecars (.lrc per track, cover.jpg /
    .nolrc per album), and (when `beets_db` is given) links each track's beets
    item id by path. Read-only with respect to the files themselves. When
    `progress` is given, fires a ProgressEvent per file scanned.

    `checkpoint` (the caller's commit — the caller still owns the transaction) is
    invoked every `commit_every` files so a long scan keeps partial progress
    durable and observable mid-run instead of vanishing on interrupt.

    Incremental re-scan: a file whose recorded presence at this location matches
    on (rel_path, byte_size, mtime) is assumed unchanged — its stored identity and
    per-copy file_sha256 are reused and only observed_utc is refreshed, skipping
    the expensive decoded-audio MD5 / sha256 hashing. `rehash=True` forces every
    file to be re-hashed regardless (a full-integrity pass / escape hatch).
    """
    now = int(time.time()) if now is None else now
    result = InventoryResult(location=location.name)
    root = Path(root)
    if not root.is_dir():
        return result

    # Positively identify the location on disk before recording anything.
    ensure_marker(root, uuid=location.uuid, name=location.name, now=now)

    beets_index = _load_beets_index(beets_db)
    audio_files, sidecar_files, total_bytes = _walk_tree(root)
    total = len(audio_files) + len(sidecar_files)
    done = 0
    done_bytes = 0
    emit(progress, phase="scan", done=0, total=total,
         done_bytes=0, total_bytes=total_bytes)

    scan_id = scan_repo.start_scan(conn, location.id, now)
    status = ScanStatus.OK

    def _maybe_checkpoint() -> None:
        if checkpoint is not None and commit_every > 0 and done % commit_every == 0:
            checkpoint()

    # Built during the audio pass, consumed by the sidecar pass.
    dir_albums: dict[Path, set[int]] = defaultdict(set)  # dir -> album ids of tracks in it
    stem_audio: dict[tuple[Path, str], int] = {}         # (dir, stem) -> audio id
    seen_albums: set[int] = set()
    try:
        for path in audio_files:
            result.scanned += 1
            try:
                # Merge over the empty shape so indexing below can never KeyError,
                # even if _read_tags ever regresses to a partial dict — without a
                # broad KeyError catch that would mask unrelated bugs downstream.
                tags = {**_EMPTY_TAGS, **_read_tags(path)}
                rel_path = str(path.relative_to(root))
                st = path.stat()
                byte_size = st.st_size
                mtime = st.st_mtime_ns

                cid, digest = _identify_or_reuse(
                    conn, path, location_id=location.id, rel_path=rel_path,
                    byte_size=byte_size, mtime=mtime, rehash=rehash,
                )
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
                    rel_path=rel_path,
                    file_sha256=digest,
                    byte_size=byte_size,
                    mtime=mtime,
                )
                done_bytes += byte_size
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
            done += 1
            emit(progress, phase="audio", done=done, total=total,
                 done_bytes=done_bytes, total_bytes=total_bytes,
                 current=str(path.relative_to(root)))
            _maybe_checkpoint()

        for path, role in sidecar_files:
            try:
                if role is SidecarRole.LRC or _is_track_nolrc(path):
                    # .lrc and per-track <base>.nolrc both parent to the track
                    # sharing their stem in the same directory.
                    audio_id = stem_audio.get((path.parent, path.stem.lower()))
                    if audio_id is None:
                        continue  # orphan sidecar with no matching track in its dir
                    parent_kind, parent_id = SidecarParentKind.TRACK, audio_id
                else:
                    album_id = _resolve_album_for_dir(path.parent, dir_albums)
                    if album_id is None:
                        continue  # no album to attach this cover/.nolrc to
                    parent_kind, parent_id = SidecarParentKind.ALBUM, album_id

                rel_path = str(path.relative_to(root))
                st = path.stat()
                byte_size = st.st_size
                mtime = st.st_mtime_ns
                digest = _sidecar_digest_or_reuse(
                    conn, path, location_id=location.id, rel_path=rel_path,
                    byte_size=byte_size, mtime=mtime, rehash=rehash,
                )
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
                    rel_path=rel_path,
                    file_sha256=digest,
                    byte_size=byte_size,
                    mtime=mtime,
                )
                result.sidecars += 1
                result.sidecars_updated += 1 if existed else 0
                result.sidecars_new += 0 if existed else 1
            except OSError as exc:
                result.errors += 1
                result.error_paths.append(f"{path}: {exc}")
            done += 1
            emit(progress, phase="sidecar", done=done, total=total,
                 done_bytes=done_bytes, total_bytes=total_bytes,
                 current=str(path.relative_to(root)))
            _maybe_checkpoint()
    except BaseException:
        status = ScanStatus.INTERRUPTED
        raise
    finally:
        result.albums = len(seen_albums)
        scan_repo.finish_scan(conn, scan_id, files_seen=result.scanned, status=status, now=now)

    return result
