# SpindleBot Music Pipeline

## What this is

SpindleBot is an event-driven pipeline for ripping, tagging, and managing a lossless music library on macOS. It handles two primary flows:

**Import:** XLD rips a CD → writes a `.log` to the Import area → fswatch triggers `music-watcher.sh` → `spindlebot import` → pretag → `beet import` → multidisc fix → **beet move → Processing** → posttag → fetch-art → fetch-lyrics → **per-album promote to Pending (only if lyric-complete)** → archive log → notify. An album lands in Pending only once every track has a terminal `.lrc`/`.nolrc` marker, so Pending is complete-by-construction and sync/prune can trust it. Albums left in Processing (transient lyric errors) are caught up by `spindlebot finalize`.

Also triggered automatically when a directory is dropped into the Import area (e.g. Amazon download).

> **Working areas (renamed Apr 2026, Phase A):** "Staging" → **Import** (active import) and "Library" → **Pending** (processed albums awaiting distribution), both relocated under `~/Library/Application Support/SpindleBot/`. Config keys are `core.import_dir` / `core.pending_dir` (legacy `staging_dir`/`library_dir` still honored); env vars are `SPINDLEBOT_IMPORT_DIR` / `SPINDLEBOT_PENDING_DIR`.
> **Processing area (added Jul 2026, Option C):** a third area **Processing** between Import and Pending holds in-flight albums while art/lyrics are fetched; an album is promoted to Pending only once `album_lyrics_complete()` holds. This eliminates the fetch-lyrics window in which a mount-sync could prune audio out of Pending mid-fetch and strand late lyric sidecars. Config key `core.processing_dir` (default `~/Library/Application Support/SpindleBot/Processing`); env var `SPINDLEBOT_PROCESSING_DIR`. The promote/finalize orchestration lives in `services/promote.py`; `spindlebot finalize` re-fetches lyrics and promotes anything still stuck.

**Sync:** launchd detects the retention-drive mount (WatchPaths, generated from the first enabled local_drive `[[destinations]]`) → `music-sync.sh` → inventory → review + acknowledge → sync (copy→verify→record presence) → prune (release Pending copies verified on retention) → beets DB path reconciliation → notify

## Phase status (April 2026)

| Phase | Status | Notes |
|-------|--------|-------|
| 1 — Config & Portability | Complete | `config.toml`, `bootstrap.sh`, `spindlebot check`, all scripts use `$SPINDLEBOT_*` env vars |
| 2 — Modular Architecture | Complete | All import pipeline logic in Python (`runner.py` + `stages/`). Shell scripts are one-liner shims. Full test coverage. |
| 3 — Sync pipeline in Python | Not started | `music-sync.sh` still shell; next target for Pythonification |
| 4–6 | Not started | |

> The table above is the **original roadmap**. It is superseded by the active epic below, which uses its own phase labels (**A, 0, 1, 2, …**). Don't conflate the two numbering schemes.

## Content-addressed library refactor (ACTIVE epic)

Replacing "library = whatever is at known paths" with a **SpindleBot-owned SQLite DB** (`~/.config/spindlebot/spindlebot.db`) that is the system of record for content **identity**, every **location** a file lives, and (later) version history. Long-form design + locked decisions: `~/.claude/plans/immutable-shimmying-blanket.md` (user-local; not in the repo).

**Status:** Phase A (#26), Phase 0 (#27), Phase 1 (#28), Phase sidecars (#30), Phase 2 (reconciler + `review` + vclock/lyric/conflict substrate, #31) — all merged. **Phase 3 (destructive sync) merged:** `services/sync.py` `execute_pending` runs acknowledged `pending_action` COPY rows via copy→verify-dest-sha256→write-presence (merged #35, non-destructive) incl. sidecars (#36); `prune_released` + `spindlebot prune` release the authoring copy once verified on retention (dry-run default). **Two distinct destructive ops, different gates:** (a) **PRUNE** (release the non-retention authoring/Pending copy) fires at the FIRST retention copy — Daniel's chosen model; releasing a non-retention copy never lowers the retention count, so `min_copies` is only a *warning* (`below_floor`), not a gate. (b) **DELETE** (remove a *retention* copy) IS gated: never drop retention copies below `min_copies` — built via `services/sync.py` + `spindlebot delete` (#39, dry-run default). The **LIVE CUTOVER has landed** (#38): `music-sync.sh` is now the content-addressed mount-sync (inventory→review→sync→prune; no `rsync --remove-source-files`). Active line of work is **Phase 4 (lyrics bidirectional sync)**: **4.0 the causal-lineage substrate** (#48) — schema v7 `lyric_version_presence` + `services/lyrics_sync.py`; the reconciler infers linear-edit vs concurrent-conflict vs behind from scan history instead of stamping every observed `.lrc` with a naive single-location vclock. No bytes moved yet — **4.1** is auto-propagation of clean wins + conflict-file preservation, **4.2** the `conflicts list|resolve` CLI. Later: 5 plugins + AI re-timer, 6 DB snapshot, 7 daemon.

**Layering (keep consistent):**
- `spindlebot/core/` — pure, no DB/IO side effects, no `print`: `identity.py` (hashing), `enums.py`, `models.py` (frozen dataclasses + `from_row`), `errors.py`.
- `spindlebot/db/` — `connection.py` (`open_db` = WAL + `foreign_keys=ON` + migrate); `schema.sql`=v1 … `schema_v7.sql`=v7; `migrations.py` (append-only `[(version, file)]`, forward-only, `user_version`-keyed). `repositories/` is the **only** layer that issues SQL; the caller owns the transaction (repos don't commit).
- `spindlebot/services/` — orchestration over repos+core; side-effect-free re `print`; returns typed results. `inventory.py`, `locations.py` (registry + deterministic `location_uuid`), `volumes.py` (marker files), `reconciler.py` (planner: diff DB vs observed → `pending_action`; never touches bytes/FS), `promote.py` (promote a lyric-complete album out of Processing into Pending via a beets-native `beet move path:<dir>/`; `finalize_processing` sweeps the whole area), `lyrics_sync.py` (per-doc lyric lineage: fold each location's observed `.lrc` sha into current/behind/concurrent against the head; pure DB reasoning, no bytes).
- `cli.py` — thin client; `print` only here; every command supports `--json`.

**Conventions:**
- **Identity** = decoded-audio MD5 (FLAC STREAMINFO `md5_signature`), fallback to whole-file sha256, recorded via `IdentityKind`. File sha256 is per-copy *integrity*, never identity.
- **Closed sets are `StrEnum`s** (`LocationKind`, `IdentityKind`, `ScanStatus`, `SidecarRole`, `SidecarParentKind`, `RunKind`, `ActionKind`, `ContentKind`, `ConflictStatus`) — stored as TEXT, validated on read+write, fail loud on unknown. No bare string literals for these.
- **Schema is minimal per phase** — add new tables in a *new* migration version; never edit a shipped schema file. Current `user_version` = **7**. v1: `location`/`audio_content`/`audio_presence`; v2: `location.root_path` + `location_scan`; v3: `album`/`album_track`/`sidecar_content`/`sidecar_presence`; v4: `run`/`pending_action`; v5: `lyric_doc`/`lyric_version`/`conflict`; v6: `mtime` on both presence tables + `(location_id, rel_path)` indexes (incremental rescan); v7: `lyric_version_presence` (per-`(doc, location)` version each location holds — the causal memory Phase 4.0 lineage needs). Polymorphic ids (`sidecar_content.parent_id`, `pending_action.content_id`) carry **no FK** by design — deleters must clean up explicitly.
- **Locations are first-class**, identified by a marker file `.spindlebot-location-<uuid>` at `root_path` (a path — may be a *subfolder* of a shared volume, not a whole volume). A *missing* marker is never treated as a wiped drive; a *foreign* marker refuses resolution.
- **beets overlay**: `audio_content.beets_item_id` linked by path during inventory (read-only); advisory, nullable, never depended on.
- **Tests**: use the controllable-STREAMINFO-md5 fake-FLAC fixture (`_write_flac` in `tests/test_identity.py` / `tests/test_inventory.py`). Tests are the contract.

**Workflow:** one feature branch per phase off latest `main`; sub-tasks are ordered commits on that branch (each green); push + open a PR only when asked; PRs squash-merge to `main`; `git pull` main before the next branch.

## Current file map

```
spindlebot/
  cli.py                         — CLI entry point: check / config / import / import-staging /
                                     inventory / review / finalize / collection-audit /
                                     fetch-lyrics / fetch-art / notify / restart
  config.py                      — typed config dataclasses, loads config.toml + secrets.toml,
                                     env var overrides
  disc.py                        — AUDIO_EXTENSIONS, find_audio_files(), check_wait(),
                                     count_discs()
  staging.py                     — scan_staging() → list[StagingItem]
  pipeline/
    runner.py                    — ImportRunner: orchestrates all import stages (incl. move→Processing
                                     + per-album promote to Pending), echo
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
    albums.py                    — album_key(): deterministic album grouping (mb_albumid → albumartist+album)
    vclock.py                    — pure version vectors: dominates / concurrent / merge / bump / json
    collection.py                — CollectionItem (the common reduction every external
                                     collection source adapts to) + LibraryAlbum + resolve_media
    collection_match.py          — ARTIST-SCOPED matcher: normalize → artist candidates →
                                     exact/containment/score → owned|uncertain|missing
    enums.py                     — LocationKind / IdentityKind / ScanStatus / SidecarRole /
                                     SidecarParentKind / RunKind / ActionKind / ContentKind /
                                     ConflictStatus / MediaKind
    models.py                    — frozen row dataclasses (+ from_row): Location, AudioContent,
                                     AudioPresence, Album, SidecarContent, SidecarPresence, Run,
                                     PendingAction, LyricDoc, LyricVersion, LyricVersionPresence,
                                     Conflict
    errors.py                    — SpindleBotError, MarkerMismatch, UnknownLocation
  db/                            — SpindleBot's own SQLite system-of-record
    connection.py                — open_db(): WAL, foreign_keys, migrate
    schema.sql … schema_v7.sql   — versioned DDL (user_version 1–7); migrations.py applies in order
    repositories/                — ONLY SQL layer: audio_repo, location_repo, presence_repo,
                                     scan_repo, album_repo, sidecar_repo, sidecar_presence_repo,
                                     run_repo, action_repo, lyric_repo, lyric_version_presence_repo,
                                     conflict_repo
  collections/                   — OPTIONAL external collection sources (assistive; nothing in
                                     the import/sync path depends on this). Each provider splits
                                     into an impure client + a PURE transformer, so every
                                     source quirk is testable against a recorded fixture.
    base.py                      — CollectionProvider Protocol + name→factory registry
    discogs.py                   — DiscogsClient (paging/throttle/cache) + to_items() (pure)
    fixture.py                   — hand-written JSON collection; test double AND the
                                     supported way in for anyone not on Discogs
  services/                      — orchestration over repos+core (no print side effects)
    inventory.py                 — scan a location → upsert content + albums + sidecars + presence
    library_index.py             — the library as a LibraryIndex; `auto` (DEFAULT) unions
                                     beets + the SpindleBot album table, because NEITHER is
                                     a superset (measured: 67 albums db-only, 2 beets-only).
                                     Refuses to answer from an empty index.
    collection_audit.py          — provider → media filter → match → AuditReport buckets
    collection_report.py         — render_html(): AuditReport → a self-contained HTML page
                                     (inline CSS/JS, lrc-editor palette). Pure: returns a
                                     string, the CLI writes the file
    locations.py                 — register_from_config; deterministic location_uuid
    volumes.py                   — marker files (.spindlebot-location-<uuid>), resolve_root
    reconciler.py                — planner: diff DB vs observed → pending_action (copy/missing/
                                     conflict); min_copies floor; requires a target scan; no bytes
    promote.py                   — promote_album() moves a lyric-complete album out of Processing
                                     into Pending (beet move path:<dir>/); finalize_processing()
                                     sweeps the whole Processing area (retry lyrics + promote)
    lyrics_sync.py               — reconcile_doc(): per-doc lyric causal lineage — classify each
                                     location's observed .lrc as current/behind/concurrent vs the
                                     head via core.vclock + scan history; no bytes, no files

music-watcher.sh                 — fswatch daemon: fires spindlebot import on .log or dir drop
                                     installed to ~/.local/bin/ by setup.sh
music-import.sh                  — shim: sources bootstrap.sh, exec's spindlebot import "$@"
music-sync.sh                    — content-addressed sync to the retention drive (first enabled local_drive dest)
music-notify.sh                  — legacy notify shim (superseded by stages/notify.py)
music-pretag.py                  — legacy root-level script (superseded by stages/pretag.py)
music-fetch-lyrics.py            — legacy root-level script (superseded by stages/fetch_lyrics.py)
music-fetch-art.py               — legacy root-level script (superseded by stages/fetch_art.py)
setup.sh                         — first-time environment setup: config files, bootstrap.sh,
                                     music-watcher.sh → ~/.local/bin/, plists → ~/Library/LaunchAgents/
(launchd agents com.strangestlens.music-watcher + com.strangestlens.music-sync
                                     are GENERATED per-machine by setup.sh — home dir, log dir, and
                                     the retention volume to watch all come from config, not baked in)
lrc-editor                       — standalone Flask/WaveSurfer.js lyrics timing editor (single
                                     executable); its "AI Arrange" button POSTs /ai-arrange, which runs
                                     lyric_timing retime as a background subprocess of the AI venv;
                                     /audit page runs lyric_timing audit, remembers paths in
                                     ~/.config/spindlebot/lrc-editor-state.json, loads rows into
                                     the editor via POST /load

lyric_timing/                    — OPTIONAL AI lyric-timing subsystem (peer package; heavy deps
                                     NOT in core spindlebot). Plan: ~/.claude/plans/ai-lyric-timing-plan.md
  lrc.py                         — parse_lrc (file-order-preserving) / format_lrc
                                     (byte-compatible with lrc-editor)
  detector.py                    — audit_lrc(): flag .lrc files needing re-timing
                                     (all-identical / low-distinct / crammed-early / non-monotonic)
  aligner.py                     — align(): word→line matching, interpolation, monotonicity,
                                     confidence — the offline-testable core
  backends/base.py               — AlignmentBackend Protocol + Word (swappable + mockable)
  backends/mock.py               — deterministic fake for tests
  backends/torchaudio_backend.py — real backend: Demucs vocal sep + chunked wav2vec2 CTC forced
                                     alignment (torchaudio; memory bounded by 30s windows, not track
                                     length; lazy heavy imports; run from the AI venv)
  cli.py                         — python -m lyric_timing audit|retime
setup-ai.sh                      — creates the AI venv at ~/.local/share/spindlebot/ai-venv
                                     (Python 3.13) from requirements-ai.txt

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
  test_albums.py                 — core/albums album_key
  test_vclock.py                 — core/vclock version-vector logic
  test_db.py                     — connection + schema migrations (v1–v7)
  test_repositories.py           — audio/location/presence repos
  test_sidecar_repositories.py   — album/sidecar/sidecar-presence repos
  test_run_action_repositories.py— run + pending_action repos
  test_lyric_conflict_repositories.py — lyric_doc/lyric_version + conflict repos
  test_locations.py              — location registry + LocationKind
  test_volumes.py                — marker files + root resolution
  test_inventory.py              — inventory service (+ albums, sidecars, beets linkage, scan status)
  test_reconciler.py             — reconciler planner (copy/missing/conflict, min_copies, scan gate,
                                     lyric lineage: linear-edit/behind/concurrent, per-copy observed_utc)
  test_lyrics_sync.py            — lyric lineage service (current/behind/concurrent, legacy-head
                                     repair, stable-uuid actor, per-observation version presence)
  test_review_cli.py             — spindlebot review CLI (plan + acknowledge)
  test_collection_match.py       — THE matcher contract: normalization + a match table whose
                                     rows are real Discogs/beets pairs (incl. the containment
                                     cases a whole-string fuzzy match got wrong)
  test_collection_discogs.py     — Discogs transformer against tests/fixtures/
                                     discogs_collection_page1.json + client paging/throttle/
                                     cache/errors (injected fetcher; never hits the network)
  test_collection_audit.py       — audit service, fixture provider, registry, library index
  test_collection_cli.py         — spindlebot collection-audit CLI (text + --json)
  test_collection_report.py      — HTML report: structure, escaping, http(s)-only URL
                                     sanitization, self-containment, --html CLI wiring
  test_lyric_timing_lrc.py       — lyric_timing/lrc parse/format
  test_lyric_timing_detector.py  — audit heuristics
  test_lyric_timing_aligner.py   — word→line assignment, interpolation, monotonicity (mock backend)
  test_lyric_timing_cli.py       — audit + retime CLIs (mock backend)
  test_lyric_timing_torchaudio.py— real-backend integration; skipped unless
                                     LYRIC_TIMING_IT_AUDIO/LYRIC_TIMING_IT_LRC set (never in CI)
  test_lrc_editor_ai.py          — lrc-editor /ai-arrange job orchestration (mock backend)
  test_lrc_editor_audit.py       — lrc-editor /audit page: run job, saved-state recall, /load
  shell/                         — bats shell tests (shellcheck + integration)
```

## ImportRunner stage sequence

`spindlebot/pipeline/runner.py` — `ImportRunner.run()`:

1. **trigger validation** — directory → `album_dir = trigger`; `.log` → `album_dir = trigger.parent`; other → logged and skipped
2. **double-fire guard** — `.log` mode only: already-archived log → clean exit
3. **disc check** — `check_wait()`: wait if multi-disc set incomplete (bypass with `--force`)
4. **pretag** — normalize tags before beet sees them
5. **beet import** — streams live to terminal when echo callback set
   - **duplicate handling** (per album batch): a green `beet import` that adds NO new items is checked against the existing library (match by `musicbrainz_albumid`, else BOTH `albumartist`+`album` — a one-sided fallback could false-match an unrelated album — on items added *before* the import). A match ⇒ already-in-library duplicate: log the `⏭` milestone, move the rip to `duplicates_dir/<artist>/<album>/`, notify, and skip the remaining stages for it. No match ⇒ a *different* no-op failure: warn and LEAVE the files in Import (never moved/discarded). If the post-import `beet ls` verification itself *fails* (nonzero exit), the result is UNKNOWN — skip duplicate handling and leave the files in place rather than risk moving a real import.
6. **multidisc fix** — patch `disctotal` + `multidisc` flex attr in beets DB via `sqlite3`
7. **beet move** — relocate to canonical library paths
8. **posttag** — strip beet alias tags, truncate DATE to year
9. **fetch-art + fetch-lyrics** — embed art, write `.lrc` sidecars
10. **archive** — move XLD `.log` to archive dir
11. **auto-sync-or-hint** — only on a fully successful run with `spindlebot_cfg` set (mirrors the `via_processing` gate). If `core.auto_sync_on_import` is **false** (default): log a tail-friendly HINT to run `music-sync.sh` manually. If **true**: check whether the retention destination path (`ImportConfig.retention_path`, the first enabled **local_drive** `[[destinations]]` — an rclone path can never serve as the mount check) exists — mounted ⇒ log `🔄 auto-syncing…` and invoke the self-guarding `music-sync.sh` (do NOT reimplement sync); not mounted ⇒ log a reconnect hint and don't invoke. A sync failure here is logged, never promoted to an import failure. Injectable via `ImportRunner(sync_runner=…)` + `ImportConfig.sync_script`/`retention_path` so tests never shell out.

`ImportRunner.__init__` accepts an optional `echo: Callable[[str], None]` for live terminal
feedback, and an optional `sync_runner: Callable[[Path], int]` (defaults to running
`sync_script` via subprocess) so auto-sync is testable without shelling out.
`_log(msg, *, echo=True)` always writes to the log file; routes to echo only for
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

**9. `lyric_timing retime` runs from the repo root, in the AI venv**
The AI venv doesn't have `lyric_timing` installed — it resolves off the CWD. Any caller
must set `cwd` to the pipeline dir (`lrc-editor` passes `PIPELINE_DIR`), or the subprocess
dies with `No module named lyric_timing`. Same reason `audit` shells out with
`cwd=PIPELINE_DIR`.

**10. `retime` stdout is a data channel**
`--json` and the default LRC output go to stdout and nothing else may. Demucs and torch
print progress freely, so `cmd_retime` wraps the whole `align()` call in
`contextlib.redirect_stdout(sys.stderr)`. Anything new that prints during alignment must
stay on stderr or it will corrupt the caller's parse — this already broke `--json` once.

**11. Heavy imports in `lyric_timing` stay lazy**
`torch`/`torchaudio`/`demucs` are imported inside methods, never at module scope, so
`audit` and the whole test suite run on a bare Python. `_ai_deps_available()` probes for
`torch` via `importlib.util.find_spec` rather than catching an `ImportError` mid-alignment.
MPS watermark env vars must be set *before* torch is first imported.

**12. Collection matching is artist-scoped — do not "simplify" it to one fuzzy score**
Resolve the artist first, then compare titles only within that artist's albums. A whole-string
similarity over `"artist title"` measurably does not work: a multi-disc rip carries a
`" - <disc title>"` suffix from MusicBrainz and Discogs release names run long or short, so
`Ummagumma` vs `Ummagumma - Live Album` scored 0.78 — below every usable threshold and
indistinguishable from an album that genuinely isn't there. Seven real albums were reported
missing that way. The match table in `tests/test_collection_match.py` guards each case.

**13. Non-Latin scripts must survive normalization**
`normalize_*` strips combining marks only from ASCII bases. Blanket NFKD + combining-mark
removal also strips Japanese dakuten (パ decays to ハ, a different kana), and an ASCII-only
character class empties a katakana title entirely — which throws away the only thing a
dual-script release can match on. A library tagged in a different script than the collection
lists (e.g. beets `ベック / ハイパースペース(2020)` vs Discogs `Beck / Hyperspace (2020)`)
remains unmatchable without transliteration; that is what the ignore list is for, not a reason
to add a heavy dependency to `spindlebot/`.

## Code quality non-negotiables

- shellcheck clean on all `.sh` files before commit. Use `# shellcheck disable=SC####` with a comment explaining why — no blanket suppressions.
- ruff for Python linting (configured in `requirements.txt`).
- No print-driven side effects in library code. `print()` only in CLI entry points.
- beets template vars (`$albumartist`, `$path`, etc.) must be in single quotes in shell — use `# shellcheck disable=SC2016` with the comment `"beet template var, not a bash var"`.
- All new Python modules go in `spindlebot/` — or, for the optional AI subsystem, in `lyric_timing/` — and must have corresponding tests in `tests/`. Nothing that pulls a heavy dependency (torch, demucs) may land in `spindlebot/`; that boundary is what keeps the core pipeline and CI light.

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
- Processing area: `~/Library/Application Support/SpindleBot/Processing` (in-flight import processing; albums promote to Pending once lyric-complete; `core.processing_dir`, env `SPINDLEBOT_PROCESSING_DIR`)
- Pending area: `~/Library/Application Support/SpindleBot/Pending` → `/Volumes/DwRugged/Music/Library` (processed, lyric-complete albums awaiting distribution; formerly `~/Music/Library`)
- Duplicates area: `~/Library/Application Support/SpindleBot/Duplicates` (already-in-library rips moved here by import instead of stranding in Import; `core.duplicates_dir`, env `SPINDLEBOT_DUPLICATES_DIR`)
- beets DB: `~/.config/beets/library.db`
- launchd agents: `com.strangestlens.music-watcher`, `com.strangestlens.music-sync`

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
python3 -m spindlebot finalize [--dry-run] [--json]          # retry lyrics + promote lyric-complete albums out of Processing → Pending
python3 -m spindlebot fetch-art <album_dir> [--dry-run] [--force]
python3 -m spindlebot fetch-lyrics <album_dir> [--dry-run] [--force]

python3 -m spindlebot collection-audit [--handle <name>] [--media cd,vinyl] [--index auto|beets|db] [--refresh] [--json]
                                                             # optional: what's in the Discogs
                                                             # collection but not in the library
python3 -m spindlebot inventory [--location <name>] [--json]  # scan a location into the DB (read-only re: bytes)
python3 -m spindlebot review --location <name> [--json]       # plan reconciliation (run inventory first); no bytes moved
python3 -m spindlebot review --acknowledge-run <run_id>       # acknowledge a run's proposed actions
python3 -m spindlebot restart                                # restart launchd agents

./setup-ai.sh                                                # one-time: install AI lyric-timing deps (heavy)
python3 -m lyric_timing audit <dir-or-lrc...> [--json]       # flag .lrc files needing re-timing (no heavy deps)
~/.local/share/spindlebot/ai-venv/bin/python -m lyric_timing retime <audio> <lrc> \
    [--overwrite] [--json] [--no-vocal-sep]                  # AI re-time via forced alignment (run from repo root)
```
