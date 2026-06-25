# SpindleBot Music Pipeline

## What this is

SpindleBot is an event-driven pipeline for ripping, tagging, and managing a lossless music library on macOS. It handles two primary flows:

**Import:** XLD rips a CD → writes a `.log` to the Import area → fswatch triggers `music-watcher.sh` → `spindlebot import` → pretag → `beet import` → multidisc fix → beet move → posttag → fetch-art → fetch-lyrics → archive log → notify

Also triggered automatically when a directory is dropped into the Import area (e.g. Amazon download).

> **Working areas (renamed Apr 2026, Phase A):** "Staging" → **Import** (active import) and "Library" → **Pending** (processed albums awaiting distribution), both relocated under `~/Library/Application Support/SpindleBot/`. Config keys are `core.import_dir` / `core.pending_dir` (legacy `staging_dir`/`library_dir` still honored); env vars are `SPINDLEBOT_IMPORT_DIR` / `SPINDLEBOT_PENDING_DIR`.

**Sync:** launchd detects DwRugged mount → `music-sync-rugged.sh` → fetch-art → posttag → rsync → beets DB path reconciliation → fetch-lyrics → notify

## Phase status (April 2026)

| Phase | Status | Notes |
|-------|--------|-------|
| 1 — Config & Portability | Complete | `config.toml`, `bootstrap.sh`, `spindlebot check`, all scripts use `$SPINDLEBOT_*` env vars |
| 2 — Modular Architecture | Complete | All import pipeline logic in Python (`runner.py` + `stages/`). Shell scripts are one-liner shims. Full test coverage. |
| 3 — Sync pipeline in Python | Not started | `music-sync-rugged.sh` still shell; next target for Pythonification |
| 4–6 | Not started | |

> The table above is the **original roadmap**. It is superseded by the active epic below, which uses its own phase labels (**A, 0, 1, 2, …**). Don't conflate the two numbering schemes.

## Content-addressed library refactor (ACTIVE epic)

Replacing "library = whatever is at known paths" with a **SpindleBot-owned SQLite DB** (`~/.config/spindlebot/spindlebot.db`) that is the system of record for content **identity**, every **location** a file lives, and (later) version history. Long-form design + locked decisions: `~/.claude/plans/immutable-shimmying-blanket.md` (user-local; not in the repo).

**Status:** Phase A (working-dir rename, #26), Phase 0 (DB foundation + `inventory`, #27), Phase 1 (first-class locations + generalized inventory + enums, #28) — all merged. **Next: `feat/sidecars`** (album table + `sidecar_content`/`sidecar_presence`; `.lrc` stem-paired to its track, `cover.jpg`/`.nolrc` album-level). Later: 2 reconciler + pre-sync review, 3 sync executor (replaces `rsync --remove-source-files` with copy→verify→prune), 4 lyrics bidirectional sync, 5 plugins + AI re-timer, 6 DB snapshot, 7 daemon.

**Layering (keep consistent):**
- `spindlebot/core/` — pure, no DB/IO side effects, no `print`: `identity.py` (hashing), `enums.py`, `models.py` (frozen dataclasses + `from_row`), `errors.py`.
- `spindlebot/db/` — `connection.py` (`open_db` = WAL + `foreign_keys=ON` + migrate); `schema.sql`=v1, `schema_v2.sql`=v2; `migrations.py` (append-only `[(version, file)]`, forward-only, `user_version`-keyed). `repositories/` is the **only** layer that issues SQL; the caller owns the transaction (repos don't commit).
- `spindlebot/services/` — orchestration over repos+core; side-effect-free re `print`; returns typed results. `inventory.py`, `locations.py` (registry + deterministic `location_uuid`), `volumes.py` (marker files).
- `cli.py` — thin client; `print` only here; every command supports `--json`.

**Conventions:**
- **Identity** = decoded-audio MD5 (FLAC STREAMINFO `md5_signature`), fallback to whole-file sha256, recorded via `IdentityKind`. File sha256 is per-copy *integrity*, never identity.
- **Closed sets are `StrEnum`s** (`LocationKind`, `IdentityKind`, `ScanStatus`) — stored as TEXT, validated on read+write, fail loud on unknown. No bare string literals for these.
- **Schema is minimal per phase** — add new tables in a *new* migration version; never edit a shipped schema file. v2 tables: `location`, `audio_content`, `audio_presence`, `location_scan`. No album/sidecar tables yet.
- **Locations are first-class**, identified by a marker file `.spindlebot-location-<uuid>` at `root_path` (a path — may be a *subfolder* of a shared volume, not a whole volume). A *missing* marker is never treated as a wiped drive; a *foreign* marker refuses resolution.
- **beets overlay**: `audio_content.beets_item_id` linked by path during inventory (read-only); advisory, nullable, never depended on.
- **Tests**: use the controllable-STREAMINFO-md5 fake-FLAC fixture (`_write_flac` in `tests/test_identity.py` / `tests/test_inventory.py`). Tests are the contract.

**Workflow:** one feature branch per phase off latest `main`; sub-tasks are ordered commits on that branch (each green); push + open a PR only when asked; PRs squash-merge to `main`; `git pull` main before the next branch.

## Current file map

```
spindlebot/
  cli.py                         — CLI entry point: check / config / import / import-staging /
                                     fetch-lyrics / fetch-art / notify / restart
  config.py                      — typed config dataclasses, loads config.toml + secrets.toml,
                                     env var overrides
  disc.py                        — AUDIO_EXTENSIONS, find_audio_files(), check_wait(),
                                     count_discs()
  staging.py                     — scan_staging() → list[StagingItem]
  pipeline/
    runner.py                    — ImportRunner: orchestrates all 10 import stages, echo
                                     callback for live terminal output
    stages/
      pretag.py                  — pretag() + posttag(): normalize tags pre/post beet import,
                                     Bandcamp encoding fix
      notify.py                  — notify(): macOS + Telegram notifications
      fetch_lyrics.py            — fetch_lyrics(): lrclib .lrc sidecar fetching
      fetch_art.py               — fetch_art(): embed album art (CAA → iTunes fallback),
                                     writes cover.jpg sidecar
  core/                          — pure, side-effect-free (see ACTIVE epic section)
    identity.py                  — audio_md5 / file_sha256 / audio_content_id (ContentId)
    enums.py                     — LocationKind / IdentityKind / ScanStatus (StrEnum)
    models.py                    — frozen Location / AudioContent / AudioPresence (+ from_row)
    errors.py                    — SpindleBotError, MarkerMismatch, UnknownLocation
  db/                            — SpindleBot's own SQLite system-of-record
    connection.py                — open_db(): WAL, foreign_keys, migrate
    schema.sql / schema_v2.sql   — versioned DDL; migrations.py applies by user_version
    repositories/                — ONLY SQL layer: audio_repo, location_repo,
                                     presence_repo, scan_repo
  services/                      — orchestration over repos+core (no print side effects)
    inventory.py                 — scan a location → upsert content + presence (read-only re: audio)
    locations.py                 — register_from_config; deterministic location_uuid
    volumes.py                   — marker files (.spindlebot-location-<uuid>), resolve_root

music-watcher.sh                 — fswatch daemon: fires spindlebot import on .log or dir drop
                                     installed to ~/.local/bin/ by setup.sh
music-import.sh                  — shim: sources bootstrap.sh, exec's spindlebot import "$@"
music-sync-rugged.sh             — sync to DwRugged (still shell, Phase 3 target)
music-notify.sh                  — legacy notify shim (superseded by stages/notify.py)
music-pretag.py                  — legacy root-level script (superseded by stages/pretag.py)
music-fetch-lyrics.py            — legacy root-level script (superseded by stages/fetch_lyrics.py)
music-fetch-art.py               — legacy root-level script (superseded by stages/fetch_art.py)
setup.sh                         — first-time environment setup: config files, bootstrap.sh,
                                     music-watcher.sh → ~/.local/bin/, plists → ~/Library/LaunchAgents/
com.strangestlens.music-watcher.plist       — launchd agent for the fswatch watcher daemon
com.strangestlens.music-sync-rugged.plist   — launchd agent for DwRugged sync
lrc-editor/                      — standalone Flask/WaveSurfer.js lyrics timing editor

tests/
  test_config.py
  test_disc_check.py
  test_fetch_art.py
  test_fetch_lyrics.py
  test_notify.py
  test_pretag.py
  test_runner.py
  test_staging.py
  test_identity.py               — core/identity (controllable-md5 fake-FLAC fixture)
  test_db.py                     — connection + schema migrations
  test_repositories.py           — audio/location/presence repos
  test_locations.py              — location registry + LocationKind
  test_volumes.py                — marker files + root resolution
  test_inventory.py              — inventory service (+ beets linkage, scan status)
  shell/                         — bats shell tests (shellcheck + integration)
```

## ImportRunner stage sequence

`spindlebot/pipeline/runner.py` — `ImportRunner.run()`:

1. **trigger validation** — directory → `album_dir = trigger`; `.log` → `album_dir = trigger.parent`; other → logged and skipped
2. **double-fire guard** — `.log` mode only: already-archived log → clean exit
3. **disc check** — `check_wait()`: wait if multi-disc set incomplete (bypass with `--force`)
4. **pretag** — normalize tags before beet sees them
5. **beet import** — streams live to terminal when echo callback set
6. **multidisc fix** — patch `disctotal` + `multidisc` flex attr in beets DB via `sqlite3`
7. **beet move** — relocate to canonical library paths
8. **posttag** — strip beet alias tags, truncate DATE to year
9. **fetch-art + fetch-lyrics** — embed art, write `.lrc` sidecars
10. **archive** — move XLD `.log` to archive dir

`ImportRunner.__init__` accepts an optional `echo: Callable[[str], None]` for live terminal
feedback. `_log(msg, *, echo=True)` always writes to the log file; routes to echo only for
user-facing milestones (emoji-prefixed). Verbose/internal messages pass `echo=False`.

## Testing

Tests are the contract, not the implementation. A failing test after a code change means:
investigate whether the behavior broke first. Only change a test if the intended behavior
intentionally changed — and that should be an explicit decision, not a response to friction.
Never silently update tests to make CI green.

CI runs on every push/PR:
- `python` job: `pytest tests/ --ignore=tests/shell`
- `shell` job: shellcheck + bats `tests/shell/`

Both must pass. shellcheck must be clean — no suppressions without a comment explaining why.

## Known gotchas

**1. multidisc flex attribute**
`beet modify multidisc=` **deletes** the DB row, causing `$multidisc` to render as the literal
string `"$multidisc"` (truthy) in templates. Always INSERT via `sqlite3` directly. This is
handled in `runner.py` `_fix_multidisc()` — don't change the approach.

**2. disctotal from MusicBrainz**
Can report `disctotal=2` for single-disc albums (DualDiscs, conceptual A/B sides). The runner
patches this post-import based on actual disc count, not MusicBrainz metadata.

**3. beets `path:` query syntax**
Always use a trailing slash: `path:/full/path/` — without it, matches may be missed.

**4. DwRugged path reconciliation**
After rsync, the beets DB still has local paths. The sync script updates them via `sqlite3
UPDATE`. This must happen before any lyrics fetch on DwRugged.

**5. bootstrap.sh sourcing**
Every shell script sources `~/.config/spindlebot/bootstrap.sh`, which evals
`python -m spindlebot config shell`. If Python or the config fails, all `$SPINDLEBOT_*` vars
will be empty. Scripts should fail loudly, not silently.

**6. PYTHONPATH in shell scripts**
When calling spindlebot modules from shell scripts, always `export PYTHONPATH="$SPINDLEBOT_PIPELINE_DIR"` on a separate line before `exec`. Using `exec VAR=val cmd` syntax doesn't work — the assignment gets prepended to the binary path.

**7. `SPINDLEBOT_IMPORT_DIR` not `SPINDLEBOT_IMPORT`**
The bootstrap env var for the import area is `SPINDLEBOT_IMPORT_DIR` (and the Pending area is `SPINDLEBOT_PENDING_DIR`). Using a name without the `_DIR` suffix silently resolves to empty — fswatch will then watch the wrong directory (the cwd at daemon launch) with no error. `music-watcher.sh` guards against this at startup with an explicit empty-check.

**8. fetch_art test fixtures**
Tests that need controlled art-fetching behaviour must include `musicbrainz_albumid` in the
FLAC fixture tags. Without it, `_fetch_from_caa` is skipped entirely (no MBID → `if mbid:`
not entered), so `_fetch_from_itunes` runs for real against the network.

## Code quality non-negotiables

- shellcheck clean on all `.sh` files before commit. Use `# shellcheck disable=SC####` with a comment explaining why — no blanket suppressions.
- ruff for Python linting (configured in `requirements.txt`).
- No print-driven side effects in library code. `print()` only in CLI entry points.
- beets template vars (`$albumartist`, `$path`, etc.) must be in single quotes in shell — use `# shellcheck disable=SC2016` with the comment `"beet template var, not a bash var"`.
- All new Python modules go in `spindlebot/` and must have corresponding tests in `tests/`.

## Design decisions

- Use `subprocess` (not shell) for `beet` and `rsync` calls in Python — easier to test and capture output.
- Each pipeline stage: typed input → typed result, no global state.
- `mutagen` for all tag reading/writing — never shell out to `metaflac` or `id3v2`.
- `AUDIO_EXTENSIONS` in `spindlebot/disc.py` is the single source of truth for supported formats.
- Bandcamp `COMMENT` tags (`"Visit https://...bandcamp.com"`) are preserved — do not strip them.

## Future constraints to keep in mind

**DAP/multi-device library tracking:** Daniel has DAPs (digital audio players) each with their
own Library folders. The current model ("library = whatever is at known paths") will eventually
need a device registry: which copies exist, on which device, at what sync state. This is
orthogonal to beets' per-track metadata. When building Phase 3 destination flexibility,
destinations should be first-class named entities with sync state — not just rsync targets.

**The beets DB is not the long-term source of truth.** It knows about local paths. A future
SpindleBot DB would track copies across devices.

## Environment

- macOS, Apple Silicon
- Python: `/opt/homebrew/bin/python3` (3.11+, currently resolves to 3.14 — tests pass on both)
- beet: `/opt/homebrew/bin/beet`
- Config: `~/.config/spindlebot/config.toml` + `secrets.toml`
- Import area: `~/Library/Application Support/SpindleBot/Import` (active import; formerly `~/Music/Staging`)
- Pending area: `~/Library/Application Support/SpindleBot/Pending` → `/Volumes/DwRugged/Music/Library` (processed albums awaiting distribution; formerly `~/Music/Library`)
- beets DB: `~/.config/beets/library.db`
- launchd agents: `com.strangestlens.music-watcher`, `com.strangestlens.music-sync-rugged`

## Setup and useful commands

```bash
cd ~/Music/music-pipeline
./setup.sh                                                   # first time (and after moving pipeline dir)
                                                             # installs music-watcher.sh + plists
python3 -m spindlebot check                                  # validate environment
python3 -m pytest tests/ -v                                  # run test suite
bats tests/shell/                                            # run shell tests

python3 -m spindlebot import-staging --dry-run               # preview what's in the Import area
python3 -m spindlebot import "~/Library/Application Support/SpindleBot/Import/Album/"        # import a specific directory
python3 -m spindlebot import "~/Library/Application Support/SpindleBot/Import/Album.log" --force   # skip disc check
python3 -m spindlebot fetch-art <album_dir> [--dry-run] [--force]
python3 -m spindlebot fetch-lyrics <album_dir> [--dry-run] [--force]
python3 -m spindlebot restart                                # restart launchd agents
```
