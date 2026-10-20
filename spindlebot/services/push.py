"""
Push: fill ONE location from the rest of the library in a single pass.

`music-sync.sh` is the same orchestration for the retention drive, but it reads
the single enabled `[[destinations]]` entry, so it can only ever target that one
drive. A location that exists only as a `[[locations]]` entry — a DAP, say — has
no automation at all, and filling it means running inventory, review and sync by
hand. `push_location` is that sequence as one callable:

    refresh the authoring library → inventory the target → reconcile
      → acknowledge → sync (scoped to this location)

The authoring library is rescanned first for the same reason `music-sync.sh`
opens with an unscoped `spindlebot inventory`: nothing in the import pipeline
writes to this DB, so an album ripped since the last scan exists on disk but not
in any table the reconciler reads. Skip that step and a push run right after a
rip plans nothing and reports success.

**It deliberately stops there — it never prunes.** `prune_released` has no
per-location scope: it releases every Pending file verified on ANY retention
location. A DAP configured with `is_retention = true` would therefore make a
post-push prune release the authoring copy on the strength of the DAP alone.
Leaving prune out is what makes push safe to run against a retention DAP today,
before retention is decoupled from the min_copies floor. Nothing here deletes
bytes: inventory is read-only, the reconciler only writes `pending_action` rows,
and `execute_pending` is copy-only.

`dry_run` stops after planning: it still inventories (a plan built on a stale
scan is worthless) and still records the proposed actions, but acknowledges
nothing and copies nothing — so `--dry-run` answers "what would this push do?"
without a prompt the caller would have to answer.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from spindlebot.core.models import Location
from spindlebot.core.progress import ProgressCallback
from spindlebot.db.repositories import action_repo, location_repo
from spindlebot.services.inventory import InventoryResult, inventory_location
from spindlebot.services.reconciler import ReconcileResult, reconcile_location
from spindlebot.services.sync import CopyFn, SyncResult, execute_pending
from spindlebot.services.volumes import resolve_root

#: Stage name → progress callback. The CLI renders one labelled bar per stage;
#: a service never knows about terminals.
ProgressFor = Callable[[str], ProgressCallback | None]

SOURCE_INVENTORY = "source inventory"
INVENTORY = "inventory"
REVIEW = "review"
SYNC = "sync"


@dataclass
class PushResult:
    location: str
    dry_run: bool
    run_id: int | None = None          # the reconcile run that planned the work
    acknowledged: int = 0
    source_inventory: InventoryResult | None = None   # the authoring library
    inventory: InventoryResult | None = None          # the target
    source_scan_skipped: str | None = None            # why, when it was skipped
    reconcile: ReconcileResult | None = None
    sync: SyncResult | None = None
    stopped_at: str | None = None      # stage push stopped BEFORE, if it stopped early
    stopped_reason: str | None = None

    @property
    def ok(self) -> bool:
        """True when every stage that should have run did so cleanly.

        Exactly ONE early stop is a success: the dry run's own stop before the
        copies, which is the point of asking for it. Every other stop means the
        pass didn't do what it was asked — including a planner refusal under
        --dry-run, which is a refusal to plan, not a completed preview.
        Inventory's per-file read errors aren't fatal either (an unreadable file
        is simply not re-confirmed present, which proposes a non-destructive
        update_presence at worst), but they do make the run not-ok, so
        automation can tell a complete pass from a partial one.
        """
        if self.stopped_at is not None and not (self.dry_run and self.stopped_at == SYNC):
            return False
        for inv in (self.source_inventory, self.inventory):
            if inv is not None and inv.errors:
                return False
        if self.sync is not None and (self.sync.failed or self.sync.errors):
            return False
        return True


def _progress(progress_for: ProgressFor | None, stage: str) -> ProgressCallback | None:
    return progress_for(stage) if progress_for is not None else None


def _authoring_location(conn) -> Location | None:
    """The non-retention authoring library (Pending) — the source new music
    arrives at. Content flows out of it toward retention, never back in."""
    return next(
        (loc for loc in location_repo.list_all(conn)
         if loc.is_authoritative_audio and not loc.is_retention and loc.enabled),
        None,
    )


def push_location(
    conn,
    *,
    location: Location,
    root: str | Path,
    min_copies: int = 1,
    now: int | None = None,
    beets_db: str | Path | None = None,
    dry_run: bool = False,
    rehash: bool = False,
    copy_fn: CopyFn | None = None,   # None → sync's own rsync default
    progress_for: ProgressFor | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> PushResult:
    """Inventory `location`, plan what it is missing, and copy it there.

    `root` is the location's resolved on-disk root (the caller resolves it via
    `volumes.resolve_root`, so an unmounted or foreign-marked drive is refused
    before any work starts). The caller owns the transaction; `checkpoint` is
    the caller's commit, passed down so a long scan or a long copy stays durable
    and is invoked once between acknowledging and copying — a crash mid-sync
    must not roll back the acknowledgement and re-plan from scratch.

    One `now` spans every stage so that every row the pass writes — the scan,
    the refreshed presence rows, the proposed actions, the runs — carries the
    same stamp and the pass is attributable from the DB alone. Correctness does
    not hang on it: the reconciler's "was this re-confirmed?" cutoff is the scan
    row's own `started_utc`, which inventory wrote, not the `now` handed to the
    planner.
    """
    now = int(time.time()) if now is None else now
    result = PushResult(location=location.name, dry_run=dry_run)

    # Refresh the authoring library FIRST, exactly as `music-sync.sh` does with
    # its unscoped `spindlebot inventory`. Nothing in the import pipeline writes
    # to this DB, so albums imported since the last scan are not recorded
    # anywhere the reconciler reads — plan without this and a push right after a
    # rip copies none of the new tracks and still reports success. Skipped when
    # the authoring library IS the target (it is about to be scanned anyway).
    source = _authoring_location(conn)
    if source is not None and source.id != location.id:
        source_root = resolve_root(source)
        if source_root is None:
            # Not a reason to abort: the target scan is what the planner needs,
            # and a push from an already-pruned library is legitimate. Record it
            # so the plan is never silently built on stale source facts.
            result.source_scan_skipped = (
                f"{source.name} is not mounted or not identified "
                f"(expected at {source.root_path}) — planning from recorded state"
            )
        else:
            result.source_inventory = inventory_location(
                conn, location=source, root=source_root, now=now, beets_db=beets_db,
                progress=_progress(progress_for, SOURCE_INVENTORY),
                checkpoint=checkpoint, rehash=rehash,
            )
            if checkpoint is not None:
                checkpoint()

    result.inventory = inventory_location(
        conn, location=location, root=root, now=now, beets_db=beets_db,
        progress=_progress(progress_for, INVENTORY), checkpoint=checkpoint,
        rehash=rehash,
    )
    if checkpoint is not None:
        checkpoint()

    # Any other location can source a copy — the authoring library or another
    # retention location, so a DAP can be filled from the retention drive even
    # after Pending has been pruned.
    sources = [loc for loc in location_repo.list_all(conn) if loc.id != location.id]
    result.reconcile = reconcile_location(
        conn, target=location, source_locations=sources, min_copies=min_copies,
        now=now, progress=_progress(progress_for, REVIEW),
    )
    result.run_id = result.reconcile.run_id

    # Can't happen in practice — inventory just wrote a scan for this location —
    # but the planner's contract is "no scan, no plan", so honour it rather than
    # syncing against a plan that was never made.
    if not result.reconcile.target_scanned:
        result.stopped_at = REVIEW
        result.stopped_reason = f"{location.name} has never been inventoried"
        return result

    if dry_run:
        result.stopped_at = SYNC
        result.stopped_reason = "dry run — nothing acknowledged, nothing copied"
        return result

    result.acknowledged = action_repo.acknowledge_run(conn, result.reconcile.run_id, now)
    if checkpoint is not None:
        checkpoint()

    # Scoped to this destination — but NOT to this run: an acknowledged copy
    # left unexecuted by an earlier push (the DAP was unplugged mid-run) is
    # still owed to this location, and is exactly what the next push should
    # finish. This is `sync --location` semantics, unchanged.
    result.sync = execute_pending(
        conn, now=now, dest_location_id=location.id,
        progress=_progress(progress_for, SYNC), checkpoint=checkpoint,
        **({} if copy_fn is None else {"copy_fn": copy_fn}),
    )
    return result
