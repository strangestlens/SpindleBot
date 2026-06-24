"""Tests for the DB repository layer (audio_content, location, audio_presence)."""
from __future__ import annotations

import pytest

from spindlebot.core.identity import ContentId
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import audio_repo, location_repo, presence_repo


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


# ── location_repo ─────────────────────────────────────────────────────────────

def test_location_upsert_inserts_then_fetches(conn):
    loc = location_repo.upsert(conn, uuid="u1", name="Pending", kind="library",
                               is_authoritative_audio=True)
    assert loc.id > 0
    assert loc.name == "Pending"
    assert loc.is_authoritative_audio is True
    assert loc.is_retention is False
    assert location_repo.get_by_uuid(conn, "u1") == loc


def test_location_upsert_updates_in_place(conn):
    first = location_repo.upsert(conn, uuid="u1", name="Old", kind="local_drive")
    second = location_repo.upsert(conn, uuid="u1", name="New", kind="local_drive",
                                  is_retention=True)
    assert second.id == first.id          # same row
    assert second.name == "New"
    assert second.is_retention is True
    assert len(location_repo.list_all(conn)) == 1


# ── audio_repo ────────────────────────────────────────────────────────────────

def test_audio_upsert_inserts(conn):
    cid = ContentId("audio_md5", "a" * 32)
    a = audio_repo.upsert(conn, cid, now=1000, artist="Boards", title="Roygbiv")
    assert a.id > 0
    assert a.identity == "a" * 32
    assert a.identity_kind == "audio_md5"
    assert a.first_seen_utc == 1000 and a.last_seen_utc == 1000
    assert audio_repo.count(conn) == 1


def test_audio_upsert_preserves_first_seen_and_updates_last_seen(conn):
    cid = ContentId("audio_md5", "b" * 32)
    first = audio_repo.upsert(conn, cid, now=1000, title="v1")
    second = audio_repo.upsert(conn, cid, now=2000, title="v2")
    assert second.id == first.id
    assert second.first_seen_utc == 1000   # preserved
    assert second.last_seen_utc == 2000    # advanced
    assert second.title == "v2"
    assert audio_repo.count(conn) == 1


def test_audio_upsert_does_not_clear_beets_item_id(conn):
    cid = ContentId("audio_md5", "c" * 32)
    audio_repo.upsert(conn, cid, now=1000, beets_item_id=42)
    refreshed = audio_repo.upsert(conn, cid, now=2000)  # no beets id this time
    assert refreshed.beets_item_id == 42   # COALESCE keeps it


# ── presence_repo ─────────────────────────────────────────────────────────────

def _audio(conn, identity):
    return audio_repo.upsert(conn, ContentId("audio_md5", identity), now=0)


def test_presence_set_and_get(conn):
    loc = location_repo.upsert(conn, uuid="u1", name="Pending", kind="library")
    a = _audio(conn, "a" * 32)
    p = presence_repo.set_presence(conn, audio_id=a.id, location_id=loc.id,
                                   present=True, observed_utc=10,
                                   rel_path="Artist/Album/01.flac",
                                   file_sha256="deadbeef", byte_size=123)
    assert p.present is True
    assert p.rel_path == "Artist/Album/01.flac"
    assert presence_repo.get(conn, a.id, loc.id) == p


def test_presence_update_flips_present(conn):
    loc = location_repo.upsert(conn, uuid="u1", name="X", kind="local_drive")
    a = _audio(conn, "a" * 32)
    presence_repo.set_presence(conn, audio_id=a.id, location_id=loc.id,
                               present=True, observed_utc=10)
    presence_repo.set_presence(conn, audio_id=a.id, location_id=loc.id,
                               present=False, observed_utc=20)
    p = presence_repo.get(conn, a.id, loc.id)
    assert p.present is False and p.observed_utc == 20
    # still one row, not two
    assert len(presence_repo.list_for_location(conn, loc.id)) == 1


def test_list_for_location_present_filter(conn):
    loc = location_repo.upsert(conn, uuid="u1", name="X", kind="local_drive")
    here = _audio(conn, "a" * 32)
    gone = _audio(conn, "b" * 32)
    presence_repo.set_presence(conn, audio_id=here.id, location_id=loc.id,
                               present=True, observed_utc=1)
    presence_repo.set_presence(conn, audio_id=gone.id, location_id=loc.id,
                               present=False, observed_utc=1)
    present = presence_repo.list_for_location(conn, loc.id, present=True)
    assert [p.audio_id for p in present] == [here.id]


def test_count_retention_copies_only_counts_present_retention(conn):
    pending = location_repo.upsert(conn, uuid="lib", name="Pending", kind="library",
                                   is_authoritative_audio=True, is_retention=False)
    rugged = location_repo.upsert(conn, uuid="rug", name="DwRugged", kind="local_drive",
                                  is_retention=True)
    sdcard = location_repo.upsert(conn, uuid="sd", name="DAP", kind="local_drive",
                                  is_retention=True)
    a = _audio(conn, "a" * 32)
    # present on non-retention Pending — must NOT count
    presence_repo.set_presence(conn, audio_id=a.id, location_id=pending.id,
                               present=True, observed_utc=1)
    # present on retention DwRugged — counts
    presence_repo.set_presence(conn, audio_id=a.id, location_id=rugged.id,
                               present=True, observed_utc=1)
    # absent on retention SD card — must NOT count
    presence_repo.set_presence(conn, audio_id=a.id, location_id=sdcard.id,
                               present=False, observed_utc=1)
    assert presence_repo.count_retention_copies(conn, a.id) == 1
