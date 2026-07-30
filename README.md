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
CD → XLD → Import area                          DwRugged mounted
        │                                               │
  [.log written / folder dropped]                 launchd fires
        │                                       music-sync-rugged.sh
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
  │ archive log    │ → ~/Music/All Discs/
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
| `~/Music/All Discs/` | Archived XLD `.log` files |
| `/Volumes/DwRugged/Music/Library/` | Retention library on the external drive |
| `~/.config/spindlebot/config.toml` + `secrets.toml` | SpindleBot config + credentials |
| `~/.config/spindlebot/spindlebot.db` | SpindleBot's content-identity + location DB |
| `~/.config/beets/config.yaml` | beets config (`directory:` must match the Pending area) |
| `~/.config/beets/library.db` | beets item DB |
| `~/.config/beets/watcher.log` | import pipeline log |
| `~/.config/beets/rugged-sync.log` | sync pipeline log |

## Commands

Everything is `python3 -m spindlebot <command>`; all support `--json`. See
[`HANDOFF.md`](HANDOFF.md#command-surface) for the full reference.

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
python3 -m spindlebot restart                        # restart the launchd agents
```

`prune` and `delete` default to **dry-run** — pass `--execute` to touch bytes.

## Scripts

| Script | Role |
|--------|------|
| `music-watcher.sh` | fswatch daemon; fires `spindlebot import` on a `.log` or folder drop (installed to `~/.local/bin/`) |
| `music-import.sh` | shim → `spindlebot import` |
| `music-sync-rugged.sh` | content-addressed sync to DwRugged; fires on drive mount |
| `music-notify.sh` | legacy notify shim (superseded by `stages/notify.py`) |
| `setup.sh` | one-time environment setup; installs the watcher and launchd agents |

The daemons run under launchd: `com.strangestlens.music-watcher` (import) and
`com.strangestlens.music-sync-rugged` (sync). Bounce them with `spindlebot restart`.

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
- **`multidisc` flex attr** must be INSERTed via `sqlite3`; `beet modify
  multidisc=` deletes the row and breaks the template. Handled in `runner.py`.
- **Partial disc imports** (only disc 1 of 2) wait forever for disc 2 — use
  `spindlebot import --force` to bypass, or patch `disctotal` in the FLACs first.
- **FLAC lyrics tags** are lowercase and ignored by some DAPs (e.g. Snowsky) — the
  pipeline writes `.lrc` sidecar files instead.

The full, current gotcha list lives in [`CLAUDE.md`](CLAUDE.md#known-gotchas).

## Lyrics playback and editing

### Quick playback check

`mpv` picks up `.lrc` sidecars automatically and shows them as subtitles over art:

```bash
mpv "/Volumes/DwRugged/Music/Library/Artist/Album/track.flac"
```

### lrc-editor — visual timestamp editor

A standalone browser-based waveform editor for adjusting lyric timing:

```bash
./lrc-editor "/Volumes/DwRugged/Music/Library/Artist/Album/track.flac"
```

- Loads the `.lrc` sidecar alongside the FLAC automatically (creates a working
  copy if none exists; **Commit** writes the file).
- Opens in the browser with a waveform and draggable timestamp markers.
- Toolbar flow: **Save Draft** → **Preview in mpv** → **Commit**.

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
