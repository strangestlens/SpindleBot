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

from spindlebot.core.enums import (
    ActionKind,
    ContentKind,
    RunKind,
    ScanStatus,
    SidecarParentKind,
    SidecarRole,
)
from spindlebot.core.models import Location
from spindlebot.core.progress import ProgressCallback, emit
from spindlebot.db.repositories import (
    action_repo,
    conflict_repo,
    presence_repo,
    run_repo,
    scan_repo,
    sidecar_presence_repo,
    sidecar_repo,
)
from spindlebot.services import lyrics_sync
from spindlebot.services.lyrics_sync import LyricObservation


@dataclass
class ReconcileResult:
    location: str
    run_id: int
    copies: int = 0           # COPY actions proposed
    missing: int = 0          # update_presence (absent) actions proposed
    below_floor: int = 0      # contents that drop below min_copies if marked absent
    below_floor_ids: list[int] = field(default_factory=list)
    conflicts: int = 0        # cross-location lyric divergences flagged
    target_scanned: bool = True  # False → target never inventoried; planning skipped


def _present_lrc(conn, location_id: int) -> dict[int, tuple[str | None, int, int]]:
    """Present .lrc sidecars at a location.

    sidecar_id -> (this copy's sha, audio_id, when this copy was last scanned).
    """
    out: dict[int, tuple[str | None, int, int]] = {}
    for p in sidecar_presence_repo.list_for_location(conn, location_id, present=True):
        sc = sidecar_repo.get_by_id(conn, p.sidecar_id)
        if (sc is not None and sc.role == SidecarRole.LRC
                and sc.parent_kind == SidecarParentKind.TRACK):
            out[p.sidecar_id] = (p.file_sha256, sc.parent_id, p.observed_utc)
    return out


def _lyric_observations(
    conn, locations: list[Location]
) -> dict[int, list[LyricObservation]]:
    """Per audio_id, the .lrc observed present across the given locations.

    A track's .lrc maps to one sidecar_content row (keyed by parent track), so
    the same sidecar_id resolves to the same audio_id at every location; only the
    per-copy file_sha256 differs. Copies with no recorded sha are skipped.
    """
    obs: dict[int, list[LyricObservation]] = {}
    for loc in locations:
        for _sid, (sha, audio_id, observed_utc) in _present_lrc(conn, loc.id).items():
            if not sha:
                continue
            obs.setdefault(audio_id, []).append(
                LyricObservation(location_id=loc.id, uuid=loc.uuid,
                                 name=loc.name, sha=sha, observed_utc=observed_utc)
            )
    return obs


def reconcile_location(
    conn,
    *,
    target: Location,
    source_locations: list[Location],
    min_copies: int = 1,
    now: int | None = None,
    progress: ProgressCallback | None = None,
) -> ReconcileResult:
    """Plan reconciliation for `target` against every location that could source it.

    `source_locations` is any location content can be copied FROM — the authoring
    library AND other retention locations (so once Pending is pruned, a retention
    target like a DAP can still be filled from DwRugged). The authoritative source
    is preferred when content lives on more than one. When `progress` is given,
    fires a ProgressEvent per proposed action (indeterminate total).
    """
    now = int(time.time()) if now is None else now
    run_id = run_repo.start_run(conn, RunKind.RECONCILE, location_id=target.id, now=now)
    result = ReconcileResult(location=target.name, run_id=run_id)
    status = ScanStatus.OK
    # Prefer the authoring library as the copy source; dedup then records it.
    sources = sorted(source_locations, key=lambda loc: not loc.is_authoritative_audio)

    def _tick(phase: str) -> None:
        proposed_n = result.copies + result.missing + result.conflicts
        emit(progress, phase=phase, done=proposed_n, total=0)

    try:
        # A trustworthy plan needs to know what is ACTUALLY on the target now.
        # Without a scan, target presence is empty/stale: COPY would propose the
        # whole library (even if the files are already there), MISSING can't tell
        # stale from gone. Require an inventory first — skip planning otherwise.
        scan = scan_repo.latest_scan(conn, target.id)
        if scan is None:
            result.target_scanned = False
            return result

        target_present = {
            p.audio_id: p
            for p in presence_repo.list_for_location(conn, target.id, present=True)
        }

        # Content flows OUT of the non-retention authoring library toward
        # retention, never back in — so never propose copies INTO it (else a
        # review of Pending after a prune would offer to re-fill it).
        target_is_authoring = target.is_authoritative_audio and not target.is_retention
        copy_sources = [] if target_is_authoring else sources

        # COPY: content the (retention) target doesn't have, from any source.
        proposed: set[int] = set()
        for src in copy_sources:
            if src.id == target.id:
                continue
            for p in presence_repo.list_for_location(conn, src.id, present=True):
                if p.audio_id in target_present or p.audio_id in proposed:
                    continue
                action_repo.add(
                    conn, run_id=run_id, action_kind=ActionKind.COPY,
                    content_kind=ContentKind.AUDIO, content_id=p.audio_id,
                    source_location_id=src.id, dest_location_id=target.id,
                    rel_path=p.rel_path, now=now,
                    reason=f"present on {src.name}, absent on {target.name}",
                )
                proposed.add(p.audio_id)
                result.copies += 1
                _tick("copy")

        # COPY sidecars: authoritative sidecars (.lrc / cover / .nolrc) the
        # target lacks — so everything, not just the audio, reaches retention.
        #
        # KNOWN LIMIT: sidecar_presence keeps one rel_path per (sidecar, location),
        # so a multi-file sidecar (e.g. a per-disc cover.jpg duplicated across disc
        # subfolders → one album COVER row) copies only the recorded path; the
        # other identical files aren't tracked and don't get copied. .lrc is
        # per-track so unaffected. Consequence for prune: it must verify the EXACT
        # path on retention before removing a Pending file, never just the content
        # — else an un-copied per-disc cover could be the only copy pruned away.
        # Proper fix is the "track all paths per (content, location)" model.
        target_sidecars = {
            sp.sidecar_id
            for sp in sidecar_presence_repo.list_for_location(conn, target.id, present=True)
        }
        proposed_sc: set[int] = set()
        for src in copy_sources:
            if src.id == target.id:
                continue
            for sp in sidecar_presence_repo.list_for_location(conn, src.id, present=True):
                if sp.sidecar_id in target_sidecars or sp.sidecar_id in proposed_sc:
                    continue
                action_repo.add(
                    conn, run_id=run_id, action_kind=ActionKind.COPY,
                    content_kind=ContentKind.SIDECAR, content_id=sp.sidecar_id,
                    source_location_id=src.id, dest_location_id=target.id,
                    rel_path=sp.rel_path, now=now,
                    reason=f"sidecar present on {src.name}, absent on {target.name}",
                )
                proposed_sc.add(sp.sidecar_id)
                result.copies += 1
                _tick("copy")

        # MISSING: target rows not re-confirmed by the target's latest scan.
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
            _tick("missing")

        # LYRIC LINEAGE: fold every location's observed .lrc into causal lineage.
        # Agreement (shared sha) is no conflict; a linear edit silently advances
        # the head (4.1 propagates it); a behind location is a future target. Only
        # genuinely CONCURRENT versions open a conflict — deduped one-per-track,
        # with the resolve action re-proposed each run so the latest plan is whole.
        lyric_locs = [target] + [s for s in sources if s.id != target.id]
        for audio_id, observations in _lyric_observations(conn, lyric_locs).items():
            lineage = lyrics_sync.reconcile_doc(
                conn, audio_id=audio_id, observations=observations, now=now
            )
            # Only act when the TARGET is a party to the divergence (holds a
            # concurrent version). A conflict between two OTHER locations belongs
            # to their own reviews, not this target's — and the conflict row is
            # deduped, so whichever party reviews first opens it once.
            loser = next((h for h in lineage.concurrent
                          if h.location_id == target.id), None)
            if loser is None:
                continue
            winner = next((h for h in lineage.held if h.is_head), None)
            if conflict_repo.find_open_for_audio(conn, audio_id) is None:
                conflict_repo.open_conflict(
                    conn, audio_id=audio_id,
                    winner_version=lineage.head_version_id,
                    loser_version=loser.version_id, now=now,
                )
            action_repo.add(
                conn, run_id=run_id, action_kind=ActionKind.RESOLVE_CONFLICT,
                content_kind=ContentKind.AUDIO, content_id=audio_id,
                source_location_id=winner.location_id if winner else None,
                dest_location_id=target.id, now=now,
                reason=(
                    "lyrics diverge: head on "
                    f"{winner.location_name if winner else 'another copy'}, "
                    f"concurrent on {target.name}"
                ),
            )
            result.conflicts += 1
            _tick("conflict")
    except BaseException:
        status = ScanStatus.INTERRUPTED
        raise
    finally:
        if not result.target_scanned:
            note = f"{target.name} has never been inventoried — run inventory first"
        elif result.below_floor:
            note = f"{result.below_floor} content(s) below min_copies={min_copies}"
        else:
            note = None
        run_repo.finish_run(conn, run_id, status=status, now=now, note=note)

    return result
