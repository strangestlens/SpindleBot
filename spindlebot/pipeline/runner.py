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
  5. beet import          — beet import <album_dir>
  6. multidisc fix        — patch disctotal + multidisc flex attr in beets DB
  7. beet move            — relocate files to canonical library paths
  8. posttag              — strip beet alias tags, truncate DATE to year
  9. fetch art + lyrics   — embed art, write .lrc sidecars
 10. archive              — mv XLD .log to archive dir (.log mode only)
"""
from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from spindlebot.disc import check_wait, count_discs
from spindlebot.pipeline.stages.notify import notify
from spindlebot.pipeline.stages.pretag import posttag, pretag


@dataclass
class ImportConfig:
    trigger: Path
    force: bool
    beet: Path
    python: Path
    db: Path
    pending_dir: Path   # processed albums awaiting distribution (beets canonical dir)
    import_dir: Path    # active import area (rips/downloads land here)
    archive: Path
    pipeline_dir: Path
    log_file: Path
    # Full SpindleBotConfig — used by stages that need notifications/secrets.
    # Optional so existing tests that build ImportConfig directly don't break.
    spindlebot_cfg: Optional[object] = None


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

        # Stage 3: disc check
        if not cfg.force:
            wait = check_wait(str(album_dir))
            if wait:
                _, have, need = wait.split(":")
                self._log(f"⏸  multi-disc: have {have} of {need} discs — waiting for the rest")
                self._log("   (run with --force to import immediately)", echo=False)
                result.success = True
                result.stages.append(StageResult("disc_check", success=True, message=f"waiting:{have}:{need}"))
                return result
            self._log("✓  disc check")
            result.stages.append(StageResult("disc_check", success=True))
        else:
            self._log("⏭  disc check skipped (--force)")
            result.stages.append(StageResult("disc_check", success=True, skipped=True))

        # Stage 4: pretag
        self._log(f"🏷  pretagging")
        self._log(f"Running pretag on: {album_dir}", echo=False)
        try:
            pretag(str(album_dir))
            result.stages.append(StageResult("pretag", success=True))
        except Exception as exc:
            self._log(f"✗  pretag failed: {exc}")
            result.stages.append(StageResult("pretag", success=False, message=str(exc)))
            return result

        # Stage 5: beet import
        # Capture timestamp immediately before beet runs so all subsequent queries
        # scope to this import only — not every album imported today.
        import_start = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        self._log("💿 importing with beet...")
        self._log(f"Starting beet import on: {album_dir}", echo=False)
        if self._echo is not None:
            # Stream beet output live to the terminal — capture is skipped so
            # the user sees progress in real time (beet can take a while).
            beet_proc = subprocess.run(
                [str(cfg.beet), "import", str(album_dir)],
            )
        else:
            beet_proc = subprocess.run(
                [str(cfg.beet), "import", str(album_dir)],
                capture_output=True,
                text=True,
            )
            if beet_proc.stdout.strip():
                self._log(beet_proc.stdout.strip())
            if beet_proc.stderr.strip():
                self._log(beet_proc.stderr.strip())

        if beet_proc.returncode != 0:
            self._log(f"✗  beet import failed (exit {beet_proc.returncode})")
            self._log(f"Import FAILED (exit {beet_proc.returncode}): {album_dir}", echo=False)
            result.stages.append(StageResult("beet_import", success=False, message=f"exit {beet_proc.returncode}"))
            return result

        self._log(f"Import complete: {album_dir}", echo=False)
        result.stages.append(StageResult("beet_import", success=True))

        # Get artist/album name for notification
        ls_proc = subprocess.run(
            [str(cfg.beet), "ls", "-f", "$albumartist - $album", f"added:{import_start}.."],
            capture_output=True,
            text=True,
        )
        lines = sorted(set(ls_proc.stdout.strip().splitlines()))
        result.artist_album = lines[0] if lines else album_dir.name

        # Notify (fire-and-forget, non-critical)
        if cfg.spindlebot_cfg is not None:
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

        # Stage 6: multidisc fix
        actual_discs = count_discs(str(album_dir))
        self._fix_multidisc(actual_discs, import_start)
        result.stages.append(StageResult("multidisc", success=True))

        # Stage 7: beet move
        subprocess.run(
            [str(cfg.beet), "move", f"added:{import_start}..", f"path:{cfg.pending_dir}/"],
            capture_output=True,
            text=True,
        )
        self._log("Moved files to correct paths", echo=False)

        # Get canonical paths of imported files (local library only)
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

        # Stage 9: fetch lyrics + art
        if cfg.spindlebot_cfg is not None:
            from spindlebot.pipeline.stages.fetch_art import fetch_art as _fetch_art
            from spindlebot.pipeline.stages.fetch_lyrics import fetch_lyrics as _fetch_lyrics
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

        # Stage 10: archive XLD logs (.log-triggered runs only)
        if has_log:
            cfg.archive.mkdir(parents=True, exist_ok=True)
            for logfile in sorted(cfg.import_dir.glob("*.log")):
                dest = cfg.archive / logfile.name
                logfile.rename(dest)
                self._log(f"Archived XLD log to: {dest}", echo=False)
            self._log("📦 log archived")

        result.success = True
        return result

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
