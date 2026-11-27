"""Retry acknowledged COPY work without replanning equivalent transfers."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import mutagen.flac
import pytest

from spindlebot.core.enums import ActionKind
from spindlebot.core.identity import file_sha256
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import action_repo
from spindlebot.services.inventory import inventory_location
from spindlebot.services.push import push_location
from spindlebot.services.reconciler import reconcile_location
from tests.test_push import REL, _copy, _loc, _seed


@pytest.fixture
def library(tmp_path):
    conn = open_db(tmp_path / "library.db")
    source, target = _seed(conn, tmp_path)
    audio = Path(source.root_path) / REL
    audio.with_suffix(".lrc").write_text("[00:01.00]Lyrics\n")
    audio.with_suffix(".nolrc").write_bytes(b"")
    (audio.parent / "cover.jpg").write_bytes(b"cover")
    for loc in (source, target):
        inventory_location(conn, location=loc, root=loc.root_path, now=100)
    conn.commit()
    try:
        yield conn, source, target
    finally:
        conn.close()


def _push(conn, target, **kwargs):
    return push_location(conn, location=target, root=target.root_path,
                         checkpoint=conn.commit, **kwargs)


def _fail_copy(src, dst):
    raise OSError("temporary disconnection")


def _pending(conn, target):
    return action_repo.list_pending_execution(
        conn, action_kind=ActionKind.COPY, dest_location_id=target.id)


def _make_restoration(conn, target):
    first = _push(conn, target, copy_fn=_copy, now=200)
    assert first.sync.copied == 4
    for action in action_repo.list_for_run(conn, first.run_id):
        (Path(target.root_path) / action.rel_path).unlink()


@pytest.mark.parametrize("restoration", [False, True])
@pytest.mark.parametrize("completed", [0, 1])
def test_interrupted_push_reuses_its_durable_queue(library, restoration, completed):
    conn, source, target = library
    if restoration:
        _make_restoration(conn, target)
    copied_before_interrupt = []

    def interrupt(src, dst):
        if len(copied_before_interrupt) == completed:
            raise KeyboardInterrupt("device disconnected")
        _copy(src, dst)
        copied_before_interrupt.append(src)

    with pytest.raises(KeyboardInterrupt):
        _push(conn, target, copy_fn=interrupt, now=300)
    queued = _pending(conn, target)
    assert len(queued) == 4 - completed
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()
    # Model a separate process retry, including durable acknowledgements and
    # any completed file/presence checkpoints from the interrupted process.
    resumed = open_db(db_path)
    try:
        copies = []

        def record(src, dst):
            copies.append(src)
            _copy(src, dst)

        retry = _push(resumed, target, copy_fn=record, now=400)
        assert retry.reconcile.copies == retry.acknowledged == 0
        assert action_repo.list_for_run(resumed, retry.run_id) == []
        assert retry.sync.copied == len(queued) and retry.ok
        assert len(copies) == len(queued)
        assert _pending(resumed, target) == []
        for old in queued:
            assert action_repo.get(resumed, old.id) == replace(old, executed_utc=400)
            assert file_sha256(Path(target.root_path) / old.rel_path) == old.expected_sha256
        assert (Path(source.root_path) / REL).is_file()  # push never prunes
    finally:
        resumed.close()


def test_preview_does_not_replan_already_acknowledged_copies(library):
    conn, _, target = library
    first = _push(conn, target, copy_fn=_fail_copy, now=200)
    assert first.sync.failed == 4
    queued = _pending(conn, target)
    preview = _push(conn, target, dry_run=True, now=300)
    assert preview.reconcile.copies == 0 and preview.sync is None
    assert action_repo.list_for_run(conn, preview.run_id) == []
    assert _pending(conn, target) == queued


def test_unacknowledged_preview_does_not_block_a_fresh_plan(library):
    conn, _, target = library
    preview = _push(conn, target, dry_run=True, now=200)
    old = action_repo.list_for_run(conn, preview.run_id)
    result = _push(conn, target, copy_fn=_copy, now=300)
    assert result.reconcile.copies == result.acknowledged == result.sync.copied == 4
    assert all(action_repo.get(conn, action.id) == action for action in old)


def test_an_executed_restoration_does_not_block_a_later_loss(library):
    conn, _, target = library
    _make_restoration(conn, target)
    first = _push(conn, target, copy_fn=_copy, now=300)
    assert first.sync.copied == 4
    old = action_repo.list_for_run(conn, first.run_id)
    for action in old:
        (Path(target.root_path) / action.rel_path).unlink()
    second = _push(conn, target, copy_fn=_copy, now=400)
    assert second.reconcile.copies == second.sync.copied == 4
    assert all(action_repo.get(conn, action.id) == action for action in old)


def test_another_destinations_queue_does_not_suppress_copies(library, tmp_path):
    conn, source, target = library
    other = _loc(conn, "OtherRetention", tmp_path / "OtherRetention", is_retention=True)
    inventory_location(conn, location=other, root=other.root_path, now=150)
    plan = reconcile_location(conn, target=other, source_locations=[source], now=200)
    action_repo.acknowledge_run(conn, plan.run_id, now=200)
    old = _pending(conn, other)
    result = _push(conn, target, copy_fn=_copy, now=300)
    assert result.reconcile.copies == result.sync.copied == 4
    assert _pending(conn, other) == old
    assert all(not (Path(other.root_path) / action.rel_path).exists() for action in old)


@pytest.mark.parametrize("name", ["01.flac", "01.lrc", "01.nolrc", "cover.jpg"])
def test_changed_source_bytes_get_a_fresh_plan_without_rewriting_approval(library, name):
    conn, source, target = library
    _push(conn, target, copy_fn=_fail_copy, now=200)
    queued = _pending(conn, target)
    path = Path(source.root_path) / "Artist/Album" / name
    if path.suffix == ".flac":
        tags = mutagen.flac.FLAC(path)
        tags["title"] = ["New metadata, same audio identity"]
        tags.save()
    else:
        path.write_bytes(path.read_bytes() + b"updated")
    stale = next(a for a in queued if a.rel_path == "Artist/Album/" + name)
    result = _push(conn, target, copy_fn=_copy, now=300, rehash=True)
    assert result.reconcile.copies == 1
    assert result.sync.copied == 4 and result.sync.failed == 1
    assert _pending(conn, target) == [stale]
    fresh = action_repo.list_for_run(conn, result.run_id)
    assert len(fresh) == 1 and fresh[0].expected_sha256 != stale.expected_sha256
    assert fresh[0].expected_sha256 == file_sha256(path)
    assert file_sha256(Path(target.root_path) / stale.rel_path) == file_sha256(path)


def test_a_renamed_restoration_source_gets_a_fresh_plan(library):
    conn, source, target = library
    _make_restoration(conn, target)
    _push(conn, target, copy_fn=_fail_copy, now=300)
    old = _pending(conn, target)
    (Path(source.root_path) / "Artist").rename(Path(source.root_path) / "Renamed")
    result = _push(conn, target, copy_fn=_copy, now=400)
    assert result.reconcile.copies == result.sync.copied == 4
    assert result.sync.skipped == 4 and _pending(conn, target) == old
    for action in action_repo.list_for_run(conn, result.run_id):
        assert action.source_rel_path.startswith("Renamed/")
        assert action.rel_path.startswith("Artist/")
        assert (Path(target.root_path) / action.rel_path).is_file()


def test_changed_restoration_mode_is_not_silently_reused(library):
    conn, source, target = library
    _push(conn, target, copy_fn=_fail_copy, now=200)
    old = _pending(conn, target)
    # Another actor supplied the first fill; losing it now is a restoration,
    # whose no-overwrite mode must not be replaced by an older generic COPY.
    for action in old:
        _copy(Path(source.root_path) / action.rel_path, Path(target.root_path) / action.rel_path)
    inventory_location(conn, location=target, root=target.root_path, now=250)
    for action in old:
        (Path(target.root_path) / action.rel_path).unlink()
    preview = _push(conn, target, dry_run=True, now=300)
    assert preview.reconcile.copies == 4
    assert all(a.dest_must_be_absent for a in action_repo.list_for_run(conn, preview.run_id))
    assert _pending(conn, target) == old and all(not a.dest_must_be_absent for a in old)


def test_legacy_source_path_fallback_is_equivalent(library):
    conn, _, target = library
    _push(conn, target, copy_fn=_fail_copy, now=200)
    conn.execute("UPDATE pending_action SET source_rel_path=NULL")
    queued = _pending(conn, target)
    result = _push(conn, target, copy_fn=_copy, now=300)
    assert result.reconcile.copies == 0 and result.sync.copied == 4
    assert all(action_repo.get(conn, old.id) == replace(old, executed_utc=300) for old in queued)


def test_legacy_missing_checksum_does_not_block_a_fresh_plan(library):
    conn, _, target = library
    _push(conn, target, copy_fn=_fail_copy, now=200)
    conn.execute("UPDATE pending_action SET expected_sha256=NULL")
    queued = _pending(conn, target)
    result = _push(conn, target, copy_fn=_copy, now=300)
    assert result.reconcile.copies == result.sync.copied == result.sync.failed == 4
    assert _pending(conn, target) == queued


def test_a_different_source_location_gets_a_fresh_plan(library, tmp_path):
    conn, source, target = library
    _push(conn, target, copy_fn=_fail_copy, now=200)
    old = _pending(conn, target)
    archive = _loc(conn, "Archive", tmp_path / "Archive", is_retention=True)
    for action in old:
        _copy(Path(source.root_path) / action.rel_path, Path(archive.root_path) / action.rel_path)
    inventory_location(conn, location=archive, root=archive.root_path, now=250)
    for action in old:
        (Path(source.root_path) / action.rel_path).unlink()
    result = _push(conn, target, copy_fn=_copy, now=300)
    assert result.reconcile.copies == result.sync.copied == 4
    assert result.sync.skipped == 4 and _pending(conn, target) == old
    assert all(a.source_location_id == archive.id
               for a in action_repo.list_for_run(conn, result.run_id))
