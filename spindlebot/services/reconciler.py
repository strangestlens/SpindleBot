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

from spindlebot.core import vclock
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
    lyric_repo,
    presence_repo,
    run_repo,
    scan_repo,
    sidecar_presence_repo,
    sidecar_repo,
)


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


def _present_lrc(conn, location_id: int) -> dict[int, tuple[str | None, int]]:
    """Present .lrc sidecars at a location: sidecar_id -> (this copy's sha, audio_id)."""
    out: dict[int, tuple[str | None, int]] = {}
    for p in sidecar_presence_repo.list_for_location(conn, location_id, present=True):
        sc = sidecar_repo.get_by_id(conn, p.sidecar_id)
        if (sc is not None and sc.role == SidecarRole.LRC
                and sc.parent_kind == SidecarParentKind.TRACK):
            out[p.sidecar_id] = (p.file_sha256, sc.parent_id)
    return out


def _ensure_version(conn, doc_id: int, sha: str, origin: str, now: int):
    """Get-or-create (dedup by sha) a lyric version stamped with origin's vclock."""
    for v in lyric_repo.list_versions(conn, doc_id):
        if v.sha256 == sha:
            return v
    return lyric_repo.add_version(
        conn, doc_id=doc_id, sha256=sha,
        vclock_json=vclock.to_json({origin: 1}), source="scan", now=now,
    )


def _flag_lyric_divergence(conn, *, target, auth, audio_id, target_sha, auth_sha,
                           run_id, now, result) -> bool:
    """Record a cross-location lyric divergence and propose a resolve action.

    At this phase the two versions have independent single-location vclocks, so
    they are concurrent — a genuine divergence for a human to adjudicate, never
    auto-resolved. winner/loser here just name the two diverging sides; real
    adjudication is Phase 4.

    The conflict ROW is deduped (one open conflict per track), but the
    resolve_conflict ACTION is proposed every run — exactly like COPY/MISSING,
    so the latest run is always a complete, acknowledgeable plan.

    Phase-4 note: the dedup checks only OPEN conflicts, so once resolution exists
    a resolved conflict whose bytes still differ would re-open here. That's
    moot until Phase 4 propagates the winning lyric (making the shas match); when
    the conflicts CLI lands, key the dedup on the version pair, not just status.
    """
    doc = lyric_repo.ensure_doc(conn, audio_id, now)
    tv = _ensure_version(conn, doc.id, target_sha, target.name, now)
    av = _ensure_version(conn, doc.id, auth_sha, auth.name, now)
    if not vclock.concurrent(vclock.from_json(tv.vclock_json),
                             vclock.from_json(av.vclock_json)):
        return False
    if conflict_repo.find_open_for_audio(conn, audio_id) is None:
        conflict_repo.open_conflict(conn, audio_id=audio_id, winner_version=av.id,
                                    loser_version=tv.id, now=now)
    action_repo.add(
        conn, run_id=run_id, action_kind=ActionKind.RESOLVE_CONFLICT,
        content_kind=ContentKind.AUDIO, content_id=audio_id,
        source_location_id=auth.id, dest_location_id=target.id, now=now,
        reason=f"lyrics differ between {auth.name} and {target.name}",
    )
    result.conflicts += 1
    return True


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

        # CONFLICT: same track's .lrc present on target and an authoritative
        # location with different per-copy content → flag a divergence.
        target_lrc = _present_lrc(conn, target.id)
        if target_lrc:
            conflicted: set[int] = set()   # flag each track's divergence once per run
            for src in sources:
                if src.id == target.id:
                    continue
                src_lrc = _present_lrc(conn, src.id)
                for sid, (t_sha, audio_id) in target_lrc.items():
                    if audio_id in conflicted:
                        continue
                    other = src_lrc.get(sid)
                    if other is None:
                        continue
                    a_sha, _ = other
                    if not t_sha or not a_sha or t_sha == a_sha:
                        continue
                    if _flag_lyric_divergence(
                        conn, target=target, auth=src, audio_id=audio_id,
                        target_sha=t_sha, auth_sha=a_sha,
                        run_id=run_id, now=now, result=result,
                    ):
                        conflicted.add(audio_id)
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
