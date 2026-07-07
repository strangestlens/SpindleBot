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


def test_copies_a_sidecar_and_records_sidecar_presence(conn, tmp_path):
    from spindlebot.core.enums import ActionKind, ContentKind, SidecarParentKind, SidecarRole
    from spindlebot.db.repositories import (
        album_repo,
        run_repo,
        sidecar_presence_repo,
        sidecar_repo,
    )
    pending = _loc(conn, "pending", "Pending", tmp_path / "Pending")
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", is_retention=True)
    album = album_repo.upsert(conn, album_key="k", now=0)
    sc = sidecar_repo.upsert(conn, parent_kind=SidecarParentKind.ALBUM,
                             parent_id=album.id, role=SidecarRole.COVER,
                             sha256="h", now=0)
    rel = "Artist/Album/cover.jpg"
    src = Path(pending.root_path) / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"\xff\xd8jpeg-bytes" * 100)
    sidecar_presence_repo.set_presence(conn, sidecar_id=sc.id, location_id=pending.id,
                                       present=True, observed_utc=0, rel_path=rel,
                                       file_sha256=file_sha256(src), byte_size=src.stat().st_size)
    run_id = run_repo.start_run(conn, RunKind.RECONCILE, now=0)
    a = action_repo.add(conn, run_id=run_id, action_kind=ActionKind.COPY,
                        content_kind=ContentKind.SIDECAR, content_id=sc.id,
                        source_location_id=pending.id, dest_location_id=rugged.id,
                        rel_path=rel, now=0)
    action_repo.acknowledge(conn, [a.id], now=0)

    result = execute_pending(conn, copy_fn=_good_copy, now=1000)
    assert result.copied == 1 and result.failed == 0
    dst = Path(rugged.root_path) / rel
    assert dst.is_file() and file_sha256(dst) == file_sha256(src)
    pres = sidecar_presence_repo.get(conn, sc.id, rugged.id)
    assert pres is not None and pres.present is True and pres.rel_path == rel


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


def test_sync_emits_progress(conn, tmp_path):
    _setup(conn, tmp_path)
    events = []
    execute_pending(conn, copy_fn=_good_copy, now=1000, progress=events.append)
    assert events[0].phase == "sync" and events[0].total == 1
    assert events[-1].done == 1


# ── default rsync copy + CLI wiring ───────────────────────────────────────────

@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_rsync_copy_is_byte_faithful(tmp_path):
    from spindlebot.services.sync import _rsync_copy
    src = tmp_path / "a" / "src.flac"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"lossless" * 1000)
    dst = tmp_path / "b" / "c" / "dst.flac"
    _rsync_copy(src, dst)
    assert dst.is_file() and file_sha256(dst) == file_sha256(src)


def _cli_cfg(tmp_path):
    from types import SimpleNamespace
    core = SimpleNamespace(db_path=tmp_path / "spindlebot.db", min_copies=2,
                           pending_dir=tmp_path / "Pending")
    return SimpleNamespace(core=core, locations=[], destinations=[])


def test_cmd_sync_no_pending_is_a_clean_noop(tmp_path, capsys):
    from spindlebot.cli import cmd_sync
    rc = cmd_sync(_cli_cfg(tmp_path), ["--json"])
    import json
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and data["copied"] == 0 and data["failed"] == 0


# ── audit #35: durability, progress completeness, exit code ───────────────────

def test_each_copy_is_committed_so_a_crash_keeps_done_work(tmp_path):
    # Per-action checkpoint: a crash mid-batch keeps the copies already verified,
    # so the next run doesn't re-copy/re-hash the whole library.
    db = tmp_path / "spindlebot.db"
    conn = open_db(db)
    pending = _loc(conn, "pending", "Pending", tmp_path / "Pending")
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", is_retention=True)
    a1, a2 = _audio(conn, "a" * 32), _audio(conn, "b" * 32)
    for a, rel in [(a1, "1.flac"), (a2, "2.flac")]:
        (Path(pending.root_path) / rel).write_bytes(rel.encode() * 100)
        _propose_copy(conn, audio=a, src_loc=pending, dst_loc=rugged, rel_path=rel)
    conn.commit()

    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("power loss mid-sync")
        _good_copy(src, dst)

    with pytest.raises(RuntimeError):
        execute_pending(conn, copy_fn=flaky, now=1000, checkpoint=conn.commit)
    conn.close()

    other = open_db(db)
    assert presence_repo.get(other, a1.id, rugged.id) is not None   # first copy durable
    assert presence_repo.get(other, a2.id, rugged.id) is None       # second never recorded
    assert len(action_repo.list_pending_execution(other)) == 1      # only the un-done one
    other.close()


def test_progress_reaches_total_even_with_a_skip(conn, tmp_path):
    pending = _loc(conn, "pending", "Pending", tmp_path / "Pending")
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", is_retention=True)
    good, orphan = _audio(conn, "a" * 32), _audio(conn, "b" * 32)
    (Path(pending.root_path) / "good.flac").write_bytes(b"x" * 200)
    _propose_copy(conn, audio=good, src_loc=pending, dst_loc=rugged, rel_path="good.flac")
    _propose_copy(conn, audio=orphan, src_loc=pending, dst_loc=rugged,
                  rel_path="missing.flac")   # no source file → skipped

    events = []
    result = execute_pending(conn, copy_fn=_good_copy, now=1000, progress=events.append)
    assert result.copied == 1 and result.skipped == 1
    assert events[-1].done == events[-1].total == 2   # bar completes despite the skip


def test_cmd_sync_nonzero_when_acknowledged_copy_skipped_with_error(tmp_path, capsys):
    import json
    from types import SimpleNamespace

    from spindlebot.cli import cmd_sync
    from spindlebot.config import LocationConfig
    from spindlebot.core.enums import LocationKind
    from spindlebot.services.locations import get_by_name, register_from_config

    db = tmp_path / "spindlebot.db"
    pending_dir = tmp_path / "Pending"
    core = SimpleNamespace(db_path=db, min_copies=2, pending_dir=pending_dir)
    # dest configured but its root doesn't exist → "unmounted"
    locs = [LocationConfig(name="DwRugged", kind=LocationKind.LOCAL_DRIVE,
                           root_path=str(tmp_path / "GONE"), is_retention=True)]
    cfg = SimpleNamespace(core=core, locations=locs, destinations=[])

    conn = open_db(db)
    register_from_config(conn, cfg, 0)
    pending, rugged = get_by_name(conn, "Pending"), get_by_name(conn, "DwRugged")
    audio = _audio(conn)
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "x.flac").write_bytes(b"data")
    _propose_copy(conn, audio=audio, src_loc=pending, dst_loc=rugged, rel_path="x.flac")
    conn.commit()
    conn.close()

    rc = cmd_sync(cfg, ["--json", "--quiet"])
    data = json.loads(capsys.readouterr().out)
    # an acknowledged copy we couldn't do (dest unmounted) is a failure signal
    assert rc == 1
    assert data["copied"] == 0 and data["skipped"] == 1 and data["errors"]
