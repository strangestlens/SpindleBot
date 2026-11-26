"""COPY must verify the bytes captured by the plan, not a later observation."""
from __future__ import annotations

import shutil
import struct
from pathlib import Path

import mutagen.flac
import pytest

from spindlebot.core.enums import ContentKind, LocationKind
from spindlebot.core.identity import file_sha256
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    action_repo,
    location_repo,
    presence_repo,
    sidecar_presence_repo,
)
from spindlebot.services import volumes
from spindlebot.services.inventory import inventory_location
from spindlebot.services.reconciler import reconcile_location
from spindlebot.services.sync import execute_pending


def _write_flac(path: Path, *, audio_md5_bytes: bytes = b"\x01" * 16) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    streaminfo = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00" * 6
        + struct.pack(">Q", (44100 << 44) | (15 << 36))
        + audio_md5_bytes
    )
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 0x22]) + streaminfo)
    track = mutagen.flac.FLAC(path)
    track.add_tags()
    track["artist"] = ["Artist"]
    track["album"] = ["Album"]
    track["title"] = ["Track"]
    track.save()


@pytest.fixture(params=[ContentKind.AUDIO, ContentKind.SIDECAR])
def planned_copy(request, tmp_path):
    with open_db(tmp_path / "spindlebot.db") as conn:
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
        audio_path = Path(source.root_path) / "Artist/Album/01.flac"
        _write_flac(audio_path)
        audio_path.with_suffix(".lrc").write_text("[00:01.00]Original lyrics\n")
        for loc in locations:
            inventory_location(conn, location=loc, root=Path(loc.root_path), now=100)
        plan = reconcile_location(conn, target=target, source_locations=[source], now=200)
        action = next(a for a in action_repo.list_for_run(conn, plan.run_id)
                      if a.content_kind == request.param)
        action_repo.acknowledge(conn, [action.id], now=201)
        repo = presence_repo if request.param == ContentKind.AUDIO else sidecar_presence_repo
        yield conn, source, target, action, repo


def _copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


@pytest.mark.parametrize("refresh_presence", [False, True])
def test_replaced_source_is_rejected_before_copy(planned_copy, refresh_presence):
    conn, source, target, action, repo = planned_copy
    src = Path(source.root_path) / action.rel_path
    original = src.read_bytes()
    # Retagging keeps decoded-audio identity but changes the expected file bytes.
    if action.content_kind == ContentKind.AUDIO:
        track = mutagen.flac.FLAC(src)
        track["title"] = ["Replacement title"]
        track.save()
    else:
        src.write_text("[00:02.00]Replacement lyrics\n")
    assert src.read_bytes() != original
    if refresh_presence:
        inventory_location(conn, location=source, root=Path(source.root_path), now=300)
        assert repo.get(conn, action.content_id, source.id).file_sha256 == file_sha256(src)

    calls = []

    def recording_copy(src, dst):
        calls.append((src, dst))
        _copy(src, dst)

    result = execute_pending(conn, copy_fn=recording_copy, now=400)
    assert result.failed == 1 and result.copied == 0
    assert calls == []
    assert repo.get(conn, action.content_id, target.id) is None
    assert action_repo.get(conn, action.id).executed_utc is None
    assert "source" in result.errors[0] and "planned" in result.errors[0]
    assert not (Path(target.root_path) / action.rel_path).exists()

    # Restoring the exact planned bytes permits retry without changing the plan.
    src.write_bytes(original)
    retry = execute_pending(conn, copy_fn=_copy, now=500)
    assert retry.copied == 1 and retry.failed == 0


def test_unchanged_planned_source_copies_successfully(planned_copy):
    conn, source, target, action, repo = planned_copy
    expected = file_sha256(Path(source.root_path) / action.rel_path)
    result = execute_pending(conn, copy_fn=_copy, now=400)
    assert result.copied == 1 and result.failed == 0
    assert repo.get(conn, action.content_id, target.id).file_sha256 == expected
    assert action_repo.get(conn, action.id).executed_utc == 400


def test_source_changed_during_copy_never_records_presence(planned_copy):
    conn, source, target, action, repo = planned_copy

    def changing_copy(src, dst):
        src.write_bytes(src.read_bytes() + b"changed during transfer")
        _copy(src, dst)

    result = execute_pending(conn, copy_fn=changing_copy, now=400)
    assert result.failed == 1 and result.copied == 0
    assert repo.get(conn, action.content_id, target.id) is None
    assert action_repo.get(conn, action.id).executed_utc is None


@pytest.mark.parametrize("expected", [None, ""])
def test_missing_planned_hash_is_refused_without_copy(planned_copy, expected):
    conn, source, target, action, repo = planned_copy
    # Model a pre-v9 action (NULL) or an unavailable inventory checksum (empty).
    conn.execute("UPDATE pending_action SET expected_sha256 = ? WHERE id = ?",
                 (expected, action.id))
    calls = []
    result = execute_pending(conn, copy_fn=lambda s, d: calls.append((s, d)), now=400)
    assert result.failed == 1 and result.copied == 0
    assert calls == []
    assert repo.get(conn, action.content_id, target.id) is None
    assert action_repo.get(conn, action.id).executed_utc is None
    assert "missing planned source sha256" in result.errors[0]


def test_plan_keeps_its_hash_after_presence_refresh(planned_copy):
    conn, source, target, action, repo = planned_copy
    original_sha = file_sha256(Path(source.root_path) / action.rel_path)
    assert action.expected_sha256 == original_sha
    src = Path(source.root_path) / action.rel_path
    src.write_bytes(src.read_bytes() + b"changed")
    inventory_location(conn, location=source, root=Path(source.root_path), now=300)
    assert repo.get(conn, action.content_id, source.id).file_sha256 != original_sha
    conn.commit()
    assert action_repo.get(conn, action.id).expected_sha256 == original_sha


@pytest.mark.parametrize("planned_copy", [ContentKind.AUDIO], indirect=True)
def test_different_recording_cannot_be_copied_under_planned_identity(planned_copy):
    conn, source, target, action, repo = planned_copy
    src = Path(source.root_path) / action.rel_path
    _write_flac(src, audio_md5_bytes=b"\x02" * 16)
    inventory_location(conn, location=source, root=Path(source.root_path), now=300)
    result = execute_pending(conn, copy_fn=_copy, now=400)
    assert result.failed == 1 and result.copied == 0
    assert repo.get(conn, action.content_id, target.id) is None
    assert action_repo.get(conn, action.id).executed_utc is None
    assert not (Path(target.root_path) / action.rel_path).exists()
