"""Tests for the reconciler planner (services/reconciler.py). No bytes touched."""
from __future__ import annotations

import pytest

from spindlebot.core import vclock
from spindlebot.core.enums import (
    ActionKind,
    RunKind,
    ScanStatus,
    SidecarParentKind,
    SidecarRole,
)
from spindlebot.core.identity import ContentId
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    action_repo,
    audio_repo,
    conflict_repo,
    location_repo,
    lyric_repo,
    presence_repo,
    run_repo,
    scan_repo,
    sidecar_presence_repo,
    sidecar_repo,
)
from spindlebot.services.reconciler import reconcile_location


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


def _loc(conn, uuid, name, **kw):
    return location_repo.upsert(conn, uuid=uuid, name=name,
                                kind=kw.pop("kind", "local_drive"), **kw)


def _audio(conn, identity):
    return audio_repo.upsert(conn, ContentId("audio_md5", identity), now=0)


def _present(conn, audio_id, loc_id, *, observed_utc=100):
    presence_repo.set_presence(conn, audio_id=audio_id, location_id=loc_id,
                               present=True, observed_utc=observed_utc,
                               rel_path=f"a/{audio_id}.flac")


def _scan(conn, loc_id, started_utc=1):
    """Mark a location as having been inventoried (reconcile requires this)."""
    return scan_repo.start_scan(conn, loc_id, started_utc)


def _pending(conn):
    return _loc(conn, "pending", "Pending", kind="library",
                is_authoritative_audio=True, is_retention=False)


def _rugged(conn):
    return _loc(conn, "retention_drive", "RetentionDrive", is_retention=True)


# ── COPY planning ─────────────────────────────────────────────────────────────

def test_proposes_copy_for_authoritative_content_absent_on_target(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)
    x, y = _audio(conn, "x" * 32), _audio(conn, "y" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, y.id, pending.id)
    _present(conn, x.id, retention_drive.id)   # retention_drive already has x
    _scan(conn, retention_drive.id)

    result = reconcile_location(conn, target=retention_drive,
                                source_locations=[pending], now=1000)

    assert result.copies == 1
    actions = action_repo.list_for_run(conn, result.run_id)
    copy = [a for a in actions if a.action_kind == ActionKind.COPY]
    assert len(copy) == 1
    assert copy[0].content_id == y.id
    assert copy[0].source_location_id == pending.id
    assert copy[0].dest_location_id == retention_drive.id
    assert copy[0].acknowledged is False


def test_no_copy_when_target_already_complete(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, x.id, retention_drive.id)
    _scan(conn, retention_drive.id)
    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)
    assert result.copies == 0


def test_copy_deduped_across_multiple_authoritative_locations(conn):
    pending = _pending(conn)
    other = _loc(conn, "other", "Other", is_authoritative_audio=True, is_retention=False)
    retention_drive = _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, x.id, other.id)
    _scan(conn, retention_drive.id)
    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending, other], now=1000)
    assert result.copies == 1   # proposed once, not per source


# ── copy sources (any present location) ───────────────────────────────────────

def test_sources_a_copy_from_a_retention_location(conn):
    # After Pending is pruned, a DAP must still fill from RetentionDrive (retention→retention).
    retention_drive = _rugged(conn)
    dap = _loc(conn, "dap", "DAP", is_retention=True)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, retention_drive.id)
    _scan(conn, dap.id)
    result = reconcile_location(conn, target=dap, source_locations=[retention_drive], now=1000)
    assert result.copies == 1
    copy = [a for a in action_repo.list_for_run(conn, result.run_id)
            if a.action_kind == ActionKind.COPY][0]
    assert copy.source_location_id == retention_drive.id and copy.content_id == x.id


def test_prefers_authoritative_source_when_content_on_both(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)
    dap = _loc(conn, "dap", "DAP", is_retention=True)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, pending.id)   # authoring
    _present(conn, x.id, retention_drive.id)    # and retention
    _scan(conn, dap.id)
    # retention listed first, but the authoring library must win the source
    result = reconcile_location(conn, target=dap,
                               source_locations=[retention_drive, pending], now=1000)
    assert result.copies == 1
    copy = [a for a in action_repo.list_for_run(conn, result.run_id)
            if a.action_kind == ActionKind.COPY][0]
    assert copy.source_location_id == pending.id


def test_never_proposes_copies_into_the_authoring_library(conn):
    # After a prune, reviewing Pending must NOT offer to re-fill it from retention.
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, retention_drive.id)   # on retention; Pending doesn't have it (pruned)
    _scan(conn, pending.id)
    result = reconcile_location(conn, target=pending,
                               source_locations=[retention_drive], now=1000)
    assert result.copies == 0


# ── sidecar copies ────────────────────────────────────────────────────────────

def test_proposes_sidecar_copy_absent_on_target(conn):
    from spindlebot.core.enums import SidecarParentKind, SidecarRole
    from spindlebot.db.repositories import sidecar_repo
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, x.id, retention_drive.id)          # audio already on the target
    _lrc(conn, x.id, pending.id, "lrc-sha")  # but its .lrc only on pending
    _scan(conn, retention_drive.id)

    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)
    actions = action_repo.list_for_run(conn, result.run_id)
    sidecar_copies = [a for a in actions
                      if a.action_kind == ActionKind.COPY and a.content_kind == "sidecar"]
    assert result.copies == 1 and len(sidecar_copies) == 1
    lrc = sidecar_repo.get(conn, parent_kind=SidecarParentKind.TRACK,
                           parent_id=x.id, role=SidecarRole.LRC)
    assert sidecar_copies[0].content_id == lrc.id
    assert sidecar_copies[0].dest_location_id == retention_drive.id


def test_no_sidecar_copy_when_target_already_has_it(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, pending.id, "lrc-sha")
    _lrc(conn, x.id, retention_drive.id, "lrc-sha")   # target already has the sidecar
    _scan(conn, retention_drive.id)
    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)
    assert result.copies == 0


# ── MISSING detection + min_copies ────────────────────────────────────────────

def test_missing_detected_when_target_row_predates_latest_scan(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, x.id, retention_drive.id, observed_utc=100)   # last seen at t=100
    scan_repo.start_scan(conn, retention_drive.id, 500)          # newer scan at t=500

    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)

    assert result.missing == 1
    upd = [a for a in action_repo.list_for_run(conn, result.run_id)
           if a.action_kind == ActionKind.UPDATE_PRESENCE]
    assert len(upd) == 1 and upd[0].content_id == x.id
    # retention_drive was x's only retention copy → dropping it breaches min_copies=1
    assert result.below_floor == 1 and x.id in result.below_floor_ids


def test_missing_not_flagged_below_floor_when_another_retention_copy_exists(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)
    dap = _loc(conn, "dap", "DAP", is_retention=True)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, retention_drive.id, observed_utc=100)
    _present(conn, x.id, dap.id, observed_utc=100)   # second retention copy
    scan_repo.start_scan(conn, retention_drive.id, 500)

    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)
    assert result.missing == 1
    assert result.below_floor == 0   # dap still holds a copy


def test_recently_confirmed_rows_are_not_missing(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, retention_drive.id, observed_utc=500)  # seen at/after scan start
    scan_repo.start_scan(conn, retention_drive.id, 500)
    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)
    assert result.missing == 0


def test_unscanned_target_skips_all_planning(conn):
    # Without an inventory of the target, propose nothing — a never-scanned drive
    # full of audio must not yield a spurious whole-library copy plan.
    pending, retention_drive = _pending(conn), _rugged(conn)
    x, y = _audio(conn, "x" * 32), _audio(conn, "y" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, y.id, pending.id)
    _present(conn, x.id, retention_drive.id, observed_utc=100)   # stale, but no scan exists

    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)

    assert result.target_scanned is False
    assert result.copies == 0 and result.missing == 0 and result.conflicts == 0
    assert action_repo.list_for_run(conn, result.run_id) == []
    run = run_repo.get(conn, result.run_id)
    assert run.status == ScanStatus.OK and "never been inventoried" in (run.note or "")


# ── run bookkeeping ───────────────────────────────────────────────────────────

def test_reconcile_records_a_finished_run(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)
    _scan(conn, retention_drive.id)
    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)
    run = run_repo.get(conn, result.run_id)
    assert run.kind == RunKind.RECONCILE
    assert run.status == ScanStatus.OK
    assert run.finished_utc == 1000
    assert run.location_id == retention_drive.id


# ── lyric divergence detection ────────────────────────────────────────────────

def _lrc(conn, audio_id, loc_id, file_sha, *, observed_utc=0):
    """Attach the track's .lrc sidecar present at a location with a per-copy sha."""
    sc = sidecar_repo.upsert(conn, parent_kind=SidecarParentKind.TRACK,
                             parent_id=audio_id, role=SidecarRole.LRC,
                             sha256="canon", now=0)
    sidecar_presence_repo.set_presence(conn, sidecar_id=sc.id, location_id=loc_id,
                                       present=True, observed_utc=observed_utc,
                                       file_sha256=file_sha, rel_path=f"{audio_id}.lrc")
    return sc


def test_divergent_lyrics_across_locations_flag_a_conflict(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, x.id, retention_drive.id)
    _lrc(conn, x.id, pending.id, "sha-AAA")
    _lrc(conn, x.id, retention_drive.id, "sha-BBB")   # same track, different lyric content
    _scan(conn, retention_drive.id)

    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)

    assert result.conflicts == 1
    c = conflict_repo.find_open_for_audio(conn, x.id)
    assert c is not None
    actions = [a for a in action_repo.list_for_run(conn, result.run_id)
               if a.action_kind == ActionKind.RESOLVE_CONFLICT]
    assert len(actions) == 1 and actions[0].content_id == x.id
    # the two sides were recorded as concurrent versions (no auto-winner)
    doc = lyric_repo.get_doc(conn, x.id)
    versions = lyric_repo.list_versions(conn, doc.id)
    assert len(versions) == 2
    assert vclock.concurrent(vclock.from_json(versions[0].vclock_json),
                             vclock.from_json(versions[1].vclock_json))


def test_conflict_flagged_once_across_multiple_diverging_sources(conn):
    # With generalized sources, a track diverging from two sources must still be
    # one conflict + one resolve action, not double-counted.
    pending, retention_drive = _pending(conn), _rugged(conn)
    dap = _loc(conn, "dap", "DAP", is_retention=True)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, dap.id, "sha-TTT")      # target
    _lrc(conn, x.id, pending.id, "sha-AAA")  # source 1 diverges
    _lrc(conn, x.id, retention_drive.id, "sha-BBB")   # source 2 diverges
    _scan(conn, dap.id)
    result = reconcile_location(conn, target=dap,
                               source_locations=[pending, retention_drive], now=1000)
    assert result.conflicts == 1
    actions = [a for a in action_repo.list_for_run(conn, result.run_id)
               if a.action_kind == ActionKind.RESOLVE_CONFLICT]
    assert len(actions) == 1


def test_identical_lyrics_are_not_a_conflict(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, pending.id, "same-sha")
    _lrc(conn, x.id, retention_drive.id, "same-sha")
    _scan(conn, retention_drive.id)
    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)
    assert result.conflicts == 0
    assert conflict_repo.list_open(conn) == []


def test_lyric_on_one_side_only_is_not_a_conflict(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, pending.id, "sha-AAA")   # retention_drive has no .lrc for x
    _scan(conn, retention_drive.id)
    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)
    assert result.conflicts == 0


def test_conflict_row_deduped_but_action_reproposed_each_run(conn):
    # The conflict ROW is opened once, but the resolve_conflict ACTION is
    # re-proposed every run (like COPY/MISSING) so acknowledging the LATEST run
    # always covers it — otherwise the action would be stranded in run 1.
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, pending.id, "sha-AAA")
    _lrc(conn, x.id, retention_drive.id, "sha-BBB")
    _scan(conn, retention_drive.id)

    first = reconcile_location(conn, target=retention_drive,
                              source_locations=[pending], now=1000)
    second = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=2000)

    assert first.conflicts == 1 and second.conflicts == 1   # re-proposed
    assert len(conflict_repo.list_open(conn)) == 1          # one conflict row
    doc = lyric_repo.get_doc(conn, x.id)
    assert len(lyric_repo.list_versions(conn, doc.id)) == 2  # no duplicate versions
    # the latest run carries its own acknowledgeable resolve_conflict action
    latest = [a for a in action_repo.list_for_run(conn, second.run_id)
              if a.action_kind == ActionKind.RESOLVE_CONFLICT]
    assert len(latest) == 1 and latest[0].content_id == x.id


def test_linear_edit_advances_head_without_a_conflict(conn):
    # A location improving a lyric (newer bytes off the current head) fast-forwards
    # the head and is NOT a conflict — 4.1 will propagate it to the behind side.
    from spindlebot.db.repositories import lyric_version_presence_repo
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, pending.id, "sha-1")
    _lrc(conn, x.id, retention_drive.id, "sha-1")   # agree first
    _scan(conn, retention_drive.id)
    first = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)
    assert first.conflicts == 0

    _lrc(conn, x.id, pending.id, "sha-2")  # pending improves the lyric
    second = reconcile_location(conn, target=retention_drive,
                                source_locations=[pending], now=2000)

    assert second.conflicts == 0
    assert conflict_repo.list_open(conn) == []
    doc = lyric_repo.get_doc(conn, x.id)
    versions = lyric_repo.list_versions(conn, doc.id)
    assert len(versions) == 2
    head = lyric_repo.head_version(conn, doc.id)
    old = next(v for v in versions if v.id != head.id)
    assert vclock.strictly_dominates(vclock.from_json(head.vclock_json),
                                     vclock.from_json(old.vclock_json))
    # pending now holds the new head; retention_drive is behind (still the old version)
    assert lyric_version_presence_repo.get(conn, doc.id, pending.id).version_id == head.id
    assert lyric_version_presence_repo.get(conn, doc.id, retention_drive.id).version_id == old.id


def test_behind_location_is_not_a_conflict(conn):
    # retention_drive (target) holds an OLD version while the head advanced on pending.
    from spindlebot.db.repositories import lyric_version_presence_repo
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, pending.id, "sha-1")
    _lrc(conn, x.id, retention_drive.id, "sha-1")
    _scan(conn, retention_drive.id)
    reconcile_location(conn, target=retention_drive, source_locations=[pending], now=1000)
    _lrc(conn, x.id, pending.id, "sha-2")  # head advances on pending
    result = reconcile_location(conn, target=retention_drive,
                                source_locations=[pending], now=2000)

    assert result.conflicts == 0
    doc = lyric_repo.get_doc(conn, x.id)
    head = lyric_repo.head_version(conn, doc.id)
    # retention_drive's held version is strictly older than the head → behind, not conflict
    rugged_v = lyric_version_presence_repo.get(conn, doc.id, retention_drive.id)
    assert rugged_v.version_id != head.id


def test_version_presence_records_source_scan_time_not_reconcile_now(conn):
    # A source location's .lrc was last confirmed by a scan long before this
    # reconcile runs. Its lyric_version_presence.observed_utc must reflect that
    # real scan time (from sidecar_presence), not the reconcile `now` — otherwise
    # a cached, stale copy would look freshly confirmed.
    from spindlebot.db.repositories import lyric_version_presence_repo
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, retention_drive.id, "sha-1", observed_utc=2000)  # target, fresh
    _lrc(conn, x.id, pending.id, "sha-1", observed_utc=100)  # source, long stale
    _scan(conn, retention_drive.id)
    reconcile_location(conn, target=retention_drive, source_locations=[pending], now=2000)

    doc = lyric_repo.get_doc(conn, x.id)
    assert lyric_version_presence_repo.get(conn, doc.id, retention_drive.id).observed_utc == 2000
    assert lyric_version_presence_repo.get(conn, doc.id, pending.id).observed_utc == 100


def test_conflict_not_proposed_for_target_not_party_to_divergence(conn):
    # pending<->retention_drive diverge; dap (the target) agrees with the head side. The
    # dap review must NOT attach that conflict to itself — it isn't a party.
    pending, retention_drive = _pending(conn), _rugged(conn)
    dap = _loc(conn, "dap", "DAP", is_retention=True)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, pending.id, "sha-AAA")   # becomes head (lowest location id)
    _lrc(conn, x.id, retention_drive.id, "sha-BBB")    # concurrent with head
    _lrc(conn, x.id, dap.id, "sha-AAA")       # dap holds the head version
    _scan(conn, dap.id)

    result = reconcile_location(conn, target=dap,
                                source_locations=[pending, retention_drive], now=1000)

    assert result.conflicts == 0
    actions = [a for a in action_repo.list_for_run(conn, result.run_id)
               if a.action_kind == ActionKind.RESOLVE_CONFLICT]
    assert actions == []
    # no conflict row opened during a review dap isn't party to
    assert conflict_repo.find_open_for_audio(conn, x.id) is None


def test_resolve_action_names_head_and_concurrent_sides(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)   # pending id < retention_drive id
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, pending.id, "sha-AAA")   # head side (winner)
    _lrc(conn, x.id, retention_drive.id, "sha-BBB")    # target holds the concurrent version
    _scan(conn, retention_drive.id)
    result = reconcile_location(conn, target=retention_drive,
                                source_locations=[pending], now=1000)
    action = next(a for a in action_repo.list_for_run(conn, result.run_id)
                  if a.action_kind == ActionKind.RESOLVE_CONFLICT)
    # source = a head-holder (winner), dest = the target (concurrent/loser side)
    assert action.source_location_id == pending.id
    assert action.dest_location_id == retention_drive.id
    assert "head on Pending" in action.reason
    assert "concurrent on RetentionDrive" in action.reason


def test_run_note_records_below_floor(conn):
    pending, retention_drive = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, retention_drive.id, observed_utc=100)
    scan_repo.start_scan(conn, retention_drive.id, 500)
    result = reconcile_location(conn, target=retention_drive,
                               source_locations=[pending], now=1000)
    run = run_repo.get(conn, result.run_id)
    assert "below min_copies" in (run.note or "")
