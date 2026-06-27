"""Tests for the lyric_doc/lyric_version + conflict repositories (Phase 4 substrate)."""
from __future__ import annotations

import pytest

from spindlebot.core import vclock as vc
from spindlebot.core.enums import ConflictStatus
from spindlebot.core.identity import ContentId
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import audio_repo, conflict_repo, lyric_repo


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


def _audio(conn, identity="a" * 32):
    return audio_repo.upsert(conn, ContentId("audio_md5", identity), now=0)


# ── lyric_repo ────────────────────────────────────────────────────────────────

def test_ensure_doc_is_idempotent(conn):
    a = _audio(conn)
    d1 = lyric_repo.ensure_doc(conn, a.id, now=1000)
    d2 = lyric_repo.ensure_doc(conn, a.id, now=2000)
    assert d1.id == d2.id
    assert d2.created_utc == 1000        # preserved
    assert d1.head_version_id is None


def test_add_version_appends_and_touches_doc(conn):
    a = _audio(conn)
    doc = lyric_repo.ensure_doc(conn, a.id, now=1000)
    v1 = lyric_repo.add_version(conn, doc_id=doc.id, sha256="h1",
                                vclock_json=vc.to_json({"Pending": 1}),
                                source="scan", authored_utc=1000, now=1500)
    v2 = lyric_repo.add_version(conn, doc_id=doc.id, sha256="h2",
                                vclock_json=vc.to_json({"Pending": 2}),
                                source="edit", now=2000)
    assert [v.id for v in lyric_repo.list_versions(conn, doc.id)] == [v1.id, v2.id]
    assert lyric_repo.get_doc_by_id(conn, doc.id).updated_utc == 2000
    assert vc.from_json(v2.vclock_json) == {"Pending": 2}


def test_set_head_and_head_version(conn):
    a = _audio(conn)
    doc = lyric_repo.ensure_doc(conn, a.id, now=0)
    assert lyric_repo.head_version(conn, doc.id) is None
    v = lyric_repo.add_version(conn, doc_id=doc.id, sha256="h",
                              vclock_json=vc.to_json({"A": 1}), now=10)
    lyric_repo.set_head(conn, doc.id, v.id, now=20)
    assert lyric_repo.head_version(conn, doc.id).id == v.id


def test_doc_unique_per_track_and_versions_cascade(conn):
    a = _audio(conn)
    doc = lyric_repo.ensure_doc(conn, a.id, now=0)
    lyric_repo.add_version(conn, doc_id=doc.id, sha256="h",
                          vclock_json="{}", now=0)
    # deleting the underlying track cascades doc -> versions
    conn.execute("DELETE FROM audio_content WHERE id = ?", (a.id,))
    assert lyric_repo.get_doc(conn, a.id) is None
    assert conn.execute("SELECT COUNT(*) FROM lyric_version").fetchone()[0] == 0


# ── conflict_repo ─────────────────────────────────────────────────────────────

def test_open_resolve_and_list_conflicts(conn):
    a = _audio(conn)
    c = conflict_repo.open_conflict(conn, audio_id=a.id, winner_version=1,
                                    loser_version=2,
                                    loser_kept_path="x.conflict-2026.lrc", now=100)
    assert c.status == ConflictStatus.OPEN
    assert [x.id for x in conflict_repo.list_open(conn)] == [c.id]
    assert conflict_repo.find_open_for_audio(conn, a.id).id == c.id

    conflict_repo.resolve(conn, c.id, now=200)
    assert conflict_repo.get(conn, c.id).status == ConflictStatus.RESOLVED
    assert conflict_repo.get(conn, c.id).resolved_utc == 200
    assert conflict_repo.list_open(conn) == []
    assert conflict_repo.find_open_for_audio(conn, a.id) is None
