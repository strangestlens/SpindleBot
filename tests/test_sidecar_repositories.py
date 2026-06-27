"""Tests for the sidecar DB repositories (album, sidecar_content, sidecar_presence)."""
from __future__ import annotations

import pytest

from spindlebot.core.enums import SidecarParentKind, SidecarRole
from spindlebot.core.identity import ContentId
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    album_repo,
    audio_repo,
    location_repo,
    sidecar_presence_repo,
    sidecar_repo,
)


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


# ── enums ─────────────────────────────────────────────────────────────────────

def test_sidecar_enums_closed_sets():
    assert set(SidecarRole) == {SidecarRole.LRC, SidecarRole.COVER, SidecarRole.NOLRC}
    assert set(SidecarParentKind) == {SidecarParentKind.TRACK, SidecarParentKind.ALBUM}
    with pytest.raises(ValueError):
        SidecarRole("jpg")
    with pytest.raises(ValueError):
        SidecarParentKind("disc")


# ── album_repo ────────────────────────────────────────────────────────────────

def test_album_upsert_inserts_then_fetches(conn):
    al = album_repo.upsert(conn, album_key="k1", now=1000,
                           albumartist="Boards", album="Geogaddi")
    assert al.id > 0
    assert al.album_key == "k1" and al.album == "Geogaddi"
    assert al.first_seen_utc == 1000 and al.last_seen_utc == 1000
    assert album_repo.get_by_key(conn, "k1") == al
    assert album_repo.count(conn) == 1


def test_album_upsert_preserves_first_seen_and_mb_albumid(conn):
    album_repo.upsert(conn, album_key="k1", now=1000, mb_albumid="mb-1")
    second = album_repo.upsert(conn, album_key="k1", now=2000)  # no mb id this pass
    assert second.first_seen_utc == 1000
    assert second.last_seen_utc == 2000
    assert second.mb_albumid == "mb-1"   # COALESCE keeps it
    assert album_repo.count(conn) == 1


def test_album_link_track_is_idempotent(conn):
    al = album_repo.upsert(conn, album_key="k1", now=0)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    b = audio_repo.upsert(conn, ContentId("audio_md5", "b" * 32), now=0)
    album_repo.link_track(conn, al.id, a.id)
    album_repo.link_track(conn, al.id, a.id)   # duplicate, ignored
    album_repo.link_track(conn, al.id, b.id)
    assert album_repo.list_track_ids(conn, al.id) == sorted([a.id, b.id])


# ── sidecar_repo ──────────────────────────────────────────────────────────────

def _album(conn):
    return album_repo.upsert(conn, album_key="k1", now=0)


def test_sidecar_upsert_inserts(conn):
    al = _album(conn)
    sc = sidecar_repo.upsert(conn, parent_kind=SidecarParentKind.ALBUM,
                             parent_id=al.id, role=SidecarRole.COVER,
                             sha256="cover-hash", now=1000)
    assert sc.id > 0
    assert sc.parent_kind == SidecarParentKind.ALBUM
    assert sc.role == SidecarRole.COVER
    assert sc.sha256 == "cover-hash"
    assert sidecar_repo.count(conn) == 1


def test_sidecar_upsert_updates_hash_in_place(conn):
    al = _album(conn)
    first = sidecar_repo.upsert(conn, parent_kind="album", parent_id=al.id,
                                role="cover", sha256="h1", now=1000)
    second = sidecar_repo.upsert(conn, parent_kind="album", parent_id=al.id,
                                 role="cover", sha256="h2", now=2000)
    assert second.id == first.id          # same row (triple unchanged)
    assert second.sha256 == "h2"
    assert second.first_seen_utc == 1000 and second.last_seen_utc == 2000
    assert sidecar_repo.count(conn) == 1


def test_sidecar_distinct_roles_and_parents_coexist(conn):
    al = _album(conn)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    sidecar_repo.upsert(conn, parent_kind="album", parent_id=al.id,
                        role="cover", sha256="h", now=0)
    sidecar_repo.upsert(conn, parent_kind="album", parent_id=al.id,
                        role="nolrc", sha256="h", now=0)
    sidecar_repo.upsert(conn, parent_kind="track", parent_id=a.id,
                        role="lrc", sha256="h", now=0)
    assert sidecar_repo.count(conn) == 3
    parent = sidecar_repo.list_for_parent(conn, parent_kind="album", parent_id=al.id)
    assert {s.role for s in parent} == {SidecarRole.COVER, SidecarRole.NOLRC}


def test_sidecar_get_returns_none_when_absent(conn):
    al = _album(conn)
    assert sidecar_repo.get(conn, parent_kind="album", parent_id=al.id,
                            role="cover") is None


# ── sidecar_presence_repo ─────────────────────────────────────────────────────

def _cover(conn):
    al = _album(conn)
    return sidecar_repo.upsert(conn, parent_kind="album", parent_id=al.id,
                               role="cover", sha256="h", now=0)


def test_sidecar_presence_set_and_get(conn):
    loc = location_repo.upsert(conn, uuid="u1", name="Pending", kind="library")
    sc = _cover(conn)
    p = sidecar_presence_repo.set_presence(
        conn, sidecar_id=sc.id, location_id=loc.id, present=True, observed_utc=10,
        rel_path="Artist/Album/cover.jpg", file_sha256="deadbeef", byte_size=999)
    assert p.present is True
    assert p.rel_path == "Artist/Album/cover.jpg"
    assert sidecar_presence_repo.get(conn, sc.id, loc.id) == p


def test_sidecar_presence_update_flips_present(conn):
    loc = location_repo.upsert(conn, uuid="u1", name="X", kind="local_drive")
    sc = _cover(conn)
    sidecar_presence_repo.set_presence(conn, sidecar_id=sc.id, location_id=loc.id,
                                       present=True, observed_utc=10)
    sidecar_presence_repo.set_presence(conn, sidecar_id=sc.id, location_id=loc.id,
                                       present=False, observed_utc=20)
    p = sidecar_presence_repo.get(conn, sc.id, loc.id)
    assert p.present is False and p.observed_utc == 20
    assert len(sidecar_presence_repo.list_for_location(conn, loc.id)) == 1


def test_sidecar_presence_present_filter(conn):
    loc = location_repo.upsert(conn, uuid="u1", name="X", kind="local_drive")
    al = _album(conn)
    here = sidecar_repo.upsert(conn, parent_kind="album", parent_id=al.id,
                               role="cover", sha256="h", now=0)
    gone = sidecar_repo.upsert(conn, parent_kind="album", parent_id=al.id,
                               role="nolrc", sha256="h", now=0)
    sidecar_presence_repo.set_presence(conn, sidecar_id=here.id, location_id=loc.id,
                                       present=True, observed_utc=1)
    sidecar_presence_repo.set_presence(conn, sidecar_id=gone.id, location_id=loc.id,
                                       present=False, observed_utc=1)
    present = sidecar_presence_repo.list_for_location(conn, loc.id, present=True)
    assert [p.sidecar_id for p in present] == [here.id]
