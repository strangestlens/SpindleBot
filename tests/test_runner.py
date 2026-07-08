"""
Tests for spindlebot.pipeline.runner — ImportRunner orchestration logic.

Strategy: mock check_wait, pretag, posttag (already tested elsewhere) and
subprocess.run (external tools). Assert on ImportResult stages and watcher.log.
"""
from __future__ import annotations

import shutil
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
    (tmp_path / "Processing").mkdir(exist_ok=True)
    (tmp_path / "pipeline").mkdir(exist_ok=True)
    (tmp_path / "Duplicates").mkdir(exist_ok=True)
    db = tmp_path / "library.db"
    db.touch()

    return ImportConfig(
        trigger=trigger if trigger is not None else staging / "Album.log",
        force=force,
        beet=tmp_path / "bin" / "beet",
        python=tmp_path / "bin" / "python",
        db=db,
        pending_dir=tmp_path / "Library",
        processing_dir=tmp_path / "Processing",
        import_dir=staging,
        archive=tmp_path / "AllDiscs",
        duplicates_dir=tmp_path / "Duplicates",
        pipeline_dir=tmp_path / "pipeline",
        log_file=tmp_path / "logs" / "watcher.log",
    )


def _successful_subprocess_sequence(library: Path) -> list[MagicMock]:
    """Standard subprocess.run side_effect for a happy-path import.

    Mirrors the call order in ImportRunner: per-batch import, then the
    "added since import_start" ls (must report a NEW item so the batch isn't
    treated as an already-in-library duplicate), then the multidisc modify,
    then the run-wide name/move/paths queries.
    """
    return [
        MagicMock(returncode=0, stdout="", stderr=""),               # beet import
        MagicMock(                                                   # beet ls (_items_added_since)
            returncode=0,
            stdout=str(library / "track.flac") + "\n",
            stderr="",
        ),
        MagicMock(returncode=0, stdout="", stderr=""),                # beet modify (multidisc)
        MagicMock(returncode=0, stdout="Artist - Album\n", stderr=""), # beet ls (album name)
        MagicMock(returncode=0, stdout="", stderr=""),                # beet move
        MagicMock(                                                    # beet ls (paths)
            returncode=0,
            stdout=str(library / "track.flac") + "\n",
            stderr="",
        ),
        MagicMock(returncode=0),                                      # spare
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


def _fresh_import_stub_beet(argv, *args, **kwargs):
    """side_effect modeling beets where every imported album is NEW.

    The runner probes `beet ls -f $path added:<start>..` after each import to
    decide whether that album actually produced new items. Report a fabricated
    path for that forward-looking query so each batch imports normally (not a
    duplicate). Every other call (import / modify / move / backward-looking
    duplicate lookups) returns benign empty success.
    """
    argv = list(argv)
    if len(argv) >= 2 and argv[1] == "ls" and any(
        a.startswith("added:") and a.endswith("..") for a in argv
    ):
        return MagicMock(returncode=0, stdout="/lib/new/track.flac\n", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")


# Back-compat alias for existing mixed-import tests.
_stub_beet = _fresh_import_stub_beet


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


# ── Processing → promote flow ─────────────────────────────────────────────────
#
# NOTE: runner.subprocess.run and promote.subprocess.run are the SAME module
# attribute (`subprocess.run`). A single _SUBPROCESS patch therefore intercepts
# the promote service's `beet move` too — so _runner_beet_stub also models that
# move (relocating the album dir into Pending). Patching both paths separately
# would clobber each other.


def _mark_lrc(flac: Path) -> None:
    (flac.parent / (flac.stem + ".lrc")).write_text("[00:00.00] la\n")


def _runner_beet_stub(processing_dir: Path, album_paths: list[Path], pending_dir: Path):
    """subprocess.run side_effect for the runner in the Processing world.

    * `beet ls -f $path added:..` (per-batch verification AND run-wide paths
      query) → the real album file paths planted under processing_dir, so the
      fetch/promote loop operates on genuine on-disk albums.
    * `beet ls -f $albumartist - $album ...` → a label.
    * `beet move path:<dir>/` (the promote) → relocate that album dir's files
      into the NESTED pending_dir/<albumartist>/<album>/ layout (the album dir's
      parent is the artist), mirroring this repo's beets `paths.default`.
    * import / modify / the run-wide `beet move -d` → benign no-ops.
    """
    def stub(argv, *args, **kwargs):
        argv = list(argv)
        cmd = argv[1] if len(argv) >= 2 else ""
        if cmd == "ls":
            is_name = any(a == "$albumartist - $album" for a in argv)
            if is_name:
                return MagicMock(returncode=0, stdout="Artist - Album\n", stderr="")
            # Only report files that still live under processing_dir — once an
            # album promotes to Pending its paths must drop out of the window.
            present = [str(p) for p in album_paths if Path(p).exists()]
            return MagicMock(returncode=0, stdout="\n".join(present) + "\n", stderr="")
        if cmd == "move":
            path_arg = next((a for a in argv if a.startswith("path:")), None)
            if path_arg:  # the promote move (scoped to a single album)
                src = Path(path_arg[len("path:"):].rstrip("/"))
                if src.exists():
                    dest = pending_dir / src.parent.name / src.name
                    dest.mkdir(parents=True, exist_ok=True)
                    for f in list(src.iterdir()):
                        shutil.move(str(f), str(dest / f.name))
                    src.rmdir()
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    return stub


def test_runner_promotes_complete_album_to_pending(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)
    cfg.spindlebot_cfg = MagicMock()

    # Plant a complete album under Processing (as if beet move landed it there).
    album = cfg.processing_dir / "Artist" / "Album"
    _write_flac(album / "01. Track.flac", tags={"albumartist": "Artist", "album": "Album"})
    _write_flac(album / "02. Track.flac", tags={"albumartist": "Artist", "album": "Album"})
    for flac in album.glob("*.flac"):
        _mark_lrc(flac)
    album_paths = sorted(album.glob("*.flac"))

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_COUNT_DISCS, return_value=1), \
         patch("spindlebot.pipeline.runner.notify",
               return_value=MagicMock(macos_error=None, telegram_error=None)), \
         patch("spindlebot.pipeline.stages.fetch_art.fetch_art",
               return_value=MagicMock(embedded=0, skipped=0, missing=0, errors=0)), \
         patch("spindlebot.pipeline.stages.fetch_lyrics.fetch_lyrics",
               return_value=MagicMock(synced=2, plain=0, missing=0, errors=[])), \
         patch(_SUBPROCESS,
               side_effect=_runner_beet_stub(cfg.processing_dir, album_paths, cfg.pending_dir)):
        result = ImportRunner(cfg).run()

    assert result.success
    # Files ended up under Pending, not Processing.
    assert (cfg.pending_dir / "Artist" / "Album" / "01. Track.flac").exists()
    assert not album.exists()
    assert log_contains(cfg, "→ Pending")
    promote_stage = next(s for s in result.stages if s.name == "promote")
    assert promote_stage.success and not promote_stage.skipped


def test_runner_leaves_incomplete_album_in_processing(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)
    cfg.spindlebot_cfg = MagicMock()

    album = cfg.processing_dir / "Artist" / "Album"
    _write_flac(album / "01. Track.flac", tags={"albumartist": "Artist", "album": "Album"})
    _write_flac(album / "02. Track.flac", tags={"albumartist": "Artist", "album": "Album"})
    # Only the first track resolved — a transient error left 02 with no marker.
    _mark_lrc(album / "01. Track.flac")
    album_paths = sorted(album.glob("*.flac"))

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_COUNT_DISCS, return_value=1), \
         patch("spindlebot.pipeline.runner.notify",
               return_value=MagicMock(macos_error=None, telegram_error=None)), \
         patch("spindlebot.pipeline.stages.fetch_art.fetch_art",
               return_value=MagicMock(embedded=0, skipped=0, missing=0, errors=0)), \
         patch("spindlebot.pipeline.stages.fetch_lyrics.fetch_lyrics",
               return_value=MagicMock(synced=1, plain=0, missing=0, errors=["02. Track.flac"])), \
         patch(_SUBPROCESS,
               side_effect=_runner_beet_stub(cfg.processing_dir, album_paths, cfg.pending_dir)):
        result = ImportRunner(cfg).run()

    assert result.success
    # Album stayed in Processing, nothing under Pending.
    assert (album / "02. Track.flac").exists()
    assert not (cfg.pending_dir / "Artist" / "Album").exists()
    assert log_contains(cfg, "waiting on lyrics: 02. Track.flac")
    promote_stage = next(s for s in result.stages if s.name == "promote")
    assert promote_stage.skipped


def test_runner_promote_move_failure_fails_the_run(tmp_path):
    """A lyric-complete album whose promote move FAILS (DB locked / beets error)
    makes the overall run fail — it's a real failure, unlike waiting-on-lyrics.
    The album stays in Processing for `spindlebot finalize` to retry."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)
    cfg.spindlebot_cfg = MagicMock()

    album = cfg.processing_dir / "Artist" / "Album"
    _write_flac(album / "01. Track.flac", tags={"albumartist": "Artist", "album": "Album"})
    _mark_lrc(album / "01. Track.flac")  # complete
    album_paths = sorted(album.glob("*.flac"))

    def stub(argv, *args, **kwargs):
        argv = list(argv)
        cmd = argv[1] if len(argv) >= 2 else ""
        if cmd == "move" and any(a.startswith("path:") for a in argv):
            # The promote move fails.
            return MagicMock(returncode=1, stdout="", stderr="database is locked")
        if cmd == "ls":
            if any(a == "$albumartist - $album" for a in argv):
                return MagicMock(returncode=0, stdout="Artist - Album\n", stderr="")
            present = [str(p) for p in album_paths if Path(p).exists()]
            return MagicMock(returncode=0, stdout="\n".join(present) + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_COUNT_DISCS, return_value=1), \
         patch("spindlebot.pipeline.runner.notify",
               return_value=MagicMock(macos_error=None, telegram_error=None)), \
         patch("spindlebot.pipeline.stages.fetch_art.fetch_art",
               return_value=MagicMock(embedded=0, skipped=0, missing=0, errors=0)), \
         patch("spindlebot.pipeline.stages.fetch_lyrics.fetch_lyrics",
               return_value=MagicMock(synced=1, plain=0, missing=0, errors=[])), \
         patch(_SUBPROCESS, side_effect=stub):
        result = ImportRunner(cfg).run()

    assert not result.success, "a promote move failure must fail the run"
    promote_stage = next(s for s in result.stages if s.name == "promote")
    assert not promote_stage.success
    assert "database is locked" in promote_stage.message
    # Album stayed in Processing (the failing move didn't relocate it).
    assert (album / "01. Track.flac").exists()
    assert not (cfg.pending_dir / "Artist" / "Album").exists()
    assert log_contains(cfg, "promote failed (stays in Processing)")


def test_runner_mixed_run_promotes_complete_holds_incomplete(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)
    cfg.spindlebot_cfg = MagicMock()

    done = cfg.processing_dir / "Artist" / "Done"
    _write_flac(done / "01. Track.flac", tags={"albumartist": "Artist", "album": "Done"})
    _mark_lrc(done / "01. Track.flac")

    stuck = cfg.processing_dir / "Artist" / "Stuck"
    _write_flac(stuck / "01. Track.flac", tags={"albumartist": "Artist", "album": "Stuck"})
    # No marker → incomplete.

    album_paths = sorted(done.glob("*.flac")) + sorted(stuck.glob("*.flac"))

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_COUNT_DISCS, return_value=1), \
         patch("spindlebot.pipeline.runner.notify",
               return_value=MagicMock(macos_error=None, telegram_error=None)), \
         patch("spindlebot.pipeline.stages.fetch_art.fetch_art",
               return_value=MagicMock(embedded=0, skipped=0, missing=0, errors=0)), \
         patch("spindlebot.pipeline.stages.fetch_lyrics.fetch_lyrics",
               return_value=MagicMock(synced=0, plain=0, missing=0, errors=[])), \
         patch(_SUBPROCESS,
               side_effect=_runner_beet_stub(cfg.processing_dir, album_paths, cfg.pending_dir)):
        result = ImportRunner(cfg).run()

    assert result.success
    # Complete one promoted, incomplete one held.
    assert (cfg.pending_dir / "Artist" / "Done" / "01. Track.flac").exists()
    assert not done.exists()
    assert (stuck / "01. Track.flac").exists()
    assert not (cfg.pending_dir / "Artist" / "Stuck").exists()

    promote_stages = [s for s in result.stages if s.name == "promote"]
    assert sum(1 for s in promote_stages if not s.skipped) == 1
    assert sum(1 for s in promote_stages if s.skipped) == 1


def test_runner_stage7_move_failure_aborts(tmp_path):
    """A failing Stage-7 move (beet move -d Processing, nonzero exit) aborts the
    run — posttag/lyrics/promote must NOT run, so nothing is fetched against
    files still in Pending and no incomplete album is stranded there."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)
    cfg.spindlebot_cfg = MagicMock()

    posttag_called = {"hit": False}

    def stub(argv, *args, **kwargs):
        argv = list(argv)
        cmd = argv[1] if len(argv) >= 2 else ""
        # Stage-7 move into Processing (has -d) fails; the promote move (path:)
        # would succeed, but the run must abort before ever reaching it.
        if cmd == "move" and "-d" in argv:
            return MagicMock(returncode=1, stdout="", stderr="disk full")
        if cmd == "ls":
            is_name = any(a == "$albumartist - $album" for a in argv)
            if is_name:
                return MagicMock(returncode=0, stdout="Artist - Album\n", stderr="")
            return MagicMock(returncode=0, stdout="/proc/a/01.flac\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    def spy_posttag(files):
        posttag_called["hit"] = True
        return 0

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, side_effect=spy_posttag), \
         patch(_COUNT_DISCS, return_value=1), \
         patch("spindlebot.pipeline.runner.notify",
               return_value=MagicMock(macos_error=None, telegram_error=None)), \
         patch("spindlebot.pipeline.stages.fetch_lyrics.fetch_lyrics") as mock_lyrics, \
         patch(_SUBPROCESS, side_effect=stub):
        result = ImportRunner(cfg).run()

    assert not result.success
    move_stage = next(s for s in result.stages if s.name == "move")
    assert not move_stage.success
    assert "disk full" in move_stage.message
    assert log_contains(cfg, "beet move to Processing failed")
    # Later stages never ran.
    assert not posttag_called["hit"]
    mock_lyrics.assert_not_called()
    assert not any(s.name == "promote" for s in result.stages)
    assert not any(s.name == "posttag" for s in result.stages)


def test_runner_without_spindlebot_cfg_lands_in_pending(tmp_path):
    """With spindlebot_cfg=None the art/lyrics/promote stages don't run, so the
    album must move straight to Pending (prior behavior) — never stranded in
    Processing with nothing to promote it out."""
    cfg = _make_config(tmp_path)  # spindlebot_cfg defaults to None
    cfg.trigger.touch()
    _init_db(cfg)

    track = cfg.pending_dir / "Artist" / "Album" / "01. Track.flac"

    def stub(argv, *args, **kwargs):
        argv = list(argv)
        cmd = argv[1] if len(argv) >= 2 else ""
        if cmd == "move":
            # No -d → move to configured directory (Pending). Model it by
            # planting the file under Pending so the paths ls can report it.
            if not any(a == "-d" for a in argv):
                track.parent.mkdir(parents=True, exist_ok=True)
                track.write_bytes(b"x")
            return MagicMock(returncode=0, stdout="", stderr="")
        if cmd == "ls":
            is_name = any(a == "$albumartist - $album" for a in argv)
            if is_name:
                return MagicMock(returncode=0, stdout="Artist - Album\n", stderr="")
            out = str(track) + "\n" if track.exists() else "/proc/a/01.flac\n"
            return MagicMock(returncode=0, stdout=out, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_COUNT_DISCS, return_value=1), \
         patch(_SUBPROCESS, side_effect=stub) as mock_sub:
        result = ImportRunner(cfg).run()

    assert result.success
    # Landed in Pending, not Processing.
    assert track.exists()
    assert not any(cfg.processing_dir.rglob("*.flac"))
    # The Stage-7 move targeted Pending (path:), never Processing (-d).
    move_calls = [c.args[0] for c in mock_sub.call_args_list
                  if len(c.args[0]) >= 2 and c.args[0][1] == "move"]
    assert move_calls, "a move must have been issued"
    assert all("-d" not in argv for argv in move_calls)
    assert any(a == f"path:{cfg.pending_dir}/" for argv in move_calls for a in argv)
    # No promote stage (nothing routed through Processing).
    assert not any(s.name == "promote" for s in result.stages)


# ── auto-sync on import ───────────────────────────────────────────────────────
#
# Reuses the Processing-world scaffolding: a lyric-complete album under
# Processing that promotes to Pending, then the end-of-run auto-sync-or-hint
# block fires (spindlebot_cfg is set). The sync itself is injected, so nothing
# shells out.


def _run_complete_processing_import(cfg, *, sync_runner=None):
    """Drive a full, successful single-album import through the Processing flow.

    Returns the ImportResult. The album is lyric-complete so it promotes and the
    run succeeds, reaching the auto-sync-or-hint block.
    """
    album = cfg.processing_dir / "Artist" / "Album"
    _write_flac(album / "01. Track.flac", tags={"albumartist": "Artist", "album": "Album"})
    _mark_lrc(album / "01. Track.flac")
    album_paths = sorted(album.glob("*.flac"))

    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_COUNT_DISCS, return_value=1), \
         patch("spindlebot.pipeline.runner.notify",
               return_value=MagicMock(macos_error=None, telegram_error=None)), \
         patch("spindlebot.pipeline.stages.fetch_art.fetch_art",
               return_value=MagicMock(embedded=0, skipped=0, missing=0, errors=0)), \
         patch("spindlebot.pipeline.stages.fetch_lyrics.fetch_lyrics",
               return_value=MagicMock(synced=1, plain=0, missing=0, errors=[])), \
         patch(_SUBPROCESS,
               side_effect=_runner_beet_stub(cfg.processing_dir, album_paths, cfg.pending_dir)):
        return ImportRunner(cfg, sync_runner=sync_runner).run()


def test_auto_sync_default_false_logs_hint_and_does_not_sync(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)
    cfg.spindlebot_cfg = MagicMock()
    # auto_sync_on_import defaults to False; retention mounted would be irrelevant.
    cfg.retention_path = tmp_path  # exists, but must be ignored when flag is off

    sync = MagicMock(return_value=0)
    result = _run_complete_processing_import(cfg, sync_runner=sync)

    assert result.success
    sync.assert_not_called()
    assert log_contains(cfg, "Not auto-syncing")
    assert log_contains(cfg, "core.auto_sync_on_import = true")
    # The hint points at the SAME resolved entrypoint auto-sync would invoke
    # (pipeline_dir fallback here), never a hard-coded ~/.local/bin shim.
    assert log_contains(cfg, str(cfg.pipeline_dir / "music-sync-rugged.sh"))
    assert not log_contains(cfg, "~/.local/bin")


def test_auto_sync_hint_uses_configured_sync_script(tmp_path):
    """An explicit cfg.sync_script overrides the pipeline_dir fallback in the
    hint, matching what the auto-sync branch would actually run."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)
    cfg.spindlebot_cfg = MagicMock()
    cfg.sync_script = tmp_path / "custom" / "my-sync.sh"

    result = _run_complete_processing_import(cfg, sync_runner=MagicMock(return_value=0))

    assert result.success
    assert log_contains(cfg, f"Run {cfg.sync_script} to push to DwRugged")


def test_auto_sync_true_mounted_invokes_sync(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)
    cfg.spindlebot_cfg = MagicMock()
    cfg.auto_sync_on_import = True
    cfg.retention_path = tmp_path  # exists → "mounted"
    cfg.sync_script = tmp_path / "pipeline" / "music-sync-rugged.sh"

    sync = MagicMock(return_value=0)
    result = _run_complete_processing_import(cfg, sync_runner=sync)

    assert result.success
    sync.assert_called_once_with(cfg.sync_script)
    assert log_contains(cfg, "auto-syncing to DwRugged")
    assert not log_contains(cfg, "isn't mounted")


def test_auto_sync_true_not_mounted_does_not_invoke(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)
    cfg.spindlebot_cfg = MagicMock()
    cfg.auto_sync_on_import = True
    cfg.retention_path = tmp_path / "DwRugged" / "does-not-exist"  # not mounted

    sync = MagicMock(return_value=0)
    result = _run_complete_processing_import(cfg, sync_runner=sync)

    assert result.success
    sync.assert_not_called()
    assert log_contains(cfg, "isn't mounted")


def test_auto_sync_failure_logged_but_import_succeeds(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)
    cfg.spindlebot_cfg = MagicMock()
    cfg.auto_sync_on_import = True
    cfg.retention_path = tmp_path  # mounted

    sync = MagicMock(return_value=3)  # sync script exits nonzero
    result = _run_complete_processing_import(cfg, sync_runner=sync)

    assert result.success, "a sync failure must not turn a successful import into a failure"
    sync.assert_called_once()
    assert log_contains(cfg, "auto-sync exited 3")


def test_failed_import_never_auto_syncs(tmp_path):
    """Flag on + drive mounted, but the import itself fails → no auto-sync."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    cfg.spindlebot_cfg = MagicMock()
    cfg.auto_sync_on_import = True
    cfg.retention_path = tmp_path  # mounted

    sync = MagicMock(return_value=0)
    beet_fail = MagicMock(returncode=1, stdout="", stderr="beet error")
    with patch(_CHECK_WAIT, return_value=None), \
         patch(_PRETAG, return_value=True), \
         patch(_SUBPROCESS, return_value=beet_fail):
        result = ImportRunner(cfg, sync_runner=sync).run()

    assert not result.success
    sync.assert_not_called()
    assert not log_contains(cfg, "auto-syncing to DwRugged")


def test_default_sync_runner_logs_stderr_tail_on_failure(tmp_path):
    """A sync script that dies before opening rugged-sync.log (bootstrap/config
    error) leaves its stderr as the only trace — the default runner must log a
    tail of it (file only, echo=False) while preserving the exit code."""
    cfg = _make_config(tmp_path)
    runner = ImportRunner(cfg)

    fail = MagicMock(returncode=1, stdout="",
                     stderr="line1\nERROR: SpindleBot not configured. Run setup.sh.")
    echoed = []
    runner._echo = echoed.append
    with patch(_SUBPROCESS, return_value=fail):
        rc = runner._default_sync_runner(tmp_path / "music-sync-rugged.sh")

    assert rc == 1
    assert log_contains(cfg, "ERROR: SpindleBot not configured")
    assert echoed == [], "sync output tail must go to the log file only"

    # Success produces no output-tail logging.
    ok = MagicMock(returncode=0, stdout="noise", stderr="")
    with patch(_SUBPROCESS, return_value=ok):
        rc = runner._default_sync_runner(tmp_path / "music-sync-rugged.sh")
    assert rc == 0
    assert not log_contains(cfg, "noise")


# ── already-in-library duplicate handling ─────────────────────────────────────


def _make_beet_fake(existing: dict[str, str]):
    """Build a subprocess.run side_effect modeling a beets library that already
    contains the albums in `existing` (albumartist -> existing dir path).

    Semantics used by the runner:
      * `beet ls -f $path added:<start>..`  (forward window) -> items this run
        just imported. Empty when the album was already present (a no-op).
      * `beet ls -f $path <query> added:..<start>` (backward window) -> the
        pre-existing album. Non-empty only for albums in `existing`.
    All other calls (import / modify / move / name ls) succeed benignly.
    """
    def fake(argv, *args, **kwargs):
        argv = list(argv)
        is_ls = len(argv) >= 2 and argv[1] == "ls"
        forward = any(a.startswith("added:") and a.endswith("..") for a in argv)
        backward = any(a.startswith("added:..") for a in argv)

        if is_ls and forward:
            # Forward window (items added by THIS run) is empty: every album fed
            # to this fake is an already-present no-op. New-album cases use
            # _fresh_import_stub_beet, which reports a fresh item here instead.
            return MagicMock(returncode=0, stdout="", stderr="")

        if is_ls and backward:
            artist = _query_value(argv, "albumartist:")
            mbid = _query_value(argv, "mb_albumid:")
            match = existing.get(artist) or (existing.get(mbid) if mbid else None)
            if match:
                stdout = f"{match}/01.flac\n{match}/02.flac\n"
                return MagicMock(returncode=0, stdout=stdout, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return MagicMock(returncode=0, stdout="", stderr="")

    return fake


def _query_value(argv: list[str], prefix: str) -> str:
    for a in argv:
        if a.startswith(prefix):
            return a[len(prefix):]
    return ""


def test_duplicate_album_moved_to_duplicates(tmp_path):
    """An album already in the library is detected as a duplicate: the ⏭ line
    is logged, its files move to Duplicates/<artist>/<album>/, notify fires,
    and nothing is left in Import."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    imp = cfg.import_dir
    _write_flac(imp / "dup1.flac", tags={"albumartist": "Radiohead", "album": "Kid A",
                                         "discnumber": 1, "disctotal": 1})

    fake_cfg = MagicMock()  # stand-in SpindleBotConfig so notify() is attempted
    cfg.spindlebot_cfg = fake_cfg

    fake = _make_beet_fake({"Radiohead": "/Volumes/DwRugged/Music/Radiohead/Kid A"})

    with patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch("spindlebot.pipeline.runner.notify",
               return_value=MagicMock(macos_error=None, telegram_error=None)) as mock_notify, \
         patch(_SUBPROCESS, side_effect=fake):
        result = ImportRunner(cfg).run()

    assert result.success
    assert log_contains(cfg, "⏭")
    assert log_contains(cfg, "already in library")
    assert log_contains(cfg, "/Volumes/DwRugged/Music/Radiohead/Kid A")

    # File relocated, not left in Import.
    assert not (imp / "dup1.flac").exists()
    moved = cfg.duplicates_dir / "Radiohead" / "Kid A" / "dup1.flac"
    assert moved.exists(), "duplicate rip must land in Duplicates/<artist>/<album>/"

    mock_notify.assert_called_once()

    # No multidisc/posttag stage for a duplicate; duplicate_check recorded and
    # reported truthfully — it RAN (moved files + notified), so not `skipped`.
    stage_names = [s.name for s in result.stages]
    assert "duplicate_check" in stage_names
    assert "multidisc" not in stage_names
    dup_stage = next(s for s in result.stages if s.name == "duplicate_check")
    assert dup_stage.success
    assert not dup_stage.skipped
    assert "moved to Duplicates" in dup_stage.message


def test_no_op_without_library_match_leaves_files(tmp_path):
    """Nothing imported AND beets has no matching album → a distinct warning,
    files left in Import (never moved to Duplicates)."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    imp = cfg.import_dir
    _write_flac(imp / "orphan.flac", tags={"albumartist": "Nobody", "album": "Nowhere",
                                           "discnumber": 1, "disctotal": 1})

    fake = _make_beet_fake({})  # library has nothing

    with patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_SUBPROCESS, side_effect=fake):
        result = ImportRunner(cfg).run()

    assert result.success
    assert log_contains(cfg, "no matching album in the library")
    # Left where it was; NOT moved to Duplicates.
    assert (imp / "orphan.flac").exists()
    assert not (cfg.duplicates_dir / "Nobody").exists()


def test_partial_tags_not_treated_as_duplicate(tmp_path):
    """A no-op batch with album set but EMPTY albumartist must not be branded a
    duplicate — a one-sided match could hit an unrelated album. Files stay."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    imp = cfg.import_dir
    # album present, albumartist absent (and no artist to fall back on).
    _write_flac(imp / "partial.flac", tags={"album": "Ambiguous", "discnumber": 1,
                                            "disctotal": 1})

    backward_queries = []

    def fake(argv, *args, **kwargs):
        argv = list(argv)
        is_ls = len(argv) >= 2 and argv[1] == "ls"
        forward = any(a.startswith("added:") and a.endswith("..") for a in argv)
        backward = any(a.startswith("added:..") for a in argv)
        if is_ls and backward:
            backward_queries.append(argv)
            # If a lookup somehow ran, pretend a match exists — the test proves
            # the runner never issues it for a one-sided batch.
            return MagicMock(returncode=0, stdout="/lib/Some/Album/01.flac\n", stderr="")
        if is_ls and forward:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_SUBPROCESS, side_effect=fake):
        result = ImportRunner(cfg).run()

    assert result.success
    # No pre-existing-album lookup was issued (both fields required).
    assert backward_queries == []
    # Treated as an unmatched no-op: warned, left in Import, not moved.
    assert log_contains(cfg, "no matching album in the library")
    assert (imp / "partial.flac").exists()
    assert not any(cfg.duplicates_dir.rglob("*.flac"))


def test_failed_beet_ls_does_not_move_files(tmp_path):
    """A transient `beet ls` failure (nonzero exit) must be treated as UNKNOWN,
    not as 'nothing imported' — no duplicate handling, files left in place."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    imp = cfg.import_dir
    _write_flac(imp / "flaky.flac", tags={"albumartist": "Radiohead", "album": "Kid A",
                                          "discnumber": 1, "disctotal": 1})

    def fake(argv, *args, **kwargs):
        argv = list(argv)
        # The post-import "added since" verification ls fails.
        if len(argv) >= 2 and argv[1] == "ls" and any(
            a.startswith("added:") and a.endswith("..") for a in argv
        ):
            return MagicMock(returncode=1, stdout="", stderr="db locked")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch("spindlebot.pipeline.runner.notify",
               return_value=MagicMock(macos_error=None, telegram_error=None)) as mock_notify, \
         patch(_SUBPROCESS, side_effect=fake):
        result = ImportRunner(cfg).run()

    assert result.success
    # Nothing moved, nothing branded a duplicate, no duplicate notify.
    assert (imp / "flaky.flac").exists()
    assert not any(cfg.duplicates_dir.rglob("*.flac"))
    assert not log_contains(cfg, "already in library")
    assert log_contains(cfg, "could not verify import result")
    mock_notify.assert_not_called()


def test_new_album_still_imports_normally(tmp_path):
    """Regression: a brand-new album imports through all stages, no duplicate
    handling."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    imp = cfg.import_dir
    _write_flac(imp / "new.flac", tags={"albumartist": "Fresh", "album": "Debut",
                                        "discnumber": 1, "disctotal": 1})

    with patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch(_SUBPROCESS, side_effect=_fresh_import_stub_beet):
        result = ImportRunner(cfg).run()

    assert result.success
    stage_names = [s.name for s in result.stages]
    assert "multidisc" in stage_names
    assert "duplicate_check" not in stage_names
    # Nothing moved to Duplicates.
    assert not any(cfg.duplicates_dir.rglob("*.flac"))
    assert not log_contains(cfg, "already in library")


def test_mixed_new_and_duplicate_in_one_run(tmp_path):
    """One new album + one duplicate in a flat Import: the new one imports, the
    duplicate moves aside — both handled in a single run (per-album batching)."""
    cfg = _make_config(tmp_path)
    cfg.trigger.touch()
    _init_db(cfg)

    imp = cfg.import_dir
    _write_flac(imp / "new.flac", tags={"albumartist": "Fresh", "album": "Debut",
                                        "discnumber": 1, "disctotal": 1})
    _write_flac(imp / "dup.flac", tags={"albumartist": "Radiohead", "album": "Kid A",
                                        "discnumber": 1, "disctotal": 1})

    cfg.spindlebot_cfg = MagicMock()

    # The forward "added since" query immediately follows its batch's import, so
    # remember which album just imported and answer accordingly: the new album
    # reports a fresh item, the duplicate reports nothing.
    last_import = {"targets": []}

    def fake(argv, *args, **kwargs):
        argv = list(argv)
        cmd = argv[1] if len(argv) >= 2 else ""
        is_ls = cmd == "ls"
        forward = any(a.startswith("added:") and a.endswith("..") for a in argv)
        backward = any(a.startswith("added:..") for a in argv)

        if cmd == "import":
            last_import["targets"] = [str(t) for t in argv[2:]]
            return MagicMock(returncode=0, stdout="", stderr="")

        if is_ls and forward:
            # Fresh album just imported → report a new item; duplicate → empty.
            if any("new.flac" in t for t in last_import["targets"]):
                return MagicMock(returncode=0, stdout=str(imp / "new.flac") + "\n",
                                 stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        if is_ls and backward:
            artist = _query_value(argv, "albumartist:")
            if artist == "Radiohead":
                return MagicMock(returncode=0,
                                 stdout="/lib/Radiohead/Kid A/01.flac\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return MagicMock(returncode=0, stdout="", stderr="")

    with patch(_PRETAG, return_value=True), \
         patch(_POSTTAG, return_value=0), \
         patch("spindlebot.pipeline.runner.notify",
               return_value=MagicMock(macos_error=None, telegram_error=None)), \
         patch("spindlebot.pipeline.stages.fetch_art.fetch_art",
               return_value=MagicMock(embedded=0, skipped=0, missing=0, errors=0)), \
         patch("spindlebot.pipeline.stages.fetch_lyrics.fetch_lyrics",
               return_value=MagicMock(synced=0, plain=0, missing=0, errors=0)), \
         patch(_SUBPROCESS, side_effect=fake):
        result = ImportRunner(cfg).run()

    assert result.success
    # Duplicate moved aside.
    assert not (imp / "dup.flac").exists()
    assert (cfg.duplicates_dir / "Radiohead" / "Kid A" / "dup.flac").exists()
    assert log_contains(cfg, "already in library")

    # Both albums produced a beet_import success; exactly one was a duplicate.
    assert sum(1 for s in result.stages if s.name == "beet_import" and s.success) == 2
    assert sum(1 for s in result.stages if s.name == "duplicate_check") == 1
