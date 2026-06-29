"""
SpindleBot CLI.

Usage:
    python -m spindlebot check                         Validate config and tool availability
    python -m spindlebot config shell                  Print config as shell-sourceable exports
    python -m spindlebot config get <key>              Print a single value (e.g. core.pending_dir)
    python -m spindlebot import <trigger> [--force]    Run import pipeline for an album
    python -m spindlebot import-staging [--dry-run]    Import everything currently in the Import area
    python -m spindlebot inventory [--location <name>] [--json] [-v|--quiet]   Scan a location into the SpindleBot DB (read-only re: audio)
    python -m spindlebot review --location <name> [--json] [-v|--quiet]        Plan reconciliation (propose copies/missing); no bytes moved
    python -m spindlebot review --acknowledge-run <run_id>        Acknowledge every proposed action in a run
    python -m spindlebot review --acknowledge <id[,id...]>        Acknowledge specific proposed actions
    python -m spindlebot notify <title> <message>      Send a test notification via all channels
    python -m spindlebot fetch-lyrics <dir> [--dry-run] [--force]   Fetch .lrc files for an album
    python -m spindlebot fetch-art <dir> [--dry-run] [--force]      Fetch/embed album art
    python -m spindlebot restart                                     Restart launchd agents
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# ── check ─────────────────────────────────────────────────────────────────────

def cmd_check(cfg) -> int:
    ok = True

    def check(label: str, condition: bool, fix: str = "") -> None:
        nonlocal ok
        mark = "✓" if condition else "✗"
        print(f"  {mark}  {label}")
        if not condition:
            ok = False
            if fix:
                print(f"       → {fix}")

    print("\nCore paths:")
    check("import_dir exists",
          cfg.core.import_dir.exists(),
          f"mkdir -p '{cfg.core.import_dir}'")
    check("pending_dir exists",
          cfg.core.pending_dir.exists(),
          f"mkdir -p '{cfg.core.pending_dir}'")
    check("log_dir exists",
          cfg.core.log_dir.exists(),
          f"mkdir -p '{cfg.core.log_dir}'")
    check("archive_dir exists",
          cfg.core.archive_dir.exists(),
          f"mkdir -p '{cfg.core.archive_dir}'")

    print("\nTools:")
    check("beet executable",
          cfg.tools.beet.is_file() and os.access(cfg.tools.beet, os.X_OK),
          "Install beets: pip install beets")
    check("python executable",
          cfg.tools.python.is_file() and os.access(cfg.tools.python, os.X_OK))
    check("beets_db exists",
          cfg.tools.beets_db.exists(),
          f"Run: {cfg.tools.beet} ls   (creates DB on first run)")
    if cfg.tools.mpv:
        check("mpv executable",
              cfg.tools.mpv.is_file() and os.access(cfg.tools.mpv, os.X_OK),
              "Install mpv: brew install mpv")

    print("\nPipeline:")
    check("pipeline_dir contains scripts",
          (cfg.pipeline_dir / "music-import.sh").exists(),
          f"Check pipeline_dir: {cfg.pipeline_dir}")

    print("\nDestinations:")
    if not cfg.destinations:
        print("  ⚠   No destinations configured")
    for dest in cfg.destinations:
        if dest.enabled:
            check(f"{dest.name}  ({dest.path})",
                  Path(dest.path).exists(),
                  f"Mount or create: {dest.path}")
        else:
            print(f"  -   {dest.name} (disabled)")

    print("\nCredentials:")
    check("Telegram bot token set",
          bool(cfg.secrets.telegram.bot_token),
          "Add to ~/.config/spindlebot/secrets.toml  or  set SPINDLEBOT_TELEGRAM_TOKEN")
    check("Telegram chat ID set",
          bool(cfg.secrets.telegram.chat_id),
          "Add to ~/.config/spindlebot/secrets.toml  or  set SPINDLEBOT_TELEGRAM_CHAT_ID")
    check("Genius API key set",
          bool(cfg.secrets.genius.api_key),
          "Add to ~/.config/spindlebot/secrets.toml  or  set SPINDLEBOT_GENIUS_KEY")

    print()
    if ok:
        print("All checks passed ✓")
        return 0
    else:
        print("Some checks failed — see suggestions above.")
        return 1


# ── config shell ──────────────────────────────────────────────────────────────

def cmd_config_shell(cfg) -> int:
    """Emit shell-safe exports for every config value needed by shell scripts."""
    # Primary destination path (first enabled one)
    dest_path = next(
        (d.path for d in cfg.destinations if d.enabled), ""
    )

    exports = {
        "SPINDLEBOT_PENDING_DIR":      str(cfg.core.pending_dir),
        "SPINDLEBOT_IMPORT_DIR":       str(cfg.core.import_dir),
        "SPINDLEBOT_LOG_DIR":          str(cfg.core.log_dir),
        "SPINDLEBOT_ARCHIVE_DIR":      str(cfg.core.archive_dir),
        "SPINDLEBOT_BEET":             str(cfg.tools.beet),
        "SPINDLEBOT_PYTHON":           str(cfg.tools.python),
        "SPINDLEBOT_BEETS_DB":         str(cfg.tools.beets_db),
        "SPINDLEBOT_BEETS_CONFIG":     str(cfg.tools.beets_config),
        "SPINDLEBOT_PIPELINE_DIR":     str(cfg.pipeline_dir),
        "SPINDLEBOT_DESTINATION_PATH": dest_path,
        "SPINDLEBOT_TELEGRAM_TOKEN":   cfg.secrets.telegram.bot_token,
        "SPINDLEBOT_TELEGRAM_CHAT_ID": cfg.secrets.telegram.chat_id,
        "SPINDLEBOT_LYRICS_DELAY":     str(cfg.lyrics.request_delay_seconds),
        "SPINDLEBOT_MACOS_NOTIFY":     "1" if cfg.notifications.macos_notify else "0",
        "SPINDLEBOT_TELEGRAM_ENABLED": "1" if cfg.notifications.telegram_enabled else "0",
    }

    for key, val in exports.items():
        safe = val.replace("'", "'\\''")   # escape single quotes for shell
        print(f"export {key}='{safe}'")

    return 0


# ── config get ────────────────────────────────────────────────────────────────

def cmd_config_get(cfg, key: str) -> int:
    """Print a single dotted config value, e.g. core.pending_dir."""
    obj = cfg
    for part in key.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            print(f"Unknown config key: {key}", file=sys.stderr)
            return 1
    print(obj)
    return 0


# ── import-staging ────────────────────────────────────────────────────────────

def cmd_import_staging(cfg, args: list[str]) -> int:
    """
    Scan the import area and dispatch each found album through the import
    pipeline sequentially.

    With --dry-run, prints what would be imported without actually running
    music-import.sh.  Use this to preview the dispatch list before committing.
    """
    import subprocess
    from spindlebot.staging import scan_staging

    dry_run = "--dry-run" in args
    import_dir = cfg.core.import_dir

    items = scan_staging(import_dir)

    if not items:
        print(f"Nothing to import in {import_dir}")
        return 0

    print(f"Found {len(items)} item(s) in {import_dir}:")
    for item in items:
        kind_label = "log" if item.kind == "log" else "dir"
        print(f"  [{kind_label}] {item.path.name}")

    if dry_run:
        print("\n(dry run — not dispatching)")
        return 0

    print()
    import_script = str(cfg.pipeline_dir / "music-import.sh")
    errors = 0
    for item in items:
        print(f"→ importing: {item.path.name}")
        result = subprocess.call([import_script, str(item.path)])
        if result != 0:
            print(f"  warning: import exited {result} for {item.path.name}", file=sys.stderr)
            errors += 1

    if errors:
        print(f"\n{errors} import(s) reported errors — check the log.", file=sys.stderr)
        return 1
    return 0


# ── progress ──────────────────────────────────────────────────────────────────

def _make_progress(args: list[str], label: str):
    """Build a progress callback from CLI flags → (callback, reporter|None).

    Progress always goes to stderr so stdout stays clean (e.g. for --json).
    --quiet/--no-progress silences it; --verbose/-v switches to a scrolling
    per-file log; otherwise an in-place bar (TTY) / periodic lines (logs).
    """
    if "--quiet" in args or "--no-progress" in args:
        return None, None
    if "--verbose" in args or "-v" in args:
        def cb(ev):
            print(f"  [{ev.done}/{ev.total or '?'}] {ev.current}", file=sys.stderr)
        return cb, None
    from spindlebot.cli_progress import ProgressReporter
    reporter = ProgressReporter(stream=sys.stderr, label=label)
    return reporter.update, reporter


# ── inventory ─────────────────────────────────────────────────────────────────

def cmd_inventory(cfg, args: list[str]) -> int:
    """
    Scan a location and record content identity + presence in the SpindleBot DB.
    Read-only with respect to the audio files — it never moves or edits them.

    With no flag, scans the local Pending area. With `--location <name>`, scans
    that registered location once it's mounted and identified (e.g. DwRugged).
    """
    import json as _json
    import time
    from dataclasses import asdict
    from pathlib import Path

    from spindlebot.db.connection import open_db
    from spindlebot.services.inventory import inventory_location
    from spindlebot.services.locations import get_by_name, register_from_config
    from spindlebot.services.volumes import resolve_root

    want_json = "--json" in args
    location_name = None
    if "--location" in args:
        idx = args.index("--location")
        if idx + 1 < len(args):
            location_name = args[idx + 1]

    def fail(msg: str) -> int:
        if want_json:
            print(_json.dumps({"error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1

    now = int(time.time())
    conn = open_db(cfg.core.db_path)
    try:
        register_from_config(conn, cfg, now)
        if location_name:
            location = get_by_name(conn, location_name)
            if location is None:
                return fail(f"Unknown location: {location_name}")
            root = resolve_root(location)
            if root is None:
                return fail(
                    f"Location '{location_name}' is not mounted or not identified "
                    f"(expected at {location.root_path})"
                )
        else:
            location = get_by_name(conn, "Pending")
            root = Path(cfg.core.pending_dir)

        progress_cb, reporter = _make_progress(args, f"inventory {location.name}")
        result = inventory_location(
            conn, location=location, root=root, now=now,
            beets_db=cfg.tools.beets_db, progress=progress_cb,
        )
        if reporter is not None:
            reporter.close()
        conn.commit()
    finally:
        conn.close()

    if want_json:
        print(_json.dumps(asdict(result)))
    else:
        print(
            f"Inventoried {result.location} ({root}): "
            f"{result.scanned} scanned, {result.new} new, "
            f"{result.updated} updated, {result.albums} album(s), "
            f"{result.sidecars} sidecar(s), {result.errors} error(s)"
        )
        for ep in result.error_paths:
            print(f"  ! {ep}", file=sys.stderr)
    return 0 if result.errors == 0 else 1


# ── review ────────────────────────────────────────────────────────────────────

def cmd_review(cfg, args: list[str]) -> int:
    """
    Pre-sync review: plan reconciliation for a location and/or acknowledge the
    proposed actions. Planning is read-only and moves no bytes — it only writes
    pending_action rows. Acknowledging just flags intent; the Phase-3 executor is
    what eventually acts. Run `inventory --location <name>` first so the plan
    reflects a fresh scan.
    """
    import json as _json
    import time

    from spindlebot.db.connection import open_db
    from spindlebot.db.repositories import action_repo, audio_repo, location_repo
    from spindlebot.services.locations import get_by_name, register_from_config
    from spindlebot.services.reconciler import reconcile_location

    want_json = "--json" in args

    def _opt(flag: str) -> str | None:
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                return args[i + 1]
        return None

    def fail(msg: str) -> int:
        if want_json:
            print(_json.dumps({"error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1

    now = int(time.time())
    conn = open_db(cfg.core.db_path)
    try:
        register_from_config(conn, cfg, now)

        # ── acknowledge modes ──────────────────────────────────────────────
        ack_run = _opt("--acknowledge-run")
        ack_ids = _opt("--acknowledge")
        if ack_run is not None:
            n = action_repo.acknowledge_run(conn, int(ack_run), now)
            conn.commit()
            print(_json.dumps({"acknowledged": n, "run_id": int(ack_run)}) if want_json
                  else f"Acknowledged {n} action(s) in run {ack_run}.")
            return 0
        if ack_ids is not None:
            ids = [int(x) for x in ack_ids.split(",") if x.strip()]
            n = action_repo.acknowledge(conn, ids, now)
            conn.commit()
            print(_json.dumps({"acknowledged": n, "ids": ids}) if want_json
                  else f"Acknowledged {n} action(s).")
            return 0

        # ── plan mode ──────────────────────────────────────────────────────
        location_name = _opt("--location")
        if not location_name:
            return fail("Usage: spindlebot review --location <name> [--json]")
        target = get_by_name(conn, location_name)
        if target is None:
            return fail(f"Unknown location: {location_name}")
        authoritative = [
            loc for loc in location_repo.list_all(conn) if loc.is_authoritative_audio
        ]
        progress_cb, reporter = _make_progress(args, f"review {target.name}")
        result = reconcile_location(
            conn, target=target, authoritative_locations=authoritative,
            min_copies=cfg.core.min_copies, now=now, progress=progress_cb,
        )
        if reporter is not None:
            reporter.close()
        actions = action_repo.list_for_run(conn, result.run_id)
        conn.commit()

        if want_json:
            from dataclasses import asdict
            print(_json.dumps({
                **asdict(result),
                "actions": [
                    {"id": a.id, "kind": str(a.action_kind),
                     "content_kind": str(a.content_kind), "content_id": a.content_id,
                     "source_location_id": a.source_location_id,
                     "dest_location_id": a.dest_location_id,
                     "rel_path": a.rel_path, "reason": a.reason}
                    for a in actions
                ],
            }))
            return 0

        if not result.target_scanned:
            print(f"{result.location} has never been inventoried — run "
                  f"`spindlebot inventory --location {result.location}` first.",
                  file=sys.stderr)
            return 1
        print(
            f"Reconciled {result.location} (run {result.run_id}): "
            f"{result.copies} to copy, {result.missing} missing, "
            f"{result.conflicts} lyric conflict(s), "
            f"{result.below_floor} below min_copies={cfg.core.min_copies}"
        )
        for a in actions:
            if a.content_kind == "audio":
                au = audio_repo.get_by_id(conn, a.content_id)
                label = f"{au.artist or '?'} — {au.title or '?'}" if au else f"audio {a.content_id}"
            else:
                label = f"{a.content_kind} {a.content_id}"
            print(f"  [{a.id}] {a.action_kind}: {label}  ({a.reason})")
        if actions:
            print(f"\nAcknowledge: spindlebot review --acknowledge-run {result.run_id}")
        return 0
    finally:
        conn.close()


# ── entry point ───────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv)[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    # Lazy import so `--help` works without a valid config
    from spindlebot.config import load
    try:
        cfg = load()
    except Exception as exc:
        print(f"Error loading config: {exc}", file=sys.stderr)
        return 1

    command = args[0]

    if command == "check":
        return cmd_check(cfg)

    if command == "import":
        trigger = next((a for a in args[1:] if not a.startswith("-")), None)
        if trigger is None:
            print("Usage: spindlebot import <trigger> [--force]", file=sys.stderr)
            return 1
        force = "--force" in args
        from spindlebot.pipeline.runner import ImportConfig, ImportRunner
        import_cfg = ImportConfig(
            trigger=Path(trigger),
            force=force,
            beet=cfg.tools.beet,
            python=cfg.tools.python,
            db=cfg.tools.beets_db,
            pending_dir=cfg.core.pending_dir,
            import_dir=cfg.core.import_dir,
            archive=cfg.core.archive_dir,
            pipeline_dir=cfg.pipeline_dir,
            log_file=cfg.core.log_dir / "watcher.log",
            spindlebot_cfg=cfg,
        )
        print(f"💿 {Path(trigger).name}")
        runner = ImportRunner(import_cfg, echo=lambda msg: print(f"  {msg}"))
        result = runner.run()
        if result.success and result.artist_album:
            print(f"\n✅ {result.artist_album}")
        elif result.success:
            print("\n✅ done")
        else:
            print("\n✗  import failed — check the log", file=sys.stderr)
        return 0 if result.success else 1

    if command == "import-staging":
        return cmd_import_staging(cfg, args[1:])

    if command == "inventory":
        return cmd_inventory(cfg, args[1:])

    if command == "review":
        return cmd_review(cfg, args[1:])

    if command == "notify":
        if len(args) < 3:
            print("Usage: spindlebot notify <title> <message>", file=sys.stderr)
            return 1
        from spindlebot.pipeline.stages.notify import notify
        title, message = args[1], args[2]
        result = notify(title, message, cfg)
        if result.macos_sent:
            print("macOS notification sent")
        elif cfg.notifications.macos_notify and result.macos_error:
            print(f"macOS notification failed: {result.macos_error}", file=sys.stderr)
        if result.telegram_sent:
            print("Telegram notification sent")
        elif cfg.notifications.telegram_enabled and result.telegram_error:
            print(f"Telegram notification failed: {result.telegram_error}", file=sys.stderr)
        return 0 if result.any_sent else 1

    if command == "fetch-lyrics":
        if len(args) < 2:
            print("Usage: spindlebot fetch-lyrics <album_dir> [--dry-run] [--force]",
                  file=sys.stderr)
            return 1
        album_dir = next(a for a in args[1:] if not a.startswith("--"))
        dry_run = "--dry-run" in args
        force = "--force" in args
        from spindlebot.pipeline.stages.fetch_lyrics import fetch_lyrics
        result = fetch_lyrics(album_dir, cfg, dry_run=dry_run, force=force)
        print(
            f"{'[dry-run] ' if dry_run else ''}"
            f"synced={result.synced} plain={result.plain} "
            f"skipped={result.skipped} missing={result.missing}"
        )
        if result.errors:
            print(f"errors: {result.errors}", file=sys.stderr)
        return 0

    if command == "fetch-art":
        if len(args) < 2:
            print("Usage: spindlebot fetch-art <album_dir> [--dry-run] [--force]",
                  file=sys.stderr)
            return 1
        album_dir = next(a for a in args[1:] if not a.startswith("--"))
        dry_run = "--dry-run" in args
        force = "--force" in args
        from spindlebot.pipeline.stages.fetch_art import fetch_art
        result = fetch_art(album_dir, cfg, dry_run=dry_run, force=force)
        print(
            f"{'[dry-run] ' if dry_run else ''}"
            f"embedded={result.embedded} skipped={result.skipped} missing={result.missing}"
        )
        if result.errors:
            print(f"errors: {result.errors}", file=sys.stderr)
        return 0

    if command == "restart":
        import subprocess
        agents = [
            "com.strangestlens.music-watcher",
            "com.strangestlens.music-sync-rugged",
        ]
        uid = os.getuid()
        any_failed = False
        for agent in agents:
            target = f"gui/{uid}/{agent}"
            result = subprocess.run(
                ["launchctl", "kickstart", "-k", target],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"  restarted  {agent}")
            else:
                # agent may not be loaded (e.g. sync agent when drive not mounted)
                print(f"  skipped    {agent}  ({result.stderr.strip()})")
        return 0

    if command == "config":
        if len(args) < 2:
            print("Usage: spindlebot config shell|get <key>", file=sys.stderr)
            return 1
        sub = args[1]
        if sub == "shell":
            return cmd_config_shell(cfg)
        if sub == "get":
            if len(args) < 3:
                print("Usage: spindlebot config get <key>", file=sys.stderr)
                return 1
            return cmd_config_get(cfg, args[2])

    print(f"Unknown command: {command}", file=sys.stderr)
    return 1
