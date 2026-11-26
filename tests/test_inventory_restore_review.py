"""Inventory must not authorize old occupants or revive a rolled-back scan."""
from __future__ import annotations

from pathlib import Path

import pytest

from spindlebot.core.enums import (
    ActionKind, ContentKind, IdentityKind, ScanStatus, SidecarParentKind, SidecarRole,
)
from spindlebot.core.identity import ContentId
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    action_repo,
    audio_repo,
    presence_repo,
    scan_repo,
    sidecar_presence_repo,
    sidecar_repo,
)
from spindlebot.services import inventory
from spindlebot.services.reconciler import reconcile_location
from tests.test_restore_missing import _write_flac, seeded as _seeded


@pytest.fixture
def seeded(tmp_path):
    yield from _seeded.__wrapped__(tmp_path)


@pytest.mark.parametrize("kind", [ContentKind.AUDIO, ContentKind.SIDECAR])
def test_observed_replacement_retires_the_previous_occupant(seeded, kind):
    conn, source, target = seeded
    repo = presence_repo if kind == ContentKind.AUDIO else sidecar_presence_repo
    name = "01.flac" if kind == ContentKind.AUDIO else "01.lrc"
    rel = "Artist/Album/" + name
    prior = next(p for p in repo.list_for_location(conn, target.id) if p.rel_path == rel)
    prior_id = prior.audio_id if kind == ContentKind.AUDIO else prior.sidecar_id

    # Same path, different recording: its track sidecars acquire a different parent too.
    audio = Path(target.root_path) / "Artist/Album/01.flac"
    _write_flac(audio, audio_md5_bytes=b"\x02" * 16)
    inventory.inventory_location(conn, location=target, root=target.root_path, now=100, rehash=True)
    retired = repo.get(conn, prior_id, target.id)
    assert not retired.present and retired.rel_path is None
    assert retired.absence_scan_id is None

    (Path(target.root_path) / rel).unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    assert not any(a.action_kind == ActionKind.COPY and a.content_kind == kind
                   and a.content_id == prior_id
                   for a in action_repo.list_for_run(conn, plan.run_id))


@pytest.mark.parametrize("kind", [ContentKind.AUDIO, ContentKind.SIDECAR])
def test_missing_path_with_ambiguous_historical_occupants_is_not_authorization(seeded, kind):
    conn, source, target = seeded
    other = audio_repo.upsert(conn, ContentId(IdentityKind.AUDIO_MD5, "02" * 16), now=100)
    if kind == ContentKind.AUDIO:
        repo, content_id, id_field, name = presence_repo, other.id, "audio_id", "01.flac"
    else:
        sidecar = sidecar_repo.upsert(
            conn, parent_kind=SidecarParentKind.TRACK, parent_id=other.id,
            role=SidecarRole.LRC, sha256="replacement", now=100,
        )
        repo, content_id, id_field, name = sidecar_presence_repo, sidecar.id, "sidecar_id", "01.lrc"
    rel = "Artist/Album/" + name
    # Model a database from an older scan that recorded B without retiring A.
    repo.set_presence(conn, **{id_field: content_id}, location_id=target.id,
                      present=True, rel_path=rel, observed_utc=100)
    (Path(target.root_path) / rel).unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    plan = reconcile_location(conn, target=target, source_locations=[source], now=300)
    assert not any(a.action_kind == ActionKind.COPY and a.content_kind == kind
                   for a in action_repo.list_for_run(conn, plan.run_id))
    assert all(p.rel_path != rel for p in repo.list_for_location(conn, target.id))


@pytest.mark.parametrize("preparation", ["index", "walk", "progress"])
def test_failed_preparation_blocks_old_absence_after_connection_closes(seeded, monkeypatch, preparation):
    conn, source, target = seeded
    dst = Path(target.root_path) / "Artist/Album/01.flac"
    dst.unlink()
    inventory.inventory_location(conn, location=target, root=target.root_path, now=200)
    conn.commit()
    previous_scan = scan_repo.latest_scan(conn, target.id)["id"]
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    dst.write_bytes(b"replacement")

    failure_type = KeyboardInterrupt if preparation == "progress" else RuntimeError

    def failed(*args, **kwargs):
        raise failure_type("preparation interrupted")

    kwargs = {}
    if preparation == "progress":
        kwargs["progress"] = failed
    else:
        monkeypatch.setattr(inventory, "_load_beets_index" if preparation == "index" else "_walk_tree", failed)
    with pytest.raises(failure_type, match="preparation interrupted"):
        inventory.inventory_location(
            conn, location=target, root=target.root_path, now=300,
            checkpoint=conn.commit, commit_every=0, **kwargs,
        )
    conn.close()  # Drop uncommitted finish_scan, as a caller does on failure.
    reopened = open_db(db_path)
    try:
        latest = scan_repo.latest_scan(reopened, target.id)
        assert latest["id"] != previous_scan and latest["status"] != ScanStatus.OK
        plan = reconcile_location(reopened, target=target, source_locations=[source], now=400)
        assert plan.copies == 0
    finally:
        reopened.close()
