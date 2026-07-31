# SpindleBot — Music Pipeline

Event-driven pipeline for ripping, tagging, and managing a lossless (FLAC) music
library on macOS. It matches rips against MusicBrainz via beets, fetches album art
and synced lyrics, and distributes lyric-complete albums to a retention drive,
backed by a SQLite database that is the system of record for content **identity**
and every **location** a copy lives.

- **New here?** Read [`HANDOFF.md`](HANDOFF.md) for setup and the command surface.
- **Working on the code (human or agent)?** [`CLAUDE.md`](CLAUDE.md) is the source
  of truth for architecture, layering, and conventions.
- **Where it's going?** [`ROADMAP.md`](ROADMAP.md).

## Flow

```
Import                                          Sync
────────────────────────────────               ──────────────────────────────
CD → XLD → Import area                          Retention drive mounted
        │                                               │
  [.log written / folder dropped]                 launchd fires
        │                                          music-sync.sh
  fswatch → music-watcher.sh                            │
        │                                       ┌───────┴──────────┐
  spindlebot import                             │ inventory        │ scan into DB
        │                                       │ review           │ plan copies
  ┌─────┴──────────┐                            │ sync             │ copy→verify→record
  │ pretag         │ feat./artist cleanup       │ prune            │ release verified
  │ beet import    │ MusicBrainz match          │ notify           │ macOS + Telegram
  │ multidisc fix  │ actual disc count          └──────────────────┘
  │ beet move      │ → Processing
  │ posttag        │ date trunc, alias cleanup
  │ fetch art      │ CAA → iTunes fallback
  │ fetch lyrics   │ lrclib → .lrc / .nolrc
  │ promote        │ → Pending (only if lyric-complete)
  │ archive log    │ → archive dir (configurable)
  │ notify         │ macOS + Telegram
  └────────────────┘
```

An album is promoted from **Processing** to **Pending** only once every track has
a terminal `.lrc`/`.nolrc` marker (lyric-complete). Pending is therefore
complete-by-construction, so a mount-sync can never prune audio out from under a
running lyric fetch. Albums stuck in Processing are swept up by `spindlebot
finalize`.

## Working areas

| Path | Purpose |
|------|---------|
| `~/Library/Application Support/SpindleBot/Import/` | XLD rips / downloads land here for processing (formerly `~/Music/Staging`) |
| `~/Library/Application Support/SpindleBot/Processing/` | In-flight albums; art + lyrics fetched here |
| `~/Library/Application Support/SpindleBot/Pending/` | Lyric-complete albums awaiting distribution (formerly `~/Music/Library`) |
| `~/Library/Application Support/SpindleBot/Duplicates/` | Rips already in the library are parked here, not stranded in Import |
| *archive dir* | Archived XLD `.log` files — configurable via `core.archive_dir` |
| *retention drive* | The permanent library — a configurable `[[destinations]]` target, e.g. `/Volumes/<RetentionDrive>/Music/Library/` |
| `~/.config/spindlebot/config.toml` + `secrets.toml` | SpindleBot config + credentials |
| `~/.config/spindlebot/spindlebot.db` | SpindleBot's content-identity + location DB |
| `~/.config/beets/config.yaml` | beets config (`directory:` must match the Pending area) |
| `~/.config/beets/library.db` | beets item DB |
| `~/.config/beets/watcher.log` | import pipeline log |
| `~/.config/beets/music-sync.log` | sync pipeline log |

> The retention drive and archive dir are **configuration**, not fixed paths.
> Set them in `config.toml` (`[[destinations]]` and `core.archive_dir`). Examples
> throughout this repo use `DwRugged` — that's the author's specific external
> drive; substitute your own.

## Commands

Everything is `python3 -m spindlebot <command>`. The DB/sync commands
(`finalize`, `inventory`, `review`, `sync`, `prune`, `delete`) support `--json`
for structured output. See [`HANDOFF.md`](HANDOFF.md#command-surface) for the
full reference.

```bash
python3 -m spindlebot check                          # validate config + environment
python3 -m spindlebot import <dir|.log> [--force]    # import one album (--force skips disc wait)
python3 -m spindlebot import-staging [--dry-run]     # import everything in the Import area
python3 -m spindlebot finalize [--dry-run]           # retry lyrics + promote lyric-complete albums
python3 -m spindlebot inventory [--location <name>]  # scan a location into the DB
python3 -m spindlebot review --location <name>       # plan reconciliation (no bytes moved)
python3 -m spindlebot sync [--location <name>]       # execute acknowledged copies
python3 -m spindlebot prune [--execute]              # release Pending copies verified on retention
python3 -m spindlebot fetch-art  <dir> [--dry-run] [--force]
python3 -m spindlebot fetch-lyrics <dir> [--dry-run] [--force]
python3 -m spindlebot collection-audit [--handle <name>]  # what's on the shelf but not ripped
python3 -m spindlebot restart                        # restart the launchd agents
```

`prune` and `delete` default to **dry-run** — pass `--execute` to touch bytes.

## Collection audit (optional)

Compares a collection you already maintain elsewhere against the digital
library and lists the discs you own but haven't ripped. Entirely assistive — it
reads the library and writes nothing but a fetch cache. Unconfigured, it simply
doesn't run.

```bash
python3 -m spindlebot collection-audit --handle your-discogs-handle
```

Put the handle in `config.toml` under `[collection]` and the flag becomes
optional; `--handle` always overrides it.

| Flag | Effect |
|------|--------|
| `--source discogs\|fixture` | Where the collection comes from |
| `--media cd,vinyl` | Which media count as rippable (default `cd`, which includes CDrs) |
| `--index auto\|beets\|db` | Which view of the library to compare against (default `auto`, the union — see below) |
| `--refresh` | Re-fetch instead of using the cached collection |
| `--strict` | Treat uncertain matches as missing |
| `--all` | Also list what you already own |
| `--json` | Structured output |

**Discogs** needs no credentials for a public collection. A personal access
token in `secrets.toml` (`[discogs] token`) raises the API rate limit from 25 to
60 requests/minute and is required for a private collection.

**Adding another source** means writing one provider: an impure client that
fetches, and a pure transformer that maps its payload to `CollectionItem`. The
`fixture` provider — a JSON file you write by hand — is a working example, and
doubles as the way in for anyone not on Discogs.

Results land in three buckets, not two. `uncertain` exists so that a
normalization miss sends you to check a row rather than to re-rip a disc you
already own.

### Which library index?

Neither backend is a complete picture, and they go stale in opposite directions:

| Index | Knows about | Blind to |
|-------|-------------|----------|
| `beets` | Everything it imported and still tracks | Albums that reached a drive without a `beet import` |
| `db` | Everything `spindlebot inventory` has scanned at any location | A fresh import that hasn't synced yet |

Measured on a real library: 67 albums existed **only** in the SpindleBot DB and
2 existed **only** in beets. So `auto` (the default) unions them — an album
counts as owned if either index knows it, which can only ever shrink the missing
list. Every run prints the breakdown (`library (beets 112, db 177) — 176 unique
album(s)`), because when an album is wrongly reported missing, the index is the
first suspect.

If both indexes come back empty the audit **fails** rather than reporting your
entire collection as missing.

## Scripts

| Script | Role |
|--------|------|
| `music-watcher.sh` | fswatch daemon; fires `python3 -m spindlebot import` on a `.log` or folder drop (installed to `~/.local/bin/`) |
| `music-import.sh` | shim → `python3 -m spindlebot import` |
| `music-sync.sh` | content-addressed sync to the retention drive; fires on drive mount |
| `music-notify.sh` | legacy notify shim (superseded by `stages/notify.py`) |
| `setup.sh` | one-time environment setup; installs the watcher and launchd agents |
| `setup-ai.sh` | optional; builds the AI venv for lyric re-timing |

The daemons run under launchd: `com.strangestlens.music-watcher` (import) and
`com.strangestlens.music-sync` (sync). Bounce them with `python3 -m spindlebot restart`.

## Beet path template

```
Single disc:  Artist/Album/NN. Title.flac
Multi-disc:   Artist/Album [Disk N]/NN. Title.flac
```

Multi-disc is determined by the **actual ripped disc count**, not MusicBrainz
`disctotal` — this avoids false positives from DualDiscs and deluxe editions.

## Known gotchas

- **Apostrophes in paths** break BSD `xargs` — always use `while IFS= read -r`.
- **posttag must run last** — beet re-adds alias tags on write; posttag cleans them.
- **`multidisc` flex attr** must be INSERTed via `sqlite3`; `beet modify multidisc=`
  deletes the row and breaks the template. Handled in `runner.py`.
- **Partial disc imports** (only disc 1 of 2) wait forever for disc 2 — use
  `python3 -m spindlebot import --force` to bypass, or patch `disctotal` in the FLACs first.
- **FLAC lyrics tags** are lowercase and ignored by some DAPs (e.g. Snowsky) — the
  pipeline writes `.lrc` sidecar files instead.

The full, current gotcha list lives in [`CLAUDE.md`](CLAUDE.md#known-gotchas).

## Lyrics playback and editing

### Quick playback check

`mpv` picks up `.lrc` sidecars automatically and shows them as subtitles over art:

```bash
mpv "/Volumes/<RetentionDrive>/Music/Library/Artist/Album/track.flac"
```

### lrc-editor — visual timestamp editor

A standalone browser-based waveform editor for adjusting lyric timing:

```bash
./lrc-editor "/Volumes/<RetentionDrive>/Music/Library/Artist/Album/track.flac"
```

- Loads the `.lrc` sidecar alongside the FLAC automatically (creates a working
  copy if none exists; **Commit** writes the file).
- Opens in the browser with a waveform and draggable timestamp markers.
- Toolbar flow: **Save Draft** → **Preview in mpv** → **Commit**.
- **Audit** and **AI Arrange** drive the optional AI timing subsystem — see
  [AI lyric timing](#ai-lyric-timing-optional) below.

#### Editing

| Action | How |
|--------|-----|
| Select line | Click row or marker; `[`/`]` to step |
| Edit text | `e` or `Enter`, or double-click |
| Add line | `a` — inserts at current playback position |
| Delete line | `Delete`/`Backspace` — confirm, then gone |
| Adjust timestamp | Drag the marker, or nudge with `,`/`.` |
| Undo | `Ctrl+Z` |

#### Keyboard shortcuts

| Key | Action | Key | Action |
|-----|--------|-----|--------|
| `Space` | Play / Pause | `[` / `]` | Prev / Next marker |
| `m` | Mute toggle | `,` / `.` | Nudge marker ±0.1s |
| `Home` | Jump to start | `Shift+,` / `Shift+.` | Nudge marker ±1s |
| `←` / `→` | Seek ±1s | `a` | Add line at position |
| `Ctrl+←` / `Ctrl+→` | Seek ±5s | `e` / `Enter` | Edit selected line |
| `Shift+←` / `Shift+→` | Fine seek ±0.1s | `Delete` / `Backspace` | Delete selected line |
| | | `Ctrl+Z` | Undo |
| | | `?` | Shortcut reference |

**Confirm dialogs:** `Enter`/`y` confirm, `Escape`/`n` cancel.
**Help modal:** `Escape`, `Space`, `Enter`, or `?` to close.

### AI lyric timing (optional)

lrclib doesn't always have *synced* lyrics. When it only has plain text, the
fetch stage still writes an `.lrc` — but every line carries `[00:00.00]`, which
is useless for playback. The `lyric_timing/` package fixes those files: it finds
them (`audit`) and re-times them against the audio by forced alignment
(`retime`).

It's a **peer package to `spindlebot/`, not part of it.** The heavy dependencies
(torch, torchaudio, demucs) live in a dedicated venv so the core pipeline stays
light and CI never loads a model.

#### Setup

```bash
./setup-ai.sh    # builds ~/.local/share/spindlebot/ai-venv (override: $SPINDLEBOT_AI_VENV)
```

Idempotent; a previously working venv is restored if every install attempt
fails. Models (~700 MB: Demucs `htdemucs` ~300 MB + wav2vec2 ~360 MB) download to `~/.cache` on
the first alignment run, not during setup.

`audit` needs none of this — it's pure text heuristics and runs on a bare
`python3`.

#### audit — find the mistimed files

```bash
python3 -m lyric_timing audit <dir-or-.lrc ...> [--json]
```

Recurses directories for `.lrc` files and flags the suspicious ones:

| Reason | Meaning |
|--------|---------|
| `all-timestamps-identical` | every line at the same time (the `[00:00.00]` case) |
| `low-distinct-timestamps` | under 30% distinct times — bulk-stamped, not individually timed |
| `timestamps-crammed-early` | all lyrics land in the first half of the track *and* are tightly bunched |
| `non-monotonic` | timestamps go backwards in file order |
| `no-timed-lines` | has lyric content but no parseable timestamps |

The heuristics are tuned to avoid false-positiving hand-timed songs: a
well-spread file that simply ends early (long instrumental tail, a lone
`[Instrumental]` marker) is left alone. Track duration comes from the sibling
audio file via mutagen when present; without it the crammed-early check is
skipped.

#### retime — fix one file

```bash
~/.local/share/spindlebot/ai-venv/bin/python -m lyric_timing retime \
    <audio> <lrc> [--overwrite] [--json] [--no-vocal-sep]
```

Run it **from the repo root** so `lyric_timing` is importable by the venv's
Python. It keeps the lyric *text* exactly as-is and only recomputes timestamps:

1. Demucs (`htdemucs`) isolates the vocal stem — skip with `--no-vocal-sep`
   (faster, notably worse on dense mixes).
2. wav2vec2 CTC forced alignment over 30-second windows produces word
   timestamps. Windowing is what keeps peak memory flat in track length.
3. Words are matched to lyric lines positionally, so a repeated chorus line
   resolves to its own occurrence instead of all snapping to the first.
4. Unmatched or low-confidence lines are filled by interpolation between
   confident anchors; times are then forced monotonic and clamped to the track.

Parenthetical ad-libs (`walk away (walk away)`) are stripped for the alignment
pass only — they're backing-vocal echoes that overlap the lead and distort
neighbouring lines. Output keeps the original text.

Non-destructive by default: the new LRC goes to stdout. `--overwrite` writes it
back; `--json` emits `[{time, text, confidence}]` instead. Anything the model
stack prints while working (Demucs progress) goes to stderr, so stdout stays
parseable.

#### In the editor

**Audit** (toolbar) opens `/audit`: pick a folder and an output JSON path with
the native macOS pickers, hit **Run Audit**, and get a table of just the
suspicious files with their reasons and line counts. **Edit** on any row loads
that track straight into the editor. Both paths and the last results are
remembered in `~/.config/spindlebot/lrc-editor-state.json`, so reopening the
page restores the previous run.

**AI Arrange** runs `retime` on the currently loaded track, using the lyric text
already in the editor (lay out or import the text first — it aligns text, it
doesn't transcribe). It runs as a `nice`'d, memory-capped background subprocess
of the AI venv and can take a few minutes.

When it lands:

- New times are applied through the undo stack — `Ctrl+Z` reverts the whole
  arrangement.
- Deliberate empty-text markers (e.g. one bounding an instrumental outro) keep
  their manual times.
- Markers below 0.5 confidence turn **orange**. That's the model flagging its
  own weak spots — nudge those by hand.
- **Nothing auto-saves.** **Commit** still writes the file.

#### Accuracy

Benchmarked against a hand-timed album: mean error 0.35s and 0.59s on two
tracks, 94% and 100% of lines within 1s. Known weak spots — repeated outro
chants, long instrumental interludes, vocals buried in dense mixes — generally
self-report as low confidence rather than failing silently.
