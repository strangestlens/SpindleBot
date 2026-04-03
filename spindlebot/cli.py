"""
SpindleBot CLI.

Usage:
    python -m spindlebot check                         Validate config and tool availability
    python -m spindlebot config shell                  Print config as shell-sourceable exports
    python -m spindlebot config get <key>              Print a single value (e.g. core.library_dir)
    python -m spindlebot import <log|dir> [--force]   Run import pipeline for a staged album
    python -m spindlebot import-staging [--dry-run]   Import everything currently in Staging
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
    check("library_dir exists",
          cfg.core.library_dir.exists(),
          f"mkdir -p '{cfg.core.library_dir}'")
    check("staging_dir exists",
          cfg.core.staging_dir.exists(),
          f"mkdir -p '{cfg.core.staging_dir}'")
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
        "SPINDLEBOT_LIBRARY_DIR":      str(cfg.core.library_dir),
        "SPINDLEBOT_STAGING_DIR":      str(cfg.core.staging_dir),
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
    """Print a single dotted config value, e.g. core.library_dir."""
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
    Scan the staging directory and dispatch each found album through the import
    pipeline sequentially.

    With --dry-run, prints what would be imported without actually running
    music-import.sh.  Use this to preview the dispatch list before committing.
    """
    import subprocess
    from spindlebot.staging import scan_staging

    dry_run = "--dry-run" in args
    staging_dir = cfg.core.staging_dir

    items = scan_staging(staging_dir)

    if not items:
        print(f"Nothing to import in {staging_dir}")
        return 0

    print(f"Found {len(items)} item(s) in {staging_dir}:")
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
        if len(args) < 2:
            print("Usage: spindlebot import <log-or-dir> [--force]", file=sys.stderr)
            return 1
        target = next(a for a in args[1:] if a != "--force")
        force = "--force" in args
        import subprocess
        cmd = [str(cfg.pipeline_dir / "music-import.sh"), target]
        if force:
            cmd.append("--force")
        return subprocess.call(cmd)

    if command == "import-staging":
        return cmd_import_staging(cfg, args[1:])

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
