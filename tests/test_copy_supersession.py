"""Stricter COPY approvals retire weaker writers without erasing approval history."""
from __future__ import annotations

from dataclasses import replace

import pytest

from spindlebot.core.enums import ActionKind, ContentKind, RunKind
from spindlebot.db import migrations
from spindlebot.db.connection import connect, open_db
from spindlebot.db.repositories import action_repo, location_repo, run_repo


def _copy_plan(conn, target, *, strict=False, path="Album/track.flac", kind=ContentKind.AUDIO,
               content_id=1):
    run = run_repo.start_run(conn, RunKind.RECONCILE, now=100)
    return action_repo.add(
        conn, run_id=run, action_kind=ActionKind.COPY, content_kind=kind,
        content_id=content_id, dest_location_id=target.id, rel_path=path,
        source_rel_path="Source/track.flac", expected_sha256="planned-sha",
        dest_must_be_absent=strict, now=100,
    )


@pytest.fixture
def queue(tmp_path):
    conn = open_db(tmp_path / "queue.db")
    target = location_repo.upsert(conn, uuid="target", name="RetentionDrive", kind="local_drive")
    try:
        yield conn, target
    finally:
        conn.close()


def test_supersession_is_transactional_and_dry_planning_does_not_retire(queue):
    conn, target = queue
    old = _copy_plan(conn, target)
    action_repo.acknowledge(conn, [old.id], now=150)
    approved = action_repo.get(conn, old.id)
    strict = _copy_plan(conn, target, strict=True)
    conn.commit()
    assert action_repo.list_pending_execution(conn) == [approved]
    action_repo.acknowledge_run(conn, strict.run_id, now=200)
    assert [a.id for a in action_repo.list_pending_execution(conn)] == [strict.id]
    conn.rollback()
    assert action_repo.get(conn, old.id) == approved
    assert not action_repo.get(conn, strict.id).acknowledged
    assert action_repo.list_pending_execution(conn) == [approved]


def test_only_newer_approved_restorations_retire_writers_to_the_same_path(queue):
    conn, target = queue
    other = location_repo.upsert(conn, uuid="other", name="PortablePlayer", kind="local_drive")
    completed = _copy_plan(conn, target)
    action_repo.acknowledge(conn, [completed.id], now=101)
    action_repo.mark_executed(conn, completed.id, now=102)
    completed = action_repo.get(conn, completed.id)
    old = _copy_plan(conn, target)
    other_path = _copy_plan(conn, target, path="Album/other.flac")
    other_target = _copy_plan(conn, other)
    for action in (old, other_path, other_target):
        action_repo.acknowledge(conn, [action.id], now=150)
    # Physical path safety applies even when the newer plan names a different
    # content identity or kind; both actions would write the same file.
    strict = _copy_plan(conn, target, strict=True, kind=ContentKind.SIDECAR, content_id=99)
    action_repo.acknowledge(conn, [strict.id], now=200)
    retired = action_repo.get(conn, old.id)
    assert retired.superseded_by_action_id == strict.id and retired.superseded_utc == 200
    assert action_repo.get(conn, completed.id) == completed
    assert action_repo.get(conn, other_path.id).superseded_utc is None
    assert action_repo.get(conn, other_target.id).superseded_utc is None
    # A genuinely later generic proposal is not an old writer and is retained.
    later = _copy_plan(conn, target)
    action_repo.acknowledge(conn, [later.id], now=200)
    assert action_repo.get(conn, later.id).superseded_utc is None
    # Re-acknowledgement must neither reactivate nor retimestamp a retired row.
    assert action_repo.acknowledge(conn, [old.id, strict.id], now=400) == 0
    assert action_repo.get(conn, old.id) == retired


def test_an_old_draft_acknowledged_later_is_a_new_approval(queue):
    conn, target = queue
    draft = _copy_plan(conn, target)
    strict = _copy_plan(conn, target, strict=True)
    action_repo.acknowledge(conn, [strict.id], now=200)
    action_repo.acknowledge(conn, [draft.id], now=300)
    approved_draft = action_repo.get(conn, draft.id)
    assert approved_draft.superseded_utc is None
    assert approved_draft.approval_order > action_repo.get(conn, strict.id).approval_order
    assert [a.id for a in action_repo.list_pending_execution(conn)] == [draft.id, strict.id]


def test_normalization_respects_executor_destination_scope(queue):
    conn, target = queue
    other = location_repo.upsert(conn, uuid="other", name="PortablePlayer", kind="local_drive")
    plans = []
    for loc in (target, other):
        plans.append((_copy_plan(conn, loc), _copy_plan(conn, loc, strict=True)))
    # Reproduce two legacy mixed queues without the new acknowledgement hook.
    conn.execute("UPDATE pending_action SET acknowledged=1, acknowledged_utc=150")
    old_target, strict_target = plans[0]
    old_other, _ = plans[1]
    assert action_repo.supersede_weaker_copies(conn, now=200, dest_location_id=target.id) == 1
    assert action_repo.get(conn, old_target.id).superseded_by_action_id == strict_target.id
    assert action_repo.get(conn, old_other.id).superseded_utc is None


def test_v11_preserves_legacy_plans_and_history_purge_cannot_reactivate_them(tmp_path):
    import sqlite3
    db = tmp_path / "upgrade.db"
    conn = connect(db)
    for version, filename in migrations.MIGRATIONS:
        if version > 10:
            break
        conn.executescript((migrations._SCHEMA_DIR / filename).read_text())
        conn.execute(f"PRAGMA user_version={version}")
    target = location_repo.upsert(conn, uuid="target", name="RetentionDrive", kind="local_drive")
    # Seed the shipped v10 columns directly; current repository writes use
    # additional v11 metadata that deliberately does not exist yet.
    legacy_ids = []
    for strict_mode in (False, True):
        run_id = run_repo.start_run(conn, RunKind.RECONCILE, now=100)
        cur = conn.execute(
            "INSERT INTO pending_action (run_id,action_kind,content_kind,content_id,"
            "dest_location_id,rel_path,source_rel_path,expected_sha256,"
            "dest_must_be_absent,created_utc,acknowledged,acknowledged_utc) "
            "VALUES (?,'copy','audio',1,?,'Album/track.flac','Source/track.flac',"
            "'planned-sha',?,100,1,150)", (run_id, target.id, int(strict_mode)),
        )
        legacy_ids.append(cur.lastrowid)
    old, strict = [action_repo.get(conn, action_id) for action_id in legacy_ids]
    original = old
    conn.commit()
    conn.close()
    conn = open_db(db)
    try:
        assert migrations.current_version(conn) == 11
        original = replace(original, approval_order=0)
        assert action_repo.get(conn, old.id) == original
        assert migrations.migrate(conn) == 11
        assert action_repo.supersede_weaker_copies(conn, now=200) == 1
        retired = action_repo.get(conn, old.id)
        assert retired == replace(original, superseded_utc=200, superseded_by_action_id=strict.id)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE pending_action SET superseded_by_action_id=999 WHERE id=?", (old.id,))
        # FK clears the reference, while the durable timestamp keeps retirement.
        conn.execute("DELETE FROM run WHERE id=?", (strict.run_id,))
        assert action_repo.get(conn, old.id) == replace(retired, superseded_by_action_id=None)
        assert action_repo.list_pending_execution(conn) == []
        assert action_repo.acknowledge(conn, [old.id], now=300) == 0
        conn.commit()
    finally:
        conn.close()
    reopened = open_db(db)
    try:
        assert migrations.current_version(reopened) == 11
        assert action_repo.get(reopened, old.id).superseded_utc == 200
        assert action_repo.list_pending_execution(reopened) == []
    finally:
        reopened.close()


def test_a_batch_approval_favors_stricter_mode_even_with_earlier_creation(queue):
    conn, target = queue
    strict = _copy_plan(conn, target, strict=True)
    generic = _copy_plan(conn, target)
    action_repo.acknowledge(conn, [strict.id, generic.id], now=200)
    assert [a.id for a in action_repo.list_pending_execution(conn)] == [strict.id]
    assert action_repo.get(conn, generic.id).superseded_by_action_id == strict.id
    assert action_repo.get(conn, generic.id).approval_order == action_repo.get(conn, strict.id).approval_order


def test_legacy_unknown_approval_order_favors_stricter_mode(queue):
    conn, target = queue
    strict = _copy_plan(conn, target, strict=True)
    _copy_plan(conn, target)
    conn.execute("UPDATE pending_action SET acknowledged=1")
    action_repo.supersede_weaker_copies(conn, now=300)
    assert [a.id for a in action_repo.list_pending_execution(conn)] == [strict.id]
