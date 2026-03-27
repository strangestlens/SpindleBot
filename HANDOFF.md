# SpindleBot — Claude Code Handoff

This document captures everything needed to continue development of SpindleBot from
where Cowork left off. Read this before touching any code.

---

## What SpindleBot Is

An event-driven music pipeline for ripping, tagging, and managing a lossless FLAC
library on macOS. Two-phase architecture:

**Phase A — Import** (triggered by XLD finishing a CD rip):
```
XLD rips CD → .log file written to Staging → fswatch fires music-import.sh →
pretag → beet import → posttag (DB fixes) → fetch-lyrics → macOS + Telegram notify
```

**Phase B — Sync** (triggered by DwRugged external drive mounting):
```
launchd detects mount → music-sync-rugged.sh → fetch-art → posttag → rsync →
beets DB path reconciliation → fetch-lyrics (safety net) → notify
```

Key tools: `beets` (MusicBrainz matching, SQLite library DB), `XLD` (CD ripper),
`fswatch` (file watcher), `launchd` (macOS daemon), `lrc-editor` (Flask/WaveSurfer.js
lyrics timing editor).

Repo: `github.com/strangestlens/SpindleBot`
Local: `~/Music/music-pipeline`

---

## Current State (as of end of Cowork session, March 2026)

### Phase 1 (Configuration & Portability) — COMPLETE

Everything that was hardcoded is now config-driven. The full wiring:

1. **`setup.sh`** — run once; creates `~/.config/spindlebot/`, copies example files,
   generates `~/.config/spindlebot/bootstrap.sh` with Python path and pipeline dir
   baked in, installs `tomli` if Python < 3.11, runs `spindlebot check`.

2. **`~/.config/spindlebot/bootstrap.sh`** (generated) — sourced by every shell script:
   ```bash
   eval "$(PYTHONPATH="$PIPELINE_DIR" "$PYTHON" -m spindlebot config shell)"
   ```
   Exports all 15 `$SPINDLEBOT_*` env vars into the shell's environment.

3. **`spindlebot/` Python package**:
   - `config.py` — loads `config.toml` + `secrets.toml`, typed dataclasses, env var
     overrides, auto-detected `pipeline_dir`
   - `cli.py` — `check`, `config shell`, `config get` commands
   - `__main__.py` — entry point for `python -m spindlebot`

4. **Shell scripts** — all rewritten to source bootstrap.sh and use `$SPINDLEBOT_*`:
   - `music-import.sh` — full import pipeline
   - `music-sync-rugged.sh` — sync to DwRugged
   - `music-notify.sh` — macOS + Telegram notifications

5. **`music-fetch-lyrics.py`** — `REQUEST_DELAY` now reads `$SPINDLEBOT_LYRICS_DELAY`

6. **`config.yaml` / `beets-config.yaml`** — absolute paths replaced with `~/...`

7. **`config.toml.example`** / **`secrets.toml.example`** — templates for the user's
   `~/.config/spindlebot/` directory

8. **`requirements.txt`**, **`.gitignore`**, **`tests/test_config.py`** — added

### Validated

`python3 -m spindlebot check` passes all checks on Daniel's machine as of this session.

### NOT yet pushed to remote

The sandbox had no outbound TCP. Daniel needs to push manually:
```bash
cd ~/Music/music-pipeline
git add -A
git commit -m "Phase 1: configuration & portability"
git push origin main
```

---

## File Map

```
music-pipeline/
├── spindlebot/                  # NEW: Python package (Phase 1)
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py                # All config loading lives here
│   └── cli.py                   # check / config shell / config get
├── tests/                       # NEW
│   ├── __init__.py
│   └── test_config.py           # 17 unittest-compatible tests
├── music-import.sh              # UPDATED: sources bootstrap.sh
├── music-sync-rugged.sh         # UPDATED: sources bootstrap.sh
├── music-notify.sh              # UPDATED: credentials from env vars
├── music-fetch-lyrics.py        # UPDATED: REQUEST_DELAY from env var
├── music-pretag.py              # UNCHANGED
├── music-fetch-art.py           # UNCHANGED
├── lrc-editor.py                # UNCHANGED (1,315-line Flask/WaveSurfer app)
├── config.yaml                  # UPDATED: ~/... paths (live beets config)
├── beets-config.yaml            # UPDATED: ~/... paths (duplicate; can be removed)
├── config.toml.example          # NEW: template for ~/.config/spindlebot/
├── secrets.toml.example         # NEW: credentials template
├── setup.sh                     # NEW: one-time setup script
├── requirements.txt             # NEW: tomli>=2.0; python_version < "3.11"
├── .gitignore                   # UPDATED: __pycache__, *.pyc, config.toml, secrets.toml
├── ROADMAP.md                   # Development plan (6 phases)
└── HANDOFF.md                   # This file
```

---

## Env Vars Exported by `bootstrap.sh`

| Variable | Source |
|---|---|
| `SPINDLEBOT_LIBRARY_DIR` | `core.library_dir` |
| `SPINDLEBOT_STAGING_DIR` | `core.staging_dir` |
| `SPINDLEBOT_LOG_DIR` | `core.log_dir` |
| `SPINDLEBOT_ARCHIVE_DIR` | `core.archive_dir` |
| `SPINDLEBOT_BEET` | `tools.beet` |
| `SPINDLEBOT_PYTHON` | `tools.python` |
| `SPINDLEBOT_BEETS_DB` | `tools.beets_db` |
| `SPINDLEBOT_BEETS_CONFIG` | `tools.beets_config` |
| `SPINDLEBOT_PIPELINE_DIR` | auto-detected from `config.py` location |
| `SPINDLEBOT_DESTINATION_PATH` | first enabled destination's `path` |
| `SPINDLEBOT_TELEGRAM_TOKEN` | `secrets.telegram.bot_token` |
| `SPINDLEBOT_TELEGRAM_CHAT_ID` | `secrets.telegram.chat_id` |
| `SPINDLEBOT_LYRICS_DELAY` | `lyrics.request_delay_seconds` |
| `SPINDLEBOT_MACOS_NOTIFY` | `notifications.macos_notify` (0 or 1) |
| `SPINDLEBOT_TELEGRAM_ENABLED` | `notifications.telegram_enabled` (0 or 1) |

Env vars also override secrets: `SPINDLEBOT_TELEGRAM_TOKEN`, `SPINDLEBOT_TELEGRAM_CHAT_ID`,
`SPINDLEBOT_GENIUS_KEY`.

---

## Things That Still Need Doing

### Immediate / Small

- **`beets-config.yaml` is a duplicate** of `config.yaml`. The live beets config is
  `config.yaml`. `beets-config.yaml` can be deleted, or the names should be reconciled.
- **`music-fetch-lyrics.py` shebang** is `#!/opt/homebrew/bin/python3`. It's always
  called via `$PYTHON "$FETCH_LYRICS"` so it doesn't matter in practice, but `#!/usr/bin/env python3` would be cleaner.
- **Genius API key** is still hardcoded in `config.yaml` (beets reads it directly).
  Long-term it should move to `secrets.toml`, which requires either generating beets
  config from SpindleBot config or using a beets config `include:` trick.
- **`python3 -m pytest tests/`** — the sandbox couldn't install pytest. Tests are
  written in unittest style and will run with pytest fine on Daniel's Mac (Python 3.11).
  Verify: `cd ~/Music/music-pipeline && python3 -m pytest tests/ -v`

### Phase 2 — Modular Architecture & Testing (next major effort)

This is the biggest and most valuable phase. See `ROADMAP.md` for full detail.

The goal: replace the two monolithic shell scripts with a proper Python package of
discrete, testable stages. Target structure:

```
spindlebot/
├── config.py          # already done
├── cli.py             # already done (extend with start/stop/import/sync commands)
├── pipeline/
│   ├── runner.py      # stage orchestrator
│   └── stages/
│       ├── pretag.py        # absorb music-pretag.py
│       ├── beet_import.py   # subprocess wrapper around beet
│       ├── posttag.py       # DB fixes, disctotal patching
│       ├── fetch_art.py     # absorb music-fetch-art.py
│       ├── fetch_lyrics.py  # absorb music-fetch-lyrics.py
│       ├── sync.py          # rsync + DB reconciliation
│       └── notify.py        # absorb music-notify.sh
├── watchers/
│   ├── staging.py     # fswatch wrapper → triggers import pipeline
│   └── destination.py # mount event → triggers sync pipeline
├── lyrics/
│   ├── lrclib.py      # already partially done in music-fetch-lyrics.py
│   ├── shazam.py      # new: plain text fallback
│   └── aligner.py     # new: proportional timestamp estimation
└── destinations/
    ├── base.py
    ├── local_drive.py  # rsync
    └── rclone.py       # rclone to B2/S3/NAS
```

Shell scripts become thin wrappers:
```bash
# music-import.sh after Phase 2
source "$HOME/.config/spindlebot/bootstrap.sh"
exec "$SPINDLEBOT_PYTHON" -m spindlebot import "$1"
```

Key design decisions already made:
- Use `subprocess` (not shell) for beet and rsync calls — easier to test and capture
- Each stage takes a typed input and returns a typed result — no global state
- `runner.py` handles stage sequencing, logging, and failure capture
- Tests use `pytest` + `tmp_path` + `unittest.mock` for external calls

### Phase 3 — Destination Flexibility

Config already supports `[[destinations]]` arrays. Phase 2's `sync.py` should iterate
all enabled destinations rather than just the first one.

### Phase 4 — Error Recovery & Resilience

- `retry_with_backoff()` helper wrapping all external API calls
- `~/.config/spindlebot/failed.jsonl` failure journal
- `spindlebot retry-failed` command
- Pre-flight checks before import and sync

### Phase 5 — Input Sources

Digital download watcher (`~/Music/Downloads`). Runs same pipeline as CD rip but in
"gentle mode" (normalize rather than strip tags, since downloads usually have good
metadata).

### Phase 6 — Remote Library Access

Don't build. Deploy **Navidrome** (Go, single binary, Subsonic API, native `.lrc`
support, ~200 MB RAM for 40k tracks). Point it at the DwRugged library. Expose via
Tailscale or Caddy reverse proxy. Client: Symfonium on iOS.

---

## Known Quirks / Gotchas

- **Multi-disc detection**: The import script waits until all discs are present before
  importing. It checks `disctotal` tags in FLAC files. MusicBrainz sometimes reports
  `disctotal=2` for single-disc deluxe editions — the posttag step patches this by
  counting actual ripped disc numbers and updating the beets DB directly via sqlite3.

- **`multidisc` flex attribute**: beets caches inline values at import time. The
  `multidisc` inline computes `"1"` (truthy) or `""` (falsy for `%if{}`). Setting it
  via `beet modify multidisc=` *deletes* the row, making `$multidisc` render as the
  literal string `"$multidisc"` (truthy!) in templates. Fix: always INSERT the row
  directly via sqlite3. This is already done in music-import.sh.

- **beets `path:` query syntax**: Use `path:/full/path/` with trailing slash in beet
  queries to match items under a directory. Without trailing slash it may not match.

- **DwRugged path**: After rsync, the beets DB still has local paths. The sync script
  updates them with `sqlite3 UPDATE items SET path = replace(...)`. This must happen
  before fetching lyrics on DwRugged (which uses the DB to find tracks).

- **lrc-editor**: A self-contained 1,315-line Python/Flask app serving WaveSurfer.js.
  Not integrated with the pipeline config system yet. Runs standalone:
  `python3 lrc-editor.py`. Stores state in-memory; no DB.

- **Sandbox constraints (Cowork)**: The Cowork sandbox is a Linux ARM64 VM with no
  outbound TCP (pip/npm/git push all blocked). Claude Code proper should not have
  this limitation.

---

## Useful Commands

```bash
# Validate everything
python3 -m spindlebot check

# Emit all config as env vars (what bootstrap.sh evals)
python3 -m spindlebot config shell

# Get a single value
python3 -m spindlebot config get core.library_dir

# Run tests
python3 -m pytest tests/ -v

# Manually trigger import for a staging folder
./music-import.sh /path/to/staging/Album\ Dir/album.log

# Manually trigger sync
./music-sync-rugged.sh

# Fetch lyrics for an album
python3 music-fetch-lyrics.py ~/Music/Library/Artist/Album/

# Start lrc-editor
python3 lrc-editor.py
# then open http://localhost:5000
```

---

## Daniel's Setup

- macOS, Apple Silicon
- Python: `/opt/homebrew/bin/python3` (3.11+)
- beet: `/opt/homebrew/bin/beet`
- mpv: `/opt/homebrew/bin/mpv`
- Music library: `~/Music/Library` (local) → `/Volumes/DwRugged/Music/Library` (external)
- Staging: `~/Music/Staging`
- beets DB: `~/.config/beets/library.db`
- Config: `~/.config/spindlebot/config.toml` + `secrets.toml`
- Telegram notifications: configured and working
- Genius API: configured in `config.yaml` (beets plugin) and `secrets.toml`
