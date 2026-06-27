"""Tests for the reconciler planner (services/reconciler.py). No bytes touched."""
from __future__ import annotations

import pytest

from spindlebot.core.enums import ActionKind, RunKind, ScanStatus
from spindlebot.core.identity import ContentId
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    action_repo,
    audio_repo,
    location_repo,
    presence_repo,
    run_repo,
    scan_repo,
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
    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending, other], now=1000)
    assert result.copies == 1   # proposed once, not per source


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


def test_missing_skipped_without_a_scan(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, rugged.id, observed_utc=100)   # stale, but no scan exists
    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)
    assert result.missing == 0


# ── run bookkeeping ───────────────────────────────────────────────────────────

def test_reconcile_records_a_finished_run(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)
    run = run_repo.get(conn, result.run_id)
    assert run.kind == RunKind.RECONCILE
    assert run.status == ScanStatus.OK
    assert run.finished_utc == 1000
    assert run.location_id == rugged.id


def test_run_note_records_below_floor(conn):
    pending, rugged = _pending(conn), _rugged(conn)
    x = _audio(conn, "x" * 32)
    _present(conn, x.id, rugged.id, observed_utc=100)
    scan_repo.start_scan(conn, rugged.id, 500)
    result = reconcile_location(conn, target=rugged,
                               authoritative_locations=[pending], now=1000)
    run = run_repo.get(conn, result.run_id)
    assert "below min_copies" in (run.note or "")
