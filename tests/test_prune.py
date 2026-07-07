"""Tests for prune_released — release Pending files verified on retention. Destructive."""
from __future__ import annotations

from pathlib import Path

import pytest

from spindlebot.core.enums import SidecarParentKind, SidecarRole
from spindlebot.core.identity import ContentId, file_sha256
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    album_repo,
    audio_repo,
    location_repo,
    presence_repo,
    sidecar_presence_repo,
    sidecar_repo,
)
from spindlebot.services import volumes
from spindlebot.services.sync import prune_released


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


def _loc(conn, uuid, name, root, *, authoritative=False, retention=False):
    root.mkdir(parents=True, exist_ok=True)
    volumes.ensure_marker(root, uuid=uuid, name=name, now=0)
    return location_repo.upsert(
        conn, uuid=uuid, name=name,
        kind="library" if authoritative else "local_drive",
        root_path=str(root), is_authoritative_audio=authoritative, is_retention=retention)


def _put(loc, rel, data=b"lossless-bytes" * 64):
    f = Path(loc.root_path) / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


def _pending(conn, tmp_path):
    return _loc(conn, "pending", "Pending", tmp_path / "Pending", authoritative=True)


def _rugged(conn, tmp_path):
    return _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", retention=True)


def _retained_audio(conn, tmp_path, rel="Artist/Album/01.flac"):
    """Same audio file on Pending AND DwRugged at the same path/hash."""
    pending, rugged = _pending(conn, tmp_path), _rugged(conn, tmp_path)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    pf, rf = _put(pending, rel), _put(rugged, rel)
    sha = file_sha256(pf)
    for loc in (pending, rugged):
        presence_repo.set_presence(conn, audio_id=a.id, location_id=loc.id, present=True,
                                   observed_utc=0, rel_path=rel, file_sha256=sha, byte_size=pf.stat().st_size)
    return pending, rugged, a, rel, pf, rf


def test_prunes_a_retained_file_and_marks_it_absent(conn, tmp_path):
    pending, rugged, a, rel, pf, rf = _retained_audio(conn, tmp_path)
    result = prune_released(conn, now=1000, dry_run=False)

    assert result.pruned == 1 and result.bytes_freed > 0
    assert not pf.exists()                       # Pending copy released
    assert rf.exists()                           # retained copy untouched
    assert presence_repo.get(conn, a.id, pending.id).present is False
    assert presence_repo.get(conn, a.id, rugged.id).present is True


def test_dry_run_reports_but_deletes_nothing(conn, tmp_path):
    pending, rugged, a, rel, pf, rf = _retained_audio(conn, tmp_path)
    result = prune_released(conn, now=1000, dry_run=True)
    assert result.pruned == 1 and result.dry_run is True
    assert pf.exists()                           # nothing actually deleted
    assert presence_repo.get(conn, a.id, pending.id).present is True


def test_never_prunes_when_not_on_retention(conn, tmp_path):
    pending = _pending(conn, tmp_path)
    _rugged(conn, tmp_path)                       # exists but doesn't hold the file
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    pf = _put(pending, "x.flac")
    presence_repo.set_presence(conn, audio_id=a.id, location_id=pending.id, present=True,
                               observed_utc=0, rel_path="x.flac", file_sha256=file_sha256(pf),
                               byte_size=pf.stat().st_size)
    result = prune_released(conn, now=1000, dry_run=False)
    assert result.pruned == 0 and result.skipped == 1
    assert pf.exists()                           # kept — the only copy


def test_never_prunes_on_hash_mismatch(conn, tmp_path):
    pending, rugged, a, rel, pf, rf = _retained_audio(conn, tmp_path)
    # corrupt the retained copy on disk AND its recorded hash so it no longer matches
    rf.write_bytes(b"corrupted")
    presence_repo.set_presence(conn, audio_id=a.id, location_id=rugged.id, present=True,
                               observed_utc=0, rel_path=rel, file_sha256="deadbeef", byte_size=9)
    result = prune_released(conn, now=1000, dry_run=False)
    assert result.pruned == 0 and result.skipped == 1
    assert pf.exists()                           # kept — retained copy not trustworthy


def test_never_prunes_when_retained_file_gone_from_disk(conn, tmp_path):
    pending, rugged, a, rel, pf, rf = _retained_audio(conn, tmp_path)
    rf.unlink()                                  # DB says present, but it's gone
    result = prune_released(conn, now=1000, dry_run=False)
    assert result.pruned == 0 and result.skipped == 1
    assert pf.exists()


def test_never_prunes_a_retention_location(conn, tmp_path):
    # content retained on two retention drives; neither is the authoring library
    rugged = _rugged(conn, tmp_path)
    dap = _loc(conn, "dap", "DAP", tmp_path / "DAP", retention=True)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rf, df = _put(rugged, "y.flac"), _put(dap, "y.flac")
    sha = file_sha256(rf)
    for loc in (rugged, dap):
        presence_repo.set_presence(conn, audio_id=a.id, location_id=loc.id, present=True,
                                   observed_utc=0, rel_path="y.flac", file_sha256=sha,
                                   byte_size=rf.stat().st_size)
    result = prune_released(conn, now=1000, dry_run=False)
    assert result.pruned == 0                    # retention locations are never prunable
    assert rf.exists() and df.exists()


def test_prunes_a_sidecar_verified_on_retention(conn, tmp_path):
    pending, rugged = _pending(conn, tmp_path), _rugged(conn, tmp_path)
    album = album_repo.upsert(conn, album_key="k", now=0)
    sc = sidecar_repo.upsert(conn, parent_kind=SidecarParentKind.ALBUM, parent_id=album.id,
                             role=SidecarRole.COVER, sha256="h", now=0)
    rel = "Artist/Album/cover.jpg"
    pf, rf = _put(pending, rel, b"\xff\xd8jpeg"), _put(rugged, rel, b"\xff\xd8jpeg")
    sha = file_sha256(pf)
    for loc in (pending, rugged):
        sidecar_presence_repo.set_presence(conn, sidecar_id=sc.id, location_id=loc.id,
                                           present=True, observed_utc=0, rel_path=rel,
                                           file_sha256=sha, byte_size=pf.stat().st_size)
    result = prune_released(conn, now=1000, dry_run=False)
    assert result.pruned == 1 and not pf.exists() and rf.exists()


def test_path_verified_keeps_a_cover_retained_at_a_different_path(conn, tmp_path):
    # per-disc cover: same sidecar, but Pending path != the retained path → keep it.
    pending, rugged = _pending(conn, tmp_path), _rugged(conn, tmp_path)
    album = album_repo.upsert(conn, album_key="k", now=0)
    sc = sidecar_repo.upsert(conn, parent_kind=SidecarParentKind.ALBUM, parent_id=album.id,
                             role=SidecarRole.COVER, sha256="h", now=0)
    pf = _put(pending, "Album [Disc 1]/cover.jpg", b"\xff\xd8jpeg")
    rf = _put(rugged, "Album [Disc 2]/cover.jpg", b"\xff\xd8jpeg")
    sha = file_sha256(pf)
    sidecar_presence_repo.set_presence(conn, sidecar_id=sc.id, location_id=pending.id,
                                       present=True, observed_utc=0,
                                       rel_path="Album [Disc 1]/cover.jpg",
                                       file_sha256=sha, byte_size=pf.stat().st_size)
    sidecar_presence_repo.set_presence(conn, sidecar_id=sc.id, location_id=rugged.id,
                                       present=True, observed_utc=0,
                                       rel_path="Album [Disc 2]/cover.jpg",
                                       file_sha256=sha, byte_size=rf.stat().st_size)
    result = prune_released(conn, now=1000, dry_run=False)
    assert result.pruned == 0 and result.skipped == 1
    assert pf.exists()                           # different path on retention → not safe


def test_prune_cleans_up_empty_dirs(conn, tmp_path):
    pending, rugged, a, rel, pf, rf = _retained_audio(conn, tmp_path)
    prune_released(conn, now=1000, dry_run=False)
    # Artist/Album/ was the only content → removed once empty; root + marker remain
    assert not (Path(pending.root_path) / "Artist").exists()
    assert volumes.read_marker(Path(pending.root_path)) is not None


def test_prune_warns_when_left_below_min_copies(conn, tmp_path):
    # one retention copy, min_copies=2 → released but flagged single-copy
    pending, rugged, a, rel, pf, rf = _retained_audio(conn, tmp_path)
    result = prune_released(conn, now=1000, dry_run=False, min_copies=2)
    assert result.pruned == 1 and result.below_floor == 1
    assert not pf.exists()   # still released — at-first-copy, warning not a gate


def test_prune_no_warning_when_at_min_copies(conn, tmp_path):
    pending, rugged = _pending(conn, tmp_path), _rugged(conn, tmp_path)
    dap = _loc(conn, "dap", "DAP", tmp_path / "DAP", retention=True)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rel = "A/01.flac"
    pf, rf, df = _put(pending, rel), _put(rugged, rel), _put(dap, rel)
    sha = file_sha256(pf)
    for loc in (pending, rugged, dap):
        presence_repo.set_presence(conn, audio_id=a.id, location_id=loc.id, present=True,
                                   observed_utc=0, rel_path=rel, file_sha256=sha,
                                   byte_size=pf.stat().st_size)
    result = prune_released(conn, now=1000, dry_run=False, min_copies=2)
    assert result.pruned == 1 and result.below_floor == 0   # two retention copies


# ── CLI: dry-run default, --execute to delete ─────────────────────────────────

def _put_at(root: Path, rel: str, data=b"lossless" * 64) -> Path:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


def _cli_cfg_and_seed(tmp_path):
    from types import SimpleNamespace

    from spindlebot.config import LocationConfig
    from spindlebot.core.enums import LocationKind
    from spindlebot.services.locations import get_by_name, register_from_config

    core = SimpleNamespace(db_path=tmp_path / "spindlebot.db", min_copies=2,
                           pending_dir=tmp_path / "Pending")
    locs = [LocationConfig(name="DwRugged", kind=LocationKind.LOCAL_DRIVE,
                           root_path=str(tmp_path / "DwRugged"), is_retention=True)]
    cfg = SimpleNamespace(core=core, locations=locs, destinations=[])

    conn = open_db(core.db_path)
    register_from_config(conn, cfg, 0)
    pending, rugged = get_by_name(conn, "Pending"), get_by_name(conn, "DwRugged")
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rel = "A/B/01.flac"
    pf = _put_at(core.pending_dir, rel)
    rf = _put_at(tmp_path / "DwRugged", rel)
    sha = file_sha256(pf)
    for loc in (pending, rugged):
        presence_repo.set_presence(conn, audio_id=a.id, location_id=loc.id, present=True,
                                   observed_utc=0, rel_path=rel, file_sha256=sha,
                                   byte_size=pf.stat().st_size)
    conn.commit()
    conn.close()
    return cfg, pf, rf


def test_cmd_prune_is_dry_run_by_default(tmp_path, capsys):
    import json
    from spindlebot.cli import cmd_prune
    cfg, pf, rf = _cli_cfg_and_seed(tmp_path)
    rc = cmd_prune(cfg, ["--json"])                 # no --execute
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and data["dry_run"] is True and data["pruned"] == 1
    assert pf.exists()                              # nothing deleted without --execute


def test_cmd_prune_execute_deletes(tmp_path, capsys):
    import json
    from spindlebot.cli import cmd_prune
    cfg, pf, rf = _cli_cfg_and_seed(tmp_path)
    rc = cmd_prune(cfg, ["--execute", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and data["dry_run"] is False and data["pruned"] == 1
    assert not pf.exists() and rf.exists()
