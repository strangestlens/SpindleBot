# SpindleBot — Music Pipeline

Event-driven pipeline for ripping, tagging, and managing a lossless (FLAC) music
library on macOS. It matches rips against MusicBrainz via beets, fetches album
art and synced lyrics, and distributes lyric-complete albums to a retention
drive — backed by a SQLite database that is the system of record for content
**identity** and every **location** a copy lives.

Nothing polls. Two events start work: a rip finishing, and the retention drive
being mounted.

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

## Quickstart

```bash
git clone https://github.com/strangestlens/SpindleBot.git ~/Music/music-pipeline
cd ~/Music/music-pipeline
./setup.sh
$EDITOR ~/.config/spindlebot/config.toml    # at minimum: your [[destinations]]
python3 -m spindlebot check
```

Full walkthrough, including the one-time inventory each destination needs:
[docs/getting-started.md](docs/getting-started.md).

## Working areas

| Area | Role |
|------|------|
| **Import** | XLD rips and downloads land here for processing |
| **Processing** | In-flight albums; art and lyrics are fetched here |
| **Pending** | Lyric-complete albums awaiting distribution |
| **Duplicates** | Rips already in the library are parked here, not stranded in Import |

Defaults live under `~/Library/Application Support/SpindleBot/`; every path,
including the retention drive and archive dir, is
[configuration](docs/configuration.md), not a fixed location. Examples
throughout these docs use a drive named `DwRugged` — that's the author's; use
your own.

An album is promoted from Processing to Pending **only once every track has a
terminal `.lrc`/`.nolrc` marker**. Pending is therefore complete-by-construction,
so a mount-sync can never prune audio out from under a running lyric fetch.
Albums stuck in Processing are swept up by `spindlebot finalize`.
[Why this exists →](docs/architecture.md#why-processing-exists)

## Commands

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
The DB and sync commands all support `--json`. Full reference:
[docs/commands.md](docs/commands.md).

## Also in here

**[Lyrics](docs/lyrics.md)** — synced lyrics are written as `.lrc` sidecars, not
FLAC tags (tags are lowercase and ignored by some DAPs). `mpv` renders them
automatically; `./lrc-editor <track.flac>` opens a waveform editor for fixing
timing.

**[AI lyric timing](docs/ai-lyric-timing.md)** *(optional)* — when lrclib has
only plain text, every line arrives stamped `[00:00.00]`. The `lyric_timing/`
peer package finds those files and re-times them against the audio by forced
alignment (Demucs + wav2vec2 CTC). Heavy deps stay in their own venv, out of the
core pipeline and CI.

**[Collection audit](docs/collection-audit.md)** *(optional)* — compares a
Discogs collection against the digital library and lists what you own but
haven't ripped. `./collection-browser` serves the same report with
click-to-ignore. Purely assistive; unconfigured, it doesn't run.

## Documentation

| Doc | What's in it |
|-----|--------------|
| [Getting started](docs/getting-started.md) | Prerequisites, `setup.sh`, first config, validation, first import |
| [Configuration](docs/configuration.md) | `config.toml` by section, secrets, env precedence, sync destinations |
| [Commands](docs/commands.md) | Full CLI reference, and what each destructive op is gated on |
| [Operations](docs/operations.md) | Daemons, logs, what a sync run does, troubleshooting |
| [Architecture](docs/architecture.md) | The flows, the working areas, content addressing, stage sequence |
| [Development](docs/development.md) | Tests, CI, linting, branch and PR workflow |
| [`CLAUDE.md`](CLAUDE.md) | The contract for changing the code: layering, conventions, gotchas |
| [`ROADMAP.md`](ROADMAP.md) | Where it's going |
| [`CHANGELOG.md`](CHANGELOG.md) | What's shipped |

## Status

Pre-1.0. The import pipeline and the content-addressed mount-sync are in daily
use; the active line of work is bidirectional lyric sync across locations. See
[`CHANGELOG.md`](CHANGELOG.md) for what's landed and [`CLAUDE.md`](CLAUDE.md)
for per-phase detail.

Requires macOS on Apple Silicon, Python 3.11+, and beets.
