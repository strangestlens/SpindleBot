"""
Sync executor: the ONLY code that moves bytes. It runs acknowledged
`pending_action` rows and nothing else — the reconciler proposes, a human
acknowledges, this executes.

Phase 3.1 covers COPY, and only COPY, and is deliberately NON-destructive: it
copies content to a location that lacks it, **verifies the destination hash
matches the source**, then records presence. It never deletes or prunes — that
is gated to later commits (delete execution, then the prune cutover). So the
worst case here is a wasted copy, never lost bytes.

Each copy: copy_fn(src → dst) → file_sha256(dst) must equal file_sha256(src) →
write audio_presence(present=1) at the destination → mark the action executed.
A hash mismatch fails the action loudly (no presence, not executed) so a re-run
retries it; per-action errors are isolated so one bad file can't stall the rest.
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
    progress: ProgressCallback | None = None,
    checkpoint: Callable[[], None] | None = None,
    commit_every: int = 1,
) -> SyncResult:
    """Execute acknowledged COPY actions (copy → verify → record presence).

    Reads only acknowledged, not-yet-executed rows. Touches bytes only via
    copy_fn, and never deletes anything. When `progress` is given, fires a
    ProgressEvent per action.

    `checkpoint` (the caller's commit — the caller still owns the transaction) is
    invoked every `commit_every` actions, default every one: each verified copy
    is expensive, so a crash must not roll back and re-copy work already done.
    """
    now = int(time.time()) if now is None else now
    run_id = run_repo.start_run(conn, RunKind.SYNC, now=now)
    result = SyncResult(run_id=run_id)
    status = ScanStatus.OK
    actions = action_repo.list_pending_execution(conn, action_kind=ActionKind.COPY)
    total = len(actions)
    done = 0
    emit(progress, phase="sync", done=0, total=total)
    try:
        for action in actions:
            try:
                if action.content_kind != ContentKind.AUDIO:
                    result.skipped += 1  # sidecar copies land in a later commit
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

                presence_repo.set_presence(
                    conn,
                    audio_id=action.content_id,
                    location_id=dst_loc.id,
                    present=True,
                    observed_utc=now,
                    rel_path=action.rel_path,
                    file_sha256=actual,
                    byte_size=dst.stat().st_size,
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
