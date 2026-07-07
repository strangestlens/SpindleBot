"""
Tests for spindlebot.pipeline.runner — ImportRunner orchestration logic.

Strategy: mock check_wait, pretag, posttag (already tested elsewhere) and
subprocess.run (external tools). Assert on ImportResult stages and watcher.log.
"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import mutagen.flac
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
        pending_dir=tmp_path / "Library",
        import_dir=staging,
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


def test_non_log_trigger_logs_and_exits(tmp_path):
    cfg = _make_config(tmp_path, trigger=tmp_path / "Staging" / "track.flac")
    result = ImportRunner(cfg).run()
    assert result.success
    assert result.stages == []
    assert log_contains(cfg, "Nothing to import")
    assert not log_contains(cfg, "Detected")


def test_non_log_trigger_echo_message(tmp_path):
    cfg = _make_config(tmp_path, trigger=tmp_path / "Staging" / "track.flac")
    echoed = []
    ImportRunner(cfg, echo=echoed.append).run()
    assert any("Nothing to import" in m for m in echoed)


def test_missing_log_exits_cleanly(tmp_path):
    cfg = _make_config(tmp_path)  # trigger default = nonexistent Album.log
    result = ImportRunner(cfg).run()
    assert result.success
    assert result.stages == []
    assert log_contains(cfg, "Already imported")
    assert not log_contains(cfg, "Detected completed rip")


def test_directory_trigger_detects_album_dir(tmp_path):
    album_dir = tmp_path / "Staging" / "Yin Yin - The Rabbit That Hunts Tigers"
    album_dir.mkdir(parents=True)
    cfg = _make_config(tmp_path, trigger=album_dir)

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, side_effect=Exception("stop")):
        try:
            ImportRunner(cfg).run()
        except Exception:
            pass

    assert log_contains(cfg, "Detected album directory")
    assert not log_contains(cfg, "Detected completed rip")


def test_directory_trigger_skips_archive(tmp_path):
    album_dir = tmp_path / "Staging" / "Album"
    album_dir.mkdir(parents=True)
    cfg = _make_config(tmp_path, trigger=album_dir)
    _init_db(cfg)
    # Plant a .log in staging — it should NOT be archived for a dir-triggered run
    stray_log = cfg.import_dir / "Album.log"
    stray_log.touch()

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_COUNT_DISCS, return_value=1), \
         patch(_SUBPROCESS, side_effect=_successful_subprocess_sequence(cfg.pending_dir)):
        ImportRunner(cfg).run()

    assert stray_log.exists(), "Directory-triggered import must not archive .log files"
    assert not log_contains(cfg, "log archived")


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
    assert log_contains(cfg, "waiting for the rest")
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
    assert log_contains(cfg, "disc check")


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
    assert log_contains(cfg, "disc check skipped (--force)")


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
    assert log_contains(cfg, "beet import failed")


# ── happy path ────────────────────────────────────────────────────────────────


def test_successful_import_all_stages(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=2), \
         patch(_COUNT_DISCS, return_value=1), \
         patch(_SUBPROCESS, side_effect=_successful_subprocess_sequence(cfg.pending_dir)):
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
         patch(_SUBPROCESS, side_effect=_successful_subprocess_sequence(cfg.pending_dir)):
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
         patch(_SUBPROCESS, side_effect=_successful_subprocess_sequence(cfg.pending_dir)):
        ImportRunner(cfg).run()

    text = cfg.log_file.read_text()
    assert "Watcher fired" in text
    assert "Detected completed rip" in text
    assert "disc check" in text
    assert "Running pretag" in text
    assert "Starting beet import" in text
    assert "Import complete" in text


# ── album-aware isolation in a flat, mixed Import ─────────────────────────────


def _write_flac(path: Path, *, tags: dict) -> None:
    """Write a minimal valid FLAC carrying the given Vorbis tags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    streaminfo = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00\x00\x00\x00\x00\x00"
        + struct.pack(">Q", (44100 << 44) | (0 << 41) | (15 << 36) | 0)
        + b"\x00" * 16
    )
    path.write_bytes(b"fLaC" + bytes([0x80, 0x00, 0x00, 0x22]) + streaminfo)
    f = mutagen.flac.FLAC(str(path))
    f.add_tags()
    for k, v in tags.items():
        f[k] = [str(v)]
    f.save()


def _beet_import_calls(mock_sub) -> list[list[str]]:
    """Return the argv of every `beet import ...` subprocess call."""
    calls = []
    for c in mock_sub.call_args_list:
        argv = c.args[0]
        if len(argv) >= 2 and argv[1] == "import":
            calls.append(list(argv))
    return calls


def _stub_beet(argv, *args, **kwargs):
    """side_effect for every beet subprocess call (import / ls / move).

    Returns a benign success for all of them so the pipeline's post-import
    ls/move/ls-paths queries don't blow up; the assertions inspect the recorded
    `import` argvs.
    """
    return MagicMock(returncode=0, stdout="", stderr="")


def test_mixed_import_imports_each_album_separately(tmp_path):
    """Two complete albums in one flat Import → two isolated beet imports,
    each fed only its own files (never the mixed pile)."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    imp = cfg.import_dir
    _write_flac(imp / "rh1.flac", tags={"albumartist": "Radiohead", "album": "Kid A",
                                        "discnumber": 1, "disctotal": 1})
    _write_flac(imp / "dp1.flac", tags={"albumartist": "Daft Punk", "album": "Discovery",
                                        "discnumber": 1, "disctotal": 1})

    with patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_SUBPROCESS, side_effect=_stub_beet) as mock_sub:
        result = ImportRunner(cfg).run()

    assert result.success
    import_calls = _beet_import_calls(mock_sub)
    assert len(import_calls) == 2, "each album must get its own beet import"

    # No import call may mix files from both albums.
    for argv in import_calls:
        targets = argv[2:]
        has_rh = any("rh1.flac" in t for t in targets)
        has_dp = any("dp1.flac" in t for t in targets)
        assert not (has_rh and has_dp), f"beet import mixed two albums: {targets}"
        # And no call passed the whole Import directory.
        assert str(imp) not in targets


def test_waiting_multidisc_does_not_block_or_contaminate_complete_album(tmp_path):
    """A stranded incomplete 2-disc set must be left intact while a complete
    album imports — the Radiohead-stranded / Daft-Punk-flattened bug."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    imp = cfg.import_dir
    # Incomplete 2-disc set: only disc 1 present → should WAIT, be skipped.
    _write_flac(imp / "rh_d1.flac", tags={"albumartist": "Radiohead", "album": "OK Computer",
                                          "discnumber": 1, "disctotal": 2})
    # Complete single-disc album that must still import.
    _write_flac(imp / "dp1.flac", tags={"albumartist": "Daft Punk", "album": "Discovery",
                                        "discnumber": 1, "disctotal": 1})

    with patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_SUBPROCESS, side_effect=_stub_beet) as mock_sub:
        result = ImportRunner(cfg).run()

    assert result.success
    import_calls = _beet_import_calls(mock_sub)
    # Exactly one album imported — the complete one.
    assert len(import_calls) == 1
    targets = import_calls[0][2:]
    assert any("dp1.flac" in t for t in targets)
    assert not any("rh_d1.flac" in t for t in targets), \
        "the waiting multi-disc album must not be imported"

    # The waiting album's file is left untouched in Import.
    assert (imp / "rh_d1.flac").exists()
    assert log_contains(cfg, "waiting for the rest")


def test_mixed_import_per_album_disctotal_is_correct(tmp_path):
    """count_discs / multidisc fix must read the specific album's files, not
    everything in Import. A 2-disc album alongside a 1-disc album must get
    disctotal=2 while the single stays 1."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    imp = cfg.import_dir
    # Complete 2-disc album.
    _write_flac(imp / "md_d1.flac", tags={"albumartist": "Set", "album": "Two Disc",
                                          "discnumber": 1, "disctotal": 2})
    _write_flac(imp / "md_d2.flac", tags={"albumartist": "Set", "album": "Two Disc",
                                          "discnumber": 2, "disctotal": 2})
    # Single-disc album.
    _write_flac(imp / "single.flac", tags={"albumartist": "Solo", "album": "One Disc",
                                           "discnumber": 1, "disctotal": 1})

    seen_disc_counts = []
    real_fix = ImportRunner._fix_multidisc

    def _capture_fix(self, actual_discs, import_start):
        seen_disc_counts.append(actual_discs)
        return real_fix(self, actual_discs, import_start)

    with patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch.object(ImportRunner, "_fix_multidisc", _capture_fix), \
         patch(_SUBPROCESS, side_effect=_stub_beet):
        result = ImportRunner(cfg).run()

    assert result.success
    # One fix call per album, with the correct per-album disc counts.
    assert sorted(seen_disc_counts) == [1, 2], \
        f"per-album disc counts wrong: {seen_disc_counts}"
