"""
Promote service — move a lyric-complete album out of the Processing area into
Pending, using a beets-native move that also updates the beets DB paths.

Why a Processing area at all: `beet import` lands audio in the beets `directory`
(= Pending), but the import pipeline then fetches lyrics for ~90s. A mount-sync
firing mid-fetch would prune the audio and strand the late-arriving .lrc/.nolrc
sidecars. So the import relocates each album to Processing, does all its work
there, and PROMOTES an album to Pending only once every track is in a terminal
lyric state (`album_lyrics_complete`). Pending becomes complete-by-construction;
sync/reconciler/prune stay untouched.

Layering: this is a service — it orchestrates over `beet` (subprocess) and the
pure `album_lyrics_complete` predicate, returns typed results, and never prints.
The runner and the `finalize` CLI command both call it.

Promote is scoped to a SINGLE album by a `path:<album_dir>/` query (trailing
slash per beets gotcha #3). `beet move` with no `-d` relocates matching items to
the configured `directory` (Pending). It must NEVER be scoped to a whole-run
`added:` window — that would drag incomplete albums along.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from spindlebot.disc import AUDIO_EXTENSIONS, find_audio_files
from spindlebot.pipeline.stages.fetch_lyrics import album_lyrics_complete


@dataclass
class PromoteResult:
    """Outcome of attempting to promote one album out of Processing."""
    album_dir: Path
    promoted: bool
    label: str = ""
    # Tracks (audio filenames) still lacking a terminal .lrc/.nolrc marker.
    # Empty when promoted. Populated when the album stayed in Processing
    # because it wasn't lyric-complete.
    waiting_on: list[str] = field(default_factory=list)
    # Set when the album WAS lyric-complete but the `beet move` to Pending
    # failed (nonzero exit): promoted stays False, the album stays in Processing,
    # and finalize will retry it on a later sweep.
    move_error: str = ""


@dataclass
class FinalizeResult:
    """Outcome of a finalize sweep over the whole Processing area."""
    promoted: list[PromoteResult] = field(default_factory=list)
    waiting: list[PromoteResult] = field(default_factory=list)

    @property
    def scanned(self) -> int:
        return len(self.promoted) + len(self.waiting)


def _incomplete_tracks(album_dir: Path) -> list[str]:
    """Audio filenames in `album_dir` lacking a terminal .lrc/.nolrc sidecar.

    Mirrors album_lyrics_complete's per-track terminal test. An album-level
    .nolrc makes the whole album complete, so returns []. Uses os.path.splitext
    (not Path.with_suffix) to match _process_file — a stem like "01. Title"
    carries dots that with_suffix would mangle.
    """
    if (album_dir / ".nolrc").exists():
        return []
    missing: list[str] = []
    for audio in find_audio_files(album_dir):
        base = os.path.splitext(str(audio))[0]
        if os.path.exists(base + ".lrc") or os.path.exists(base + ".nolrc"):
            continue
        missing.append(audio.name)
    return missing


def _default_label(album_dir: Path) -> str:
    """Path-derived "<albumartist> - <album>" label (beets nests that way).

    Reads the artist from the immediate parent directory name. Falls back to
    just the album dir name only when there is no parent name to read — i.e. the
    album dir sits at the filesystem root (parent name ""); a normal album under
    processing_dir always has an artist parent dir.
    """
    parent = album_dir.parent.name
    return f"{parent} - {album_dir.name}" if parent else album_dir.name


def promote_album(album_dir: str | Path, beet: str | Path, *, label: str = "") -> PromoteResult:
    """Promote a single Processing album to Pending iff it is lyric-complete.

    Complete → `beet move path:<album_dir>/` relocates its items to the beets
    `directory` (Pending) and updates the DB paths. Not complete → left in
    Processing, with the tracks still awaiting lyrics reported.

    The `beet move` return code is checked: a nonzero exit (DB locked, beets
    error, path mismatch) leaves the album in Processing with promoted=False and
    move_error set, so finalize retries it — never reported promoted on failure.
    """
    album_dir = Path(album_dir)
    label = label or _default_label(album_dir)

    if not album_lyrics_complete(album_dir):
        return PromoteResult(
            album_dir=album_dir,
            promoted=False,
            label=label,
            waiting_on=_incomplete_tracks(album_dir),
        )

    # Trailing slash per beets gotcha #3 — without it a path: query can miss.
    proc = subprocess.run(
        [str(beet), "move", "--yes", f"path:{album_dir}/"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return PromoteResult(
            album_dir=album_dir,
            promoted=False,
            label=label,
            move_error=err,
        )
    return PromoteResult(album_dir=album_dir, promoted=True, label=label)


def _album_dirs(processing_dir: Path) -> list[Path]:
    """Distinct directories under processing_dir that contain audio files.

    Walks the tree because beets path templates nest albums as
    <albumartist>/<album>/, so audio lives in leaf dirs, not directly under
    processing_dir. Returns each leaf audio dir once, sorted.
    """
    processing_dir = Path(processing_dir)
    if not processing_dir.exists():
        return []
    dirs: set[Path] = set()
    for p in processing_dir.rglob("*"):
        if p.is_file() and p.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS:
            dirs.add(p.parent)
    return sorted(dirs)


def finalize_processing(
    processing_dir: str | Path,
    cfg,
    *,
    dry_run: bool = False,
) -> FinalizeResult:
    """Re-fetch lyrics for each album still in Processing, then attempt promote.

    Idempotent: fetch_lyrics skips tracks that already have a terminal sidecar,
    and an already-promoted album leaves nothing in Processing to rescan. Safe to
    re-run — a second pass over the same Processing area is a no-op for anything
    already promoted, and simply retries anything still incomplete.

    With dry_run=True, lyrics are fetched in dry-run mode and no promote move is
    issued; the result still reports which albums WOULD promote vs. stay waiting.
    """
    processing_dir = Path(processing_dir)
    result = FinalizeResult()

    # Import here to avoid a hard dependency at module import time and to mirror
    # the runner's lazy stage imports.
    from spindlebot.pipeline.stages.fetch_lyrics import fetch_lyrics

    for album_dir in _album_dirs(processing_dir):
        # Retry transient-error tracks. Terminal tracks (.lrc/.nolrc) are skipped
        # inside fetch_lyrics, so this only re-queries the still-incomplete ones.
        fetch_lyrics(album_dir, cfg, dry_run=dry_run)

        if dry_run:
            complete = album_lyrics_complete(album_dir)
            pr = PromoteResult(
                album_dir=album_dir,
                promoted=complete,
                label=_default_label(album_dir),
                waiting_on=[] if complete else _incomplete_tracks(album_dir),
            )
        else:
            pr = promote_album(album_dir, cfg.tools.beet)

        if pr.promoted:
            result.promoted.append(pr)
        else:
            result.waiting.append(pr)

    return result
