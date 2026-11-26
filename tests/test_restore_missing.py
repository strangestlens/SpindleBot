"""Destination loss is repaired in one inventory/reconcile/sync pass."""
from __future__ import annotations

import shutil
import struct
from pathlib import Path

import mutagen.flac
import pytest

from spindlebot.core.enums import ActionKind, ContentKind, LocationKind, ScanStatus
from spindlebot.core.identity import file_sha256
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    action_repo,
    location_repo,
    presence_repo,
    scan_repo,
    sidecar_presence_repo,
)
from spindlebot.services import inventory, volumes
from spindlebot.services.reconciler import reconcile_location
from spindlebot.services.sync import execute_pending


def _write_flac(path, *, audio_md5_bytes=b"\x01" * 16):
    path.parent.mkdir(parents=True, exist_ok=True)
    streaminfo = (struct.pack(">HH", 4096, 4096) + b"\x00" * 6
                  + struct.pack(">Q", (44100 << 44) | (15 << 36)) + audio_md5_bytes)
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 0x22]) + streaminfo)
    f = mutagen.flac.FLAC(path)
    f.add_tags()
    for key, value in {"artist": "Artist", "album": "Album", "title": "Track"}.items():
        f[key] = [value]
    f.save()


def _copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


@pytest.fixture
def seeded(tmp_path):
    conn = open_db(tmp_path / "spindlebot.db")
    locations = []
    for name, retention in [("AuthoringLibrary", False), ("RetentionDrive", True)]:
        root = tmp_path / name
        root.mkdir()
        volumes.ensure_marker(root, uuid=name, name=name, now=0)
        locations.append(location_repo.upsert(
            conn, uuid=name, name=name, kind=LocationKind.LOCAL_DRIVE,
            root_path=str(root), is_retention=retention,
            is_authoritative_audio=not retention,
        ))
    source, target = locations
    audio = Path(source.root_path) / "Artist/Album/01.flac"
    _write_flac(audio)
    audio.with_suffix(".lrc").write_text("[00:01.00]Lyrics\n")
    audio.with_suffix(".nolrc").write_text("")
    (audio.parent / "cover.jpg").write_bytes(b"cover")
    for path in audio.parent.iterdir():
        _copy(path, Path(target.root_path) / path.relative_to(source.root_path))
    for loc in locations:
        inventory.inventory_location(conn, location=loc, root=loc.root_path, now=100)
    try:
        yield conn, source, target
    finally:
        conn.close()


@pytest.mark.parametrize("name", ["01.flac", "01.lrc", "01.nolrc", "cover.jpg"])
@pytest.mark.parametrize("scan_time", [100, 200])
def test_deleted_file_is_restored_in_one_pass(seeded, name, scan_time):
    conn, source, target = seeded
    rel = "Artist/Album/" + name
    original = (Path(source.root_path) / rel).read_bytes()
    dst = Path(target.root_path) / rel
    dst.unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=scan_time)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    actions = action_repo.list_for_run(conn, plan.run_id)
    copies = [a for a in actions if a.action_kind == ActionKind.COPY]
    assert len(copies) == 1
    action = copies[0]
    assert action.rel_path == rel and action.expected_sha256 == file_sha256(Path(source.root_path) / rel)
    action_repo.acknowledge_run(conn, plan.run_id, now=300)
    result = execute_pending(conn, copy_fn=_copy, now=400)
    assert result.copied == 1 and result.failed == result.skipped == 0
    assert dst.read_bytes() == original
    repo = presence_repo if action.content_kind == ContentKind.AUDIO else sidecar_presence_repo
    row = repo.get(conn, action.content_id, target.id)
    assert row.present and row.file_sha256 == file_sha256(dst)
    assert row.absence_scan_id is None
    assert action_repo.get(conn, action.id).executed_utc == 400
    inventory.inventory_location(conn, location=target, root=target.root_path, now=500)
    again = reconcile_location(conn, target=target, source_locations=[source], now=600)
    assert again.copies == 0 and again.missing == 0


def test_deleted_album_restores_audio_and_all_sidecars(seeded):
    conn, source, target = seeded
    shutil.rmtree(Path(target.root_path) / "Artist")
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    assert plan.copies == 4
    action_repo.acknowledge_run(conn, plan.run_id, now=300)
    result = execute_pending(conn, copy_fn=_copy, now=400)
    assert result.copied == 4 and result.failed == 0


def test_unreadable_present_file_is_not_authorized_for_restoration(seeded, monkeypatch):
    conn, source, target = seeded
    original = inventory._identify_or_reuse

    def unreadable(conn, path, **kw):
        if Path(target.root_path) in path.parents:
            raise PermissionError("unreadable")
        return original(conn, path, **kw)

    monkeypatch.setattr(inventory, "_identify_or_reuse", unreadable)
    result = inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    assert result.errors == 1
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    assert plan.copies == 0


@pytest.mark.parametrize("status", [ScanStatus.RUNNING, ScanStatus.INTERRUPTED, ScanStatus.ERROR])
def test_partial_scan_cannot_authorize_restoring_a_previously_absent_path(seeded, status):
    conn, source, target = seeded
    dst = Path(target.root_path) / "Artist/Album/01.flac"
    dst.unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    scan_id = scan_repo.start_scan(conn, target.id, 300)
    if status != ScanStatus.RUNNING:
        scan_repo.finish_scan(conn, scan_id, files_seen=0, status=status, now=301)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=400)
    assert plan.copies == 0


@pytest.mark.parametrize("name", ["01.flac", "01.lrc", "01.nolrc", "cover.jpg"])
def test_reappeared_unreadable_path_is_not_restored_from_an_old_absence(seeded, monkeypatch, name):
    conn, source, target = seeded
    rel = "Artist/Album/" + name
    dst = Path(target.root_path) / rel
    dst.unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    _copy(Path(source.root_path) / rel, dst)
    identify, digest = inventory._identify_or_reuse, inventory.file_sha256

    def unreadable_audio(conn, path, **kw):
        if path == dst:
            raise PermissionError("unreadable")
        return identify(conn, path, **kw)

    def unreadable_sidecar(path):
        if path == dst:
            raise PermissionError("unreadable")
        return digest(path)

    monkeypatch.setattr(inventory, "_identify_or_reuse", unreadable_audio)
    monkeypatch.setattr(inventory, "file_sha256", unreadable_sidecar)
    # Same clock second, but a DIFFERENT scan. Old absence is not authorization.
    result = inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    assert result.errors == 1
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    assert plan.copies == 0


def test_dangling_symlink_is_an_existing_entry_not_a_missing_path(seeded):
    conn, source, target = seeded
    dst = Path(target.root_path) / "Artist/Album/01.flac"
    dst.unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    dst.symlink_to("does-not-exist.flac")
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    assert plan.copies == 0 and dst.is_symlink()


def test_interrupted_inventory_cannot_authorize_its_recorded_absences(seeded, monkeypatch):
    conn, source, target = seeded
    (Path(target.root_path) / "Artist/Album/01.flac").unlink()
    original = inventory._confirm_absences

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("interrupted after recording absences")

    monkeypatch.setattr(inventory, "_confirm_absences", interrupted)
    with pytest.raises(RuntimeError):
        inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    conn.commit()  # Model durable partial progress after an interrupted scan.
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    assert plan.copies == 0


def test_restoration_still_checks_the_source_against_the_plan(seeded):
    conn, source, target = seeded
    rel = "Artist/Album/01.flac"
    (Path(target.root_path) / rel).unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    action_repo.acknowledge_run(conn, plan.run_id, now=300)
    src = Path(source.root_path) / rel
    src.write_bytes(src.read_bytes() + b"replaced")
    result = execute_pending(conn, copy_fn=_copy, now=400)
    assert result.failed == 1 and result.copied == 0
    assert not (Path(target.root_path) / rel).exists()
    assert all(not p.present for p in presence_repo.list_for_location(conn, target.id))


def test_absence_without_an_available_source_is_reported_without_a_copy(seeded):
    conn, source, target = seeded
    rel = "Artist/Album/01.flac"
    for loc in (source, target):
        (Path(loc.root_path) / rel).unlink()
        inventory.inventory_location(conn, location=loc, root=loc.root_path, now=200)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    assert plan.missing == 1 and plan.below_floor == 1 and plan.copies == 0


def test_a_replaced_recording_is_not_treated_as_a_missing_file(seeded):
    conn, source, target = seeded
    dst = Path(target.root_path) / "Artist/Album/01.flac"
    dst.unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    _write_flac(dst, audio_md5_bytes=b"\x02" * 16)
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    assert plan.copies == 0
    assert len(presence_repo.list_for_location(conn, target.id)) == 2


@pytest.mark.parametrize("name", ["01.flac", "01.lrc", "01.nolrc", "cover.jpg"])
def test_restores_the_recorded_destination_path_when_source_path_differs(seeded, name):
    conn, source, target = seeded
    (Path(target.root_path) / "Artist").rename(Path(target.root_path) / "Renamed")
    inventory.inventory_location(conn, location=target, root=target.root_path, now=150)
    dst = Path(target.root_path) / "Renamed/Album" / name
    dst.unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    action_repo.acknowledge_run(conn, plan.run_id, now=300)
    result = execute_pending(conn, copy_fn=_copy, now=400)
    assert result.copied == 1 and result.failed == 0
    assert dst.read_bytes() == (Path(source.root_path) / "Artist/Album" / name).read_bytes()


def test_failed_tree_walk_invalidates_an_older_absence_scan(seeded, monkeypatch):
    conn, source, target = seeded
    dst = Path(target.root_path) / "Artist/Album/01.flac"
    dst.unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    dst.write_bytes(b"replacement")

    def fail_walk(root):
        raise PermissionError("cannot enumerate")

    monkeypatch.setattr(inventory, "_walk_tree", fail_walk)
    with pytest.raises(PermissionError):
        inventory.inventory_location(conn, location=target, root=target.root_path, now=300)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=400)
    assert plan.copies == 0


@pytest.mark.parametrize("symlink", [False, True])
@pytest.mark.parametrize("name", ["01.flac", "01.lrc"])
def test_restoration_preserves_a_destination_recreated_after_planning(seeded, symlink, name):
    conn, source, target = seeded
    dst = Path(target.root_path) / "Artist/Album" / name
    dst.unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    action_repo.acknowledge_run(conn, plan.run_id, now=300)
    if symlink:
        dst.symlink_to("missing.flac")
    else:
        dst.write_bytes(b"replacement")
    result = execute_pending(conn, copy_fn=_copy, now=400)
    assert result.copied == 0 and result.failed == 1
    assert dst.is_symlink() if symlink else dst.read_bytes() == b"replacement"
    assert len(action_repo.list_pending_execution(conn)) == 1


@pytest.mark.parametrize("name", ["01.flac", "01.lrc"])
def test_restoration_preserves_a_destination_created_during_transfer(seeded, name):
    conn, source, target = seeded
    dst = Path(target.root_path) / "Artist/Album" / name
    dst.unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    action_repo.acknowledge_run(conn, plan.run_id, now=300)

    def racing_copy(src, staged):
        dst.write_bytes(b"replacement")
        _copy(src, staged)

    result = execute_pending(conn, copy_fn=racing_copy, now=400)
    assert result.copied == 0 and result.failed == 1
    assert dst.read_bytes() == b"replacement"


def test_failed_publication_never_unlinks_a_replacement_at_the_final_path(seeded, monkeypatch):
    from spindlebot.services import sync
    conn, source, target = seeded
    src = Path(source.root_path) / "Artist/Album/01.flac"
    dst = Path(target.root_path) / "Artist/Album/01.flac"
    dst.unlink()
    original_lstat, original_unlink = Path.lstat, Path.unlink
    state = {"failed": False, "replaced": False}

    def fail_copy(inp, out):
        state["failed"] = True
        raise OSError("publication interrupted")

    def stale_lstat(path):
        info = original_lstat(path)
        if path == dst and state["failed"] and not state["replaced"]:
            # A replacement arrives after the ownership check but before a
            # pathname unlink. That unlink must never be attempted.
            original_unlink(path)
            path.write_bytes(b"replacement")
            state["replaced"] = True
        return info

    monkeypatch.setattr(sync.shutil, "copyfileobj", fail_copy)
    monkeypatch.setattr(Path, "lstat", stale_lstat)
    with pytest.raises(OSError):
        sync._restore_absent(src, dst, expected=file_sha256(src), copy_fn=_copy, action_id=1)
    assert dst.exists(), "a failed publication must not unlink the final pathname"
    if state["replaced"]:
        assert dst.read_bytes() == b"replacement"


def test_a_correct_file_restored_elsewhere_completes_without_overwriting_it(seeded):
    conn, source, target = seeded
    rel = "Artist/Album/01.flac"
    dst = Path(target.root_path) / rel
    dst.unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    action_repo.acknowledge_run(conn, plan.run_id, now=300)
    _copy(Path(source.root_path) / rel, dst)
    inode = dst.stat().st_ino
    calls = []
    result = execute_pending(conn, copy_fn=lambda s, d: calls.append((s, d)), now=400)
    assert result.copied == 1 and result.failed == 0
    assert calls == [] and dst.stat().st_ino == inode
    assert action_repo.list_pending_execution(conn) == []


def _acknowledged_restore(conn, source, target):
    dst = Path(target.root_path) / 'Artist/Album/01.flac'
    dst.unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    action_repo.acknowledge_run(conn, plan.run_id, now=300)
    return dst


def test_already_restored_file_swapped_to_source_symlink_is_never_verified(seeded, monkeypatch):
    import builtins
    import os
    from spindlebot.services import sync
    conn, source, target = seeded
    dst = _acknowledged_restore(conn, source, target)
    src = Path(source.root_path) / 'Artist/Album/01.flac'
    _copy(src, dst)
    real_open, real_os_open = builtins.open, os.open
    swapped = []

    def replace():
        if not swapped:
            dst.unlink()
            dst.symlink_to(src)
            swapped.append(True)

    def path_open(path, *args, **kwargs):
        if Path(path) == dst:
            replace()
        return real_open(path, *args, **kwargs)

    def fd_open(path, flags, *args, **kwargs):
        if str(path) == dst.name and kwargs.get('dir_fd') is not None and not flags & os.O_CREAT:
            replace()
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', path_open)
    monkeypatch.setattr(sync.os, 'open', fd_open)
    result = execute_pending(conn, copy_fn=_copy, now=400)
    assert swapped
    assert result.copied == 0 and result.failed == 1
    assert not presence_repo.list_for_location(conn, target.id)[0].present


@pytest.mark.parametrize('during_copy', [False, True])
def test_restoration_never_writes_through_a_symlinked_parent(seeded, during_copy):
    conn, source, target = seeded
    dst = _acknowledged_restore(conn, source, target)
    album = dst.parent
    outside = Path(target.root_path).parent / 'outside'
    outside.mkdir()

    def redirect():
        album.rename(album.with_name('saved'))
        album.symlink_to(outside, target_is_directory=True)

    def racing_copy(src, staged):
        _copy(src, staged)
        redirect()

    if not during_copy:
        redirect()
    result = execute_pending(conn, copy_fn=racing_copy if during_copy else _copy, now=400)
    assert not (outside / dst.name).exists()
    assert result.copied == 0 and result.failed == 1


def test_restoration_rejects_an_ancestor_swap_above_the_configured_root(seeded, monkeypatch):
    from spindlebot.services import sync
    conn, source, target = seeded
    original_root = Path(target.root_path)
    configured_root = original_root / 'Music/Library'
    configured_root.mkdir(parents=True)
    for path in list(original_root.iterdir()):
        if path.name != 'Music':
            path.rename(configured_root / path.name)
    target = location_repo.upsert(
        conn, uuid=target.uuid, name=target.name, kind=target.kind,
        root_path=str(configured_root), is_retention=True,
    )
    _acknowledged_restore(conn, source, target)
    outside = original_root / 'outside'
    (outside / 'Library').mkdir(parents=True)
    real_resolve = sync.resolve_root
    redirected = []

    def resolve_then_swap(location):
        root = real_resolve(location)
        if location.id == target.id and not redirected:
            configured_root.parent.rename(original_root / 'saved')
            configured_root.parent.symlink_to(outside, target_is_directory=True)
            redirected.append(True)
        return root

    monkeypatch.setattr(sync, 'resolve_root', resolve_then_swap)
    result = execute_pending(conn, copy_fn=_copy, now=400)
    assert redirected
    assert not (outside / 'Library/Artist/Album/01.flac').exists()
    assert result.copied == 0 and result.failed == 1
