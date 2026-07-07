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
    return _loc(conn, "rugged", "DwRugged", is_retention=True)


# ── COPY planning ─────────────────────────────────────────────────────────────

def test_proposes_copy_for_authoritative_content_absent_on_target(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    x, y = _audio(conn, "x" * 32), _audio(conn, "y" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, y.id, pending.id)
    _present(conn, x.id, rugged.id)   # rugged already has x
    _scan(conn, rugged.id)

    result = reconcile_location(conn, target=rugged,
                                authoritative_locations=[pending], now=1000)

    assert result.copies == 1
    actions = action_repo.list_for_run(conn, result.run_id)
    copy = [a for a in actions if a.action_kind == ActionKind.COPY]
    assert len(copy) == 1
    assert copy[0].content_id == y.id
    assert copy[0].source_location_id == pending.id
    assert copy[0].dest_location_id == rugged.id
    assert copy[0].acknowledged is False


def test_no_copy_when_target_already_complete(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, x.id, rugged.id)
    _scan(conn, rugged.id)
    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)
    assert result.copies == 0


def test_copy_deduped_across_multiple_authoritative_locations(conn):
    pending = _pending(conn)
    other = _loc(conn, "other", "Other", is_authoritative_audio=True, is_retention=False)
    rugged = _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, x.id, other.id)
    _scan(conn, rugged.id)
    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending, other], now=1000)
    assert result.copies == 1   # proposed once, not per source


# ── sidecar copies ────────────────────────────────────────────────────────────

def test_proposes_sidecar_copy_absent_on_target(conn):
    from spindlebot.core.enums import SidecarParentKind, SidecarRole
    from spindlebot.db.repositories import sidecar_repo
    pending, rugged = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, x.id, rugged.id)          # audio already on the target
    _lrc(conn, x.id, pending.id, "lrc-sha")  # but its .lrc only on pending
    _scan(conn, rugged.id)

    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)
    actions = action_repo.list_for_run(conn, result.run_id)
    sidecar_copies = [a for a in actions
                      if a.action_kind == ActionKind.COPY and a.content_kind == "sidecar"]
    assert result.copies == 1 and len(sidecar_copies) == 1
    lrc = sidecar_repo.get(conn, parent_kind=SidecarParentKind.TRACK,
                           parent_id=x.id, role=SidecarRole.LRC)
    assert sidecar_copies[0].content_id == lrc.id
    assert sidecar_copies[0].dest_location_id == rugged.id


def test_no_sidecar_copy_when_target_already_has_it(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, pending.id, "lrc-sha")
    _lrc(conn, x.id, rugged.id, "lrc-sha")   # target already has the sidecar
    _scan(conn, rugged.id)
    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)
    assert result.copies == 0


# ── MISSING detection + min_copies ────────────────────────────────────────────

def test_missing_detected_when_target_row_predates_latest_scan(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, x.id, rugged.id, observed_utc=100)   # last seen at t=100
    scan_repo.start_scan(conn, rugged.id, 500)          # newer scan at t=500

    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)

    assert result.missing == 1
    upd = [a for a in action_repo.list_for_run(conn, result.run_id)
           if a.action_kind == ActionKind.UPDATE_PRESENCE]
    assert len(upd) == 1 and upd[0].content_id == x.id
    # rugged was x's only retention copy → dropping it breaches min_copies=1
    assert result.below_floor == 1 and x.id in result.below_floor_ids


def test_missing_not_flagged_below_floor_when_another_retention_copy_exists(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    dap = _loc(conn, "dap", "DAP", is_retention=True)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, rugged.id, observed_utc=100)
    _present(conn, x.id, dap.id, observed_utc=100)   # second retention copy
    scan_repo.start_scan(conn, rugged.id, 500)

    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)
    assert result.missing == 1
    assert result.below_floor == 0   # dap still holds a copy


def test_recently_confirmed_rows_are_not_missing(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, rugged.id, observed_utc=500)  # seen at/after scan start
    scan_repo.start_scan(conn, rugged.id, 500)
    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)
    assert result.missing == 0


def test_unscanned_target_skips_all_planning(conn):
    # Without an inventory of the target, propose nothing — a never-scanned drive
    # full of audio must not yield a spurious whole-library copy plan.
    pending, rugged = _pending(conn), _rugged(conn)
    x, y = _audio(conn, "x" * 32), _audio(conn, "y" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, y.id, pending.id)
    _present(conn, x.id, rugged.id, observed_utc=100)   # stale, but no scan exists

    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)

    assert result.target_scanned is False
    assert result.copies == 0 and result.missing == 0 and result.conflicts == 0
    assert action_repo.list_for_run(conn, result.run_id) == []
    run = run_repo.get(conn, result.run_id)
    assert run.status == ScanStatus.OK and "never been inventoried" in (run.note or "")


# ── run bookkeeping ───────────────────────────────────────────────────────────

def test_reconcile_records_a_finished_run(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    _scan(conn, rugged.id)
    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)
    run = run_repo.get(conn, result.run_id)
    assert run.kind == RunKind.RECONCILE
    assert run.status == ScanStatus.OK
    assert run.finished_utc == 1000
    assert run.location_id == rugged.id


# ── lyric divergence detection ────────────────────────────────────────────────

def _lrc(conn, audio_id, loc_id, file_sha):
    """Attach the track's .lrc sidecar present at a location with a per-copy sha."""
    sc = sidecar_repo.upsert(conn, parent_kind=SidecarParentKind.TRACK,
                             parent_id=audio_id, role=SidecarRole.LRC,
                             sha256="canon", now=0)
    sidecar_presence_repo.set_presence(conn, sidecar_id=sc.id, location_id=loc_id,
                                       present=True, observed_utc=0,
                                       file_sha256=file_sha, rel_path=f"{audio_id}.lrc")
    return sc


def test_divergent_lyrics_across_locations_flag_a_conflict(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, pending.id)
    _present(conn, x.id, rugged.id)
    _lrc(conn, x.id, pending.id, "sha-AAA")
    _lrc(conn, x.id, rugged.id, "sha-BBB")   # same track, different lyric content
    _scan(conn, rugged.id)

    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)

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


def test_identical_lyrics_are_not_a_conflict(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, pending.id, "same-sha")
    _lrc(conn, x.id, rugged.id, "same-sha")
    _scan(conn, rugged.id)
    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)
    assert result.conflicts == 0
    assert conflict_repo.list_open(conn) == []


def test_lyric_on_one_side_only_is_not_a_conflict(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, pending.id, "sha-AAA")   # rugged has no .lrc for x
    _scan(conn, rugged.id)
    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)
    assert result.conflicts == 0


def test_conflict_row_deduped_but_action_reproposed_each_run(conn):
    # The conflict ROW is opened once, but the resolve_conflict ACTION is
    # re-proposed every run (like COPY/MISSING) so acknowledging the LATEST run
    # always covers it — otherwise the action would be stranded in run 1.
    pending, rugged = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _lrc(conn, x.id, pending.id, "sha-AAA")
    _lrc(conn, x.id, rugged.id, "sha-BBB")
    _scan(conn, rugged.id)

    first = reconcile_location(conn, target=rugged,
                              authoritative_locations=[pending], now=1000)
    second = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=2000)

    assert first.conflicts == 1 and second.conflicts == 1   # re-proposed
    assert len(conflict_repo.list_open(conn)) == 1          # one conflict row
    doc = lyric_repo.get_doc(conn, x.id)
    assert len(lyric_repo.list_versions(conn, doc.id)) == 2  # no duplicate versions
    # the latest run carries its own acknowledgeable resolve_conflict action
    latest = [a for a in action_repo.list_for_run(conn, second.run_id)
              if a.action_kind == ActionKind.RESOLVE_CONFLICT]
    assert len(latest) == 1 and latest[0].content_id == x.id


def test_run_note_records_below_floor(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, rugged.id, observed_utc=100)
    scan_repo.start_scan(conn, rugged.id, 500)
    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)
    run = run_repo.get(conn, result.run_id)
    assert "below min_copies" in (run.note or "")
