"""Tests for spindlebot.services.locations (config -> location rows)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from spindlebot.config import DestinationConfig, LocationConfig
from spindlebot.core.enums import LocationKind
from spindlebot.db.connection import open_db
from spindlebot.services.locations import (
    get_by_name,
    location_uuid,
    register_from_config,
)


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


def _cfg(pending_dir, *, locations=None, destinations=None):
    return SimpleNamespace(
        core=SimpleNamespace(pending_dir=pending_dir),
        locations=locations or [],
        destinations=destinations or [],
    )


def test_location_uuid_is_stable_and_case_insensitive():
    assert location_uuid("DwRugged") == location_uuid(" dwrugged ")
    assert location_uuid("A") != location_uuid("B")


def test_location_kind_is_a_closed_set():
    assert set(LocationKind) == {
        LocationKind.LIBRARY, LocationKind.LOCAL_DRIVE, LocationKind.RCLONE
    }
    with pytest.raises(ValueError):
        LocationKind("dvd-binder")


def test_register_creates_pending_authoring_location(conn, tmp_path):
    register_from_config(conn, _cfg(tmp_path / "Pending"), now=100)
    pending = get_by_name(conn, "Pending")
    assert pending is not None
    assert pending.kind == LocationKind.LIBRARY
    assert pending.is_authoritative_audio is True
    assert pending.is_retention is False
    assert pending.root_path == str(tmp_path / "Pending")


def test_register_explicit_location(conn, tmp_path):
    loc = LocationConfig(name="DwRugged", kind=LocationKind.LOCAL_DRIVE,
                         root_path="/Volumes/DwRugged/Music/Library", is_retention=True)
    register_from_config(conn, _cfg(tmp_path / "Pending", locations=[loc]), now=100)
    rugged = get_by_name(conn, "DwRugged")
    assert rugged.kind == LocationKind.LOCAL_DRIVE
    assert rugged.is_retention is True
    assert rugged.root_path == "/Volumes/DwRugged/Music/Library"


def test_legacy_destination_becomes_retention_location(conn, tmp_path):
    dest = DestinationConfig(name="Backblaze", type="rclone", path="b2:music/Library")
    register_from_config(conn, _cfg(tmp_path / "Pending", destinations=[dest]), now=100)
    bb = get_by_name(conn, "Backblaze")
    assert bb.kind == LocationKind.RCLONE
    assert bb.is_retention is True
    assert bb.root_path == "b2:music/Library"


def test_explicit_location_wins_over_same_named_destination(conn, tmp_path):
    loc = LocationConfig(name="DwRugged", kind=LocationKind.LOCAL_DRIVE,
                         root_path="/Volumes/DwRugged/Music/Library")
    dest = DestinationConfig(name="DwRugged", type="local_drive", path="/old/path")
    register_from_config(conn, _cfg(tmp_path / "Pending",
                                    locations=[loc], destinations=[dest]), now=100)
    rugged = get_by_name(conn, "DwRugged")
    assert rugged.root_path == "/Volumes/DwRugged/Music/Library"  # not the destination's
    # Pending + DwRugged only — destination did not create a second row
    from spindlebot.db.repositories import location_repo
    assert len(location_repo.list_all(conn)) == 2


def test_disabled_entries_are_skipped(conn, tmp_path):
    loc = LocationConfig(name="Off", kind=LocationKind.LOCAL_DRIVE, enabled=False)
    dest = DestinationConfig(name="AlsoOff", type="local_drive", path="/x", enabled=False)
    register_from_config(conn, _cfg(tmp_path / "Pending",
                                    locations=[loc], destinations=[dest]), now=100)
    assert get_by_name(conn, "Off") is None
    assert get_by_name(conn, "AlsoOff") is None


def test_register_is_idempotent(conn, tmp_path):
    loc = LocationConfig(name="DwRugged", kind=LocationKind.LOCAL_DRIVE,
                         root_path="/Volumes/DwRugged/Music/Library")
    cfg = _cfg(tmp_path / "Pending", locations=[loc])
    first = register_from_config(conn, cfg, now=100)
    second = register_from_config(conn, cfg, now=200)
    assert [loc.id for loc in first] == [loc.id for loc in second]
    from spindlebot.db.repositories import location_repo
    assert len(location_repo.list_all(conn)) == 2
