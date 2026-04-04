"""
Tests for spindlebot.pipeline.runner — ImportRunner orchestration logic.

Strategy: mock check_wait, pretag, posttag (already tested elsewhere) and
subprocess.run (external tools). Assert on ImportResult stages and watcher.log.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spindlebot.pipeline.runner import ImportConfig, ImportRunner


# ── helpers ───────────────────────────────────────────────────────────────────

_PRETAG = "spindlebot.pipeline.runner.pretag"
_POSTTAG = "spindlebot.pipeline.runner.posttag"
_CHECK_WAIT = "spindlebot.pipeline.runner.check_wait"
_COUNT_DISCS = "spindlebot.pipeline.runner.count_discs"
_SUBPROCESS = "spindlebot.pipeline.runner.subprocess.run"


def _make_config(tmp_path: Path, *, force: bool = False, trigger: Path | None = None) -> ImportConfig:
    staging = tmp_path / "Staging"
    staging.mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "AllDiscs").mkdir(exist_ok=True)
    (tmp_path / "Library").mkdir(exist_ok=True)
    (tmp_path / "pipeline").mkdir(exist_ok=True)
    db = tmp_path / "library.db"
    db.touch()

    return ImportConfig(
        trigger=trigger if trigger is not None else staging / "Album.log",
        force=force,
        beet=tmp_path / "bin" / "beet",
        python=tmp_path / "bin" / "python",
        db=db,
        library=tmp_path / "Library",
        staging=staging,
        archive=tmp_path / "AllDiscs",
        pipeline_dir=tmp_path / "pipeline",
        log_file=tmp_path / "logs" / "watcher.log",
    )


def _successful_subprocess_sequence(library: Path) -> list[MagicMock]:
    """Standard subprocess.run side_effect for a happy-path import."""
    return [
        MagicMock(returncode=0, stdout="", stderr=""),               # beet import
        MagicMock(returncode=0, stdout="Artist - Album\n", stderr=""), # beet ls (album name)
        MagicMock(returncode=0),                                      # music-notify.sh
        MagicMock(returncode=0, stdout="", stderr=""),                # beet modify
        MagicMock(returncode=0, stdout="", stderr=""),                # beet move
        MagicMock(                                                    # beet ls (paths)
            returncode=0,
            stdout=str(library / "track.flac") + "\n",
            stderr="",
        ),
        MagicMock(returncode=0),                                      # fetch-lyrics
    ]


def log_contains(cfg: ImportConfig, text: str) -> bool:
    try:
        return text in cfg.log_file.read_text()
    except FileNotFoundError:
        return False


def _init_db(cfg: ImportConfig) -> None:
    with sqlite3.connect(str(cfg.db)) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path TEXT, added INTEGER)")
        conn.execute("CREATE TABLE item_attributes (entity_id INTEGER, key TEXT, value TEXT)")


# ── early-exit tests ──────────────────────────────────────────────────────────


def test_non_log_trigger_exits_cleanly(tmp_path):
    cfg = _make_config(tmp_path, trigger=tmp_path / "Staging" / "track.flac")
    result = ImportRunner(cfg).run()
    assert result.success
    assert result.stages == []
    assert not log_contains(cfg, "Detected completed rip")


def test_missing_log_exits_cleanly(tmp_path):
    cfg = _make_config(tmp_path)  # trigger default = nonexistent Album.log
    result = ImportRunner(cfg).run()
    assert result.success
    assert result.stages == []
    assert not log_contains(cfg, "Detected completed rip")


# ── disc check tests ──────────────────────────────────────────────────────────


def test_disc_check_holds_on_wait(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()

    with patch(_CHECK_WAIT, return_value="WAIT:1:2"):
        result = ImportRunner(cfg).run()

    assert result.success  # WAIT is not a failure — just waiting
    stage = result.stages[0]
    assert stage.name == "disc_check"
    assert "waiting" in stage.message
    assert log_contains(cfg, "waiting for remaining discs")
    assert log_contains(cfg, "--force")


def test_disc_check_hold_stops_further_stages(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()

    with patch(_CHECK_WAIT, return_value="WAIT:1:2"), patch(_PRETAG) as mock_pretag:
        ImportRunner(cfg).run()

    mock_pretag.assert_not_called()


def test_disc_check_pass_proceeds_to_pretag(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True) as mock_pretag, \
         patch(_SUBPROCESS, side_effect=Exception("stop")):
        try:
            ImportRunner(cfg).run()
        except Exception:
            pass

    mock_pretag.assert_called_once()
    assert log_contains(cfg, "Disc check passed")


def test_force_skips_disc_check(tmp_path):
    cfg = _make_config(tmp_path, force=True)
    cfg.trigger.touch()

    with patch(_CHECK_WAIT) as mock_check, \
         patch(_PRETAG, side_effect=Exception("stop")):
        try:
            ImportRunner(cfg).run()
        except Exception:
            pass

    mock_check.assert_not_called()
    assert log_contains(cfg, "Disc check skipped (--force)")


# ── pretag / beet import failure tests ───────────────────────────────────────


def test_pretag_failure_aborts_import(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, side_effect=RuntimeError("bad tags")), \
         patch(_SUBPROCESS) as mock_sub:
        result = ImportRunner(cfg).run()

    assert not result.success
    mock_sub.assert_not_called()  # beet import must not run
    assert log_contains(cfg, "pretag failed")


def test_beet_import_failure_aborts_pipeline(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()

    beet_fail = MagicMock(returncode=1, stdout="", stderr="beet error")
    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_SUBPROCESS, return_value=beet_fail):
        result = ImportRunner(cfg).run()

    assert not result.success
    stage = next(s for s in result.stages if s.name == "beet_import")
    assert not stage.success
    assert log_contains(cfg, "Import FAILED")


# ── happy path ────────────────────────────────────────────────────────────────


def test_successful_import_all_stages(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=2), \
         patch(_COUNT_DISCS, return_value=1), \
         patch(_SUBPROCESS, side_effect=_successful_subprocess_sequence(cfg.library)):
        result = ImportRunner(cfg).run()

    assert result.success
    completed = {s.name for s in result.stages}
    assert {"disc_check", "pretag", "beet_import", "multidisc", "posttag"} <= completed


def test_successful_import_archives_log(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_COUNT_DISCS, return_value=1), \
         patch(_SUBPROCESS, side_effect=_successful_subprocess_sequence(cfg.library)):
        ImportRunner(cfg).run()

    assert not cfg.trigger.exists(), "Log file should be archived"
    assert (cfg.archive / "Album.log").exists(), "Log should appear in archive"


def test_log_messages_written_to_watcher_log(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_COUNT_DISCS, return_value=1), \
         patch(_SUBPROCESS, side_effect=_successful_subprocess_sequence(cfg.library)):
        ImportRunner(cfg).run()

    text = cfg.log_file.read_text()
    assert "Watcher fired" in text
    assert "Detected completed rip" in text
    assert "Disc check passed" in text
    assert "Running pretag" in text
    assert "Starting beet import" in text
    assert "Import complete" in text
