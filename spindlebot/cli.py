"""
SpindleBot CLI.

Usage:
    python -m spindlebot check                         Validate config and tool availability
    python -m spindlebot config shell                  Print config as shell-sourceable exports
    python -m spindlebot config get <key>              Print a single value (e.g. core.pending_dir)
    python -m spindlebot import <trigger> [--force]    Run import pipeline for an album
    python -m spindlebot import-staging [--dry-run]    Import everything currently in the Import area
    python -m spindlebot finalize [--dry-run] [--json]  Retry lyrics + promote lyric-complete albums out of Processing into Pending
    python -m spindlebot inventory [--location <name>] [--rehash] [--json] [-v|--quiet]   Scan a location into the SpindleBot DB (read-only re: audio; --rehash re-hashes unchanged files)
    python -m spindlebot review --location <name> [--yes] [--json] [-v|--quiet]  Plan reconciliation (--yes also acknowledges); no bytes moved
    python -m spindlebot review --acknowledge-run <run_id>        Acknowledge every proposed action in a run
    python -m spindlebot review --acknowledge <id[,id...]>        Acknowledge specific proposed actions
    python -m spindlebot sync [--location <name>] [--json] [-v|--quiet]   Execute acknowledged copies (copy→verify→presence); --location scopes to one dest
    python -m spindlebot prune [--execute] [--json] [-v|--quiet]  Release Pending files verified on retention (DRY-RUN unless --execute)
    python -m spindlebot delete [--execute] [--json] [-v|--quiet]  Execute acknowledged retention-copy deletes, gated on min_copies (DRY-RUN unless --execute)
    python -m spindlebot collection-audit [--handle <name>] [--source discogs|fixture] [--media cd,vinyl] [--index auto|beets|db] [--refresh] [--strict] [--all] [--show-ignored] [--html <file>] [--json]
                                                      Compare an external collection against the library and list what's missing
    python -m spindlebot collection-ignore <id...> [--reason <text>] [--json]   Stop reporting a disc as missing
    python -m spindlebot collection-ignore --list [--json]                      Show what's ignored
    python -m spindlebot collection-ignore --remove <id...> [--json]            Un-ignore (also --unignore)
    python -m spindlebot collection-ignore --clear --yes [--json]               Un-ignore everything
    python -m spindlebot note add [--artist <a>] [--album <b>] [--track <t>] [--new] [-m <text>|-F <file>|-] [--tag <t>] [--session <id>] [--json]
                                                      Write a listening note at artist / album / track level
    python -m spindlebot note list [--artist <a>] [--album <b>] [--track <t>] [--kind artist|album|track] [--tag <t>] [--session <id>] [--since <date>] [--all] [--json]
    python -m spindlebot note show <id> [--history] [--json]      Show a note, optionally every revision
    python -m spindlebot note edit <id> [-m <text>|-F <file>|-]   Append a revision (unchanged text writes nothing)
    python -m spindlebot note rm <id> | restore <id>              Retire a note (SOFT) or bring it back
    python -m spindlebot note tag <id> <tag>... | untag <id> <tag>
    python -m spindlebot note session start [--title <text>]      Open a listening sitting
    python -m spindlebot note sessions [--since <date>] [--json]  The listening log, newest first
    python -m spindlebot notify <title> <message>      Send a test notification via all channels
    python -m spindlebot fetch-lyrics <dir> [--dry-run] [--force]   Fetch .lrc files for an album
    python -m spindlebot fetch-art <dir> [--dry-run] [--force]      Fetch/embed album art
    python -m spindlebot restart                                     Restart launchd agents
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# ── helpers ───────────────────────────────────────────────────────────────────

def _retention_path(destinations) -> Path | None:
    """Path of the first enabled local_drive destination, or None.

    Auto-sync's mount check is `path.exists()`, which is meaningless for an
    rclone remote (always false) — so only a local_drive destination can serve
    as the retention mount probe.
    """
    return next(
        (Path(d.path) for d in destinations
         if d.enabled and d.type == "local_drive"),
        None,
    )


def _retention_name(destinations) -> str | None:
    """Name of the first enabled local_drive destination (same selection as
    `_retention_path`), or None. This is the `[[destinations]]` name the sync
    script and `spindlebot review/sync --location` key off — nothing is
    hardcoded to any one drive."""
    return next(
        (d.name for d in destinations
         if d.enabled and d.type == "local_drive"),
        None,
    )


def _watch_volume(path: Path | None) -> str | None:
    """The volume mount point to watch for a retention path, e.g.
    `/Volumes/RetentionDrive/Music/Library` → `/Volumes/RetentionDrive`. launchd fires the
    sync agent when this path appears. Returns None for a path not under
    /Volumes (nothing to watch). Best-effort: a mounted volume always sits at
    `/Volumes/<name>`."""
    if path is None:
        return None
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return str(Path(parts[0], parts[1], parts[2]))
    return None


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
    # Primary destination path + name — the first enabled LOCAL_DRIVE
    # destination, the same selection as ImportConfig.retention_path. music-sync.sh
    # uses the path as REMOTE for its `[ ! -d "$REMOTE" ]` mount check (an rclone
    # remote here would make the script silently no-op even with the drive
    # mounted) and the name for `review/sync --location`. WATCH_VOLUME is the
    # `/Volumes/<name>` mount setup.sh wires into the launchd agent's WatchPaths.
    retention = _retention_path(cfg.destinations)
    dest_path = str(retention) if retention is not None else ""
    dest_name = _retention_name(cfg.destinations) or ""
    watch_volume = _watch_volume(retention) or ""

    exports = {
        "SPINDLEBOT_PENDING_DIR":      str(cfg.core.pending_dir),
        "SPINDLEBOT_IMPORT_DIR":       str(cfg.core.import_dir),
        "SPINDLEBOT_PROCESSING_DIR":   str(cfg.core.processing_dir),
        "SPINDLEBOT_LOG_DIR":          str(cfg.core.log_dir),
        "SPINDLEBOT_ARCHIVE_DIR":      str(cfg.core.archive_dir),
        "SPINDLEBOT_DUPLICATES_DIR":   str(cfg.core.duplicates_dir),
        "SPINDLEBOT_BEET":             str(cfg.tools.beet),
        "SPINDLEBOT_PYTHON":           str(cfg.tools.python),
        "SPINDLEBOT_BEETS_DB":         str(cfg.tools.beets_db),
        "SPINDLEBOT_BEETS_CONFIG":     str(cfg.tools.beets_config),
        "SPINDLEBOT_PIPELINE_DIR":     str(cfg.pipeline_dir),
        "SPINDLEBOT_DESTINATION_PATH": dest_path,
        "SPINDLEBOT_DESTINATION_NAME": dest_name,
        "SPINDLEBOT_WATCH_VOLUME":     watch_volume,
        "SPINDLEBOT_TELEGRAM_TOKEN":   cfg.secrets.telegram.bot_token,
        "SPINDLEBOT_TELEGRAM_CHAT_ID": cfg.secrets.telegram.chat_id,
        "SPINDLEBOT_LYRICS_DELAY":     str(cfg.lyrics.request_delay_seconds),
        "SPINDLEBOT_MACOS_NOTIFY":     "1" if cfg.notifications.macos_notify else "0",
        "SPINDLEBOT_TELEGRAM_ENABLED": "1" if cfg.notifications.telegram_enabled else "0",
        "SPINDLEBOT_AUTO_SYNC_ON_IMPORT": "1" if cfg.core.auto_sync_on_import else "0",
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


# ── finalize ──────────────────────────────────────────────────────────────────

def cmd_finalize(cfg, args: list[str]) -> int:
    """
    Sweep the Processing area: for each album still there, re-fetch lyrics to
    resolve transient-error tracks, then promote it to Pending if now complete.

    Idempotent — safe to re-run. This catches up albums the import left stuck in
    Processing because a lyric fetch hit a transient error.
    """
    import json as _json

    from spindlebot.services.promote import finalize_processing

    want_json = "--json" in args
    dry_run = "--dry-run" in args

    result = finalize_processing(cfg.core.processing_dir, cfg, dry_run=dry_run)

    # A promote MOVE failure (DB locked / beets error) is a real failure → exit
    # nonzero. Albums merely waiting on lyrics are expected (finalize will retry
    # them next run) → exit 0. Dry-run never issues a move, so never fails.
    move_failed = not dry_run and any(p.move_error for p in result.waiting)
    exit_code = 1 if move_failed else 0

    if want_json:
        print(_json.dumps({
            "processing_dir": str(cfg.core.processing_dir),
            "dry_run": dry_run,
            "scanned": result.scanned,
            "promoted": [
                {"label": p.label, "album_dir": str(p.album_dir)}
                for p in result.promoted
            ],
            "waiting": [
                {"label": p.label, "album_dir": str(p.album_dir),
                 "waiting_on": p.waiting_on, "move_error": p.move_error}
                for p in result.waiting
            ],
        }))
        return exit_code

    prefix = "[dry-run] " if dry_run else ""
    if result.scanned == 0:
        print(f"{prefix}Nothing in Processing.")
        return 0

    print(f"{prefix}Scanned {result.scanned} album(s) in Processing:")
    for p in result.promoted:
        arrow = "would promote →" if dry_run else "✅"
        print(f"  {arrow} {p.label} → Pending")
    for p in result.waiting:
        if p.move_error:
            print(f"  ✗  {p.label} promote failed (stays in Processing): {p.move_error}", file=sys.stderr)
        else:
            waiting = ", ".join(p.waiting_on) or "album"
            print(f"  ⏳ {p.label} waiting on lyrics: {waiting}")
    return exit_code


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
    that registered location once it's mounted and identified (e.g. the retention drive).
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
    rehash = "--force" in args or "--rehash" in args
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
            checkpoint=conn.commit,  # keep partial progress durable mid-scan
            rehash=rehash,
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
        # Any other location can source a copy — the authoring library or another
        # retention location (so a DAP can be filled from the retention drive after prune).
        sources = [loc for loc in location_repo.list_all(conn) if loc.id != target.id]
        progress_cb, reporter = _make_progress(args, f"review {target.name}")
        result = reconcile_location(
            conn, target=target, source_locations=sources,
            min_copies=cfg.core.min_copies, now=now, progress=progress_cb,
        )
        if reporter is not None:
            reporter.close()
        actions = action_repo.list_for_run(conn, result.run_id)
        # --yes: plan + acknowledge in one shot (for the automatic mount flow).
        acknowledged = 0
        if "--yes" in args and result.target_scanned:
            acknowledged = action_repo.acknowledge_run(conn, result.run_id, now)
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
                "acknowledged": acknowledged,
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
        if acknowledged:
            print(f"\nAcknowledged {acknowledged} action(s) — run `spindlebot sync`.")
        elif actions:
            print(f"\nAcknowledge: spindlebot review --acknowledge-run {result.run_id}")
        return 0
    finally:
        conn.close()


# ── sync ──────────────────────────────────────────────────────────────────────

def cmd_sync(cfg, args: list[str]) -> int:
    """
    Execute acknowledged copy actions: copy → verify destination hash → record
    presence. NON-destructive — it only adds verified copies, never deletes or
    prunes. Run `review` + acknowledge first to queue the work.
    """
    import json as _json
    import time
    from dataclasses import asdict

    from spindlebot.db.connection import open_db
    from spindlebot.services.locations import get_by_name, register_from_config
    from spindlebot.services.sync import execute_pending

    want_json = "--json" in args
    location_name = None
    if "--location" in args:
        idx = args.index("--location")
        if idx + 1 < len(args) and not args[idx + 1].startswith("-"):
            location_name = args[idx + 1]
        else:
            # a bare --location must not silently fall back to an unscoped sync
            msg = "--location requires a destination name"
            print(_json.dumps({"error": msg}) if want_json else msg,
                  file=sys.stdout if want_json else sys.stderr)
            return 1

    now = int(time.time())
    conn = open_db(cfg.core.db_path)
    try:
        register_from_config(conn, cfg, now)
        dest_id = None
        if location_name:
            dest = get_by_name(conn, location_name)
            if dest is None:
                msg = f"Unknown location: {location_name}"
                if want_json:
                    print(_json.dumps({"error": msg}))
                else:
                    print(msg, file=sys.stderr)
                return 1
            dest_id = dest.id
        progress_cb, reporter = _make_progress(args, "sync")
        result = execute_pending(conn, now=now, dest_location_id=dest_id,
                                 progress=progress_cb, checkpoint=conn.commit)
        if reporter is not None:
            reporter.close()
        conn.commit()
    finally:
        conn.close()

    if want_json:
        print(_json.dumps(asdict(result)))
    else:
        print(f"Synced (run {result.run_id}): {result.copied} copied, "
              f"{result.failed} failed, {result.skipped} skipped")
        for e in result.errors:
            print(f"  ! {e}", file=sys.stderr)
    # Nonzero if anything went wrong — a failed copy OR an acknowledged copy we
    # couldn't do (e.g. dest unmounted). result.errors holds only real problems
    # (benign skips like a not-yet-supported sidecar copy don't append to it),
    # so automation can tell "nothing to do" from "couldn't do the work".
    return 0 if (result.failed == 0 and not result.errors) else 1


# ── prune ─────────────────────────────────────────────────────────────────────

def cmd_prune(cfg, args: list[str]) -> int:
    """
    Release files from the authoring library (Pending) once they're verified on a
    retention location. DESTRUCTIVE — but DRY-RUN by default: it only reports what
    it would delete. Pass --execute to actually delete. Retention locations are
    never touched; a file is only released when the exact path+hash is confirmed
    (and re-hashed) on retention.
    """
    import json as _json
    import time
    from dataclasses import asdict

    from spindlebot.db.connection import open_db
    from spindlebot.services.locations import register_from_config
    from spindlebot.services.sync import prune_released

    want_json = "--json" in args
    execute = "--execute" in args
    verify = "--no-verify" not in args
    now = int(time.time())
    conn = open_db(cfg.core.db_path)
    try:
        register_from_config(conn, cfg, now)
        progress_cb, reporter = _make_progress(args, "prune")
        result = prune_released(conn, now=now, dry_run=not execute, verify=verify,
                                min_copies=cfg.core.min_copies, progress=progress_cb)
        if reporter is not None:
            reporter.close()
        conn.commit()
    finally:
        conn.close()

    if want_json:
        print(_json.dumps(asdict(result)))
    else:
        verb = "Would release" if result.dry_run else "Released"
        print(f"{verb} {result.pruned} file(s) "
              f"({result.bytes_freed / (1024 * 1024):.1f} MB), "
              f"{result.skipped} kept (not yet safely retained).")
        if result.below_floor:
            print(f"  ⚠ {result.below_floor} released track(s) are now on fewer "
                  f"than min_copies={cfg.core.min_copies} retention copies "
                  f"(single-copy until another retention location is added).",
                  file=sys.stderr)
        if result.dry_run and result.pruned:
            print("Re-run with --execute to actually delete.")
        for e in result.errors:
            print(f"  ! {e}", file=sys.stderr)
    return 0 if not result.errors else 1


# ── delete ────────────────────────────────────────────────────────────────────

def cmd_delete(cfg, args: list[str]) -> int:
    """
    Execute acknowledged DELETE actions — remove a RETENTION copy. DESTRUCTIVE,
    and GATED: a delete that would leave fewer than min_copies retention copies of
    the content — or whose source is not a retention location at all — is REFUSED,
    never performed. A refusal is safe/expected (warned, exit 0); only genuine
    failures exit nonzero. DRY-RUN by default (only reports what it would delete);
    pass --execute to actually delete. Run `review` + acknowledge first to queue
    the work.
    """
    import json as _json
    import time
    from dataclasses import asdict

    from spindlebot.db.connection import open_db
    from spindlebot.services.locations import register_from_config
    from spindlebot.services.sync import execute_deletes

    want_json = "--json" in args
    execute = "--execute" in args
    now = int(time.time())
    conn = open_db(cfg.core.db_path)
    try:
        register_from_config(conn, cfg, now)
        progress_cb, reporter = _make_progress(args, "delete")
        result = execute_deletes(conn, now=now, dry_run=not execute,
                                 min_copies=cfg.core.min_copies, progress=progress_cb)
        if reporter is not None:
            reporter.close()
        conn.commit()
    finally:
        conn.close()

    if want_json:
        print(_json.dumps(asdict(result)))
    else:
        verb = "Would delete" if result.dry_run else "Deleted"
        print(f"{verb} {result.deleted} retention copy(ies) "
              f"({result.bytes_freed / (1024 * 1024):.1f} MB), "
              f"{result.refused} refused (kept safe), {result.skipped} skipped.")
        for r in result.refused_reasons:
            print(f"  ⚠ {r}", file=sys.stderr)
        if result.dry_run and result.deleted:
            print("Re-run with --execute to actually delete.")
        for e in result.errors:
            print(f"  ! {e}", file=sys.stderr)
    # A refusal (below-floor, or a non-retention source) is a SAFE, expected
    # outcome — warned via refused_reasons, exit 0 (Daniel's call, mirroring
    # prune's below_floor). Nonzero is reserved for genuine failures in
    # result.errors (unmounted/unidentified location, OSError).
    return 0 if not result.errors else 1


# ── collection-audit ──────────────────────────────────────────────────────────

def cmd_collection_audit(cfg, args: list[str]) -> int:
    """
    Compare an external collection (Discogs by default) against the digital
    library and report what hasn't been ripped. Purely assistive: reads the
    library, writes nothing but a fetch cache.
    """
    import json as _json

    from spindlebot.core.collection import resolve_media
    from spindlebot.core.errors import SpindleBotError
    from spindlebot.services.collection_audit import run_audit

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

    account = _opt("--handle") or _opt("--account") or cfg.collection.account
    if not account:
        return fail(
            "No collection account. Pass --handle <name>, or set\n"
            "  [collection]\n  account = \"<name>\"\n"
            "in ~/.config/spindlebot/config.toml"
        )

    source = _opt("--source") or cfg.collection.source
    index = _opt("--index") or cfg.collection.index
    raw_media = _opt("--media")
    try:
        media = resolve_media(
            raw_media.split(",") if raw_media else cfg.collection.media
        )
    except ValueError as e:
        return fail(str(e))

    try:
        report = run_audit(
            cfg,
            account=account,
            source=source,
            media=media,
            refresh="--refresh" in args,
            index=index,
            strict="--strict" in args,
        )
    except (SpindleBotError, ValueError, RuntimeError) as e:
        return fail(str(e))

    html_out = _opt("--html")
    if html_out:
        from spindlebot.services.collection_report import render_html
        html_path = Path(html_out).expanduser()
        try:
            html_path.parent.mkdir(parents=True, exist_ok=True)
            html_path.write_text(render_html(report), encoding="utf-8")
        except OSError as e:
            return fail(f"could not write {html_path}: {e}")

    if want_json:
        print(_json.dumps({
            "source": report.source,
            "account": report.account,
            "html": str(html_path) if html_out else None,
            "media": sorted(m.value for m in report.media),
            "fetched": report.fetched,
            "considered": report.considered,
            "library_albums": report.library_albums,
            "library_sources": report.library_sources,
            "library_errors": report.library_errors,
            "counts": {
                "owned": len(report.owned),
                "uncertain": len(report.uncertain),
                "missing": len(report.missing),
                "ignored": len(report.ignored),
            },
            "items": [
                {
                    "key": m.item.key,
                    "artist": m.item.artist,
                    "title": m.item.title,
                    "year": m.item.year,
                    "media": sorted(k.value for k in m.item.media),
                    "url": m.item.url,
                    "thumb_url": m.item.thumb_url,
                    "status": m.status.value,
                    "ignored": m.ignored,
                    "reason": m.reason,
                    "score": round(m.score, 3),
                    "matched": (
                        {"albumartist": m.matched.albumartist, "album": m.matched.album}
                        if m.matched else None
                    ),
                }
                for m in report.matches
            ],
        }))
        return 0

    # Widest id in this run, so the ids column lines up without being padded to
    # some arbitrary guess.
    id_width = max((len(m.item.source_id) for m in report.matches), default=0)

    def line(m) -> str:
        # Some release titles already carry their year ("Hyperspace (2020)");
        # appending it again just reads as a bug.
        year = m.item.year
        suffix = f" ({year})" if year and str(year) not in m.item.title else ""
        # The id leads: it's what `collection-ignore` takes, and a list you
        # can't act on from is just a list.
        return f"  {m.item.source_id:<{id_width}}  {m.item.artist} — {m.item.title}{suffix}"

    media_label = "/".join(sorted(k.value for k in report.media))
    print(
        f"{report.source}:{report.account} — {report.fetched} item(s), "
        f"{report.considered} on {media_label}"
    )
    # Always show which index answered. A wrongly-missing album is almost always
    # an index that didn't know about it, so this is the first thing to check.
    breakdown = ", ".join(f"{k} {v}" for k, v in sorted(report.library_sources.items()))
    print(f"library ({breakdown}) — {report.library_albums} unique album(s)")
    for name, err in sorted(report.library_errors.items()):
        print(f"  ⚠  {name} index unavailable: {err}", file=sys.stderr)
    print()

    if report.uncertain:
        print(f"UNCERTAIN ({len(report.uncertain)}) — confirm these yourself")
        for m in report.uncertain:
            print(line(m))
            print(f"      ≈ {m.matched.albumartist} — {m.matched.album} ({m.score:.2f})")
        print()

    print(f"MISSING ({len(report.missing)})")
    for m in report.missing:
        print(line(m))

    if "--all" in args and report.owned:
        print(f"\nOWNED ({len(report.owned)})")
        for m in report.owned:
            print(line(m))

    if report.ignored and ("--all" in args or "--show-ignored" in args):
        print(f"\nIGNORED ({len(report.ignored)})")
        for m in report.ignored:
            print(line(m))

    tail = f" · {len(report.ignored)} ignored" if report.ignored else ""
    print(
        f"\n{len(report.owned)} owned · {len(report.uncertain)} uncertain · "
        f"{len(report.missing)} missing{tail}"
    )
    if report.missing:
        print(
            "\nNot going to rip one? "
            f"spindlebot collection-ignore {report.missing[0].item.source_id}"
        )
    if html_out:
        print(f"📄 {html_path}")
    return 0


# ── collection-ignore ─────────────────────────────────────────────────────────

def cmd_collection_ignore(cfg, args: list[str]) -> int:
    """
    Manage the collection-audit ignore list: discs you know you're missing and
    don't want told about again. Always reversible — `--remove` puts one back.
    """
    import json as _json

    from spindlebot.core.errors import SpindleBotError
    from spindlebot.services.collection_ignore import IgnoreStore, resolve_key

    want_json = "--json" in args
    FLAGS = {"--json", "--list", "--clear", "--yes", "--remove", "--unignore",
             "--reason", "--source"}

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

    def emit(payload: dict, lines: list[str]) -> int:
        if want_json:
            print(_json.dumps(payload))
        else:
            for line in lines:
                print(line)
        return 0

    source = _opt("--source") or cfg.collection.source
    removing = "--remove" in args or "--unignore" in args

    # Every bare token is an id; values consumed by --reason/--source are not.
    consumed = {v for v in (_opt("--reason"), _opt("--source")) if v is not None}
    tokens = [a for a in args if a not in FLAGS and a not in consumed]

    try:
        store = IgnoreStore.load(cfg.collection.ignore_path)
    except SpindleBotError as e:
        return fail(str(e))

    # ── list ─────────────────────────────────────────────────────────────────
    # A bare invocation lists, but `--remove` with no ids must NOT: asking to
    # remove something and being shown a listing is the wrong answer to the
    # wrong question.
    if removing and not tokens:
        return fail("Usage: spindlebot collection-ignore --remove <id> [<id>...]")
    if "--list" in args or (not tokens and "--clear" not in args):
        entries = store.listing()
        payload = {
            "path": str(store.path),
            "count": len(entries),
            "ignored": [
                {"key": i.key, "artist": i.artist, "title": i.title,
                 "reason": i.reason, "ignored_utc": i.ignored_utc}
                for i in entries
            ],
        }
        if not entries:
            return emit(payload, ["Nothing ignored."])
        lines = [f"Ignored ({len(entries)}) — {store.path}"]
        width = max(len(i.key) for i in entries)
        for i in entries:
            note = f"   ({i.reason})" if i.reason else ""
            lines.append(f"  {i.key:<{width}}  {i.label}{note}")
        lines.append("\nPut one back:  spindlebot collection-ignore --remove <id>")
        return emit(payload, lines)

    # ── clear ────────────────────────────────────────────────────────────────
    if "--clear" in args:
        if "--yes" not in args:
            return fail(
                f"--clear removes all {len(store)} ignored item(s). "
                "Re-run with --yes if that's what you want."
            )
        removed = store.clear()
        try:
            store.save()
        except SpindleBotError as e:
            return fail(str(e))
        return emit({"cleared": removed}, [f"Cleared {removed} ignored item(s)."])

    # ── remove ───────────────────────────────────────────────────────────────
    if removing:
        removed, unknown = [], []
        for token in tokens:
            try:
                key = resolve_key(token, source=source)
            except ValueError:
                continue
            entry = store.remove(key)
            (removed if entry else unknown).append(entry.key if entry else key)
        if removed:
            try:
                store.save()
            except SpindleBotError as e:
                return fail(str(e))
        lines = [f"  ↩ {k}" for k in removed] or ["Nothing removed."]
        lines += [f"  ?  {k} was not ignored" for k in unknown]
        if removed:
            lines.insert(0, f"Un-ignored {len(removed)} item(s):")
        return emit({"removed": removed, "not_ignored": unknown}, lines)

    # ── add ──────────────────────────────────────────────────────────────────
    keys = []
    for token in tokens:
        try:
            keys.append(resolve_key(token, source=source))
        except ValueError:
            return fail(f"invalid id: {token!r}")

    # Best-effort: pull artist/title off the LOCAL collection cache so `--list`
    # is readable later. cached_only means this can never reach the network —
    # a bookkeeping command must not stall on HTTP, and "best-effort" has to
    # cover slow as well as broken. Still wrapped, for a corrupt cache.
    details: dict = {}
    try:
        from spindlebot.collections.base import get_provider
        account = cfg.collection.account
        if account:
            provider = get_provider(source, cfg)
            details = {i.key: i for i in provider.fetch(account, cached_only=True)}
    except (SpindleBotError, ValueError, RuntimeError, OSError):
        details = {}

    reason = _opt("--reason") or ""
    added, unknown = [], []
    for key in keys:
        item = details.get(key)
        # Only meaningful when we actually know the collection. A mistyped id
        # is otherwise written and quietly does nothing forever — you'd think
        # you'd ignored a disc and it would keep showing up.
        if details and item is None:
            unknown.append(key)
        entry = store.add(
            key,
            artist=item.artist if item else "",
            title=item.title if item else "",
            reason=reason,
        )
        added.append(entry)
    try:
        store.save()
    except SpindleBotError as e:
        return fail(str(e))

    payload = {
        "added": [{"key": e.key, "artist": e.artist, "title": e.title,
                   "reason": e.reason} for e in added],
        "not_in_collection": unknown,
        "count": len(store),
    }
    lines = [f"Ignoring {len(added)} item(s):"]
    lines += [f"  ✓ {e.key}  {e.label}" for e in added]
    for key in unknown:
        print(f"  ⚠  {key} isn't in the collection — check the id", file=sys.stderr)
    lines.append(f"\nUndo:  spindlebot collection-ignore --remove {added[0].key}")
    return emit(payload, lines)


# ── entry point ───────────────────────────────────────────────────────────────

# ── note ──────────────────────────────────────────────────────────────────────

_NOTE_VALUE_FLAGS = {
    "--artist", "--album", "--track", "--tag", "--session", "--since",
    "--kind", "--index", "--title", "-m", "--message", "-F", "--file",
}
_NOTE_BOOL_FLAGS = {"--json", "--new", "--all", "--history", "-"}


def _note_opts(args: list[str], flag: str) -> list[str]:
    """Every value given for a repeatable flag, in order."""
    return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]


def _note_opt(args: list[str], *flags: str) -> str | None:
    for flag in flags:
        values = _note_opts(args, flag)
        if values:
            return values[0]
    return None


def _note_positionals(args: list[str]) -> list[str]:
    """Bare tokens, excluding flags and the values they consume."""
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in _NOTE_VALUE_FLAGS:
            skip = True
            continue
        if arg in _NOTE_BOOL_FLAGS or arg.startswith("--"):
            continue
        out.append(arg)
    return out


def _parse_since(raw: str) -> int:
    """`--since 2026-01-01` or a full ISO timestamp, as epoch seconds."""
    from datetime import date, datetime
    try:
        if len(raw) == 10:
            return int(datetime.combine(date.fromisoformat(raw), datetime.min.time()).timestamp())
        return int(datetime.fromisoformat(raw).timestamp())
    except ValueError as e:
        raise ValueError(f"--since wants YYYY-MM-DD or an ISO timestamp, got {raw!r}") from e


def _launch_editor(path: Path) -> None:
    import subprocess
    editor = os.environ.get("SPINDLEBOT_EDITOR") or os.environ.get("VISUAL") \
        or os.environ.get("EDITOR") or "vi"
    subprocess.run([*editor.split(), str(path)], check=False)


def read_note_body(
    args: list[str],
    *,
    initial: str = "",
    stdin=None,
    launch_editor=None,
) -> str:
    """Resolve a note body from the four input modes, in precedence order.

    1. `-m/--message`, repeatable, joined by a blank line — multi-paragraph
       input without fighting the shell over newlines
    2. `-F/--file <path>`
    3. `-`, or a piped stdin — reading a note out of anything upstream
    4. nothing, on a terminal — open $EDITOR

    The editor buffer gets NO commented header. Git can use `#` for that because
    `#` is not meaningful in a commit message; here it is a markdown heading, and
    a header would make "strip the comments" and "keep the author's headings"
    the same operation. The subject is already on the command line the user just
    typed.
    """
    stdin = stdin if stdin is not None else sys.stdin
    launch_editor = launch_editor or _launch_editor

    messages = _note_opts(args, "-m") + _note_opts(args, "--message")
    if messages:
        return "\n\n".join(messages)

    path = _note_opt(args, "-F", "--file")
    if path == "-" or (path is None and "-" in args):
        return stdin.read()
    if path is not None:
        return Path(path).expanduser().read_text(encoding="utf-8")

    if not stdin.isatty():
        return stdin.read()

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "NOTE_EDITMSG.md"
        scratch.write_text(initial, encoding="utf-8")
        launch_editor(scratch)
        return scratch.read_text(encoding="utf-8")


def _note_resolve(*args, **kwargs):
    """Indirection so the CLI's own accept/refuse gate is testable on its own,
    independently of what the resolver currently happens to return."""
    from spindlebot.services.note_resolve import resolve
    return resolve(*args, **kwargs)


def cmd_note(cfg, args: list[str]) -> int:
    """
    Listening notes at artist / album / track level.

    Notes are the only authored, un-regenerable data in this system, so every
    write here appends: editing a note adds a revision, deleting one is a status
    change, and `note export` exists so the writing is never trapped in SQLite.
    """
    import json as _json

    from spindlebot.core.enums import NoteSubjectKind
    from spindlebot.core.notes import canonicalize_body
    from spindlebot.db.connection import open_db
    from spindlebot.services import notes as svc
    from spindlebot.services.note_resolve import ResolutionStatus

    want_json = "--json" in args
    sub = args[0] if args and not args[0].startswith("-") else None

    def fail(msg: str, *, hints: list[str] | None = None) -> int:
        if want_json:
            print(_json.dumps({"error": msg, "candidates": hints or []}))
        else:
            print(msg, file=sys.stderr)
            for hint in hints or []:
                print(f"    {hint}", file=sys.stderr)
        return 1

    def view_payload(view) -> dict:
        return {
            "id": view.id,
            "uuid": view.note.uuid,
            "kind": str(view.subject.kind),
            "subject": view.label,
            "subject_key": view.subject.subject_key,
            "revision": view.revision,
            "tags": list(view.tags),
            "session_id": view.note.session_id,
            "created_utc": view.note.created_utc,
            "updated_utc": view.note.updated_utc,
            "body": view.body,
        }

    if sub not in {"add", "list", "show", "edit", "rm", "restore", "tag",
                   "untag", "sessions", "session"}:
        return fail(
            "Usage: spindlebot note add|list|show|edit|rm|restore|tag|untag|"
            "sessions|session start"
        )

    rest = args[1:]
    positionals = _note_positionals(rest)
    conn = open_db(cfg.core.db_path)
    try:
        # ── add ──────────────────────────────────────────────────────────────
        if sub == "add":
            artist = _note_opt(rest, "--artist")
            album = _note_opt(rest, "--album")
            track = _note_opt(rest, "--track")
            allow_new = "--new" in rest

            library = []
            try:
                from spindlebot.services import library_index
                library = library_index.load(cfg, _note_opt(rest, "--index") or "auto").albums
            except (RuntimeError, ValueError) as e:
                # Without --new there is nothing to resolve against and a note
                # would be filed under an unverified subject; with it, the user
                # has already said the library is not the authority here.
                if not allow_new:
                    return fail(f"cannot read the library: {e}")

            try:
                resolution = _note_resolve(
                    library, artist=artist, album=album, track=track, allow_new=allow_new
                )
            except ValueError as e:
                return fail(str(e))

            if resolution.status is not ResolutionStatus.RESOLVED:
                return fail(
                    f"{resolution.status}: {resolution.reason}",
                    hints=[c.label for c in resolution.candidates]
                    + ["(use --new to write about something the library doesn't have)"],
                )

            body = canonicalize_body(read_note_body(rest))
            if not body:
                return fail("empty note, nothing written")

            session_raw = _note_opt(rest, "--session")
            view = svc.add_note(
                conn,
                subject=resolution.subject,
                body=body,
                session_id=int(session_raw) if session_raw else None,
                tags=_note_opts(rest, "--tag"),
            )
            conn.commit()
            if want_json:
                print(_json.dumps(view_payload(view)))
            else:
                print(f"note {view.id}  {view.label}")
            return 0

        # ── list ─────────────────────────────────────────────────────────────
        if sub == "list":
            kind_raw = _note_opt(rest, "--kind")
            since_raw = _note_opt(rest, "--since")
            session_raw = _note_opt(rest, "--session")
            try:
                kind = NoteSubjectKind(kind_raw) if kind_raw else None
                since = _parse_since(since_raw) if since_raw else None
            except ValueError as e:
                return fail(str(e))

            views = svc.list_notes(
                conn,
                artist=_note_opt(rest, "--artist"),
                album=_note_opt(rest, "--album"),
                track=_note_opt(rest, "--track"),
                kind=kind,
                tag=_note_opt(rest, "--tag"),
                session_id=int(session_raw) if session_raw else None,
                since_utc=since,
                include_deleted="--all" in rest,
            )
            if want_json:
                print(_json.dumps({"count": len(views),
                                   "notes": [view_payload(v) for v in views]}))
                return 0
            if not views:
                print("no notes")
                return 0
            for view in views:
                tags = f"  [{' '.join(view.tags)}]" if view.tags else ""
                first = view.body.split("\n", 1)[0]
                head = first if len(first) <= 72 else first[:71] + "…"
                print(f"{view.id:>5}  {view.label}{tags}")
                print(f"       {head}")
            return 0

        # ── show ─────────────────────────────────────────────────────────────
        if sub == "show":
            if not positionals:
                return fail("Usage: spindlebot note show <id> [--history]")
            view = svc.get_note(conn, int(positionals[0]))
            if view is None:
                return fail(f"no note {positionals[0]}")
            revisions = svc.history(conn, view.id) if "--history" in rest else []
            if want_json:
                payload = view_payload(view)
                payload["history"] = [
                    {"seq": r.seq, "sha256": r.sha256, "created_utc": r.created_utc,
                     "body": r.body}
                    for r in revisions
                ]
                print(_json.dumps(payload))
                return 0
            print(f"note {view.id}  {view.label}")
            print(f"  revision {view.revision}"
                  + (f"  tags: {' '.join(view.tags)}" if view.tags else ""))
            print()
            print(view.body)
            for revision in revisions[:-1]:
                print(f"\n--- revision {revision.seq} ---")
                print(revision.body)
            return 0

        # ── edit ─────────────────────────────────────────────────────────────
        if sub == "edit":
            if not positionals:
                return fail("Usage: spindlebot note edit <id> [-m <text> | -F <file>]")
            current = svc.get_note(conn, int(positionals[0]))
            if current is None:
                return fail(f"no note {positionals[0]}")
            body = canonicalize_body(read_note_body(rest, initial=current.body))
            if not body:
                return fail("empty note, nothing written")
            view, changed = svc.edit_note(conn, note_id=current.id, body=body)
            conn.commit()
            if want_json:
                payload = view_payload(view)
                payload["changed"] = changed
                print(_json.dumps(payload))
            else:
                print(f"note {view.id}  revision {view.revision}"
                      + ("" if changed else "  (unchanged)"))
            return 0

        # ── rm / restore ─────────────────────────────────────────────────────
        if sub in {"rm", "restore"}:
            if not positionals:
                return fail(f"Usage: spindlebot note {sub} <id>")
            try:
                view = (svc.delete_note if sub == "rm" else svc.restore_note)(
                    conn, int(positionals[0])
                )
            except LookupError as e:
                return fail(str(e))
            conn.commit()
            if want_json:
                print(_json.dumps(view_payload(view)))
            else:
                action = "removed" if sub == "rm" else "restored"
                suffix = " (recoverable: note restore)" if sub == "rm" else ""
                print(f"{action} note {view.id}{suffix}")
            return 0

        # ── tag / untag ──────────────────────────────────────────────────────
        if sub in {"tag", "untag"}:
            if len(positionals) < 2:
                return fail(f"Usage: spindlebot note {sub} <id> <tag>...")
            note_id, tags = int(positionals[0]), positionals[1:]
            try:
                view = (
                    svc.tag_note(conn, note_id, tags) if sub == "tag"
                    else svc.untag_note(conn, note_id, tags[0])
                )
            except LookupError as e:
                return fail(str(e))
            conn.commit()
            if want_json:
                print(_json.dumps(view_payload(view)))
            else:
                print(f"note {view.id}  tags: {' '.join(view.tags) or '(none)'}")
            return 0

        # ── sessions ─────────────────────────────────────────────────────────
        if sub == "session":
            if not positionals or positionals[0] != "start":
                return fail("Usage: spindlebot note session start [--title <text>]")
            session = svc.start_session(conn, title=_note_opt(rest, "--title"))
            conn.commit()
            if want_json:
                print(_json.dumps({"id": session.id, "uuid": session.uuid,
                                   "title": session.title,
                                   "occurred_utc": session.occurred_utc}))
            else:
                print(f"session {session.id}"
                      + (f"  {session.title}" if session.title else ""))
            return 0

        since_raw = _note_opt(rest, "--since")
        try:
            since = _parse_since(since_raw) if since_raw else None
        except ValueError as e:
            return fail(str(e))
        sessions = svc.list_sessions(conn, since_utc=since)
        if want_json:
            print(_json.dumps({"count": len(sessions), "sessions": [
                {"id": s.session.id, "title": s.session.title,
                 "occurred_utc": s.session.occurred_utc, "notes": s.note_count}
                for s in sessions
            ]}))
            return 0
        if not sessions:
            print("no sessions")
            return 0
        from datetime import datetime, timezone
        for item in sessions:
            when = datetime.fromtimestamp(
                item.session.occurred_utc, tz=timezone.utc).strftime("%Y-%m-%d")
            title = item.session.title or "(untitled)"
            print(f"{item.session.id:>5}  {when}  {title}  — {item.note_count} note(s)")
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv)[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    # A SUBCOMMAND's --help must print help, never run the subcommand. Asking
    # `spindlebot sync --help` used to execute a real sync, because the flag was
    # passed through to a cmd_* that simply ignored it; on `delete` that
    # surprise is destructive. Checked before the config load for the same
    # reason the top-level check is: help must work on a broken config.
    if any(a in ("-h", "--help") for a in args[1:]):
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
            processing_dir=cfg.core.processing_dir,
            import_dir=cfg.core.import_dir,
            archive=cfg.core.archive_dir,
            duplicates_dir=cfg.core.duplicates_dir,
            pipeline_dir=cfg.pipeline_dir,
            log_file=cfg.core.log_dir / "watcher.log",
            spindlebot_cfg=cfg,
            auto_sync_on_import=cfg.core.auto_sync_on_import,
            retention_path=_retention_path(cfg.destinations),
            destination_name=_retention_name(cfg.destinations),
            sync_script=cfg.pipeline_dir / "music-sync.sh",
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

    if command == "finalize":
        return cmd_finalize(cfg, args[1:])

    if command == "inventory":
        return cmd_inventory(cfg, args[1:])

    if command == "review":
        return cmd_review(cfg, args[1:])

    if command == "sync":
        return cmd_sync(cfg, args[1:])

    if command == "prune":
        return cmd_prune(cfg, args[1:])

    if command == "delete":
        return cmd_delete(cfg, args[1:])

    if command == "collection-audit":
        return cmd_collection_audit(cfg, args[1:])

    if command == "collection-ignore":
        return cmd_collection_ignore(cfg, args[1:])

    if command == "note":
        return cmd_note(cfg, args[1:])

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
            "com.strangestlens.music-sync",
        ]
        uid = os.getuid()
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
