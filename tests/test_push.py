"""
Tests for `push` — the one-pass fill of a single location
(services/push.py + the `spindlebot push` CLI).

The contract these guard, in order of how much it would cost to get wrong:

1. Push NEVER prunes. The DAP this exists for is configured `is_retention`, so
   a prune after a push would release the Pending copy on the strength of the
   DAP alone. The authoring copy must still be there afterwards.
2. `--dry-run` plans but acknowledges and copies nothing.
3. The sync is scoped to the pushed location — work queued for another
   destination is left alone.
4. Running a push over an unchanged target is a no-op — it neither re-copies
   what is already there nor reports it as missing.
"""
from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path
from types import SimpleNamespace

import mutagen.flac
import pytest

from spindlebot.cli import cmd_push
from spindlebot.config import LocationConfig
from spindlebot.core.enums import ActionKind, ContentKind, LocationKind, RunKind
from spindlebot.core.identity import file_sha256
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    action_repo,
    audio_repo,
    location_repo,
    presence_repo,
    run_repo,
)
from spindlebot.services import volumes
from spindlebot.services.locations import get_by_name, register_from_config
from spindlebot.services.push import push_location

DAP = "PortablePlayer"


def _write_flac(path: Path, *, audio_md5_bytes: bytes = b"\x00" * 16,
                tags: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    streaminfo = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00\x00\x00\x00\x00\x00"
        + struct.pack(">Q", (44100 << 44) | (0 << 41) | (15 << 36) | 0)
        + audio_md5_bytes
    )
    path.write_bytes(b"fLaC" + bytes([0x80, 0x00, 0x00, 0x22]) + streaminfo)
    f = mutagen.flac.FLAC(str(path))
    f.add_tags()
    for k, v in (tags or {}).items():
        f[k] = [v]
    f.save()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


def _loc(conn, uuid, name, root, *, is_retention=False, authoritative=False):
    root.mkdir(parents=True, exist_ok=True)
    volumes.ensure_marker(root, uuid=uuid, name=name, now=0)
    return location_repo.upsert(
        conn, uuid=uuid, name=name, kind=LocationKind.LOCAL_DRIVE,
        root_path=str(root), is_retention=is_retention,
        is_authoritative_audio=authoritative,
    )


REL = "Artist/Album/01.flac"


def _seed(conn, tmp_path, *, n_tracks=1):
    """A Pending library holding `n_tracks`, and an empty retention DAP."""
    pending = _loc(conn, "pending-uuid", "Pending", tmp_path / "Pending",
                   authoritative=True)
    dap = _loc(conn, "dap-uuid", DAP, tmp_path / "Dap", is_retention=True)
    for i in range(n_tracks):
        _write_flac(
            Path(pending.root_path) / f"Artist/Album/{i + 1:02d}.flac",
            audio_md5_bytes=bytes([i + 1] * 16),
            tags={"artist": "Artist", "album": "Album", "title": f"Track {i + 1}"},
        )
    # Pending has been inventoried already — push only scans its target.
    from spindlebot.services.inventory import inventory_location
    inventory_location(conn, location=pending, root=Path(pending.root_path), now=100)
    return pending, dap


# ── the safety contract ───────────────────────────────────────────────────────

def test_push_copies_to_the_target_and_records_presence(conn, tmp_path):
    pending, dap = _seed(conn, tmp_path)
    result = push_location(conn, location=dap, root=Path(dap.root_path), copy_fn=_copy)

    assert result.sync.copied == 1 and result.sync.failed == 0
    dst = Path(dap.root_path) / REL
    assert dst.is_file()
    assert file_sha256(dst) == file_sha256(Path(pending.root_path) / REL)

    audio = audio_repo.get_by_identity(conn, bytes([1] * 16).hex())
    pres = presence_repo.get(conn, audio.id, dap.id)
    assert pres is not None and pres.present is True and pres.rel_path == REL
    assert result.ok is True


def test_push_never_prunes_the_authoring_copy(conn, tmp_path):
    """THE reason push stops at sync: the DAP is a retention location, so a
    prune would read one copy on it as licence to release the Pending copy."""
    pending, dap = _seed(conn, tmp_path)
    src = Path(pending.root_path) / REL

    push_location(conn, location=dap, root=Path(dap.root_path), copy_fn=_copy)

    assert src.is_file(), "push released the authoring copy — it must never prune"
    audio = audio_repo.get_by_identity(conn, bytes([1] * 16).hex())
    assert presence_repo.get(conn, audio.id, pending.id).present is True


def test_push_proposes_no_deletes(conn, tmp_path):
    _, dap = _seed(conn, tmp_path)
    result = push_location(conn, location=dap, root=Path(dap.root_path), copy_fn=_copy)
    kinds = {a.action_kind for a in action_repo.list_for_run(conn, result.run_id)}
    assert ActionKind.DELETE not in kinds


# ── dry run ───────────────────────────────────────────────────────────────────

def test_dry_run_plans_but_copies_nothing(conn, tmp_path):
    _, dap = _seed(conn, tmp_path)
    result = push_location(conn, location=dap, root=Path(dap.root_path),
                           dry_run=True, copy_fn=_copy)

    assert result.reconcile.copies == 1          # it still planned
    assert result.sync is None                   # ...and stopped there
    assert result.acknowledged == 0
    assert result.stopped_at == "sync"
    assert not (Path(dap.root_path) / REL).exists()
    assert result.ok is True                     # stopping early is not a failure

    actions = action_repo.list_for_run(conn, result.run_id)
    assert actions and all(a.acknowledged is False for a in actions)


def test_dry_run_still_inventories_the_target(conn, tmp_path):
    """A plan built on a stale scan is worthless, so the scan is not skipped."""
    _, dap = _seed(conn, tmp_path)
    _write_flac(Path(dap.root_path) / REL, audio_md5_bytes=bytes([1] * 16))
    result = push_location(conn, location=dap, root=Path(dap.root_path),
                           dry_run=True, copy_fn=_copy)
    assert result.inventory.scanned == 1
    # The track is already there, so nothing is planned to copy.
    assert result.reconcile.copies == 0


# ── scoping ───────────────────────────────────────────────────────────────────

def test_push_does_not_execute_another_destinations_work(conn, tmp_path):
    pending, dap = _seed(conn, tmp_path)
    other = _loc(conn, "other-uuid", "OtherDrive", tmp_path / "Other",
                 is_retention=True)
    audio = audio_repo.get_by_identity(conn, bytes([1] * 16).hex())
    run_id = run_repo.start_run(conn, RunKind.RECONCILE, now=0)
    queued = action_repo.add(
        conn, run_id=run_id, action_kind=ActionKind.COPY,
        content_kind=ContentKind.AUDIO, content_id=audio.id,
        source_location_id=pending.id, dest_location_id=other.id,
        rel_path=REL, now=0)
    action_repo.acknowledge(conn, [queued.id], now=0)

    push_location(conn, location=dap, root=Path(dap.root_path), copy_fn=_copy)

    assert not (Path(other.root_path) / REL).exists()
    assert action_repo.get(conn, queued.id).executed_utc is None


def test_push_finishes_an_earlier_unexecuted_copy_for_this_target(conn, tmp_path):
    """An acknowledged copy a previous push never ran (DAP unplugged mid-run) is
    still owed to this location — the next push should finish it."""
    pending, dap = _seed(conn, tmp_path, n_tracks=2)
    audio = audio_repo.get_by_identity(conn, bytes([2] * 16).hex())
    rel2 = "Artist/Album/02.flac"
    run_id = run_repo.start_run(conn, RunKind.RECONCILE, now=0)
    stranded = action_repo.add(
        conn, run_id=run_id, action_kind=ActionKind.COPY,
        content_kind=ContentKind.AUDIO, content_id=audio.id,
        source_location_id=pending.id, dest_location_id=dap.id,
        rel_path=rel2, now=0)
    action_repo.acknowledge(conn, [stranded.id], now=0)

    push_location(conn, location=dap, root=Path(dap.root_path), copy_fn=_copy)

    assert (Path(dap.root_path) / rel2).is_file()
    assert action_repo.get(conn, stranded.id).executed_utc is not None


# ── re-running a push ─────────────────────────────────────────────────────────

def test_a_second_push_over_an_unchanged_target_plans_nothing(conn, tmp_path):
    """The pass plans against its own fresh scan, so a target that already holds
    everything yields no copies and no spurious MISSING (which would also raise
    a below_floor warning about content that never went anywhere)."""
    _, dap = _seed(conn, tmp_path)
    _write_flac(Path(dap.root_path) / REL, audio_md5_bytes=bytes([1] * 16))

    first = push_location(conn, location=dap, root=Path(dap.root_path),
                          now=1000, copy_fn=_copy)
    assert first.reconcile.missing == 0

    second = push_location(conn, location=dap, root=Path(dap.root_path),
                           now=2000, copy_fn=_copy)
    assert second.reconcile.missing == 0
    assert second.reconcile.copies == 0
    assert second.reconcile.below_floor == 0


def test_a_file_gone_from_the_target_is_reported_missing(conn, tmp_path):
    """The flip side: a file the target really has lost is still caught."""
    _, dap = _seed(conn, tmp_path)
    dst = Path(dap.root_path) / REL
    _write_flac(dst, audio_md5_bytes=bytes([1] * 16))
    push_location(conn, location=dap, root=Path(dap.root_path), now=1000, copy_fn=_copy)

    dst.unlink()
    result = push_location(conn, location=dap, root=Path(dap.root_path),
                           now=2000, copy_fn=_copy)
    assert result.reconcile.missing == 1


# ── failure reporting ─────────────────────────────────────────────────────────

def test_an_abnormal_early_stop_is_not_ok(conn, tmp_path, monkeypatch):
    """Only the dry run's stop is a success. If the planner refuses for want of
    a scan, the pass didn't do what it was asked — it must not report ok."""
    from spindlebot.services import push as push_mod
    from spindlebot.services.inventory import InventoryResult

    _, dap = _seed(conn, tmp_path)
    monkeypatch.setattr(push_mod, "inventory_location",
                        lambda *a, **k: InventoryResult(location=DAP))

    result = push_location(conn, location=dap, root=Path(dap.root_path), copy_fn=_copy)
    assert result.stopped_at == "review"
    assert result.sync is None
    assert result.ok is False
    assert not (Path(dap.root_path) / REL).exists()


def test_a_failed_copy_makes_the_push_not_ok(conn, tmp_path):
    _, dap = _seed(conn, tmp_path)

    def _corrupting_copy(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"not the source bytes")

    result = push_location(conn, location=dap, root=Path(dap.root_path),
                           copy_fn=_corrupting_copy)
    assert result.sync.failed == 1
    assert result.ok is False


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cfg(tmp_path, dap_root):
    core = SimpleNamespace(
        db_path=tmp_path / "spindlebot.db",
        min_copies=1,
        pending_dir=tmp_path / "Pending",
    )
    locations = [LocationConfig(name=DAP, kind=LocationKind.LOCAL_DRIVE,
                                root_path=str(dap_root), is_retention=True)]
    return SimpleNamespace(core=core, locations=locations, destinations=[],
                           tools=SimpleNamespace(beets_db=None))


def _cli_setup(tmp_path):
    """Config + a seeded DB the CLI will reopen at cfg.core.db_path."""
    dap_root = tmp_path / "Dap"
    cfg = _cfg(tmp_path, dap_root)
    c = open_db(cfg.core.db_path)
    register_from_config(c, cfg, 0)
    pending = get_by_name(c, "Pending")
    pending_root = tmp_path / "Pending"
    pending_root.mkdir(parents=True, exist_ok=True)
    volumes.ensure_marker(pending_root, uuid=pending.uuid, name="Pending", now=0)
    location_repo.upsert(c, uuid=pending.uuid, name="Pending",
                         kind=pending.kind, root_path=str(pending_root),
                         is_authoritative_audio=True, is_retention=False)
    dap_root.mkdir(parents=True, exist_ok=True)
    volumes.ensure_marker(dap_root, uuid=get_by_name(c, DAP).uuid, name=DAP, now=0)
    _write_flac(pending_root / REL, audio_md5_bytes=bytes([1] * 16),
                tags={"artist": "Artist", "album": "Album", "title": "One"})
    from spindlebot.services.inventory import inventory_location
    inventory_location(c, location=get_by_name(c, "Pending"), root=pending_root, now=100)
    c.commit()
    c.close()
    return cfg, pending_root, dap_root


def test_cli_push_copies_and_reports(tmp_path, capsys):
    cfg, pending_root, dap_root = _cli_setup(tmp_path)
    rc = cmd_push(cfg, ["--location", DAP, "--quiet"])
    out = capsys.readouterr().out
    assert rc == 0
    assert (dap_root / REL).is_file()
    assert (pending_root / REL).is_file()        # still never prunes
    assert "inventory:" in out and "review (run" in out
    assert "1 copied" in out


def test_cli_push_dry_run_copies_nothing(tmp_path, capsys):
    cfg, _, dap_root = _cli_setup(tmp_path)
    rc = cmd_push(cfg, ["--location", DAP, "--dry-run", "--quiet"])
    out = capsys.readouterr().out
    assert rc == 0
    assert not (dap_root / REL).exists()
    assert "[dry-run]" in out
    assert "Re-run without --dry-run" in out


def test_cli_push_json_shape(tmp_path, capsys):
    cfg, _, _ = _cli_setup(tmp_path)
    rc = cmd_push(cfg, ["--location", DAP, "--json", "--quiet"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["location"] == DAP
    assert data["dry_run"] is False
    assert data["ok"] is True
    assert data["inventory"]["scanned"] == 0     # the DAP was empty when scanned
    assert data["reconcile"]["copies"] == 1
    assert data["sync"]["copied"] == 1
    assert data["acknowledged"] == 1


def test_cli_push_requires_a_location(tmp_path, capsys):
    cfg, _, _ = _cli_setup(tmp_path)
    assert cmd_push(cfg, ["--quiet"]) == 1
    assert "Usage" in capsys.readouterr().err


def test_cli_push_refuses_a_bare_location_flag(tmp_path, capsys):
    """A bare --location must not fall back to a default destination — push
    copies bytes, and guessing where is exactly the mistake worth refusing."""
    cfg, _, _ = _cli_setup(tmp_path)
    assert cmd_push(cfg, ["--location", "--dry-run"]) == 1
    assert "requires a location name" in capsys.readouterr().err


def test_cli_push_unknown_location_fails(tmp_path, capsys):
    cfg, _, _ = _cli_setup(tmp_path)
    assert cmd_push(cfg, ["--location", "Nope"]) == 1
    assert "Unknown location" in capsys.readouterr().err


def test_cli_push_unmounted_location_fails_before_any_work(tmp_path, capsys):
    cfg, _, dap_root = _cli_setup(tmp_path)
    shutil.rmtree(dap_root)
    rc = cmd_push(cfg, ["--location", DAP])
    assert rc == 1
    assert "not mounted" in capsys.readouterr().err
    # It refused before scanning: no scan was ever recorded for the target.
    c = open_db(cfg.core.db_path)
    try:
        from spindlebot.db.repositories import scan_repo
        assert scan_repo.latest_scan(c, get_by_name(c, DAP).id) is None
    finally:
        c.close()
