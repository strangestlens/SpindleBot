"""Tests for the `spindlebot review` CLI command (plan + acknowledge)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from spindlebot.cli import cmd_review
from spindlebot.config import LocationConfig
from spindlebot.core.enums import LocationKind, RunKind
from spindlebot.core.identity import ContentId
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    action_repo,
    audio_repo,
    presence_repo,
    run_repo,
    scan_repo,
)
from spindlebot.services.locations import get_by_name, register_from_config


def _cfg(tmp_path):
    core = SimpleNamespace(
        db_path=tmp_path / "spindlebot.db",
        min_copies=1,
        pending_dir=tmp_path / "Pending",
    )
    locations = [LocationConfig(name="DwRugged", kind=LocationKind.LOCAL_DRIVE,
                               root_path="", is_retention=True)]
    return SimpleNamespace(core=core, locations=locations, destinations=[])


def _seed_copy_scenario(cfg):
    """Audio present on authoritative Pending, absent on retention DwRugged."""
    conn = open_db(cfg.core.db_path)
    register_from_config(conn, cfg, 0)
    pending = get_by_name(conn, "Pending")
    rugged = get_by_name(conn, "DwRugged")
    audio = audio_repo.upsert(conn, ContentId("audio_md5", "x" * 32), now=0,
                              artist="Boards of Canada", title="Roygbiv")
    presence_repo.set_presence(conn, audio_id=audio.id, location_id=pending.id,
                               present=True, observed_utc=0, rel_path="BoC/Roygbiv.flac")
    scan_repo.start_scan(conn, rugged.id, 1)   # target has been inventoried
    conn.commit()
    conn.close()


def test_review_plan_lists_copy_action(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seed_copy_scenario(cfg)
    rc = cmd_review(cfg, ["--location", "DwRugged"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 to copy" in out
    assert "Boards of Canada — Roygbiv" in out
    assert "--acknowledge-run" in out   # tells the user how to confirm


def test_review_json_shape(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seed_copy_scenario(cfg)
    rc = cmd_review(cfg, ["--location", "DwRugged", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["copies"] == 1
    assert data["location"] == "DwRugged"
    assert data["actions"][0]["kind"] == "copy"
    assert data["actions"][0]["content_kind"] == "audio"


def test_review_warns_when_target_never_inventoried(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    # register DwRugged but never scan it
    conn = open_db(cfg.core.db_path)
    register_from_config(conn, cfg, 0)
    conn.commit()
    conn.close()
    rc = cmd_review(cfg, ["--location", "DwRugged"])
    assert rc == 1
    assert "never been inventoried" in capsys.readouterr().err


def test_review_unknown_location_fails(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    rc = cmd_review(cfg, ["--location", "Nope"])
    assert rc == 1
    assert "Unknown location" in capsys.readouterr().err


def test_review_requires_location_in_plan_mode(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    rc = cmd_review(cfg, [])
    assert rc == 1
    assert "Usage" in capsys.readouterr().err


def test_review_acknowledge_run(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seed_copy_scenario(cfg)
    cmd_review(cfg, ["--location", "DwRugged"])   # creates run + 1 copy action
    capsys.readouterr()

    conn = open_db(cfg.core.db_path)
    run = run_repo.latest(conn, RunKind.RECONCILE)
    conn.close()

    rc = cmd_review(cfg, ["--acknowledge-run", str(run.id)])
    assert rc == 0
    assert "Acknowledged 1 action" in capsys.readouterr().out

    conn = open_db(cfg.core.db_path)
    actions = action_repo.list_for_run(conn, run.id)
    conn.close()
    assert len(actions) == 1 and all(a.acknowledged for a in actions)


def test_review_verbose_emits_progress_to_stderr_stdout_stays_json(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seed_copy_scenario(cfg)
    rc = cmd_review(cfg, ["--location", "DwRugged", "--verbose", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    # progress on stderr, results on stdout — stdout stays pure JSON
    data = json.loads(captured.out)
    assert data["copies"] == 1
    assert "[1/" in captured.err          # the per-action verbose line


def test_review_quiet_suppresses_progress(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seed_copy_scenario(cfg)
    rc = cmd_review(cfg, ["--location", "DwRugged", "--quiet", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "[1/" not in captured.err


def test_review_acknowledge_specific_ids(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seed_copy_scenario(cfg)
    cmd_review(cfg, ["--location", "DwRugged"])
    capsys.readouterr()

    conn = open_db(cfg.core.db_path)
    run = run_repo.latest(conn, RunKind.RECONCILE)
    action_id = action_repo.list_for_run(conn, run.id)[0].id
    conn.close()

    rc = cmd_review(cfg, ["--acknowledge", str(action_id), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["acknowledged"] == 1 and data["ids"] == [action_id]
