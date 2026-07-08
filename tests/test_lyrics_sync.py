"""Tests for the lyric lineage service (services/lyrics_sync.py). DB reasoning only."""
from __future__ import annotations

import pytest

from spindlebot.core import vclock
from spindlebot.core.identity import ContentId
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    audio_repo,
    location_repo,
    lyric_repo,
    lyric_version_presence_repo,
)
from spindlebot.services import lyrics_sync
from spindlebot.services.lyrics_sync import LyricObservation


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    # lyric_version_presence.location_id is a real FK — the actor locations must exist.
    for uuid, name in [("a", "A"), ("b", "B")]:
        location_repo.upsert(c, uuid=uuid, name=name, kind="local_drive")
    yield c
    c.close()


def _audio(conn, identity="a" * 32):
    return audio_repo.upsert(conn, ContentId("audio_md5", identity), now=0)


def _obs(location_id, name, sha):
    return LyricObservation(location_id=location_id, name=name, sha=sha)


def _held(lineage, location_id):
    return next(h for h in lineage.held if h.location_id == location_id)


# ── agreement ─────────────────────────────────────────────────────────────────

def test_identical_lyrics_share_one_version_no_conflict(conn):
    a = _audio(conn)
    lineage = lyrics_sync.reconcile_doc(
        conn, audio_id=a.id, now=100,
        observations=[_obs(1, "A", "s1"), _obs(2, "B", "s1")],
    )
    versions = lyric_repo.list_versions(conn, lineage.doc_id)
    assert len(versions) == 1
    assert lineage.head_version_id == versions[0].id
    assert lineage.concurrent == []
    assert _held(lineage, 1).is_head and _held(lineage, 2).is_head


# ── linear edit ───────────────────────────────────────────────────────────────

def test_linear_edit_advances_head_and_dominates_old(conn):
    a = _audio(conn)
    # both start holding s1
    lyrics_sync.reconcile_doc(conn, audio_id=a.id, now=100,
                              observations=[_obs(1, "A", "s1"), _obs(2, "B", "s1")])
    doc_versions = lyric_repo.list_versions(conn, lyric_repo.get_doc(conn, a.id).id)
    v1 = doc_versions[0]

    # A improves the lyric; B unchanged
    lineage = lyrics_sync.reconcile_doc(
        conn, audio_id=a.id, now=200,
        observations=[_obs(1, "A", "s2"), _obs(2, "B", "s1")],
    )
    head = lyric_repo.head_version(conn, lineage.doc_id)
    assert head.id != v1.id
    assert vclock.strictly_dominates(vclock.from_json(head.vclock_json),
                                     vclock.from_json(v1.vclock_json))
    assert lineage.concurrent == []
    # A holds the new head; presence updated to it
    assert _held(lineage, 1).is_head
    assert lyric_version_presence_repo.get(conn, lineage.doc_id, 1).version_id == head.id
    # B holds the old version → behind, not a conflict
    assert _held(lineage, 2).is_behind
    assert lyric_version_presence_repo.get(conn, lineage.doc_id, 2).version_id == v1.id


# ── concurrent conflict ───────────────────────────────────────────────────────

def test_concurrent_edits_from_same_base_are_concurrent(conn):
    a = _audio(conn)
    lyrics_sync.reconcile_doc(conn, audio_id=a.id, now=100,
                              observations=[_obs(1, "A", "s1"), _obs(2, "B", "s1")])
    # A and B both edit off the shared base, independently
    lineage = lyrics_sync.reconcile_doc(
        conn, audio_id=a.id, now=200,
        observations=[_obs(1, "A", "s2"), _obs(2, "B", "s3")],
    )
    # one side becomes the (provisional) head; the other is concurrent with it
    assert len(lineage.concurrent) == 1
    conc = lineage.concurrent[0]
    head = lyric_repo.head_version(conn, lineage.doc_id)
    conc_v = lyric_repo.get_version(conn, conc.version_id)
    assert vclock.concurrent(vclock.from_json(head.vclock_json),
                             vclock.from_json(conc_v.vclock_json))
    # three distinct versions total (base + two edits); nothing auto-resolved
    assert len(lyric_repo.list_versions(conn, lineage.doc_id)) == 3


# ── behind / stale ────────────────────────────────────────────────────────────

def test_stale_location_recognized_as_behind_not_edit(conn):
    a = _audio(conn)
    lyrics_sync.reconcile_doc(conn, audio_id=a.id, now=100,
                              observations=[_obs(1, "A", "s1"), _obs(2, "B", "s1")])
    # head advances at A
    lyrics_sync.reconcile_doc(conn, audio_id=a.id, now=200,
                              observations=[_obs(1, "A", "s2"), _obs(2, "B", "s1")])
    versions_before = len(lyric_repo.list_versions(conn, lyric_repo.get_doc(conn, a.id).id))

    # B is scanned again still holding the OLD sha while head has moved on
    lineage = lyrics_sync.reconcile_doc(
        conn, audio_id=a.id, now=300, observations=[_obs(2, "B", "s1")],
    )
    b = _held(lineage, 2)
    assert b.is_behind and not b.is_concurrent and not b.is_head
    assert lineage.concurrent == []
    # no false new version was minted for the stale observation
    assert len(lyric_repo.list_versions(conn, lineage.doc_id)) == versions_before


# ── idempotency ───────────────────────────────────────────────────────────────

def test_rerun_with_unchanged_shas_is_idempotent(conn):
    a = _audio(conn)
    obs = [_obs(1, "A", "s2"), _obs(2, "B", "s3")]
    lyrics_sync.reconcile_doc(conn, audio_id=a.id, now=100,
                              observations=[_obs(1, "A", "s1"), _obs(2, "B", "s1")])
    first = lyrics_sync.reconcile_doc(conn, audio_id=a.id, now=200, observations=obs)
    n_after_first = len(lyric_repo.list_versions(conn, first.doc_id))
    head_after_first = lyric_repo.head_version(conn, first.doc_id).id

    second = lyrics_sync.reconcile_doc(conn, audio_id=a.id, now=300, observations=obs)
    assert len(lyric_repo.list_versions(conn, second.doc_id)) == n_after_first
    assert lyric_repo.head_version(conn, second.doc_id).id == head_after_first
    assert len(second.concurrent) == len(first.concurrent) == 1
