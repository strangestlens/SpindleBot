"""Tests for the run + pending_action repositories."""
from __future__ import annotations

import pytest

from spindlebot.core.enums import ActionKind, ContentKind, RunKind, ScanStatus
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import action_repo, location_repo, run_repo


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


# ── run_repo ──────────────────────────────────────────────────────────────────

def test_start_and_finish_run(conn):
    rid = run_repo.start_run(conn, RunKind.RECONCILE, now=1000, note="retention-drive-note")
    r = run_repo.get(conn, rid)
    assert r.kind == RunKind.RECONCILE
    assert r.status == ScanStatus.RUNNING
    assert r.finished_utc is None and r.note == "retention-drive-note"

    run_repo.finish_run(conn, rid, status=ScanStatus.OK, now=2000)
    done = run_repo.get(conn, rid)
    assert done.status == ScanStatus.OK
    assert done.finished_utc == 2000
    assert done.note == "retention-drive-note"   # COALESCE preserved the note


def test_latest_filters_by_kind(conn):
    run_repo.start_run(conn, RunKind.INVENTORY, now=1)
    rec = run_repo.start_run(conn, RunKind.RECONCILE, now=2)
    assert run_repo.latest(conn).id == rec
    assert run_repo.latest(conn, RunKind.INVENTORY).kind == RunKind.INVENTORY
    assert run_repo.latest(conn, RunKind.SYNC) is None


def test_run_kind_validated(conn):
    with pytest.raises(ValueError):
        run_repo.start_run(conn, "frobnicate", now=1)


# ── action_repo ───────────────────────────────────────────────────────────────

def _run(conn):
    return run_repo.start_run(conn, RunKind.RECONCILE, now=1000)


def test_add_action_is_proposed_not_acknowledged(conn):
    rid = _run(conn)
    dest = location_repo.upsert(conn, uuid="rug", name="RetentionDrive",
                                kind="local_drive", is_retention=True)
    a = action_repo.add(conn, run_id=rid, action_kind=ActionKind.COPY,
                        content_kind=ContentKind.AUDIO, content_id=7, now=1000,
                        dest_location_id=dest.id, reason="missing on retention")
    assert a.id > 0
    assert a.action_kind == ActionKind.COPY
    assert a.content_kind == ContentKind.AUDIO and a.content_id == 7
    assert a.acknowledged is False
    assert a.executed_utc is None
    assert a.reason == "missing on retention"


def test_list_for_run_filters(conn):
    rid = _run(conn)
    action_repo.add(conn, run_id=rid, action_kind=ActionKind.COPY,
                    content_kind=ContentKind.AUDIO, content_id=1, now=1)
    action_repo.add(conn, run_id=rid, action_kind=ActionKind.UPDATE_PRESENCE,
                    content_kind=ContentKind.AUDIO, content_id=2, now=1)
    assert len(action_repo.list_for_run(conn, rid)) == 2
    assert len(action_repo.list_for_run(conn, rid, acknowledged=False)) == 2
    assert len(action_repo.list_for_run(conn, rid, executed=False)) == 2
    assert len(action_repo.list_for_run(conn, rid, executed=True)) == 0


def test_acknowledge_specific_items_is_idempotent(conn):
    rid = _run(conn)
    a = action_repo.add(conn, run_id=rid, action_kind=ActionKind.COPY,
                        content_kind=ContentKind.AUDIO, content_id=1, now=1)
    b = action_repo.add(conn, run_id=rid, action_kind=ActionKind.COPY,
                        content_kind=ContentKind.AUDIO, content_id=2, now=1)
    assert action_repo.acknowledge(conn, [a.id], now=50) == 1
    assert action_repo.acknowledge(conn, [a.id], now=60) == 0  # already acked
    acked = action_repo.get(conn, a.id)
    assert acked.acknowledged is True and acked.acknowledged_utc == 50
    assert action_repo.get(conn, b.id).acknowledged is False
    assert action_repo.acknowledge(conn, [], now=70) == 0      # empty is a no-op


def test_acknowledge_run_acks_all_outstanding(conn):
    rid = _run(conn)
    a = action_repo.add(conn, run_id=rid, action_kind=ActionKind.COPY,
                        content_kind=ContentKind.AUDIO, content_id=1, now=1)
    action_repo.add(conn, run_id=rid, action_kind=ActionKind.COPY,
                    content_kind=ContentKind.AUDIO, content_id=2, now=1)
    action_repo.acknowledge(conn, [a.id], now=10)
    # only the one still-outstanding action flips
    assert action_repo.acknowledge_run(conn, rid, now=20) == 1
    assert all(x.acknowledged for x in action_repo.list_for_run(conn, rid))
