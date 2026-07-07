"""Tests for execute_deletes — remove a RETENTION copy, gated on min_copies. Destructive."""
from __future__ import annotations

from pathlib import Path

import pytest

from spindlebot.core.enums import ActionKind, ContentKind, RunKind
from spindlebot.core.identity import ContentId, file_sha256
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    action_repo,
    audio_repo,
    location_repo,
    presence_repo,
    run_repo,
)
from spindlebot.services import volumes
from spindlebot.services.sync import execute_deletes


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


def _present(conn, audio, loc, rel, f):
    presence_repo.set_presence(conn, audio_id=audio.id, location_id=loc.id, present=True,
                               observed_utc=0, rel_path=rel, file_sha256=file_sha256(f),
                               byte_size=f.stat().st_size)


def _propose_delete(conn, *, audio, loc, rel, now=0, acknowledge=True):
    run_id = run_repo.start_run(conn, RunKind.RECONCILE, now=now)
    a = action_repo.add(conn, run_id=run_id, action_kind=ActionKind.DELETE,
                        content_kind=ContentKind.AUDIO, content_id=audio.id,
                        source_location_id=loc.id, rel_path=rel, now=now)
    if acknowledge:
        action_repo.acknowledge(conn, [a.id], now=now)
    return a


def test_deletes_when_retention_stays_at_or_above_floor(conn, tmp_path):
    # two retention copies, min_copies=1 → deleting one leaves 1 ≥ floor.
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", retention=True)
    dap = _loc(conn, "dap", "DAP", tmp_path / "DAP", retention=True)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rel = "Artist/Album/01.flac"
    rf, df = _put(rugged, rel), _put(dap, rel)
    _present(conn, a, rugged, rel, rf)
    _present(conn, a, dap, rel, df)
    action = _propose_delete(conn, audio=a, loc=dap, rel=rel)

    result = execute_deletes(conn, now=1000, dry_run=False, min_copies=1)

    assert result.deleted == 1 and result.refused == 0 and result.bytes_freed > 0
    assert not df.exists()                                # DAP copy removed
    assert rf.exists()                                   # other retention copy untouched
    assert presence_repo.get(conn, a.id, dap.id).present is False
    assert presence_repo.get(conn, a.id, rugged.id).present is True
    assert action_repo.get(conn, action.id).executed_utc == 1000


def test_refuses_when_delete_would_drop_below_min_copies(conn, tmp_path):
    # only one retention copy; deleting it would leave 0 < min_copies=1 → refuse.
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", retention=True)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rel = "Artist/Album/01.flac"
    rf = _put(rugged, rel)
    _present(conn, a, rugged, rel, rf)
    action = _propose_delete(conn, audio=a, loc=rugged, rel=rel)

    result = execute_deletes(conn, now=1000, dry_run=False, min_copies=1)

    # refusal is a safe, expected outcome — a warning (refused_reasons), NOT an error
    assert result.deleted == 0 and result.refused == 1
    assert result.refused_reasons and not result.errors
    assert rf.exists()                                   # file untouched
    assert presence_repo.get(conn, a.id, rugged.id).present is True   # unchanged
    assert action_repo.get(conn, action.id).executed_utc is None     # not executed


def test_refuses_when_source_is_not_a_retention_location(conn, tmp_path):
    # A DELETE queued against the non-retention Pending (authoring) library must be
    # refused outright — delete never unlinks authoring bytes (that's prune's job).
    # Even with a retention copy elsewhere (so the floor would pass), it's refused.
    pending = _loc(conn, "pending", "Pending", tmp_path / "Pending", authoritative=True)
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", retention=True)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rel = "Artist/Album/01.flac"
    pf, rf = _put(pending, rel), _put(rugged, rel)
    _present(conn, a, pending, rel, pf)
    _present(conn, a, rugged, rel, rf)
    action = _propose_delete(conn, audio=a, loc=pending, rel=rel)

    result = execute_deletes(conn, now=1000, dry_run=False, min_copies=1)

    assert result.deleted == 0 and result.refused == 1
    assert result.refused_reasons and not result.errors  # a warning, not an error
    assert pf.exists() and rf.exists()                   # authoring bytes untouched
    assert presence_repo.get(conn, a.id, pending.id).present is True
    assert action_repo.get(conn, action.id).executed_utc is None


def test_unacknowledged_delete_never_runs(conn, tmp_path):
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", retention=True)
    dap = _loc(conn, "dap", "DAP", tmp_path / "DAP", retention=True)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rel = "Artist/Album/01.flac"
    rf, df = _put(rugged, rel), _put(dap, rel)
    _present(conn, a, rugged, rel, rf)
    _present(conn, a, dap, rel, df)
    action = _propose_delete(conn, audio=a, loc=dap, rel=rel, acknowledge=False)

    result = execute_deletes(conn, now=1000, dry_run=False, min_copies=1)

    assert result.deleted == 0 and result.refused == 0
    assert df.exists() and rf.exists()                   # nothing touched
    assert action_repo.get(conn, action.id).executed_utc is None


def test_dry_run_touches_nothing(conn, tmp_path):
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", retention=True)
    dap = _loc(conn, "dap", "DAP", tmp_path / "DAP", retention=True)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rel = "Artist/Album/01.flac"
    rf, df = _put(rugged, rel), _put(dap, rel)
    _present(conn, a, rugged, rel, rf)
    _present(conn, a, dap, rel, df)
    action = _propose_delete(conn, audio=a, loc=dap, rel=rel)

    result = execute_deletes(conn, now=1000, dry_run=True, min_copies=1)

    assert result.deleted == 1 and result.dry_run is True
    assert df.exists()                                   # nothing deleted on disk
    assert presence_repo.get(conn, a.id, dap.id).present is True   # presence unchanged
    assert action_repo.get(conn, action.id).executed_utc is None  # not marked executed


def test_non_retention_pending_copy_does_not_count_toward_floor(conn, tmp_path):
    # One retention copy + a non-retention Pending copy of the same content.
    # Deleting the retention copy would leave 0 retention copies — the Pending
    # copy must NOT make it look safe → refuse (proves the floor ignores Pending).
    pending = _loc(conn, "pending", "Pending", tmp_path / "Pending", authoritative=True)
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", retention=True)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rel = "Artist/Album/01.flac"
    pf, rf = _put(pending, rel), _put(rugged, rel)
    _present(conn, a, pending, rel, pf)
    _present(conn, a, rugged, rel, rf)
    action = _propose_delete(conn, audio=a, loc=rugged, rel=rel)

    result = execute_deletes(conn, now=1000, dry_run=False, min_copies=1)

    assert result.deleted == 0 and result.refused == 1
    assert rf.exists() and pf.exists()                   # both untouched
    assert action_repo.get(conn, action.id).executed_utc is None


def test_refuses_at_higher_min_copies(conn, tmp_path):
    # two retention copies but min_copies=2 → deleting one would leave 1 < 2 → refuse.
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", retention=True)
    dap = _loc(conn, "dap", "DAP", tmp_path / "DAP", retention=True)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rel = "Artist/Album/01.flac"
    rf, df = _put(rugged, rel), _put(dap, rel)
    _present(conn, a, rugged, rel, rf)
    _present(conn, a, dap, rel, df)
    _propose_delete(conn, audio=a, loc=dap, rel=rel)

    result = execute_deletes(conn, now=1000, dry_run=False, min_copies=2)

    assert result.deleted == 0 and result.refused == 1
    assert result.refused_reasons and not result.errors
    assert rf.exists() and df.exists()


def test_unmounted_source_is_a_genuine_error(conn, tmp_path):
    # DB says the copy is on a retention drive, but the drive isn't mounted (root
    # dir absent) → resolve_root fails → genuine error, NOT a refusal.
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", retention=True)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rel = "Artist/Album/01.flac"
    rf = _put(rugged, rel)
    _present(conn, a, rugged, rel, rf)
    _propose_delete(conn, audio=a, loc=rugged, rel=rel)
    import shutil
    shutil.rmtree(rugged.root_path)                      # "unmount" the drive

    result = execute_deletes(conn, now=1000, dry_run=False, min_copies=1)

    assert result.deleted == 0 and result.refused == 0
    assert result.errors and not result.refused_reasons  # a real failure, not a refusal


def test_only_processes_delete_actions(conn, tmp_path):
    # A COPY action in the queue must be ignored by the delete executor.
    from spindlebot.db.repositories import run_repo as rr
    rugged = _loc(conn, "rugged", "DwRugged", tmp_path / "DwRugged", retention=True)
    dap = _loc(conn, "dap", "DAP", tmp_path / "DAP", retention=True)
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rel = "Artist/Album/01.flac"
    rf, df = _put(rugged, rel), _put(dap, rel)
    _present(conn, a, rugged, rel, rf)
    _present(conn, a, dap, rel, df)
    run_id = rr.start_run(conn, RunKind.RECONCILE, now=0)
    copy = action_repo.add(conn, run_id=run_id, action_kind=ActionKind.COPY,
                           content_kind=ContentKind.AUDIO, content_id=a.id,
                           source_location_id=rugged.id, dest_location_id=dap.id,
                           rel_path=rel, now=0)
    action_repo.acknowledge(conn, [copy.id], now=0)

    result = execute_deletes(conn, now=1000, dry_run=False, min_copies=1)

    assert result.deleted == 0 and result.refused == 0 and result.skipped == 0
    assert rf.exists() and df.exists()


# ── CLI: dry-run default, --execute to delete ─────────────────────────────────


def _cli_cfg_and_seed(tmp_path, *, min_copies=1, delete_from="DAP"):
    """Seed a two-retention-drive + Pending library; queue a DELETE from
    `delete_from` ("DAP", "DwRugged", or "Pending"). Returns cfg + the file paths."""
    from types import SimpleNamespace

    from spindlebot.config import LocationConfig
    from spindlebot.core.enums import LocationKind
    from spindlebot.services.locations import get_by_name, register_from_config

    core = SimpleNamespace(db_path=tmp_path / "spindlebot.db", min_copies=min_copies,
                           pending_dir=tmp_path / "Pending")
    locs = [
        LocationConfig(name="DwRugged", kind=LocationKind.LOCAL_DRIVE,
                       root_path=str(tmp_path / "DwRugged"), is_retention=True),
        LocationConfig(name="DAP", kind=LocationKind.LOCAL_DRIVE,
                       root_path=str(tmp_path / "DAP"), is_retention=True),
    ]
    cfg = SimpleNamespace(core=core, locations=locs, destinations=[])

    for name in ("DwRugged", "DAP", "Pending"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)

    conn = open_db(core.db_path)
    register_from_config(conn, cfg, 0)
    rugged = get_by_name(conn, "DwRugged")
    dap = get_by_name(conn, "DAP")
    pending = get_by_name(conn, "Pending")
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=0)
    rel = "A/B/01.flac"
    rf, df, pf = _put(rugged, rel), _put(dap, rel), _put(pending, rel)
    _present(conn, a, rugged, rel, rf)
    _present(conn, a, dap, rel, df)
    _present(conn, a, pending, rel, pf)
    src = {"DwRugged": rugged, "DAP": dap, "Pending": pending}[delete_from]
    _propose_delete(conn, audio=a, loc=src, rel=rel)
    conn.commit()
    conn.close()
    return cfg, rf, df, pf


def test_cmd_delete_is_dry_run_by_default(tmp_path, capsys):
    import json

    from spindlebot.cli import cmd_delete
    cfg, rf, df, pf = _cli_cfg_and_seed(tmp_path)
    rc = cmd_delete(cfg, ["--json"])                 # no --execute
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and data["dry_run"] is True and data["deleted"] == 1
    assert df.exists() and rf.exists()               # nothing deleted without --execute


def test_cmd_delete_execute_deletes(tmp_path, capsys):
    import json

    from spindlebot.cli import cmd_delete
    cfg, rf, df, pf = _cli_cfg_and_seed(tmp_path)
    rc = cmd_delete(cfg, ["--execute", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0 and data["dry_run"] is False and data["deleted"] == 1
    assert not df.exists() and rf.exists()


def test_cmd_delete_below_floor_refusal_exits_zero(tmp_path, capsys):
    # min_copies=3 but only 2 retention copies → deleting one refused. Exit 0.
    import json

    from spindlebot.cli import cmd_delete
    cfg, rf, df, pf = _cli_cfg_and_seed(tmp_path, min_copies=3, delete_from="DAP")
    rc = cmd_delete(cfg, ["--execute", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0                                   # refusal is safe/expected → exit 0
    assert data["deleted"] == 0 and data["refused"] == 1
    assert data["refused_reasons"] and not data["errors"]
    assert df.exists() and rf.exists()               # nothing deleted


def test_cmd_delete_non_retention_refusal_exits_zero(tmp_path, capsys):
    # DELETE queued against the non-retention Pending library → refused. Exit 0.
    import json

    from spindlebot.cli import cmd_delete
    cfg, rf, df, pf = _cli_cfg_and_seed(tmp_path, delete_from="Pending")
    rc = cmd_delete(cfg, ["--execute", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["deleted"] == 0 and data["refused"] == 1
    assert data["refused_reasons"] and not data["errors"]
    assert pf.exists() and rf.exists() and df.exists()   # authoring bytes untouched


def test_cmd_delete_genuine_error_exits_nonzero(tmp_path, capsys):
    # The retention source drive isn't mounted → genuine error → nonzero exit.
    import json
    import shutil

    from spindlebot.cli import cmd_delete
    cfg, rf, df, pf = _cli_cfg_and_seed(tmp_path, delete_from="DAP")
    shutil.rmtree(tmp_path / "DAP")                   # "unmount" the source drive
    rc = cmd_delete(cfg, ["--execute", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1                                   # a real failure → nonzero
    assert data["errors"] and not data["refused_reasons"]
