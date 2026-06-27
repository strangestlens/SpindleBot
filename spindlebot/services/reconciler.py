"""
Reconciler: diff the DB's recorded state against what should be on a location
and PROPOSE work — never touch bytes.

It reads presence facts (written by inventory) and emits `pending_action` rows
for a human to acknowledge at review time; the Phase-3 executor is the only code
that acts, and only on acknowledged rows. Two concerns this phase:

- COPY: audio present on an authoritative location but absent on the target
  retention location → propose copying it there ("sync what's missing").
- MISSING: audio the DB still believes present on the target but NOT re-confirmed
  by the target's latest scan (observed_utc older than that scan's start) →
  propose recording the absence (update_presence, non-destructive). If recording
  it would drop the content below the min_copies retention floor, flag it.

Deletes are never auto-proposed here, and update_presence is not destructive, so
nothing this phase can lose bytes. The min_copies floor is a planning-time
warning now; it gates real deletes in Phase 3.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from spindlebot.core.enums import ActionKind, ContentKind, RunKind, ScanStatus
from spindlebot.core.models import Location
from spindlebot.db.repositories import (
    action_repo,
    presence_repo,
    run_repo,
    scan_repo,
)


@dataclass
class ReconcileResult:
    location: str
    run_id: int
    copies: int = 0           # COPY actions proposed
    missing: int = 0          # update_presence (absent) actions proposed
    below_floor: int = 0      # contents that drop below min_copies if marked absent
    below_floor_ids: list[int] = field(default_factory=list)


def reconcile_location(
    conn,
    *,
    target: Location,
    authoritative_locations: list[Location],
    min_copies: int = 1,
    now: int | None = None,
) -> ReconcileResult:
    """Plan reconciliation for `target` against the authoritative source(s)."""
    now = int(time.time()) if now is None else now
    run_id = run_repo.start_run(conn, RunKind.RECONCILE, location_id=target.id, now=now)
    result = ReconcileResult(location=target.name, run_id=run_id)
    status = ScanStatus.OK
    try:
        target_present = {
            p.audio_id: p
            for p in presence_repo.list_for_location(conn, target.id, present=True)
        }

        # COPY: authoritative content the target doesn't have.
        proposed: set[int] = set()
        for auth in authoritative_locations:
            if auth.id == target.id:
                continue
            for p in presence_repo.list_for_location(conn, auth.id, present=True):
                if p.audio_id in target_present or p.audio_id in proposed:
                    continue
                action_repo.add(
                    conn, run_id=run_id, action_kind=ActionKind.COPY,
                    content_kind=ContentKind.AUDIO, content_id=p.audio_id,
                    source_location_id=auth.id, dest_location_id=target.id,
                    rel_path=p.rel_path, now=now,
                    reason=f"present on {auth.name}, absent on {target.name}",
                )
                proposed.add(p.audio_id)
                result.copies += 1

        # MISSING: target rows not re-confirmed by the target's latest scan.
        scan = scan_repo.latest_scan(conn, target.id)
        if scan is not None:
            cutoff = scan["started_utc"]
            for audio_id, p in target_present.items():
                if p.observed_utc >= cutoff:
                    continue
                action_repo.add(
                    conn, run_id=run_id, action_kind=ActionKind.UPDATE_PRESENCE,
                    content_kind=ContentKind.AUDIO, content_id=audio_id,
                    dest_location_id=target.id, rel_path=p.rel_path, now=now,
                    reason=f"present in DB but not seen in {target.name}'s latest scan",
                )
                result.missing += 1
                if target.is_retention:
                    projected = presence_repo.count_retention_copies(conn, audio_id) - 1
                    if projected < min_copies:
                        result.below_floor += 1
                        result.below_floor_ids.append(audio_id)
    except BaseException:
        status = ScanStatus.INTERRUPTED
        raise
    finally:
        note = None
        if result.below_floor:
            note = f"{result.below_floor} content(s) below min_copies={min_copies}"
        run_repo.finish_run(conn, run_id, status=status, now=now, note=note)

    return result
