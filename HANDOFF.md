# SpindleBot — Handoff & Getting Started

*Last updated: July 2026*

This is the doc to read first if you're picking up SpindleBot to run or test it.
It covers what the system is, how to get it running on a fresh Mac, the command
surface, and where to go for deeper detail.

> **For AI agents:** the authoritative, always-current source of truth is
> [`CLAUDE.md`](CLAUDE.md) at the repo root. It documents the architecture,
> layering rules, conventions, active epic status, and known gotchas in far more
> detail than this file. Read it before touching code. This HANDOFF is the
> human on-ramp; CLAUDE.md is the contract.

Repo: `github.com/strangestlens/SpindleBot`

---

## What SpindleBot is

An event-driven pipeline for ripping, tagging, and managing a lossless (FLAC)
music library on macOS. It watches for new rips/downloads, matches and tags them
against MusicBrainz via beets, fetches art and synced lyrics, and distributes the
result to a retention drive — with a SQLite database that is the system of record
for content **identity** and every **location** a copy lives.

Two primary flows:

**Import** (a CD rip finishes, or a folder is dropped into the Import area):

```
XLD rips a CD → writes a .log to the Import area
  → fswatch (music-watcher.sh) fires `spindlebot import`
  → pretag → beet import → multidisc fix → beet move → Processing
  → posttag → fetch-art → fetch-lyrics
  → per-album promote to Pending (only once the album is lyric-complete)
  → archive the .log → notify (macOS + Telegram)
```

**Sync** (the retention drive is mounted):

```
launchd detects the mount → music-sync.sh
  → content-addressed sync: inventory → review → copy→verify→record → prune
  → notify
```

The three working areas an album moves through:

| Area | Path | Role |
|------|------|------|
| **Import** | `~/Library/Application Support/SpindleBot/Import` | XLD rips / downloads land here for processing |
| **Processing** | `~/Library/Application Support/SpindleBot/Processing` | In-flight work; art + lyrics are fetched here |
| **Pending** | `~/Library/Application Support/SpindleBot/Pending` | Lyric-complete albums awaiting distribution |
| **Duplicates** | `~/Library/Application Support/SpindleBot/Duplicates` | Rips already in the library are parked here, not stranded in Import |

An album is promoted from Processing to Pending **only once every track has a
terminal `.lrc`/`.nolrc` marker** (lyric-complete). This makes Pending
complete-by-construction, so a mount-sync can never prune audio out from under a
still-running lyric fetch. Albums that get stuck in Processing (transient lyric
errors) are swept up later by `spindlebot finalize`.

Retention destination: a configurable `[[destinations]]` target on an external
drive — e.g. `/Volumes/<RetentionDrive>/Music/Library`. (The author's is named
`DwRugged`; examples that use that name are his specific drive — substitute your
own. See "Daniel's machine" at the bottom for the real values.)

Key external tools: **beets** (MusicBrainz matching + its own SQLite item DB),
**XLD** (CD ripper), **fswatch** (file watcher), **launchd** (macOS daemons),
**mpv** (playback/preview), and the standalone **lrc-editor** (Flask/WaveSurfer.js
lyric-timing editor).

Two optional side subsystems sit alongside the pipeline, neither of which it
depends on:

- **AI lyric timing** (`lyric_timing/`, a peer package) finds and repairs
  mistimed `.lrc` files. Its heavy dependencies live in their own venv — see
  [Optional: AI lyric timing](#optional-ai-lyric-timing).
- **Collection audit** answers "which discs do I own but haven't ripped?" by
  comparing an external collection against the library — see
  [Optional: collection audit](#optional-collection-audit).

---

## Current state (July 2026)

The original portability + modularization work is **done**, and the project is
mid-way through a larger content-addressed refactor.

- **Phase 1 — Config & portability:** complete. Everything is config-driven via
  `~/.config/spindlebot/config.toml` + `secrets.toml`; every shell script sources
  a generated `bootstrap.sh` and reads `$SPINDLEBOT_*` env vars.
- **Phase 2 — Modular architecture:** complete. All import-pipeline logic lives in
  the `spindlebot/` Python package (`pipeline/runner.py` + `pipeline/stages/`).
  The shell scripts are thin shims. Full test coverage (28 test modules).
- **Content-addressed library refactor (active epic):** a SpindleBot-owned SQLite
  DB (`~/.config/spindlebot/spindlebot.db`) tracks content identity, locations,
  and copies. Phases A, 0, 1, sidecars, 2, and 3 (destructive sync incl.
  copy→verify→record, prune, and gated delete) are merged. The `music-sync.sh`
  cutover to content-addressed sync (no more `rsync --remove-source-files`) has
  landed. **Phase 4 (lyrics bidirectional sync) is underway** — 4.0, the causal-lineage
  substrate (schema v7 `lyric_version_presence` + `services/lyrics_sync.py`), is
  merged. See [`CLAUDE.md`](CLAUDE.md) → "Content-addressed library refactor" for
  the precise per-phase status.
- **AI lyric timing (side subsystem):** merged and usable. `lyric_timing/` audits
  `.lrc` files for bad timing and re-times them against the audio by forced
  alignment, wired into lrc-editor as an **Audit** page and an **AI Arrange**
  button. Optional and self-contained — see below.
- **Collection audit (side subsystem):** merged and usable. `spindlebot
  collection-audit` compares an external collection (Discogs, or a JSON file you
  write) against the library and lists the discs you own but haven't ripped,
  with an ignore list and a `collection-browser` web UI. Purely assistive —
  nothing in the import or sync path depends on it, and it's inert until
  configured. See below.

The forward-looking roadmap (lyrics bidirectional sync, remote access via
Navidrome, a Mac app) lives in [`ROADMAP.md`](ROADMAP.md).

---

## Getting started on a fresh Mac

### Prerequisites

- macOS on Apple Silicon
- Homebrew, with: `brew install python beets fswatch mpv`
- XLD (for actual CD ripping; not needed to exercise the pipeline on existing files)
- Python 3.11+ (`setup.sh` installs `tomli` automatically if you're on older 3.x)

### 1. Clone and run setup

```bash
git clone https://github.com/strangestlens/SpindleBot.git ~/Music/music-pipeline
cd ~/Music/music-pipeline
./setup.sh
```

`setup.sh` is idempotent and does the following:

1. Creates `~/.config/spindlebot/` and copies `config.toml.example` →
   `config.toml` and `secrets.toml.example` → `secrets.toml` (skips either if it
   already exists).
2. Writes `~/.config/spindlebot/bootstrap.sh` with your Python path and pipeline
   dir baked in — every shell script sources this to get `$SPINDLEBOT_*` vars.
3. Creates the Import / Processing / Pending working directories.
4. Installs `music-watcher.sh` → `~/.local/bin/`.
5. Installs and (re)loads the two launchd agents into `~/Library/LaunchAgents/`.
6. Runs `python3 -m spindlebot check`.

### 2. Fill in config and secrets

```bash
$EDITOR ~/.config/spindlebot/config.toml    # paths, tool locations, destinations
$EDITOR ~/.config/spindlebot/secrets.toml   # Telegram token, Genius API key
```

`config.toml.example` is heavily commented — read it. The key sections:

- `[core]` — the Import / Processing / Pending / archive / duplicates dirs, and
  `auto_sync_on_import` (default `false`).
- `[tools]` — absolute paths to `beet`, `python`, `mpv`, plus the beets DB and
  beets config location.
- `[notifications]` — toggle macOS and Telegram independently.
- `[lyrics]` / `[art]` — source order and tuning.
- `[collection]` — **optional**; the external-collection audit. Delete the block
  and the feature is simply off. `[discogs] token` in `secrets.toml` is optional
  too — a public collection needs no credentials.
- `[[destinations]]` — one block per sync target. `type = "local_drive"` (rsync to
  a mounted volume) or `type = "rclone"` (any rclone remote). The **first enabled
  `local_drive`** is the retention destination and the mount probe.

Secrets can also come from env vars, which override the file:
`SPINDLEBOT_TELEGRAM_TOKEN`, `SPINDLEBOT_TELEGRAM_CHAT_ID`, `SPINDLEBOT_GENIUS_KEY`.

### 3. Validate

```bash
cd ~/Music/music-pipeline
python3 -m spindlebot check
```

`check` verifies the working dirs exist, the tool binaries are executable, the
beets DB is present, each enabled destination is reachable, and the credentials
are set — printing a concrete fix suggestion for anything that fails.

### 4. Run the tests

```bash
python3 -m pytest tests/ --ignore=tests/shell    # Python suite
bats tests/shell/                                 # shell suite (needs bats + shellcheck)
```

Both must be green. CI runs the same two jobs on every push/PR.

---

## Command surface

Every command is invoked as `python3 -m spindlebot <command>`. The DB/sync
commands (`finalize`, `inventory`, `review`, `sync`, `prune`, `delete`) support
`--json` for structured output; the others print human-readable text only. The
examples below abbreviate the invocation to `spindlebot` — either substitute the
full form, or add an alias (there is no `spindlebot` executable on PATH by
default; `setup.sh` does not install one):

```bash
alias spindlebot='python3 -m spindlebot'
```

```bash
# --- inspection / setup ---
spindlebot check                          # validate config + environment
spindlebot config shell                   # emit config as shell exports (what bootstrap.sh evals)
spindlebot config get core.pending_dir    # print a single value

# --- import ---
spindlebot import <trigger> [--force]     # run the import pipeline for one album
                                          #   trigger = an album dir OR an XLD .log
                                          #   --force skips the multi-disc wait
spindlebot import-staging [--dry-run]     # import everything currently in the Import area
spindlebot finalize [--dry-run]           # retry lyrics + promote lyric-complete albums
                                          #   out of Processing → Pending

# --- content-addressed DB / sync ---
spindlebot inventory [--location <name>] [--rehash]   # scan a location into the DB (read-only re: audio)
spindlebot review --location <name> [--yes]           # plan reconciliation; --yes acknowledges
spindlebot review --acknowledge-run <run_id>          # acknowledge every action in a run
spindlebot sync [--location <name>]                   # execute acknowledged copies (copy→verify→record)
spindlebot prune [--execute]                          # release Pending copies verified on retention (DRY-RUN unless --execute)
spindlebot delete [--execute]                         # execute acknowledged retention-copy deletes (gated on min_copies)

# --- collection audit (optional; see below) ---
spindlebot collection-audit [--handle <name>]         # what's on the shelf but not ripped
spindlebot collection-ignore <id...> [--reason <t>]   # stop reporting a disc as missing
spindlebot collection-ignore --list                   # what's ignored
spindlebot collection-ignore --remove <id...>         # put one back (--unignore too)

# --- per-album utilities ---
spindlebot fetch-lyrics <dir> [--dry-run] [--force]
spindlebot fetch-art <dir> [--dry-run] [--force]
spindlebot notify <title> <message>       # send a test notification on all channels
spindlebot restart                        # restart the launchd agents
```

> **Destructive ops default to dry-run.** `prune` and `delete` only touch bytes
> with `--execute`. `prune` releases a *non-retention* Pending copy once a
> retention copy is verified — it never lowers the retention count, so `min_copies`
> is only a warning there. `delete` (removing a *retention* copy) IS gated: it
> never drops retention below `min_copies`.

The daemons — the fswatch import watcher and the retention-drive mount sync — run under
launchd (`com.strangestlens.music-watcher`, `com.strangestlens.music-sync`)
and are installed by `setup.sh`. Use `spindlebot restart` to bounce them.

---

## Optional: collection audit

Answers one question: **which discs do I own but haven't ripped?** It compares a
collection you already maintain elsewhere against the digital library. Nothing
in the import or sync path depends on it, and with no `[collection]` config it
simply doesn't run.

```bash
spindlebot collection-audit --handle your-discogs-handle
```

```
discogs:yourhandle — 212 item(s), 152 on cd
library (beets 112, db 177) — 176 unique album(s)

MISSING (47)
  1234567   Beck — Sea Change (2002)
  ...

104 owned · 1 uncertain · 47 missing

Not going to rip one? spindlebot collection-ignore 1234567
```

### Three things worth understanding before you trust it

**1. The library index is the union of beets *and* the SpindleBot DB.** Neither
is a superset of the other, and they go stale in opposite directions:

| Index | Knows | Blind to |
|-------|-------|----------|
| `beets` | Everything it imported and still tracks | Albums that reached a drive without a `beet import` |
| `db` | Everything `inventory` has scanned at any location | A fresh import that hasn't synced yet |

Measured on the author's library: 67 albums existed *only* in the DB (copied-in
files with no `beets_item_id`) and 2 *only* in beets. Auditing against beets
alone reported 95 of 152 CDs missing — 48 of them owned. So `--index auto` (the
default) unions them, and every run prints which index contributed what. If an
album is ever wrongly reported missing, **the index is the first suspect**.

If both indexes come back empty the audit fails rather than declaring your
entire collection missing.

**2. Matching is artist-scoped, and that is load-bearing.** It resolves the
artist first, then compares titles only within that artist's albums. A
whole-string similarity does not work here — a multi-disc rip carries a
MusicBrainz `" - <disc title>"` suffix, so `Ummagumma` vs
`Ummagumma - Live Album` scores 0.78, indistinguishable from an album that
genuinely isn't there. See gotcha #12 in `CLAUDE.md`.

**3. There are three buckets, not two.** `uncertain` exists so a normalization
miss sends you to eyeball a row rather than to the shelf to re-rip something you
already own.

### Ignoring

Damaged discs, gifts, a release the matcher can't reach. Without this the
missing list keeps a permanent floor of noise and stops being worth opening.

```bash
spindlebot collection-ignore 1234567 --reason "disc is cracked"
spindlebot collection-ignore --list
spindlebot collection-ignore --remove 1234567     # --unignore works too
```

Ignoring never overwrites the verdict underneath, so un-ignoring restores
exactly what the audit said before. An album you later rip is never reported as
ignored, so stale entries stop mattering on their own. Stored as JSON at
`~/.config/spindlebot/collection-ignore.json` — not a schema change.

### The web UI

```bash
./collection-browser --handle your-discogs-handle
```

The same report with an **ignore** button on every card and an **undo** on every
ignored one; counts update live. A sibling to `lrc-editor` — same palette, same
single-file shape — and deliberately outside `spindlebot/` so the pipeline
package never takes a Flask dependency. Binds to `127.0.0.1` and refuses
cross-origin POSTs.

`collection-audit --html <file>` writes the same page as a static export with no
buttons and no server.

### Sources other than Discogs

A provider is one function: account → `list[CollectionItem]`. Each splits into
an impure client and a **pure** transformer, so a source's quirks are testable
against a recorded payload with no network. Two ship today:

- `discogs` — public collections need no credentials. A token in `secrets.toml`
  (`[discogs] token`) raises the rate limit from 25 to 60 req/min and is
  required for a private collection.
- `fixture` — a JSON file you write by hand. Both the test double and the
  supported way in for anyone not on Discogs; `account` is the file path.

---

## Optional: AI lyric timing

A peer package, `lyric_timing/`, not part of the import pipeline and not required
to run SpindleBot. It exists because lrclib sometimes has only *plain* lyrics:
the fetch stage still writes an `.lrc`, but every line is stamped `[00:00.00]`.
This subsystem finds those files and re-times them against the audio.

**Why it's separate:** the real alignment backend needs torch, torchaudio, and
demucs. Those never enter core SpindleBot or CI — they go in a dedicated venv,
and the backend sits behind a `Protocol` so every timing rule is unit-tested
offline against a mock.

### Install (one time)

```bash
./setup-ai.sh    # → ~/.local/share/spindlebot/ai-venv  (override: $SPINDLEBOT_AI_VENV)
```

Idempotent, and it restores your previous venv if the install fails. It tries
Python 3.13 down to 3.10 and uses the first that can resolve
`requirements-ai.txt`. Models (~700 MB) download to `~/.cache` on the first
alignment run, not here. Verify with the command `setup-ai.sh` prints on success.

### Commands

```bash
# audit — pure text heuristics, NO heavy deps, runs on the system python3
python3 -m lyric_timing audit <dir-or-.lrc ...> [--json]

# retime — forced alignment; must run from the repo root, via the AI venv
~/.local/share/spindlebot/ai-venv/bin/python -m lyric_timing retime \
    <audio> <lrc> [--overwrite] [--json] [--no-vocal-sep]
```

`audit` recurses directories and reports one line per suspicious file:
`all-timestamps-identical`, `low-distinct-timestamps`, `timestamps-crammed-early`,
`non-monotonic`, or `no-timed-lines`. The thresholds are deliberately
conservative — a hand-timed song with a long instrumental tail should not appear.

`retime` keeps the lyric text and recomputes only the timestamps: Demucs isolates
the vocal stem, wav2vec2 CTC forced alignment over 30-second windows yields word
times, words are matched to lines positionally, and unmatched or low-confidence
lines are interpolated between confident anchors, then forced monotonic. It is
**non-destructive by default** — the new LRC goes to stdout unless you pass
`--overwrite`.

### In lrc-editor

Two toolbar entries drive the same two commands from the browser:

- **Audit** → the `/audit` page. Pick a folder and an output JSON with the native
  macOS pickers, run it, and get a table of only the suspicious files. **Edit** on
  a row loads that track directly into the editor. Paths and the last result set
  persist in `~/.config/spindlebot/lrc-editor-state.json`.
- **AI Arrange** → runs `retime` on the loaded track using the lyric text already
  in the editor, as a `nice`'d, memory-capped background subprocess of the AI
  venv. Results apply through the undo stack, deliberate empty-text markers keep
  their manual times, and anything under 0.5 confidence turns orange for manual
  review. Nothing auto-saves; **Commit** still writes the file.

`lrc-editor` finds the venv via `$SPINDLEBOT_AI_VENV` and the package via
`$SPINDLEBOT_PIPELINE_DIR` (defaulting to the editor's own directory). Without
the venv, **AI Arrange** reports "AI venv not found — run setup-ai.sh first"; the
rest of the editor is unaffected.

### Tests

`tests/test_lyric_timing_*.py` and `tests/test_lrc_editor_{ai,audit}.py` cover
parse/format, the audit heuristics, the aligner, both CLIs, and the editor's job
orchestration — all against the mock backend, so the standard `pytest` run needs
none of the AI dependencies. `tests/test_lyric_timing_torchaudio.py` exercises the
real backend and is skipped unless `LYRIC_TIMING_IT_AUDIO` and
`LYRIC_TIMING_IT_LRC` point at a real track. It never runs in CI.

---

## Where things live

```
music-pipeline/
├── spindlebot/                  # the Python package (see CLAUDE.md for full layering)
│   ├── cli.py                   # CLI entry point; the ONLY place that prints
│   ├── config.py                # typed config; loads config.toml + secrets.toml + env
│   ├── disc.py, staging.py      # audio discovery, Import-area scan
│   ├── pipeline/
│   │   ├── runner.py            # ImportRunner: orchestrates all import stages
│   │   └── stages/              # pretag, posttag, fetch_art, fetch_lyrics, notify
│   ├── core/                    # pure, side-effect-free (identity, albums, vclock, enums, models,
│   │                            #   collection + collection_match)
│   ├── collections/             # OPTIONAL external collection sources (discogs, fixture);
│   │                            #   each = impure client + PURE transformer
│   ├── db/                      # SpindleBot's own SQLite system-of-record
│   │   ├── connection.py        # open_db(): WAL + foreign_keys + migrate
│   │   ├── schema*.sql          # versioned DDL (user_version 1–7)
│   │   └── repositories/        # the ONLY SQL layer
│   └── services/                # orchestration over repos+core
│       ├── inventory.py, locations.py, volumes.py
│       ├── library_index.py     # the library as beets ∪ DB (neither is a superset)
│       ├── collection_audit.py  # provider → media filter → match → buckets
│       ├── collection_ignore.py # the "don't tell me about this disc" list
│       ├── collection_report.py # AuditReport → self-contained HTML
│       ├── reconciler.py        # planner: diff DB vs observed → pending_action
│       ├── promote.py           # Processing → Pending promotion
│       ├── lyrics_sync.py       # per-doc lyric causal lineage (Phase 4.0)
│       └── sync.py              # copy→verify→record, prune, delete
├── lyric_timing/                # OPTIONAL AI lyric-timing subsystem (peer package)
│   ├── lrc.py                   # parse/format, byte-compatible with lrc-editor
│   ├── detector.py              # audit_lrc(): heuristics for "this needs re-timing"
│   ├── aligner.py               # word→line assignment, interpolation, monotonicity
│   ├── backends/                # AlignmentBackend Protocol + mock + torchaudio
│   └── cli.py                   # python -m lyric_timing audit|retime
├── music-watcher.sh             # fswatch daemon (installed to ~/.local/bin)
├── music-import.sh              # shim → spindlebot import
├── music-sync.sh                # content-addressed sync to the retention drive
├── music-notify.sh              # legacy notify shim
├── music-*.py                   # legacy root scripts (superseded by stages/, kept for reference)
├── setup.sh                     # one-time environment setup
├── setup-ai.sh                  # optional: builds the AI venv from requirements-ai.txt
├── migrate-work-dirs.sh         # sourceable helpers for the Staging→Import / Library→Pending rename
├── com.strangestlens.*.plist    # launchd agents
├── lrc-editor                   # standalone Flask/WaveSurfer.js lyric-timing editor
│                                #   (+ the Audit page and AI Arrange button)
├── collection-browser           # standalone Flask UI for the collection audit
│                                #   (click-to-ignore); outside spindlebot/ so the
│                                #   package never takes a Flask dependency
├── config.toml.example          # config template
├── secrets.toml.example         # credentials template
├── tests/                       # pytest suite + tests/shell/ (bats)
├── CLAUDE.md                    # ← source of truth: architecture, conventions, gotchas
├── ROADMAP.md                   # forward-looking plans
└── HANDOFF.md                   # this file
```

---

## Gotchas worth knowing up front

These are the ones most likely to trip up a new operator. The full list is in
[`CLAUDE.md`](CLAUDE.md) → "Known gotchas".

- **`SPINDLEBOT_IMPORT_DIR`, not `SPINDLEBOT_IMPORT`.** The `_DIR` suffix matters;
  without it the var resolves empty and fswatch silently watches the wrong
  directory. `music-watcher.sh` guards against this at startup.
- **bootstrap.sh is load-bearing.** Every shell script sources it, and it evals
  `python -m spindlebot config shell`. If Python or the config is broken, all
  `$SPINDLEBOT_*` vars come back empty. Scripts fail loudly by design.
- **Multi-disc detection** waits for all discs before importing. MusicBrainz can
  report `disctotal=2` for single-disc DualDiscs/deluxe editions; the runner
  patches this from the actual ripped disc count. Use `import --force` to bypass
  the wait for a genuinely partial set.
- **`multidisc` flex attribute** must be INSERTed via `sqlite3` directly — `beet modify multidisc=`
  deletes the row and makes the template render the literal string `"$multidisc"`.
  Handled in `runner.py`; don't change the approach.
- **Identity is decoded-audio MD5** (FLAC STREAMINFO `md5_signature`), falling back
  to whole-file sha256. Per-copy sha256 is integrity, never identity.
- **`lyric_timing retime` must run from the repo root**, using the AI venv's
  Python. The venv doesn't have the package installed — it's found on the CWD.
  Run it from elsewhere and you get `No module named lyric_timing`.

---

## Daniel's machine (reference config)

- macOS, Apple Silicon
- Python: `/opt/homebrew/bin/python3` (3.11+; currently resolves to 3.14)
- beet: `/opt/homebrew/bin/beet`; mpv: `/opt/homebrew/bin/mpv`
- beets DB: `~/.config/beets/library.db`; beets config: `~/.config/beets/config.yaml`
- SpindleBot DB: `~/.config/spindlebot/spindlebot.db`
- SpindleBot config: `~/.config/spindlebot/config.toml` + `secrets.toml`
- Logs: `~/.config/beets/watcher.log` (import), `~/.config/beets/music-sync.log` (sync)
- Retention: `/Volumes/DwRugged/Music/Library`
- launchd agents: `com.strangestlens.music-watcher`, `com.strangestlens.music-sync`
- AI venv: `~/.local/share/spindlebot/ai-venv`; editor state: `~/.config/spindlebot/lrc-editor-state.json`
