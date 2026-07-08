"""
Import pipeline runner — Python equivalent of music-import.sh.

Called via: python -m spindlebot import <trigger> [--force]

Trigger types:
  directory  — manual drop or watcher event: album_dir = trigger, no archive step
  .log file  — XLD rip complete: album_dir = trigger.parent, log archived at end
  other      — logged and skipped (fswatch fires for individual files mid-copy)

Stages (in order):
  1. trigger validation   — resolve album_dir; log + skip unrecognised triggers
  2. double-fire guard    — .log mode only: missing log = already archived, exit cleanly
  3. disc check           — wait if multi-disc set is incomplete (unless --force)
  4. pretag               — normalize tags before beet import
  5. beet import          — beet import <album_dir>; a green import that adds
                            NOTHING and matches an existing album is an
                            already-in-library duplicate → move it to
                            duplicates_dir + notify + skip its later stages
  6. multidisc fix        — patch disctotal + multidisc flex attr in beets DB
  7. beet move            — relocate files into Processing (NOT Pending) so a
                            mid-fetch mount-sync can't prune audio / strand late
                            lyrics; a failed move ABORTS the run. When
                            spindlebot_cfg is None (no art/lyrics/promote to
                            follow), moves straight to Pending instead.
  8. posttag              — strip beet alias tags, truncate DATE to year
  9. fetch art + lyrics   — embed art, write .lrc sidecars (in Processing)
 10. promote              — per album: if lyric-complete, beet-move it to Pending;
                            else leave it in Processing (finalize catches it up).
                            A failed promote move leaves the album in Processing.
 11. archive              — mv XLD .log to archive dir (.log mode only)

Pending is complete-by-construction: an album lands there only once every track
has a terminal .lrc/.nolrc marker, so sync/reconciler/prune stay untouched.
"""
from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import shutil

from spindlebot.disc import check_wait, count_discs, find_audio_files, group_by_album
from spindlebot.pipeline.stages.notify import notify
from spindlebot.pipeline.stages.pretag import posttag, pretag


def _safe_name(name: str) -> str:
    """Filesystem-safe single path component (no separators, never empty)."""
    cleaned = name.replace("/", "_").replace("\x00", "").strip().strip(".")
    return cleaned or "Unknown"


def _unique_dest(dest: Path) -> Path:
    """Return a non-colliding path by suffixing ' (2)', ' (3)', … before the ext."""
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    n = 2
    while True:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


@dataclass
class ImportConfig:
    trigger: Path
    force: bool
    beet: Path
    python: Path
    db: Path
    pending_dir: Path   # processed albums awaiting distribution (beets canonical dir)
    processing_dir: Path  # in-flight processing area; albums promote to pending once lyric-complete
    import_dir: Path    # active import area (rips/downloads land here)
    archive: Path
    duplicates_dir: Path  # already-in-library rips moved here rather than stranded in Import
    pipeline_dir: Path
    log_file: Path
    # Full SpindleBotConfig — used by stages that need notifications/secrets.
    # Optional so existing tests that build ImportConfig directly don't break.
    spindlebot_cfg: Optional[object] = None


@dataclass
class _AlbumBatch:
    """One album's worth of work resolved out of a (possibly mixed) Import dir.

    label       — human-readable identifier for logs
    beet_target — argv tail passed to `beet import`: a single directory string
                  (single-album/untagged case) or an explicit file-path list
                  (one album out of a flat mixed dir)
    disc_source — what disc.check_wait/count_discs read: the directory string or
                  this album's file list, mirroring beet_target
    source_files— the concrete audio files backing this batch, used when a
                  duplicate is detected and the rip must be moved out of Import
    """
    label: str
    beet_target: list[str]
    disc_source: str | Path | list[Path]
    source_files: list[Path]


@dataclass
class StageResult:
    name: str
    success: bool
    skipped: bool = False
    message: str = ""


@dataclass
class ImportResult:
    success: bool
    stages: list[StageResult] = field(default_factory=list)
    artist_album: str = ""


class ImportRunner:
    def __init__(
        self,
        config: ImportConfig,
        echo: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.cfg = config
        self._echo = echo

    def _log(self, msg: str, *, echo: bool = True) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.cfg.log_file, "a") as fh:
            fh.write(f"[{ts}] {msg}\n")
        if self._echo and echo:
            self._echo(msg)

    def run(self) -> ImportResult:
        cfg = self.cfg
        result = ImportResult(success=False)
        trigger = cfg.trigger

        self._log(f"Watcher fired: {trigger}{' (--force)' if cfg.force else ''}", echo=False)

        # Stage 1: trigger validation — resolve album_dir from trigger type
        if trigger.is_dir():
            album_dir = trigger
            has_log = False
            self._log(f"Detected album directory: {album_dir}", echo=False)
        elif trigger.suffix == ".log":
            # Stage 2: double-fire guard — log may have already been archived
            if not trigger.exists():
                self._log(f"Already imported (log archived): {trigger}", echo=False)
                result.success = True
                return result
            album_dir = trigger.parent
            has_log = True
            self._log(f"Detected completed rip: {album_dir}", echo=False)
        else:
            self._log(f"Nothing to import: {trigger.name} is not a directory or .log file")
            result.success = True
            return result

        # Resolve the album batches present in album_dir. When only one album
        # (or an untagged/empty dir) is present, the batch targets the whole
        # directory — identical to the pre-album-aware behavior. When a flat
        # Import holds several distinct albums, each becomes its own batch so
        # disc logic and beet import never conflate them.
        batches = self._resolve_album_batches(album_dir)

        # Stage 3: disc check — evaluated PER album. A waiting multi-disc album
        # must not block or contaminate a complete one, so waiting batches are
        # dropped (logged) and the rest proceed.
        ready: list[_AlbumBatch] = []
        if cfg.force:
            self._log("⏭  disc check skipped (--force)")
            result.stages.append(StageResult("disc_check", success=True, skipped=True))
            ready = list(batches)
        else:
            waited: list[tuple[str, str, str]] = []
            for batch in batches:
                wait = check_wait(batch.disc_source)
                if wait:
                    _, have, need = wait.split(":")
                    waited.append((batch.label, have, need))
                    self._log(
                        f"⏸  multi-disc [{batch.label}]: have {have} of {need} discs "
                        "— waiting for the rest"
                    )
                else:
                    ready.append(batch)
            if waited:
                self._log("   (run with --force to import immediately)", echo=False)
            if not ready:
                # Everything present is still waiting — nothing to import yet.
                result.success = True
                have, need = (waited[0][1], waited[0][2]) if waited else ("0", "0")
                result.stages.append(
                    StageResult("disc_check", success=True, message=f"waiting:{have}:{need}")
                )
                return result
            self._log("✓  disc check")
            result.stages.append(StageResult("disc_check", success=True))

        # Stage 4: pretag — per-file normalization over the whole directory.
        # Order-independent; running once matches prior behavior.
        self._log(f"🏷  pretagging")
        self._log(f"Running pretag on: {album_dir}", echo=False)
        try:
            pretag(str(album_dir))
            result.stages.append(StageResult("pretag", success=True))
        except Exception as exc:
            self._log(f"✗  pretag failed: {exc}")
            result.stages.append(StageResult("pretag", success=False, message=str(exc)))
            return result

        # Stages 5 + 6: import each ready album on its own, then apply its
        # per-album multidisc fix. run_start scopes the later move/posttag/fetch
        # to everything imported in this run.
        run_start = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        for batch in ready:
            if not self._import_one_album(batch, result):
                return result

        # Get artist/album name for notification (first album alphabetically).
        ls_proc = subprocess.run(
            [str(cfg.beet), "ls", "-f", "$albumartist - $album", f"added:{run_start}.."],
            capture_output=True,
            text=True,
        )
        lines = sorted(set(ls_proc.stdout.strip().splitlines()))
        result.artist_album = lines[0] if lines else album_dir.name

        # Notify (fire-and-forget, non-critical). Skip when nothing new was
        # imported this run — an all-duplicate run already notified per album,
        # and "reply sync" would be misleading with nothing to distribute.
        if lines and cfg.spindlebot_cfg is not None:
            title = "Rip complete" if has_log else "Import complete"
            notify_result = notify(
                title,
                f"{result.artist_album} — reply 'sync' to move to DwRugged",
                cfg.spindlebot_cfg,
            )
            if notify_result.macos_error:
                self._log(f"macOS notify failed: {notify_result.macos_error}")
            if notify_result.telegram_error:
                self._log(f"Telegram notify failed: {notify_result.telegram_error}")

        import_start = run_start

        # Stage 7: beet move. When spindlebot_cfg is set (the normal pipeline),
        # the art/lyrics + promote stages will run, so route the album through
        # Processing — it promotes to Pending only once lyric-complete. When
        # spindlebot_cfg is None (tests / programmatic use), those stages are
        # skipped, so there'd be nothing to promote the album out of Processing:
        # move straight to Pending, preserving the pre-Processing behavior.
        via_processing = cfg.spindlebot_cfg is not None
        if via_processing:
            move_argv = [str(cfg.beet), "move", "-d", str(cfg.processing_dir),
                         f"added:{import_start}.."]
            dest_label = "Processing"
        else:
            move_argv = [str(cfg.beet), "move",
                         f"added:{import_start}..", f"path:{cfg.pending_dir}/"]
            dest_label = "Pending"

        move_proc = subprocess.run(move_argv, capture_output=True, text=True)
        if move_proc.returncode != 0:
            # A failed move must abort — otherwise the runner would fetch lyrics
            # against files still sitting in Pending and the promote (scoped to
            # Processing) would match nothing, silently recreating the mid-fetch
            # prune window with an incomplete album stranded in Pending.
            err = (move_proc.stderr or move_proc.stdout or "").strip() or f"exit {move_proc.returncode}"
            self._log(f"✗  beet move to {dest_label} failed: {err}")
            result.stages.append(
                StageResult("move", success=False, message=err)
            )
            return result
        self._log(f"Moved files to {dest_label}", echo=False)

        # Get canonical paths of imported files (local area only)
        paths_proc = subprocess.run(
            [str(cfg.beet), "ls", "-f", "$path", f"added:{import_start}.."],
            capture_output=True,
            text=True,
        )
        import_files = [
            p for p in paths_proc.stdout.strip().splitlines()
            if p and not p.startswith("/Volumes/")
        ]

        # Stage 8: posttag
        if import_files:
            self._log("Running posttag on imported files", echo=False)
            fixed = posttag(import_files)
            self._log(f"🏷  posttag ({fixed} file{'s' if fixed != 1 else ''} updated)")
            result.stages.append(StageResult("posttag", success=True, message=f"{fixed} files updated"))
        else:
            result.stages.append(StageResult("posttag", success=True, skipped=True))

        # Stage 9 + 10: fetch art/lyrics per album, then promote lyric-complete
        # albums to Pending. Albums stay in Processing (files under
        # processing_dir) until every track has a terminal .lrc/.nolrc marker.
        promote_move_failed = False
        if cfg.spindlebot_cfg is not None:
            from spindlebot.pipeline.stages.fetch_art import fetch_art as _fetch_art
            from spindlebot.pipeline.stages.fetch_lyrics import fetch_lyrics as _fetch_lyrics
            from spindlebot.services.promote import promote_album
            album_dirs = sorted({str(Path(p).parent) for p in import_files})
            for d in album_dirs:
                self._log(f"Fetching art for: {d}", echo=False)
                ar = _fetch_art(d, cfg.spindlebot_cfg)
                self._log(
                    f"🎨 art: embedded={ar.embedded} skipped={ar.skipped} missing={ar.missing}"
                    + (f" errors={ar.errors}" if ar.errors else "")
                )
                self._log(f"Fetching lyrics for: {d}", echo=False)
                lr = _fetch_lyrics(d, cfg.spindlebot_cfg)
                self._log(
                    f"🎵 lyrics: synced={lr.synced} plain={lr.plain} missing={lr.missing}"
                    + (f" errors={lr.errors}" if lr.errors else "")
                )

                # Path-derived "<albumartist> - <album>" label (beets nests that
                # way) so logs disambiguate across artists — no extra beet call.
                dpath = Path(d)
                label = f"{dpath.parent.name} - {dpath.name}" if dpath.parent.name else dpath.name
                pr = promote_album(d, cfg.beet, label=label)
                if pr.promoted:
                    self._log(f"✅ {pr.label} → Pending")
                    result.stages.append(
                        StageResult("promote", success=True, message=f"{pr.label} → Pending")
                    )
                elif pr.move_error:
                    # Lyric-complete but the move to Pending failed — the album
                    # stays in Processing; `spindlebot finalize` will retry it.
                    # This is a real failure (DB locked / beets error), unlike
                    # waiting-on-lyrics: surface it in the run's success status.
                    promote_move_failed = True
                    self._log(f"✗  {pr.label} promote failed (stays in Processing): {pr.move_error}")
                    result.stages.append(
                        StageResult("promote", success=False,
                                    message=f"{pr.label} move failed: {pr.move_error}")
                    )
                else:
                    waiting = ", ".join(pr.waiting_on) or "album"
                    self._log(f"⏳ {pr.label} waiting on lyrics: {waiting}")
                    result.stages.append(
                        StageResult("promote", success=True, skipped=True,
                                    message=f"{pr.label} waiting: {waiting}")
                    )

        # Stage 11: archive XLD logs (.log-triggered runs only)
        if has_log:
            cfg.archive.mkdir(parents=True, exist_ok=True)
            for logfile in sorted(cfg.import_dir.glob("*.log")):
                dest = cfg.archive / logfile.name
                logfile.rename(dest)
                self._log(f"Archived XLD log to: {dest}", echo=False)
            self._log("📦 log archived")

        # A promote MOVE failure (DB locked / beets error) is a real failure and
        # must be reflected in the run status. Waiting-on-lyrics is expected and
        # does NOT fail the run — that album is simply caught up later by
        # `spindlebot finalize`.
        result.success = not promote_move_failed
        return result

    def _resolve_album_batches(self, album_dir: Path) -> list[_AlbumBatch]:
        """Split album_dir into per-album batches by album_key.

        The common case — a directory holding exactly one album, or an
        untagged/empty directory — collapses to a SINGLE batch whose target is
        the directory itself, so behavior is byte-for-byte identical to the
        pre-album-aware runner (whole-dir beet import + whole-dir disc logic).

        Only when 2+ distinct albums are detected do we split into explicit
        per-album file-list batches, so a mixed Import never feeds a mixed pile
        to `beet import`.
        """
        groups = group_by_album(album_dir)
        if len(groups) <= 1:
            return [_AlbumBatch(
                label=album_dir.name,
                beet_target=[str(album_dir)],
                disc_source=str(album_dir),
                source_files=find_audio_files(album_dir),
            )]

        self._log(
            f"Import dir holds {len(groups)} distinct albums — importing each separately",
            echo=False,
        )
        batches: list[_AlbumBatch] = []
        for i, files in enumerate(groups.values(), start=1):
            label = self._batch_label(files) or f"{album_dir.name} #{i}"
            batches.append(_AlbumBatch(
                label=label,
                beet_target=[str(p) for p in files],
                disc_source=files,
                source_files=list(files),
            ))
        return batches

    @staticmethod
    def _batch_label(files: list[Path]) -> str:
        """Best-effort 'artist - album' label for a batch; '' if unreadable."""
        import mutagen

        for p in files:
            try:
                mf = mutagen.File(str(p), easy=True)
            except Exception:
                continue
            if mf is None:
                continue
            aa = (mf.get("albumartist") or mf.get("artist") or [""])[0]
            al = (mf.get("album") or [""])[0]
            if aa or al:
                return f"{aa} - {al}".strip(" -")
        return ""

    def _import_one_album(self, batch: _AlbumBatch, result: ImportResult) -> bool:
        """Import a single album batch + apply its per-album multidisc fix.

        Returns True on success, False on beet-import failure (caller aborts).
        Each batch captures its own start timestamp so the multidisc fix scopes
        to THIS album's rows only, never sibling albums imported in the run.
        """
        cfg = self.cfg
        import_start = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        self._log("💿 importing with beet...")
        self._log(f"Starting beet import [{batch.label}]", echo=False)

        argv = [str(cfg.beet), "import", *batch.beet_target]
        if self._echo is not None:
            # Stream beet output live to the terminal — capture is skipped so
            # the user sees progress in real time (beet can take a while).
            beet_proc = subprocess.run(argv)
        else:
            beet_proc = subprocess.run(argv, capture_output=True, text=True)
            if beet_proc.stdout.strip():
                self._log(beet_proc.stdout.strip())
            if beet_proc.stderr.strip():
                self._log(beet_proc.stderr.strip())

        if beet_proc.returncode != 0:
            self._log(f"✗  beet import failed (exit {beet_proc.returncode})")
            self._log(f"Import FAILED (exit {beet_proc.returncode}) [{batch.label}]", echo=False)
            result.stages.append(
                StageResult("beet_import", success=False, message=f"exit {beet_proc.returncode}")
            )
            return False

        self._log(f"Import complete [{batch.label}]", echo=False)
        result.stages.append(StageResult("beet_import", success=True))

        # Did this album actually produce new items? beet skips ("already in the
        # library") without a nonzero exit, so a green import can still be a
        # no-op. Anything added since import_start is genuinely new.
        ok, new_paths = self._items_added_since(import_start)
        if not ok:
            # `beet ls` itself failed — we CANNOT tell whether items were added.
            # Treat as unknown, never as "nothing imported": skip duplicate
            # handling entirely and leave the files in Import untouched.
            self._log(
                f"⚠  {batch.label} — could not verify import result "
                "(beet ls failed); leaving files in Import"
            )
            result.stages.append(
                StageResult("duplicate_check", success=True,
                            message="unknown: beet ls failed")
            )
            return True
        if not new_paths:
            self._handle_no_new_items(batch, import_start, result)
            # A duplicate (or an unmatched no-op) produced no rows to fix.
            return True

        # Stage 6: multidisc fix — disc count read from THIS album's files.
        actual_discs = count_discs(batch.disc_source)
        self._fix_multidisc(actual_discs, import_start)
        result.stages.append(StageResult("multidisc", success=True))
        return True

    def _items_added_since(self, since: str) -> tuple[bool, list[str]]:
        """(ok, paths) for beets items added at/after `since` (this batch's window).

        `ok` is False when `beet ls` itself failed (nonzero exit): the empty
        stdout of a failed query is indistinguishable from a genuine no-op, so
        the caller must treat a failure as UNKNOWN, never as "nothing imported".
        """
        proc = subprocess.run(
            [str(self.cfg.beet), "ls", "-f", "$path", f"added:{since}.."],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return False, []
        return True, [p for p in proc.stdout.strip().splitlines() if p]

    def _existing_album_dir(self, batch: _AlbumBatch, before: str) -> tuple[str, int] | None:
        """Directory + track count of a pre-existing beets album matching this
        batch, considering only items added BEFORE this import attempt.

        Match by musicbrainz_albumid when the rip carries one, else by BOTH
        albumartist AND album — a one-sided fallback (e.g. album alone, or an
        empty albumartist:) could match an unrelated album and falsely brand a
        genuinely-new rip a duplicate. Returns None when there's no confident
        match — including when `beet ls` failed (unknown, not a duplicate).
        """
        aa, al, mbid = self._batch_album_ids(batch)
        cfg = self.cfg
        if mbid:
            query = [f"mb_albumid:{mbid}", f"added:..{before}"]
        elif aa and al:
            query = [f"albumartist:{aa}", f"album:{al}", f"added:..{before}"]
        else:
            return None

        proc = subprocess.run(
            [str(cfg.beet), "ls", "-f", "$path", *query],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return None
        paths = [p for p in proc.stdout.strip().splitlines() if p]
        if not paths:
            return None
        return str(Path(paths[0]).parent), len(paths)

    @staticmethod
    def _batch_album_ids(batch: _AlbumBatch) -> tuple[str, str, str]:
        """Best-effort (albumartist, album, mb_albumid) for a batch's files."""
        from spindlebot.disc import _read_album_tags

        for p in batch.source_files:
            aa, al, mbid = _read_album_tags(p)
            if aa or al or mbid:
                return aa or "", al or "", mbid or ""
        return "", "", ""

    def _handle_no_new_items(
        self, batch: _AlbumBatch, import_start: str, result: ImportResult
    ) -> None:
        """A green import that added nothing. Either it's an already-in-library
        duplicate (move it aside + notify) or an unexplained no-op (warn, leave
        the files where they are — never silently strand or discard them)."""
        existing = self._existing_album_dir(batch, import_start)
        if existing is None:
            self._log(
                f"⚠  {batch.label} — nothing imported and no matching album in the "
                "library; leaving files in Import for inspection"
            )
            result.stages.append(
                StageResult("duplicate_check", success=True,
                            message="no-op, no library match")
            )
            return

        existing_dir, track_count = existing
        aa, al, _ = self._batch_album_ids(batch)
        artist = aa or "Unknown Artist"
        album = al or "Unknown Album"
        self._log(
            f"⏭  {artist} – {album} — already in library "
            f"({track_count} track{'s' if track_count != 1 else ''} on {existing_dir}); "
            "moved to Duplicates"
        )
        self._move_to_duplicates(batch, artist, album)
        # Not `skipped`: this stage actively ran (moved files + notified).
        result.stages.append(
            StageResult("duplicate_check", success=True,
                        message=f"duplicate: moved to Duplicates ({existing_dir})")
        )

        if self.cfg.spindlebot_cfg is not None:
            notify_result = notify(
                "Duplicate rip",
                f"{artist} – {album} already in library — moved to Duplicates",
                self.cfg.spindlebot_cfg,
            )
            if notify_result.macos_error:
                self._log(f"macOS notify failed: {notify_result.macos_error}")
            if notify_result.telegram_error:
                self._log(f"Telegram notify failed: {notify_result.telegram_error}")

    def _move_to_duplicates(self, batch: _AlbumBatch, artist: str, album: str) -> None:
        """Move a duplicate album's source files into duplicates_dir/<artist>/<album>/.

        Creates parents; tolerates an already-present destination file by
        de-duplicating the name rather than crashing.
        """
        dest_dir = self.cfg.duplicates_dir / _safe_name(artist) / _safe_name(album)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for src in batch.source_files:
            if not src.exists():
                continue
            dest = dest_dir / src.name
            if dest.exists():
                dest = _unique_dest(dest)
            shutil.move(str(src), str(dest))
        self._log(f"Moved duplicate rip to: {dest_dir}", echo=False)

    def _fix_multidisc(self, actual_discs: int, import_start: str) -> None:
        cfg = self.cfg
        pending_path = f"{cfg.pending_dir}/"

        if actual_discs > 1:
            subprocess.run(
                [str(cfg.beet), "modify", "--yes",
                 f"added:{import_start}..", f"path:{pending_path}", f"disctotal={actual_discs}"],
                capture_output=True,
            )
            with sqlite3.connect(str(cfg.db)) as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO item_attributes (entity_id, key, value)
                    SELECT id, 'multidisc', '1' FROM items
                    WHERE added >= strftime('%s', ?)
                      AND path LIKE ?
                      AND id NOT IN (
                          SELECT entity_id FROM item_attributes WHERE key='multidisc'
                      )
                    """,
                    (import_start, f"{cfg.pending_dir}/%"),
                )
            self._log(f"Multi-disc rip ({actual_discs} discs) — set disctotal={actual_discs}, multidisc=1")
        else:
            subprocess.run(
                [str(cfg.beet), "modify", "--yes",
                 f"added:{import_start}..", f"path:{pending_path}", "disctotal=1", "disc=1"],
                capture_output=True,
            )
            with sqlite3.connect(str(cfg.db)) as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO item_attributes (entity_id, key, value)
                    SELECT id, 'multidisc', '' FROM items
                    WHERE added >= strftime('%s', ?)
                      AND path LIKE ?
                      AND id NOT IN (
                          SELECT entity_id FROM item_attributes WHERE key='multidisc'
                      )
                    """,
                    (import_start, f"{cfg.pending_dir}/%"),
                )
            self._log("Single-disc rip — patched disctotal=1, ensured multidisc row exists")
