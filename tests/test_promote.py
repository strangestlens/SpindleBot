"""
Tests for spindlebot.services.promote — promote lyric-complete albums out of the
Processing area into Pending, and the finalize sweep that catches up stuck ones.

`beet` is stubbed via subprocess mocking (as in test_runner.py). A `path:<dir>/`
promote move is modeled by actually relocating the album's files from Processing
to Pending, so tests can assert on final file locations. `fetch_lyrics` is
stubbed for finalize tests to simulate a track's lyrics resolving on retry.
"""
from __future__ import annotations

import shutil
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import mutagen.flac

from spindlebot.services.promote import (
    finalize_processing,
    promote_album,
)

_SUBPROCESS = "spindlebot.services.promote.subprocess.run"
_FETCH = "spindlebot.pipeline.stages.fetch_lyrics.fetch_lyrics"


# ── fixtures ──────────────────────────────────────────────────────────────────


def _write_flac(path: Path, *, tags: dict) -> None:
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


def _make_album(root: Path, name: str, n_tracks: int = 2) -> Path:
    """Create <root>/<name>/ with n FLAC tracks (no lyric markers yet)."""
    album_dir = root / name
    for i in range(1, n_tracks + 1):
        _write_flac(
            album_dir / f"{i:02d}. Track.flac",
            tags={"albumartist": "Artist", "album": name, "title": f"Track {i}"},
        )
    return album_dir


def _mark_lrc(album_dir: Path, filename: str) -> None:
    (album_dir / (Path(filename).stem + ".lrc")).write_text("[00:00.00] la\n")


def _mark_nolrc(album_dir: Path, filename: str) -> None:
    (album_dir / (Path(filename).stem + ".nolrc")).write_text("")


def _complete_all(album_dir: Path) -> None:
    for flac in sorted(album_dir.glob("*.flac")):
        _mark_lrc(album_dir, flac.name)


def _beet_move_stub(pending_dir: Path):
    """subprocess.run side_effect modeling `beet move path:<album_dir>/`.

    Parses the album dir out of the `path:...` argument and relocates that dir's
    files (audio + sidecars) into pending_dir/<album name>/, mirroring how a real
    beets move would land them under the configured `directory`.
    """
    def stub(argv, *args, **kwargs):
        argv = list(argv)
        if len(argv) >= 2 and argv[1] == "move":
            path_arg = next((a for a in argv if a.startswith("path:")), None)
            if path_arg:
                src = Path(path_arg[len("path:"):].rstrip("/"))
                if src.exists():
                    dest = pending_dir / src.name
                    dest.mkdir(parents=True, exist_ok=True)
                    for f in list(src.iterdir()):
                        shutil.move(str(f), str(dest / f.name))
                    src.rmdir()
        return MagicMock(returncode=0, stdout="", stderr="")

    return stub


# ── promote_album ─────────────────────────────────────────────────────────────


def test_complete_album_promotes_to_pending(tmp_path):
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Complete Album")
    _complete_all(album)

    with patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)) as mock_sub:
        result = promote_album(album, "/bin/beet")

    assert result.promoted
    assert result.waiting_on == []
    # A beet move scoped to THIS album (trailing-slash path: query) was issued.
    move_calls = [c.args[0] for c in mock_sub.call_args_list if c.args[0][1] == "move"]
    assert len(move_calls) == 1
    assert any(a == f"path:{album}/" for a in move_calls[0])
    # Files landed under Pending, no longer under Processing.
    assert (pending / "Complete Album" / "01. Track.flac").exists()
    assert not album.exists()


def test_incomplete_album_stays_in_processing(tmp_path):
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Incomplete Album")
    # Only the first track resolved; the second is left with no marker (a
    # transient lyric error would leave exactly this state).
    _mark_lrc(album, "01. Track.flac")

    with patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)) as mock_sub:
        result = promote_album(album, "/bin/beet")

    assert not result.promoted
    assert result.waiting_on == ["02. Track.flac"]
    # No move issued for an incomplete album.
    assert not any(c.args[0][1] == "move" for c in mock_sub.call_args_list)
    # Files stayed in Processing.
    assert (album / "02. Track.flac").exists()
    assert not (pending / "Incomplete Album").exists()


def test_nolrc_marker_counts_as_complete(tmp_path):
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Missy Album")
    # Mixed terminal states: one found, one definitive-miss — both terminal.
    _mark_lrc(album, "01. Track.flac")
    _mark_nolrc(album, "02. Track.flac")

    with patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)):
        result = promote_album(album, "/bin/beet")

    assert result.promoted
    assert (pending / "Missy Album" / "02. Track.flac").exists()


def test_promote_move_failure_stays_in_processing(tmp_path):
    """A lyric-complete album whose `beet move` fails (nonzero exit) is NOT
    reported promoted — it stays in Processing so finalize retries it."""
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Doomed Move")
    _complete_all(album)

    def failing_move(argv, *args, **kwargs):
        if list(argv)[1] == "move":
            return MagicMock(returncode=1, stdout="", stderr="database is locked")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch(_SUBPROCESS, side_effect=failing_move):
        result = promote_album(album, "/bin/beet")

    assert not result.promoted
    assert result.move_error == "database is locked"
    assert result.waiting_on == []  # complete — it's a move failure, not lyrics
    # Files stayed in Processing; nothing under Pending.
    assert (album / "01. Track.flac").exists()
    assert not (pending / "Doomed Move").exists()


def test_promote_label_is_path_derived(tmp_path):
    """Default label reads artist from the parent dir (beets nests
    <albumartist>/<album>/) so logs disambiguate across artists."""
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing / "Radiohead", "In Rainbows")
    _complete_all(album)

    with patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)):
        result = promote_album(album, "/bin/beet")

    assert result.promoted
    assert result.label == "Radiohead - In Rainbows"


# ── finalize_processing ───────────────────────────────────────────────────────


def _cfg_stub(beet="/bin/beet"):
    cfg = MagicMock()
    cfg.tools.beet = beet
    cfg.lyrics.request_delay_seconds = 0.0
    return cfg


def test_finalize_promotes_album_whose_lyrics_now_resolve(tmp_path):
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Now Resolving")
    _mark_lrc(album, "01. Track.flac")  # 02 still incomplete

    # Simulate the retry fetch resolving the missing track.
    def fake_fetch(album_dir, cfg, *, dry_run=False, force=False):
        _mark_lrc(Path(album_dir), "02. Track.flac")
        return MagicMock(synced=1, plain=0, missing=0, errors=[])

    with patch(_FETCH, side_effect=fake_fetch), \
         patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)):
        result = finalize_processing(processing, _cfg_stub())

    assert result.scanned == 1
    assert len(result.promoted) == 1
    assert result.promoted[0].album_dir == album
    assert (pending / "Now Resolving" / "02. Track.flac").exists()


def test_finalize_leaves_still_incomplete_album(tmp_path):
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Still Broken")
    _mark_lrc(album, "01. Track.flac")  # 02 stays incomplete

    # Retry fetch still can't resolve the second track (persistent transient err).
    def fake_fetch(album_dir, cfg, *, dry_run=False, force=False):
        return MagicMock(synced=0, plain=0, missing=0, errors=["02. Track.flac"])

    with patch(_FETCH, side_effect=fake_fetch), \
         patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)):
        result = finalize_processing(processing, _cfg_stub())

    assert result.scanned == 1
    assert result.promoted == []
    assert len(result.waiting) == 1
    assert result.waiting[0].waiting_on == ["02. Track.flac"]
    assert (album / "02. Track.flac").exists()
    assert not (pending / "Still Broken").exists()


def test_finalize_is_idempotent(tmp_path):
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Idem Album")
    _complete_all(album)  # already complete going in

    fetch_calls = {"n": 0}

    def fake_fetch(album_dir, cfg, *, dry_run=False, force=False):
        fetch_calls["n"] += 1
        return MagicMock(synced=0, plain=0, missing=0, errors=[])

    with patch(_FETCH, side_effect=fake_fetch), \
         patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)):
        first = finalize_processing(processing, _cfg_stub())
        # After the first sweep the album is gone from Processing → second sweep
        # finds nothing, promotes nothing, and is a clean no-op.
        second = finalize_processing(processing, _cfg_stub())

    assert len(first.promoted) == 1
    assert (pending / "Idem Album" / "01. Track.flac").exists()
    assert second.scanned == 0
    assert second.promoted == []
    assert second.waiting == []


def test_finalize_reports_move_failure_as_waiting(tmp_path):
    """A complete album whose promote move fails surfaces in `waiting` with a
    move_error (not promoted), and stays in Processing for the next sweep."""
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Move Fails")
    _complete_all(album)

    def fake_fetch(album_dir, cfg, *, dry_run=False, force=False):
        return MagicMock(synced=0, plain=0, missing=0, errors=[])

    def failing_move(argv, *args, **kwargs):
        if list(argv)[1] == "move":
            return MagicMock(returncode=1, stdout="", stderr="beets error")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch(_FETCH, side_effect=fake_fetch), \
         patch(_SUBPROCESS, side_effect=failing_move):
        result = finalize_processing(processing, _cfg_stub())

    assert result.promoted == []
    assert len(result.waiting) == 1
    assert result.waiting[0].move_error == "beets error"
    assert (album / "01. Track.flac").exists()
    assert not (pending / "Move Fails").exists()
