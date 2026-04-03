# SpindleBot Music Pipeline

## What this is

SpindleBot is an event-driven pipeline for ripping, tagging, and managing a lossless music library on macOS. It handles two primary flows:

**Import:** XLD rips a CD → writes a `.log` to Staging → fswatch triggers `music-import.sh` → pretag → `beet import` → posttag → fetch-lyrics → notify

**Sync:** launchd detects DwRugged mount → `music-sync-rugged.sh` → fetch-art → posttag → rsync → beets DB path reconciliation → fetch-lyrics → notify

## Phase status (April 2026)

| Phase | Status | Notes |
|-------|--------|-------|
| 1 — Config & Portability | Complete | `config.toml`, `bootstrap.sh`, `spindlebot check`, all scripts use `$SPINDLEBOT_*` env vars |
| 2 — Modular Architecture | In progress | `disc.py` and `staging.py` extracted and tested. Core pipeline logic still in shell. Runner + stages structure is the next major milestone. |
| 3–6 | Not started | |

Partial Phase 5 groundwork exists: `scan_staging()`, directory import mode, and the `import-staging` CLI command are already in place.

**What "Phase 2 complete" means:** Shell scripts become one-liners (`exec "$PYTHON" -m spindlebot import "$1"`). All logic lives in `spindlebot/pipeline/stages/` as discrete, testable Python modules. `runner.py` orchestrates stage sequencing. Each stage takes typed input, returns typed result, no global state. Tests use pytest + `tmp_path` + `unittest.mock`.

## Current file map

```
spindlebot/config.py         — config loading, typed dataclasses, env var overrides
spindlebot/cli.py            — check / config shell / config get / import / import-staging
spindlebot/disc.py           — AUDIO_EXTENSIONS, find_audio_files(), check_wait(), count_discs()
spindlebot/staging.py        — scan_staging() → list[StagingItem]
music-pretag.py              — pretag() + posttag(), format-agnostic, Bandcamp encoding fix
music-import.sh              — main import pipeline (accepts .log or directory), --force flag
music-sync-rugged.sh         — sync to DwRugged
music-notify.sh              — macOS + Telegram notifications
music-fetch-lyrics.py        — lrclib lyrics fetcher
music-fetch-art.py           — album art fetcher
lrc-editor/                  — standalone Flask/WaveSurfer.js lyrics timing editor
```

## Testing

Tests are the contract, not the implementation. A failing test after a code change means: investigate whether the behavior broke first. Only change a test if the intended behavior intentionally changed — and that should be an explicit decision, not a response to friction. Never silently update tests to make CI green.

CI runs on every push/PR:
- `python` job: `pytest tests/ --ignore=tests/shell`
- `shell` job: shellcheck + bats `tests/shell/`

Both must pass. shellcheck must be clean — no suppressions without a comment explaining why.

## Known gotchas

**1. multidisc flex attribute**
`beet modify multidisc=` **deletes** the DB row, causing `$multidisc` to render as the literal string `"$multidisc"` (truthy) in templates. Always INSERT via `sqlite3` directly. This is already handled in `music-import.sh` — don't change it.

**2. disctotal from MusicBrainz**
Can report `disctotal=2` for single-disc albums (DualDiscs, conceptual A/B sides). The import script patches this post-import based on actual disc numbers ripped, not MusicBrainz metadata.

**3. beets `path:` query syntax**
Always use a trailing slash: `path:/full/path/` — without it, matches may be missed.

**4. DwRugged path reconciliation**
After rsync, the beets DB still has local paths. The sync script updates them via `sqlite3 UPDATE`. This must happen before any lyrics fetch on DwRugged.

**5. bootstrap.sh sourcing**
Every shell script sources `~/.config/spindlebot/bootstrap.sh`, which evals `python -m spindlebot config shell`. If Python or the config fails, all `$SPINDLEBOT_*` vars will be empty. Scripts should fail loudly, not silently.

**6. PYTHONPATH in shell scripts**
When calling spindlebot modules from shell scripts, always prefix with `PYTHONPATH="$SPINDLEBOT_PIPELINE_DIR"`. See `music-import.sh` for the pattern.

## Code quality non-negotiables

- shellcheck clean on all `.sh` files before commit. Use `# shellcheck disable=SC####` with a comment explaining why — no blanket suppressions.
- ruff for Python linting (configured in `requirements.txt`).
- No print-driven side effects in library code. `print()` only in CLI entry points and stage logging.
- beets template vars (`$albumartist`, `$path`, etc.) must be in single quotes in shell — use `# shellcheck disable=SC2016` with the comment `"beet template var, not a bash var"`.
- All new Python modules go in `spindlebot/` and must have corresponding tests in `tests/`.

## Design decisions

- Use `subprocess` (not shell) for `beet` and `rsync` calls in Python — easier to test and capture output.
- Each pipeline stage: typed input → typed result, no global state.
- `mutagen` for all tag reading/writing — never shell out to `metaflac` or `id3v2`.
- `AUDIO_EXTENSIONS` in `spindlebot/disc.py` is the single source of truth for supported formats.
- Bandcamp `COMMENT` tags (`"Visit https://...bandcamp.com"`) are preserved — do not strip them.

## Future constraints to keep in mind

**DAP/multi-device library tracking:** Daniel has DAPs (digital audio players) each with their own Library folders. The current model ("library = whatever is at known paths") will eventually need a device registry: which copies exist, on which device, at what sync state. This is orthogonal to beets' per-track metadata. When building Phase 3 destination flexibility, destinations should be first-class named entities with sync state — not just rsync targets.

**The beets DB is not the long-term source of truth.** It knows about local paths. A future SpindleBot DB would track copies across devices.

## Environment

- macOS, Apple Silicon
- Python: `/opt/homebrew/bin/python3` (3.11+, currently resolves to 3.14 — tests pass on both)
- beet: `/opt/homebrew/bin/beet`
- Config: `~/.config/spindlebot/config.toml` + `secrets.toml`
- Library: `~/Music/Library` → `/Volumes/DwRugged/Music/Library`
- Staging: `~/Music/Staging`
- beets DB: `~/.config/beets/library.db`

## Setup and useful commands

```bash
cd ~/Music/music-pipeline
./setup.sh                                               # first time only
python3 -m spindlebot check                              # validate environment
python3 -m pytest tests/ -v                              # run test suite
bats tests/shell/                                        # run shell tests

python3 -m spindlebot import-staging --dry-run           # preview what's in staging
python3 -m spindlebot import ~/Music/Staging/Album/      # import a specific directory
python3 -m spindlebot import ~/Music/Staging/Album.log --force   # skip disc check
```
