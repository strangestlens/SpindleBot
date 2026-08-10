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


def _make_album(root: Path, name: str, n_tracks: int = 2, *, artist: str = "Artist") -> Path:
    """Create <root>/<artist>/<name>/ with n FLAC tracks (no lyric markers yet).

    Nests <artist>/<album>/ to mirror this repo's beets `paths.default`
    (`$albumartist/…/$track. $title`), so tests reflect where beets actually
    lands files rather than a flat layout.
    """
    album_dir = root / artist / name
    for i in range(1, n_tracks + 1):
        _write_flac(
            album_dir / f"{i:02d}. Track.flac",
            tags={"albumartist": artist, "album": name, "title": f"Track {i}"},
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

    Relocates into the NESTED pending_dir/<albumartist>/<album>/ layout —
    mirroring this repo's beets `paths.default` (`$albumartist/…/$track. $title`),
    where the artist is the source album dir's parent (VA albums use
    `Compilations`).

    Moves ONLY audio, because that is all real `beet move` does: it relocates
    the ITEMS in the beets DB. Sidecars (.lrc, cover.jpg) are invisible to beets
    and stay put. An earlier version of this stub moved every file in the
    directory, which modelled beets wrongly and hid a live bug — promoted albums
    were arriving in Pending with their lyrics and art orphaned in Processing.

    Also answers the `$id` / `$path` item queries promote uses to discover where
    beets actually put the album.
    """
    moved: dict[str, Path] = {}

    def stub(argv, *args, **kwargs):
        argv = list(argv)
        cmd = argv[1] if len(argv) >= 2 else ""
        path_arg = next((a for a in argv if a.startswith("path:")), None)
        id_arg = next((a for a in argv if a.startswith("id:")), None)

        if cmd == "ls" and "$id" in argv and path_arg:
            src = Path(path_arg[len("path:"):].rstrip("/"))
            ids = [str(i) for i, _ in enumerate(sorted(src.glob("*.flac")), start=1)]
            return MagicMock(returncode=0, stdout="\n".join(ids) + "\n", stderr="")

        if cmd == "ls" and "$path" in argv and id_arg:
            hit = moved.get(id_arg[len("id:"):])
            return MagicMock(
                returncode=0, stdout=(f"{hit}\n" if hit else ""), stderr=""
            )

        if cmd == "move" and path_arg:
            src = Path(path_arg[len("path:"):].rstrip("/"))
            if src.exists():
                dest = pending_dir / src.parent.name / src.name
                dest.mkdir(parents=True, exist_ok=True)
                for i, f in enumerate(sorted(src.glob("*.flac")), start=1):
                    shutil.move(str(f), str(dest / f.name))
                    moved[str(i)] = dest / f.name
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
    assert (pending / "Artist" / "Complete Album" / "01. Track.flac").exists()
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
    assert not (pending / "Artist" / "Incomplete Album").exists()


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
    assert (pending / "Artist" / "Missy Album" / "02. Track.flac").exists()


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
    assert not (pending / "Artist" / "Doomed Move").exists()


def test_promote_label_is_path_derived(tmp_path):
    """Default label reads artist from the parent dir (beets nests
    <albumartist>/<album>/) so logs disambiguate across artists."""
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "In Rainbows", artist="Radiohead")
    _complete_all(album)

    with patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)):
        result = promote_album(album, "/bin/beet")

    assert result.promoted
    assert result.label == "Radiohead - In Rainbows"
    assert (pending / "Radiohead" / "In Rainbows" / "01. Track.flac").exists()


def test_promote_compilation_lands_under_compilations(tmp_path):
    """A VA album nests under `Compilations` (its beets artist dir), and the
    label reflects that — mirroring real beets output for compilations."""
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Electronic Toys", artist="Compilations")
    _complete_all(album)

    with patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)):
        result = promote_album(album, "/bin/beet")

    assert result.promoted
    assert result.label == "Compilations - Electronic Toys"
    assert (pending / "Compilations" / "Electronic Toys" / "01. Track.flac").exists()


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
    assert (pending / "Artist" / "Now Resolving" / "02. Track.flac").exists()


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
    assert not (pending / "Artist" / "Still Broken").exists()


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
    assert (pending / "Artist" / "Idem Album" / "01. Track.flac").exists()
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
    assert not (pending / "Artist" / "Move Fails").exists()


# ── cmd_finalize exit codes ───────────────────────────────────────────────────


def _cli_cfg(processing: Path, beet: str = "/bin/beet"):
    from types import SimpleNamespace

    return SimpleNamespace(
        core=SimpleNamespace(processing_dir=processing),
        tools=SimpleNamespace(beet=beet),
        lyrics=SimpleNamespace(request_delay_seconds=0.0),
    )


def test_cmd_finalize_move_failure_exits_nonzero(tmp_path, capsys):
    from spindlebot.cli import cmd_finalize

    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    _complete_all(_make_album(processing, "Move Fails"))

    def fake_fetch(album_dir, cfg, *, dry_run=False, force=False):
        return MagicMock(synced=0, plain=0, missing=0, errors=[])

    def failing_move(argv, *args, **kwargs):
        if list(argv)[1] == "move":
            return MagicMock(returncode=1, stdout="", stderr="database is locked")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch(_FETCH, side_effect=fake_fetch), \
         patch(_SUBPROCESS, side_effect=failing_move):
        rc = cmd_finalize(_cli_cfg(processing), [])

    assert rc == 1
    err = capsys.readouterr().err
    assert "promote failed" in err


def test_cmd_finalize_waiting_only_exits_zero(tmp_path, capsys):
    from spindlebot.cli import cmd_finalize

    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Waiting")
    _mark_lrc(album, "01. Track.flac")  # 02 stays incomplete

    def fake_fetch(album_dir, cfg, *, dry_run=False, force=False):
        # Lyrics still don't resolve — the album stays waiting, no move issued.
        return MagicMock(synced=0, plain=0, missing=0, errors=["02. Track.flac"])

    with patch(_FETCH, side_effect=fake_fetch), \
         patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)):
        rc = cmd_finalize(_cli_cfg(processing), [])

    assert rc == 0  # waiting-on-lyrics is expected, not a failure
    out = capsys.readouterr().out
    assert "waiting on lyrics" in out


def test_cmd_finalize_dry_run_never_fails(tmp_path, capsys):
    from spindlebot.cli import cmd_finalize

    processing = tmp_path / "Processing"
    (tmp_path / "Pending").mkdir()
    _complete_all(_make_album(processing, "Would Promote"))

    def fake_fetch(album_dir, cfg, *, dry_run=False, force=False):
        return MagicMock(synced=0, plain=0, missing=0, errors=[])

    # No move is ever issued in dry-run, so even a would-fail beet can't matter.
    def failing_move(argv, *args, **kwargs):
        if list(argv)[1] == "move":
            return MagicMock(returncode=1, stdout="", stderr="should not run")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch(_FETCH, side_effect=fake_fetch), \
         patch(_SUBPROCESS, side_effect=failing_move) as mock_sub:
        rc = cmd_finalize(_cli_cfg(processing), ["--dry-run"])

    assert rc == 0
    assert not any(c.args[0][1] == "move" for c in mock_sub.call_args_list)


# ── beet argv shape ───────────────────────────────────────────────────────────
#
# These assert the LITERAL argv, not just that a move happened. The suite
# previously checked only `argv[1] == "move"` with a mock that always returned
# 0, so `beet move --yes` — which real beets rejects with "no such option",
# exit 2 — passed CI while stranding every album in Processing forever.


# Options real `beet move` accepts (beets 2.x). A flag outside this set is a
# crash in production no matter what a MagicMock returns.
_BEET_MOVE_OPTIONS = {
    "-h", "--help", "-d", "--dest", "-c", "--copy",
    "-p", "--pretend", "-t", "--timid", "-e", "--export", "-a", "--album",
}


def test_promote_beet_move_argv_is_exactly_the_scoped_move(tmp_path):
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Argv Album")
    _complete_all(album)

    with patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)) as mock_sub:
        promote_album(album, "/bin/beet")

    argv = next(c.args[0] for c in mock_sub.call_args_list if c.args[0][1] == "move")
    assert argv == ["/bin/beet", "move", f"path:{album}/"]


def test_promote_passes_no_unknown_options_to_beet_move(tmp_path):
    """Every dash-prefixed argument must be a real `beet move` option.

    `beet move` is non-interactive already; there is no confirmation flag to
    pass. This is the regression guard for --yes.
    """
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Flag Album")
    _complete_all(album)

    with patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)) as mock_sub:
        promote_album(album, "/bin/beet")

    argv = next(c.args[0] for c in mock_sub.call_args_list if c.args[0][1] == "move")
    flags = [a for a in argv[2:] if a.startswith("-")]
    unknown = [f for f in flags if f.split("=")[0] not in _BEET_MOVE_OPTIONS]
    assert not unknown, f"not valid `beet move` options: {unknown}"


def test_promote_carries_sidecars_into_pending(tmp_path):
    """Pending is complete-by-construction — the .lrc and art must come along.

    `beet move` only relocates the audio items beets knows about, so without an
    explicit sidecar step an album arrives in Pending with zero lyrics and no
    cover, while sync and prune treat Pending as trustworthy.
    """
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Sidecar Album", n_tracks=3)
    _complete_all(album)
    (album / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")

    with patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)):
        result = promote_album(album, "/bin/beet")

    assert result.promoted
    assert result.sidecar_error == ""

    dest = pending / "Artist" / "Sidecar Album"
    assert sorted(p.name for p in dest.glob("*.lrc")) == [
        "01. Track.lrc", "02. Track.lrc", "03. Track.lrc",
    ]
    assert (dest / "cover.jpg").exists()
    assert (dest / "cover.jpg").read_bytes() == b"\xff\xd8\xff\xe0jpeg"
    # Nothing left orphaned behind in Processing.
    assert not album.exists()


def test_promote_reports_when_sidecars_cannot_follow(tmp_path):
    """If the album's new location can't be resolved, say so — don't lie."""
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Lost Album")
    _complete_all(album)

    def no_ids(argv, *args, **kwargs):
        argv = list(argv)
        if len(argv) >= 2 and argv[1] == "ls":
            return MagicMock(returncode=1, stdout="", stderr="boom")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch(_SUBPROCESS, side_effect=no_ids):
        result = promote_album(album, "/bin/beet")

    assert result.promoted
    assert "sidecars left in Processing" in result.sidecar_error
    # The sidecars really are still there — the report matches reality.
    assert sorted(p.name for p in album.glob("*.lrc")) == ["01. Track.lrc", "02. Track.lrc"]


def test_promote_never_clobbers_an_existing_sidecar(tmp_path):
    """A .lrc in Pending may be hand-timed in lrc-editor or AI-retimed.

    Overwriting it with a freshly fetched lrclib copy would destroy that work
    silently, so a differing destination file is kept and the incoming one is
    parked alongside for a human to resolve.
    """
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Retimed Album", n_tracks=1)
    _complete_all(album)
    (album / "01. Track.lrc").write_text("[00:00.00] freshly fetched\n")

    # An edited sidecar already sits at the destination.
    dest = pending / "Artist" / "Retimed Album"
    dest.mkdir(parents=True)
    (dest / "01. Track.lrc").write_text("[00:12.34] hand timed, do not lose\n")

    with patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)):
        result = promote_album(album, "/bin/beet")

    assert result.promoted
    # The edited file is untouched.
    assert (dest / "01. Track.lrc").read_text() == "[00:12.34] hand timed, do not lose\n"
    # The incoming one is preserved under a distinct name, and reported.
    assert (dest / "01. Track (2).lrc").read_text() == "[00:00.00] freshly fetched\n"
    assert "01. Track (2).lrc" in result.sidecar_error


def test_promote_drops_a_byte_identical_sidecar(tmp_path):
    """Same content is not a conflict — no stray '(2)' copies for a re-promote."""
    processing = tmp_path / "Processing"
    pending = tmp_path / "Pending"
    pending.mkdir()
    album = _make_album(processing, "Same Album", n_tracks=1)
    _complete_all(album)

    dest = pending / "Artist" / "Same Album"
    dest.mkdir(parents=True)
    (dest / "01. Track.lrc").write_text((album / "01. Track.lrc").read_text())

    with patch(_SUBPROCESS, side_effect=_beet_move_stub(pending)):
        result = promote_album(album, "/bin/beet")

    assert result.promoted
    assert result.sidecar_error == ""
    assert sorted(p.name for p in dest.glob("*.lrc")) == ["01. Track.lrc"]
    assert not album.exists()
