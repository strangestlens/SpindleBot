"""Tests for the sync executor (services/sync.py) — COPY only, non-destructive."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from spindlebot.core.enums import ActionKind, ContentKind, RunKind, ScanStatus
from spindlebot.core.identity import ContentId, file_sha256
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    action_repo,
    audio_repo,
    location_repo,
    presence_repo,
    run_repo,
)
from spindlebot.services import volumes
from spindlebot.services.sync import execute_pending


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


def _loc(conn, uuid, name, root, *, is_retention=False, now=0):
    """Register a location with a marker at `root` so resolve_root trusts it."""
    root.mkdir(parents=True, exist_ok=True)
    volumes.ensure_marker(root, uuid=uuid, name=name, now=now)
    return location_repo.upsert(conn, uuid=uuid, name=name, kind="local_drive",
                                root_path=str(root), is_retention=is_retention)


def _audio(conn, identity="a" * 32):
    return audio_repo.upsert(conn, ContentId("audio_md5", identity), now=0)


def _good_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _propose_copy(conn, *, audio, src_loc, dst_loc, rel_path, now=0):
    run_id = run_repo.start_run(conn, RunKind.RECONCILE, now=now)
    a = action_repo.add(conn, run_id=run_id, action_kind=ActionKind.COPY,
                        content_kind=ContentKind.AUDIO, content_id=audio.id,
                        source_location_id=src_loc.id, dest_location_id=dst_loc.id,
                        rel_path=rel_path, now=now)
    action_repo.acknowledge(conn, [a.id], now=now)
    return a


def _setup(conn, tmp_path):
    pending = _loc(conn, "pending", "Pending", tmp_path / "Pending")
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", is_retention=True)
    audio = _audio(conn)
    rel = "Artist/Album/01.flac"
    src = Path(pending.root_path) / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"FLACDATA" * 4096)
    _propose_copy(conn, audio=audio, src_loc=pending, dst_loc=rugged, rel_path=rel)
    return pending, rugged, audio, rel, src


def test_copy_verifies_and_records_presence(conn, tmp_path):
    pending, rugged, audio, rel, src = _setup(conn, tmp_path)
    result = execute_pending(conn, copy_fn=_good_copy, now=1000)

    assert result.copied == 1 and result.failed == 0
    dst = Path(rugged.root_path) / rel
    assert dst.is_file()
    assert file_sha256(dst) == file_sha256(src)              # byte-faithful copy
    pres = presence_repo.get(conn, audio.id, rugged.id)
    assert pres is not None and pres.present is True
    assert pres.file_sha256 == file_sha256(src) and pres.rel_path == rel


def test_copy_marks_action_executed_and_is_idempotent(conn, tmp_path):
    pending, rugged, audio, rel, src = _setup(conn, tmp_path)
    execute_pending(conn, copy_fn=_good_copy, now=1000)
    # the action is now executed → a second run re-copies nothing
    second = execute_pending(conn, copy_fn=_good_copy, now=2000)
    assert second.copied == 0 and second.skipped == 0 and second.failed == 0
    assert action_repo.list_pending_execution(conn) == []


def test_source_bytes_survive_the_copy(conn, tmp_path):
    # The COPY-not-MOVE regression guard vs the old rsync --remove-source-files.
    pending, rugged, audio, rel, src = _setup(conn, tmp_path)
    before = src.read_bytes()
    execute_pending(conn, copy_fn=_good_copy, now=1000)
    assert src.is_file() and src.read_bytes() == before     # source untouched


def test_hash_mismatch_fails_without_recording_presence(conn, tmp_path):
    pending, rugged, audio, rel, src = _setup(conn, tmp_path)

    def _corrupting_copy(s: Path, d: Path) -> None:
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(s.read_bytes() + b"corruption")        # not byte-faithful

    result = execute_pending(conn, copy_fn=_corrupting_copy, now=1000)
    assert result.copied == 0 and result.failed == 1
    assert presence_repo.get(conn, audio.id, rugged.id) is None   # no presence on a bad copy
    assert len(action_repo.list_pending_execution(conn)) == 1     # left for retry
    assert src.is_file()                                          # source still there


def test_unacknowledged_actions_are_not_executed(conn, tmp_path):
    pending = _loc(conn, "pending", "Pending", tmp_path / "Pending")
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", is_retention=True)
    audio = _audio(conn)
    rel = "x.flac"
    (Path(pending.root_path) / rel).write_bytes(b"data")
    run_id = run_repo.start_run(conn, RunKind.RECONCILE, now=0)
    action_repo.add(conn, run_id=run_id, action_kind=ActionKind.COPY,
                    content_kind=ContentKind.AUDIO, content_id=audio.id,
                    source_location_id=pending.id, dest_location_id=rugged.id,
                    rel_path=rel, now=0)   # NOT acknowledged

    result = execute_pending(conn, copy_fn=_good_copy, now=1000)
    assert result.copied == 0
    assert not (Path(rugged.root_path) / rel).exists()       # nothing copied


def test_skips_when_destination_not_mounted(conn, tmp_path):
    pending = _loc(conn, "pending", "Pending", tmp_path / "Pending")
    # dest registered but its root doesn't exist (not mounted) → no marker/dir
    rugged = location_repo.upsert(conn, uuid="rug", name="DwRugged",
                                  kind="local_drive", is_retention=True,
                                  root_path=str(tmp_path / "GONE"))
    audio = _audio(conn)
    rel = "x.flac"
    (Path(pending.root_path) / rel).write_bytes(b"data")
    _propose_copy(conn, audio=audio, src_loc=pending, dst_loc=rugged, rel_path=rel)

    result = execute_pending(conn, copy_fn=_good_copy, now=1000)
    assert result.copied == 0 and result.skipped == 1


def test_sync_records_a_finished_run(conn, tmp_path):
    _setup(conn, tmp_path)
    result = execute_pending(conn, copy_fn=_good_copy, now=1000)
    run = run_repo.get(conn, result.run_id)
    assert run.kind == RunKind.SYNC
    assert run.status == ScanStatus.OK and run.finished_utc == 1000
