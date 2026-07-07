"""
Sync executor: the ONLY code that moves bytes. It runs acknowledged
`pending_action` rows and nothing else — the reconciler proposes, a human
acknowledges, this executes.

`execute_pending` covers COPY and is NON-destructive: copy → verify destination
hash matches source → record presence → mark executed. A hash mismatch fails
loudly (no presence, retried); per-action errors are isolated.

`prune_released` is the destructive counterpart, and is safe by construction: it
releases a file from the non-retention authoring library (Pending) ONLY once the
exact same path+hash is verified present on a retention location — re-hashing the
retained copy on disk before deleting the original. It never touches a retention
location, and defaults to a dry run (the caller opts into deletion). So a copy is
never pruned unless a proven-intact retained copy already exists.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from spindlebot.core.enums import ActionKind, ContentKind, RunKind, ScanStatus
from spindlebot.core.errors import IntegrityMismatch
from spindlebot.core.identity import file_sha256
from spindlebot.core.progress import ProgressCallback, emit
from spindlebot.db.repositories import (
    action_repo,
    location_repo,
    presence_repo,
    run_repo,
    sidecar_presence_repo,
)
from spindlebot.services.volumes import resolve_root

CopyFn = Callable[[Path, Path], None]


def _rsync_copy(src: Path, dst: Path) -> None:
    """Default copy: rsync src → dst (a file path), creating parent dirs."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["rsync", "-a", "--", str(src), str(dst)],
        check=True, capture_output=True,
    )


@dataclass
class SyncResult:
    run_id: int
    copied: int = 0       # copies verified + presence recorded
    failed: int = 0       # copy error or hash mismatch
    skipped: int = 0      # not a COPY / unresolvable location / source missing
    errors: list[str] = field(default_factory=list)


def execute_pending(
    conn,
    *,
    copy_fn: CopyFn = _rsync_copy,
    now: int | None = None,
    dest_location_id: int | None = None,
    progress: ProgressCallback | None = None,
    checkpoint: Callable[[], None] | None = None,
    commit_every: int = 1,
) -> SyncResult:
    """Execute acknowledged COPY actions (copy → verify → record presence).

    Reads only acknowledged, not-yet-executed rows. `dest_location_id` scopes to
    a single destination — so the DwRugged mount agent only executes the work
    planned for DwRugged, not queued copies for an unmounted DAP. Touches bytes
    only via copy_fn, and never deletes anything. When `progress` is given, fires
    a ProgressEvent per action.

    `checkpoint` (the caller's commit — the caller still owns the transaction) is
    invoked every `commit_every` actions, default every one: each verified copy
    is expensive, so a crash must not roll back and re-copy work already done.
    """
    now = int(time.time()) if now is None else now
    run_id = run_repo.start_run(conn, RunKind.SYNC, now=now)
    result = SyncResult(run_id=run_id)
    status = ScanStatus.OK
    actions = action_repo.list_pending_execution(
        conn, action_kind=ActionKind.COPY, dest_location_id=dest_location_id)
    total = len(actions)
    done = 0
    emit(progress, phase="sync", done=0, total=total)
    try:
        for action in actions:
            try:
                if action.content_kind not in (ContentKind.AUDIO, ContentKind.SIDECAR):
                    result.skipped += 1
                    continue
                src_loc = location_repo.get_by_id(conn, action.source_location_id)
                dst_loc = location_repo.get_by_id(conn, action.dest_location_id)
                src_root = resolve_root(src_loc) if src_loc else None
                dst_root = resolve_root(dst_loc) if dst_loc else None
                if src_root is None or dst_root is None or not action.rel_path:
                    result.skipped += 1
                    result.errors.append(
                        f"action {action.id}: source/dest not mounted or identified")
                    continue

                src = Path(src_root) / action.rel_path
                dst = Path(dst_root) / action.rel_path
                if not src.is_file():
                    result.skipped += 1
                    result.errors.append(f"action {action.id}: source missing ({src})")
                    continue

                expected = file_sha256(src)
                copy_fn(src, dst)
                actual = file_sha256(dst)
                if actual != expected:
                    raise IntegrityMismatch(
                        f"action {action.id}: {dst} hash {actual[:12]} != "
                        f"source {expected[:12]}")

                if action.content_kind == ContentKind.AUDIO:
                    presence_repo.set_presence(
                        conn, audio_id=action.content_id, location_id=dst_loc.id,
                        present=True, observed_utc=now, rel_path=action.rel_path,
                        file_sha256=actual, byte_size=dst.stat().st_size,
                    )
                else:  # SIDECAR (the only other kind that reaches here)
                    sidecar_presence_repo.set_presence(
                        conn, sidecar_id=action.content_id, location_id=dst_loc.id,
                        present=True, observed_utc=now, rel_path=action.rel_path,
                        file_sha256=actual, byte_size=dst.stat().st_size,
                    )
                action_repo.mark_executed(conn, action.id, now)
                result.copied += 1
            except (OSError, IntegrityMismatch, subprocess.CalledProcessError) as exc:
                result.failed += 1
                result.errors.append(str(exc))
            finally:
                # finally so the count advances on every path — including the
                # `continue` skips — so the bar always reaches 100%, and each
                # completed action is committed (durable, no re-copy on crash).
                done += 1
                emit(progress, phase="sync", done=done, total=total,
                     current=action.rel_path or "")
                if checkpoint is not None and commit_every > 0 and done % commit_every == 0:
                    checkpoint()
    except BaseException:
        status = ScanStatus.INTERRUPTED
        raise
    finally:
        run_repo.finish_run(conn, run_id, status=status, now=now)

    return result


@dataclass
class PruneResult:
    run_id: int
    dry_run: bool
    pruned: int = 0        # files released (or would-be, under dry_run)
    bytes_freed: int = 0
    skipped: int = 0       # not safely retained → kept on the authoring library
    below_floor: int = 0   # released audio now on < min_copies retention copies
    errors: list[str] = field(default_factory=list)


def _cleanup_empty_dirs(directory: Path, root: Path) -> None:
    """Remove now-empty parent dirs up to (not including) root. Best-effort."""
    while directory != root and root in directory.parents:
        try:
            directory.rmdir()   # only succeeds when empty
        except OSError:
            break
        directory = directory.parent


def prune_released(
    conn,
    *,
    now: int | None = None,
    dry_run: bool = True,
    verify: bool = True,
    min_copies: int = 1,
    verify_fn: Callable[[Path], str] = file_sha256,
    progress: ProgressCallback | None = None,
) -> PruneResult:
    """Release files from the non-retention authoring library once safely retained.

    A file is pruned ONLY when the exact same rel_path + file_sha256 is present on
    a retention location AND (when `verify`) that retained copy re-hashes correctly
    on disk right now. Retention locations are never touched. Defaults to a dry run.

    This is the "prune at first retention copy" model — it does NOT wait for
    min_copies retention copies (releasing a non-retention copy never lowers the
    retention count anyway). `min_copies` is used only to WARN via
    result.below_floor when a released track is left on fewer than that many
    retention copies (e.g. one drive, awaiting a DAP).
    """
    now = int(time.time()) if now is None else now
    run_id = run_repo.start_run(conn, RunKind.SYNC, now=now,
                                note="prune (dry-run)" if dry_run else "prune")
    result = PruneResult(run_id=run_id, dry_run=dry_run)
    status = ScanStatus.OK
    locations = {loc.id: loc for loc in location_repo.list_all(conn)}
    retention = {lid for lid, loc in locations.items() if loc.is_retention}
    roots: dict[int, Path | None] = {}

    def _root(loc):
        if loc.id not in roots:
            r = resolve_root(loc)
            roots[loc.id] = Path(r) if r is not None else None
        return roots[loc.id]

    def _retained(rel_path, sha, presence_rows) -> bool:
        for rp in presence_rows:
            if (rp.location_id in retention and rp.present
                    and rp.rel_path == rel_path and rp.file_sha256 == sha):
                rroot = _root(locations[rp.location_id])
                if rroot is None:
                    continue
                rfile = rroot / rp.rel_path
                if not rfile.is_file():
                    continue
                if verify and verify_fn(rfile) != sha:
                    continue
                return True
        return False

    def _release(loc, root, rel_path, byte_size, mark_absent) -> None:
        pfile = root / rel_path
        try:
            size = pfile.stat().st_size if pfile.exists() else (byte_size or 0)
            if not dry_run:
                pfile.unlink(missing_ok=True)
                mark_absent()
                _cleanup_empty_dirs(pfile.parent, root)
            result.pruned += 1
            result.bytes_freed += size
            emit(progress, phase="prune", done=result.pruned, total=0, current=rel_path)
        except OSError as exc:
            result.errors.append(f"{pfile}: {exc}")

    try:
        for loc in locations.values():
            # Only the non-retention authoring library is ever prunable.
            if not (loc.is_authoritative_audio and not loc.is_retention and loc.enabled):
                continue
            root = _root(loc)
            if root is None:
                result.errors.append(f"{loc.name}: not mounted or identified")
                continue

            for p in presence_repo.list_for_location(conn, loc.id, present=True):
                if not p.rel_path or not p.file_sha256 or not _retained(
                        p.rel_path, p.file_sha256,
                        presence_repo.list_for_audio(conn, p.audio_id)):
                    result.skipped += 1
                    continue
                _release(loc, root, p.rel_path, p.byte_size, mark_absent=lambda p=p: (
                    presence_repo.set_presence(
                        conn, audio_id=p.audio_id, location_id=loc.id, present=False,
                        observed_utc=now, rel_path=p.rel_path,
                        file_sha256=p.file_sha256, byte_size=p.byte_size)))
                if presence_repo.count_retention_copies(conn, p.audio_id) < min_copies:
                    result.below_floor += 1

            for sp in sidecar_presence_repo.list_for_location(conn, loc.id, present=True):
                if not sp.rel_path or not sp.file_sha256 or not _retained(
                        sp.rel_path, sp.file_sha256,
                        sidecar_presence_repo.list_for_sidecar(conn, sp.sidecar_id)):
                    result.skipped += 1
                    continue
                _release(loc, root, sp.rel_path, sp.byte_size, mark_absent=lambda sp=sp: (
                    sidecar_presence_repo.set_presence(
                        conn, sidecar_id=sp.sidecar_id, location_id=loc.id, present=False,
                        observed_utc=now, rel_path=sp.rel_path,
                        file_sha256=sp.file_sha256, byte_size=sp.byte_size)))
    except BaseException:
        status = ScanStatus.INTERRUPTED
        raise
    finally:
        run_repo.finish_run(conn, run_id, status=status, now=now)

    return result
