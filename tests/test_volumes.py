"""Tests for spindlebot.services.volumes — marker files + root resolution.

All filesystem interaction uses tmp_path 'fake volumes'; nothing touches /Volumes.
"""
from __future__ import annotations

import json

import pytest

from spindlebot.core.errors import MarkerMismatch
from spindlebot.core.models import Location
from spindlebot.services import volumes


def _loc(uuid="u-rugged", root_path=None, name="DwRugged"):
    return Location(
        id=1, uuid=uuid, name=name, kind="local_drive",
        is_authoritative_audio=False, is_retention=True, enabled=True,
        last_seen_utc=None, root_path=str(root_path) if root_path else None,
    )


def test_write_then_read_marker_roundtrip(tmp_path):
    volumes.write_marker(tmp_path, uuid="u1", name="DwRugged", now=123)
    assert volumes.read_marker(tmp_path) == "u1"
    body = json.loads((tmp_path / ".spindlebot-location-u1").read_text())
    assert body == {"uuid": "u1", "name": "DwRugged", "written_utc": 123}


def test_read_marker_absent_or_missing_dir(tmp_path):
    assert volumes.read_marker(tmp_path) is None
    assert volumes.read_marker(tmp_path / "nope") is None


def test_ensure_marker_idempotent_for_same_uuid(tmp_path):
    volumes.ensure_marker(tmp_path, uuid="u1", name="X", now=1)
    volumes.ensure_marker(tmp_path, uuid="u1", name="X", now=2)  # refresh, no error
    assert volumes.read_marker(tmp_path) == "u1"
    body = json.loads((tmp_path / ".spindlebot-location-u1").read_text())
    assert body["written_utc"] == 2


def test_ensure_marker_raises_on_conflict(tmp_path):
    volumes.write_marker(tmp_path, uuid="other", name="SomeoneElse")
    with pytest.raises(MarkerMismatch):
        volumes.ensure_marker(tmp_path, uuid="u1", name="X")


def test_resolve_root_present_with_own_marker(tmp_path):
    volumes.write_marker(tmp_path, uuid="u-rugged", name="DwRugged")
    assert volumes.resolve_root(_loc(root_path=tmp_path)) == tmp_path


def test_resolve_root_present_without_marker(tmp_path):
    # No marker yet (first time) — still resolves; the marker gets written on inventory.
    assert volumes.resolve_root(_loc(root_path=tmp_path)) == tmp_path


def test_resolve_root_rejects_foreign_marker(tmp_path):
    volumes.write_marker(tmp_path, uuid="someone-else", name="Other")
    assert volumes.resolve_root(_loc(root_path=tmp_path)) is None


def test_resolve_root_not_mounted_or_no_path(tmp_path):
    assert volumes.resolve_root(_loc(root_path=tmp_path / "unmounted")) is None
    assert volumes.resolve_root(_loc(root_path=None)) is None
