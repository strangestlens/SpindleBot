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

`execute_deletes` removes a RETENTION copy — the GATED destructive op, distinct
from prune. It runs acknowledged DELETE `pending_action` rows, but only after
proving that removing THIS copy still leaves at least `min_copies` retention
copies of the content. A delete that would drop retention below the floor is
REFUSED (never touched); it also defaults to a dry run.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
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
    a single destination — so the retention mount agent only executes the work
    planned for that destination, not queued copies for an unmounted DAP. Touches bytes
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


@dataclass
class DeleteResult:
    run_id: int
    dry_run: bool
    deleted: int = 0       # retention copies removed (or would-be, under dry_run)
    bytes_freed: int = 0
    refused: int = 0       # safely declined: non-retention source or below-floor
    skipped: int = 0       # non-audio content only (DELETE is audio-only today)
    refused_reasons: list[str] = field(default_factory=list)  # why each refusal (warnings, not errors)
    errors: list[str] = field(default_factory=list)  # genuine failures (unmounted, bad rel_path, OSError)


def execute_deletes(
    conn,
    *,
    now: int | None = None,
    dry_run: bool = True,
    min_copies: int = 1,
    progress: ProgressCallback | None = None,
) -> DeleteResult:
    """Execute acknowledged DELETE actions — remove a RETENTION copy, GATED.

    Reads only acknowledged, not-yet-executed DELETE rows (the acknowledge gate:
    a proposed-but-unacknowledged delete never runs). For each, the copy being
    removed lives at the action's `source_location_id`.

    DELETE is only ever for RETENTION copies. A DELETE whose source is NOT a
    retention location is REFUSED outright — deleting a non-retention (Pending/
    authoring) copy is prune's job, and only once verified-on-retention; letting
    delete unlink authoring bytes would bypass that safety, so it is declined.

    For a retention source, it counts how many retention copies would REMAIN if
    this one were dropped (present retention copies of the content, minus this
    copy if it is itself a present retention copy). If that would fall below
    `min_copies`, the delete is REFUSED. This is the hard invariant: retention
    copies can never be driven below the floor.

    Both refusals are safe, expected outcomes (not failures): they go in
    `refused` / `refused_reasons` as warnings, never in `errors`. `errors` is
    reserved for genuine failures (unmounted/unidentified location, an unsafe
    rel_path — absolute or containing `..` — that could unlink outside the
    location root, OSError). `skipped` means strictly non-audio content.

    The non-retention Pending/authoring copy is never counted toward the floor
    (that is `count_retention_copies`'s job) — so it can never make a delete look
    safe. Defaults to a dry run (the caller opts into deletion).
    """
    now = int(time.time()) if now is None else now
    run_id = run_repo.start_run(conn, RunKind.SYNC, now=now,
                                note="delete (dry-run)" if dry_run else "delete")
    result = DeleteResult(run_id=run_id, dry_run=dry_run)
    status = ScanStatus.OK
    actions = action_repo.list_pending_execution(conn, action_kind=ActionKind.DELETE)
    total = len(actions)
    done = 0
    emit(progress, phase="delete", done=0, total=total)
    try:
        for action in actions:
            try:
                # DELETE is only defined for audio retention copies today.
                if action.content_kind != ContentKind.AUDIO:
                    result.skipped += 1
                    continue
                loc = (location_repo.get_by_id(conn, action.source_location_id)
                       if action.source_location_id is not None else None)
                root = resolve_root(loc) if loc else None
                if root is None or not action.rel_path:
                    result.errors.append(
                        f"action {action.id}: location not mounted or identified")
                    continue

                # Destructive-path hardening: rel_path must be a real relative path
                # inside root. An absolute path would make `root / rel_path` discard
                # root; a `..` segment could traverse outside it — either way we'd
                # unlink outside the location. Refuse (as an error) before building
                # the target, never touch bytes.
                rp = PurePosixPath(action.rel_path)
                if rp.is_absolute() or ".." in rp.parts:
                    result.errors.append(
                        f"action {action.id}: unsafe rel_path {action.rel_path!r} "
                        f"(absolute or contains '..') — not deleted")
                    continue

                # DELETE is only for retention copies — never unlink an authoring
                # (Pending) copy here; that's prune's verified-on-retention path.
                if not loc.is_retention:
                    result.refused += 1
                    result.refused_reasons.append(
                        f"action {action.id}: refused — source {loc.name!r} is not a "
                        f"retention location (delete is retention-only)")
                    continue

                pres = presence_repo.get(conn, action.content_id, loc.id)
                self_counts = bool(pres is not None and pres.present)
                would_remain = (
                    presence_repo.count_retention_copies(conn, action.content_id)
                    - (1 if self_counts else 0))
                if would_remain < min_copies:
                    result.refused += 1
                    result.refused_reasons.append(
                        f"action {action.id}: refused — deleting would leave "
                        f"{would_remain} retention copies (< min_copies={min_copies})")
                    continue

                target = Path(root) / action.rel_path
                size = target.stat().st_size if target.is_file() else (
                    (pres.byte_size if pres else 0) or 0)
                if not dry_run:
                    target.unlink(missing_ok=True)
                    presence_repo.set_presence(
                        conn, audio_id=action.content_id, location_id=loc.id,
                        present=False, observed_utc=now, rel_path=action.rel_path,
                        file_sha256=pres.file_sha256 if pres else None,
                        byte_size=pres.byte_size if pres else None)
                    _cleanup_empty_dirs(target.parent, Path(root))
                    action_repo.mark_executed(conn, action.id, now)
                result.deleted += 1
                result.bytes_freed += size
            except OSError as exc:
                result.errors.append(f"action {action.id}: {exc}")
            finally:
                done += 1
                emit(progress, phase="delete", done=done, total=total,
                     current=action.rel_path or "")
    except BaseException:
        status = ScanStatus.INTERRUPTED
        raise
    finally:
        run_repo.finish_run(conn, run_id, status=status, now=now)

    return result
