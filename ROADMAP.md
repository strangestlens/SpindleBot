# SpindleBot — Development Roadmap

*Last updated: March 2026*

---

## Vision

SpindleBot is a personal music intelligence system. The immediate goal is a clean, well-tested, configurable pipeline that anyone can run. The longer-term goal is a complete local-first music management platform with remote streaming access — organized, resilient, and extensible.

---

## Priority Map

| Priority | Area | Current State |
|----------|------|---------------|
| **1** | Configuration & portability | Hardcoded paths and credentials everywhere |
| **2** | Modular architecture + testing | Two monolithic shell scripts with duplicated logic |
| **3** | Destination flexibility | Single external drive, wired into the code |
| **4** | Error recovery & resilience | Log-and-continue, no retries |
| **5** | Input sources | XLD/CD only |
| **6** | Remote library access | Not yet addressed |
| — | macOS-only architecture | Acceptable; future Mac App potential |

---

## Phase 1: Configuration & Portability

**Goal:** Zero hardcoded values. The system should run from a config file and work on any Mac without code changes.

### Config File

Introduce `~/.config/spindlebot/config.toml` (or `XDG_CONFIG_HOME`). All environment-specific values move here:

```toml
[core]
library_dir     = "~/Music/Library"
staging_dir     = "~/Music/Staging"
log_dir         = "~/Music/logs"
archive_dir     = "~/Music/All Discs"

[tools]
beet            = "/opt/homebrew/bin/beet"
python          = "/opt/homebrew/bin/python3"
mpv             = "/opt/homebrew/bin/mpv"
beets_config    = "~/.config/beets/config.yaml"

[notifications]
telegram_bot_token = ""       # leave empty to disable
telegram_chat_id   = ""
macos_notify       = true

[lyrics]
request_delay_seconds = 0.3
sources               = ["lrclib", "shazam"]  # ordered preference

[art]
sources       = ["caa", "itunes"]
min_size_px   = 500
```

Credentials (Telegram token, Genius key, etc.) should additionally support reading from environment variables, prefixed `SPINDLEBOT_`, so they can be injected in CI or Docker without touching the file.

### Beets Config Templating

Currently `beets-config.yaml` has hardcoded paths. Replace with a generated config: `spindlebot beets-config --write` renders from the `[core]` and `[tools]` sections. The Genius API key moves to the credentials block.

### Config Validation

On every run, validate that:
- All required paths exist
- Tool binaries are executable
- Required API credentials are present (warn, don't fail, for optional ones)

Print a clear human-readable error when something is wrong rather than failing silently mid-pipeline.

---

## Phase 2: Modular Architecture & Testing

**Goal:** Replace two monolithic shell scripts with a collection of discrete, testable Python modules. This is the most impactful change and touches everything else.

### Package Structure

```
spindlebot/
├── __main__.py             # CLI entry point: `python -m spindlebot`
├── config.py               # Config loading, validation, schema
├── pipeline/
│   ├── runner.py           # Orchestrates stage execution
│   ├── stages/
│   │   ├── pretag.py       # Pre-import tag cleanup (was music-pretag.py)
│   │   ├── beet_import.py  # beet import wrapper
│   │   ├── posttag.py      # Post-import DB fixes + tag normalization
│   │   ├── fetch_art.py    # Album art (was music-fetch-art.py)
│   │   ├── fetch_lyrics.py # LRC lyrics (was music-fetch-lyrics.py)
│   │   ├── sync.py         # Sync to destination(s)
│   │   └── notify.py       # Notifications (was music-notify.sh)
├── watchers/
│   ├── staging.py          # Watches staging dir for .log files
│   └── destination.py      # Watches for drive/NAS mount events
├── sources/
│   ├── base.py             # AbstractInputSource
│   ├── xld.py              # XLD rip + .log detection (current behavior)
│   └── digital.py          # Digital download folder watcher (Phase 5)
├── destinations/
│   ├── base.py             # AbstractDestination
│   ├── local_drive.py      # rsync to mounted external drive
│   └── rclone.py           # rclone to any remote (B2, S3, NAS, etc.)
├── lyrics/
│   ├── lrclib.py           # lrclib.net (timestamped)
│   ├── shazam.py           # Shazam (plain text, needs alignment)
│   └── aligner.py          # Text alignment: plain → timestamped LRC
└── cli.py                  # `spindlebot` commands
```

### CLI

```bash
spindlebot start          # Start all watchers (replaces music-pipeline start)
spindlebot stop
spindlebot status

spindlebot import <path>  # Manually trigger import for a staging folder
spindlebot sync           # Manually trigger sync

# Run individual stages for debugging
spindlebot run pretag <path>
spindlebot run fetch-lyrics <album>
spindlebot run fetch-art <album>

spindlebot check          # Validate config, check tool paths, test API credentials
```

### Testing Strategy

Each stage should be independently testable with mocked external calls:

- **pretag / posttag**: Pure in-process logic — test directly with fixture FLAC files. No mocking needed.
- **beet_import**: Wrap the beet subprocess call; mock it in tests. Test the pre/post logic, not beet itself.
- **fetch_art / fetch_lyrics**: Mock the HTTP clients. Test fallback chains, retry behavior, cache marker logic.
- **sync**: Mock the rsync subprocess and sqlite3 calls.

Use `pytest` with `tmp_path` fixtures so tests never touch the real library. Run with `pytest -x --tb=short` in CI.

Target: 80%+ coverage on the `stages/` and `lyrics/` modules. The watchers and CLI can be integration-tested with a local staging fixture.

### Shell Scripts → Wrappers Only

`music-pipeline`, `music-import.sh`, `music-sync-rugged.sh` become thin stubs that call `python -m spindlebot`. Keep them during the transition so launchd plists don't need to change immediately.

---

## Phase 3: Destination Flexibility

**Goal:** Sync targets are defined in config, not code. Add one line to point at a new destination.

### Architecture

`AbstractDestination` defines `sync(source_path, album_paths) -> SyncResult`. Each implementation handles its own transport.

Config drives the list:

```toml
[[destinations]]
name    = "DwRugged"
type    = "local_drive"
path    = "/Volumes/DwRugged/Music/Library"
enabled = true

[[destinations]]
name    = "Backblaze"
type    = "rclone"
remote  = "b2:my-music-bucket/Library"
enabled = true

[[destinations]]
name    = "HomeNAS"
type    = "rclone"
remote  = "sftp:nas.local:/media/music"
enabled = false
```

The sync stage iterates enabled destinations in parallel (or sequentially — configurable). DB path reconciliation runs per destination.

### On iCloud

iCloud Drive is **not suitable** for a large lossless library. Apple's iCloud Music Library imposes a 200 MB per-file limit and converts unmatched tracks to 256 kbps AAC — both fatal for FLAC. As a generic file sync destination, iCloud has no FLAC-specific limitations, but sync reliability for directories of large files at scale is poor.

**Better options for cloud backup:**
- **Backblaze B2**: $6/TB/month (storage) + $0.01/GB egress. Rclone native support. The right choice for archival backup.
- **Wasabi**: $6.99/TB/month, zero egress fees. Good if you'll restore frequently.
- **AWS S3 Glacier Instant Retrieval**: ~$4/TB/month. Slower restores but cheapest at scale.

For a 1–2 TB FLAC library, Backblaze B2 + rclone is the pragmatic answer. Rclone can sync incrementally, verify checksums, and has a dry-run mode. Configure once in `rclone.conf`, reference the remote in `config.toml`.

---

## Phase 4: Error Recovery & Resilience

**Goal:** Transient failures don't silently drop data. Every failure is recoverable.

### Retry Logic

Wrap all external API calls (lrclib, iTunes, CAA, Telegram) in a shared `retry_with_backoff(fn, attempts=3, base_delay=1.0)` helper. On HTTP 429 (rate limited), respect the `Retry-After` header if present.

```python
# Rough shape
def retry_with_backoff(fn, attempts=3, base_delay=1.0):
    for i in range(attempts):
        try:
            return fn()
        except TransientError as e:
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** i))
```

### Failure Queue

Introduce a lightweight failure journal: `~/.config/spindlebot/failed.jsonl`. Each failed stage appends a record:

```json
{"ts": "2026-03-27T04:12:00", "stage": "fetch_lyrics", "album": "Cocteau Twins/Heaven or Las Vegas", "error": "lrclib 503", "retries": 3}
```

`spindlebot retry-failed` replays the queue. `spindlebot status` reports the failure count. This replaces the current `lyrics-missing.log` approach with something machine-readable and actionable.

### Pre-flight Checks

Before any import run: verify the staging folder is readable, beet is available, and the library directory is writable. Before any sync: verify the destination is mounted/reachable and has sufficient space. Fail fast and loud rather than partway through.

### Atomic Moves

The current rsync `--remove-source-files` approach is already good — files only leave local storage after confirmed remote write. Keep this. Add a post-sync checksum verification pass (rclone's `check` subcommand does this natively).

---

## Phase 5: Input Sources

**Goal:** The pipeline accepts music from sources beyond physical CDs.

This can come after Phases 1–4, but the architecture in Phase 2 already anticipates it (`sources/` module). The main additions:

### Digital Downloads Watcher

Monitor a `~/Music/Downloads` staging folder. When new files appear (FLAC, ALAC, MP3, or a folder of tracks), run them through the same pretag → import → posttag → fetch-lyrics pipeline. Digital downloads usually already have correct metadata, so pretag acts in "gentle mode" — normalize rather than strip.

A simple heuristic: if a folder contains audio files with consistent `ALBUM`, `ARTIST`, and `DATE` tags, treat it as a complete album and import directly. If metadata is sparse, fall back to MusicBrainz search.

### Note on Digital Albums Already in Library

Albums you've previously imported manually and normalized are already in good shape — the pipeline should recognize them as already-imported (by MusicBrainz album ID in the beets DB) and skip rather than re-process.

---

## Phase 6: Remote Library Access

**Goal:** Access the full library from anywhere, on any device.

### Recommendation: Navidrome

Don't build this. Navidrome already exists and does exactly what's needed.

**What it is:** A lightweight, self-hosted music streaming server written in Go. Single binary, ~200 MB RAM for 40,000 tracks, Docker-deployable. Implements the Subsonic/OpenSubsonic API — which means 20+ client apps already work with it.

**Why it fits SpindleBot specifically:**
- It reads from a static music directory. SpindleBot already produces a well-organized, consistently-tagged library. Navidrome will index it cleanly.
- It surfaces the metadata SpindleBot works so hard to normalize (album artist, year, genre, MusicBrainz IDs).
- It supports `.lrc` sidecar files natively for synced lyrics — exactly the files SpindleBot generates.
- It's free, open-source, and actively maintained (v0.60.3, February 2026).

**Client apps worth knowing:**
- iOS/macOS: **Symfonium**, **Substreamer** — polished, CarPlay support
- Android: **Symfonium**, **DSub**
- Desktop: **Sonixd**, web UI built-in
- The Subsonic ecosystem is wide enough that there's a good client for every platform.

**Deployment for remote access:**
1. Run Navidrome locally pointing at the library on DwRugged (or NAS).
2. Expose via Tailscale or a reverse proxy (Caddy + DDNS) for remote access.
3. No data leaves your infrastructure. No subscription fee.

**What SpindleBot contributes to this setup:** The library Navidrome indexes is only as good as the metadata feeding it. SpindleBot's value — normalized tags, consistent album artist, embedded art, `.lrc` files — becomes the foundation Navidrome builds on. The two systems are naturally complementary.

**Alternative if Navidrome isn't enough:**
- **Jellyfin** is heavier but has official mobile apps and supports video too. Good if the library eventually includes video content.
- **Plex** with Plexamp is the premium option — excellent music UX, but requires Plex Pass ($120 lifetime or $5/month) for remote streaming.

---

## Mac App Vision (Longer-Term)

macOS-only architecture is fine for now and remains true to the platform SpindleBot was built for. The logical next step is a proper Mac app:

- SwiftUI wrapper with an embedded Python runtime (via [Python.framework](https://docs.python.org/3/using/mac.html))
- The pipeline stages run as background Python processes
- SwiftUI provides: config editor, pipeline status dashboard, import queue, re-rip list management, notification preferences
- Navidrome runs as a bundled service, launched on demand
- `launchd` plists become internal app infrastructure

This is a real path to the App Store. The core pipeline Python code doesn't change — only the surface around it does.

---

## Lyric Source Note: Shazam

Shazam is a good plain-text lyrics source and worth adding to the `lyrics/` module. The challenge is that it returns unsynced text, while lrclib returns timestamped LRC. The approach:

1. **Preferred path:** lrclib for synced LRC (keep as primary).
2. **Shazam fallback:** If lrclib returns nothing, fetch plain lyrics from Shazam.
3. **Alignment:** Use the `aligner.py` module to estimate timestamps. Strategy options:
   - Proportional distribution (current "Lay Out Lyrics" behavior in lrc-editor) — good enough for ambient/slow music, not for rapid lyrics.
   - Duration-weighted line distribution (longer lines → more time) — slightly better.
   - Eventual: use audio analysis (onset detection, silence gaps) to segment the track and align text to segments. This is genuinely hard but would be impressive.
4. The resulting LRC is marked as "estimated" (a non-standard comment line at the top: `[ti:Album Title] [re:spindlebot-estimated]`) so you can identify and manually fix in lrc-editor if desired.

---

## Summary: Work Order

```
Phase 1 — Config          ████████░░░░░░░░░░░░░░  Foundation — do this first
Phase 2 — Modular / Tests ████████████████░░░░░░  Biggest lift, highest payoff
Phase 3 — Destinations    ██████░░░░░░░░░░░░░░░░  Depends on Phase 1
Phase 4 — Error Recovery  ██████░░░░░░░░░░░░░░░░  Depends on Phase 2
Phase 5 — Input Sources   ████░░░░░░░░░░░░░░░░░░  After 1–4 are solid
Phase 6 — Navidrome       ████░░░░░░░░░░░░░░░░░░  Can run in parallel with 2–4
Mac App                   ██░░░░░░░░░░░░░░░░░░░░  Long-term; blocks on Phase 2
```

Phases 1 and 2 together are the real investment. Once the codebase is modular and tested, everything else — new destinations, new input sources, new lyrics providers, the Navidrome integration — becomes a matter of adding a module and a few config lines rather than editing shell scripts.
