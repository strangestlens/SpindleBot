"""
Import pipeline runner — Python equivalent of music-import.sh.

Called via: python -m spindlebot import <trigger> [--force]

Stages (in order):
  1. trigger validation   — non-.log files are silently ignored
  2. double-fire guard    — missing log = already archived, exit cleanly
  3. disc check           — wait if multi-disc set is incomplete (unless --force)
  4. pretag               — normalize tags before beet import
  5. beet import          — beet import <album_dir>
  6. multidisc fix        — patch disctotal + multidisc flex attr in beets DB
  7. beet move            — relocate files to canonical library paths
  8. posttag              — strip beet alias tags, truncate DATE to year
  9. fetch lyrics         — .lrc sidecars via music-fetch-lyrics.py
 10. archive              — mv XLD .log files to archive dir
"""
from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from spindlebot.disc import check_wait, count_discs
from spindlebot.pipeline.stages.pretag import posttag, pretag


@dataclass
class ImportConfig:
    trigger: Path
    force: bool
    beet: Path
    python: Path
    db: Path
    library: Path
    staging: Path
    archive: Path
    pipeline_dir: Path
    log_file: Path


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
    def __init__(self, config: ImportConfig) -> None:
        self.cfg = config

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.cfg.log_file, "a") as fh:
            fh.write(f"[{ts}] {msg}\n")

    def run(self) -> ImportResult:
        cfg = self.cfg
        result = ImportResult(success=False)
        trigger = cfg.trigger

        self._log(f"Watcher fired: {trigger}{' (--force)' if cfg.force else ''}")

        # Stage 1: trigger validation — non-.log events are silently ignored
        if trigger.suffix != ".log":
            result.success = True
            return result

        # Stage 2: double-fire guard — log may have already been archived
        if not trigger.exists():
            result.success = True
            return result

        album_dir = trigger.parent
        self._log(f"Detected completed rip: {album_dir}")

        # Stage 3: disc check
        if not cfg.force:
            wait = check_wait(str(album_dir))
            if wait:
                _, have, need = wait.split(":")
                self._log(f"Multi-disc album: have {have} of {need} discs — waiting for remaining discs before importing")
                self._log("  (run with --force to import immediately)")
                result.success = True
                result.stages.append(StageResult("disc_check", success=True, message=f"waiting:{have}:{need}"))
                return result
            self._log("Disc check passed — proceeding with import")
            result.stages.append(StageResult("disc_check", success=True))
        else:
            self._log("Disc check skipped (--force)")
            result.stages.append(StageResult("disc_check", success=True, skipped=True))

        # Stage 4: pretag
        self._log(f"Running pretag on: {album_dir}")
        try:
            pretag(str(album_dir))
            result.stages.append(StageResult("pretag", success=True))
        except Exception as exc:
            self._log(f"pretag failed — aborting import: {exc}")
            result.stages.append(StageResult("pretag", success=False, message=str(exc)))
            return result

        # Stage 5: beet import
        self._log(f"Starting beet import on: {album_dir}")
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
            self._log(f"Import FAILED (exit {beet_proc.returncode}): {album_dir}")
            result.stages.append(StageResult("beet_import", success=False, message=f"exit {beet_proc.returncode}"))
            return result

        self._log(f"Import complete: {album_dir}")
        result.stages.append(StageResult("beet_import", success=True))

        today = date.today().isoformat()

        # Get artist/album name for notification
        ls_proc = subprocess.run(
            [str(cfg.beet), "ls", "-f", "$albumartist - $album", f"added:{today}.."],
            capture_output=True,
            text=True,
        )
        lines = sorted(set(ls_proc.stdout.strip().splitlines()))
        result.artist_album = lines[0] if lines else album_dir.name

        # Notify (fire-and-forget, non-critical)
        subprocess.run(
            [str(cfg.pipeline_dir / "music-notify.sh"), "Rip complete",
             f"{result.artist_album} — reply 'sync' to move to DwRugged"],
            capture_output=True,
        )

        # Stage 6: multidisc fix
        actual_discs = count_discs(str(album_dir))
        self._fix_multidisc(actual_discs, today)
        result.stages.append(StageResult("multidisc", success=True))

        # Stage 7: beet move
        subprocess.run(
            [str(cfg.beet), "move", f"added:{today}..", f"path:{cfg.library}/"],
            capture_output=True,
            text=True,
        )
        self._log("Moved files to correct paths")

        # Get canonical paths of imported files (local library only)
        paths_proc = subprocess.run(
            [str(cfg.beet), "ls", "-f", "$path", f"added:{today}.."],
            capture_output=True,
            text=True,
        )
        import_files = [
            p for p in paths_proc.stdout.strip().splitlines()
            if p and not p.startswith("/Volumes/")
        ]

        # Stage 8: posttag
        if import_files:
            self._log("Running posttag on imported files")
            fixed = posttag(import_files)
            self._log(f"posttag done: {fixed} files updated")
            result.stages.append(StageResult("posttag", success=True, message=f"{fixed} files updated"))
        else:
            result.stages.append(StageResult("posttag", success=True, skipped=True))

        # Stage 9: fetch lyrics
        album_dirs = sorted({str(Path(p).parent) for p in import_files})
        fetch_lyrics = cfg.pipeline_dir / "music-fetch-lyrics.py"
        for d in album_dirs:
            self._log(f"Fetching lyrics for: {d}")
            subprocess.run([str(cfg.python), str(fetch_lyrics), d], capture_output=True)

        # Stage 10: archive XLD logs
        cfg.archive.mkdir(parents=True, exist_ok=True)
        for logfile in sorted(cfg.staging.glob("*.log")):
            dest = cfg.archive / logfile.name
            logfile.rename(dest)
            self._log(f"Archived XLD log to: {dest}")

        result.success = True
        return result

    def _fix_multidisc(self, actual_discs: int, today: str) -> None:
        cfg = self.cfg
        library_path = f"{cfg.library}/"

        if actual_discs > 1:
            subprocess.run(
                [str(cfg.beet), "modify", "--yes",
                 f"added:{today}..", f"path:{library_path}", f"disctotal={actual_discs}"],
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
                    (today, f"{cfg.library}/%"),
                )
            self._log(f"Multi-disc rip ({actual_discs} discs) — set disctotal={actual_discs}, multidisc=1")
        else:
            subprocess.run(
                [str(cfg.beet), "modify", "--yes",
                 f"added:{today}..", f"path:{library_path}", "disctotal=1", "disc=1"],
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
                    (today, f"{cfg.library}/%"),
                )
            self._log("Single-disc rip — patched disctotal=1, ensured multidisc row exists")
