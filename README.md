# Music Pipeline

Automated CD ripping pipeline for Daniel's music library.

## Flow

```
CD → XLD → Import area
                    │
              [XLD writes .log]
                    │
              fswatch detects .log
                    │
          music-import.sh fires
                    │
          ┌─────────┴──────────┐
          │  1. pretag         │  music-pretag.py (feat., artist cleanup)
          │  2. beet import    │  MusicBrainz match → Pending area
          │  3. multidisc fix  │  based on actual ripped discs, not MB metadata
          │  4. beet move      │  rename files to match path template
          │  5. fetch lyrics   │  music-fetch-lyrics.py → .lrc sidecar files
          │  6. notify         │  macOS + Telegram
          │  7. archive log    │  → ~/Music/All Discs/
          └────────────────────┘
                    │
            [DwRugged mounted]
                    │
          music-sync-rugged.sh fires
                    │
          ┌─────────┴──────────┐
          │  1. fetch art      │  music-fetch-art.py (CAA → iTunes fallback)
          │  2. posttag        │  music-pretag.py --post (date trunc, alias cleanup)
          │  3. rsync          │  --remove-source-files → /Volumes/DwRugged/Music/Library/
          │  4. update DB      │  sqlite: Pending-area paths → DwRugged paths
          │  5. fetch lyrics   │  catch anything import missed (race condition safety net)
          │  6. notify         │  macOS + Telegram
          └────────────────────┘
```

## Key paths

| Path | Purpose |
|------|---------|
| `~/Library/Application Support/SpindleBot/Import/` | Import area: XLD rips / downloads land here; cleared after import (formerly `~/Music/Staging`) |
| `~/Library/Application Support/SpindleBot/Pending/` | Pending area: beet's canonical dir; transient, emptied after sync (formerly `~/Music/Library`) |
| `~/Music/All Discs/` | Archived XLD .log files |
| `/Volumes/DwRugged/Music/Library/` | Permanent library on external drive |
| `~/.config/beets/config.yaml` | Beet config (directory: must match the Pending area) |
| `~/.config/beets/watcher.log` | Import pipeline log |
| `~/.config/beets/rugged-sync.log` | Sync pipeline log |

## Scripts

| Script | Role |
|--------|------|
| `music-import.sh` | Main import pipeline; triggered by fswatch on the Import area |
| `music-sync-rugged.sh` | Sync to DwRugged; triggered on drive mount or `sync` command |
| `music-pretag.py` | Tag cleanup pre-import (`--post` mode for final cleanup before sync) |
| `music-fetch-art.py` | Album art fetcher (runs before sync) |
| `music-fetch-lyrics.py` | LRC sidecar fetcher (lrclib.net, falls back to embedded tag) |
| `music-notify.sh` | macOS + Telegram notifications |
| `music-pipeline` | start/stop/restart/status for fswatch watcher daemon |

## Beet path template

```
Single disc:  Artist/Album/NN. Title.flac
Multi-disc:   Artist/Album [Disk N]/NN. Title.flac
```

Multi-disc is determined by actual ripped disc count, NOT MusicBrainz `disctotal`
(avoids false positives from DualDiscs, deluxe editions, etc.).

## Known gotchas

- **Apostrophes in paths** break BSD xargs — always use `while IFS= read -r` instead
- **posttag must run last** — beet writes re-add alias tags, posttag cleans them
- **beet fetchart** fails after sync (DB paths point to DwRugged, art script can't find files) — use `music-fetch-art.py` instead, runs before sync
- **FLAC lyrics tags** are lowercase (`lyrics`/`unsyncedlyrics`) — Snowsky Disc ignores them; use `.lrc` sidecar files
- **Partial disc imports** (only have disc 1 of 2): patch `disctotal=1` in the Import-area FLACs before pipeline runs, otherwise it waits forever for disc 2
- **Hidden track albums** (e.g. Tool/Undertow): beet will import silence tracks — see RERIP.md for cleanup procedure

## Sync command

```bash
bash ~/.local/bin/music-sync-rugged.sh
```

Or just say "sync" — Ash knows what to do.

## Lyrics editing workflow

### Quick playback check
mpv picks up `.lrc` sidecar files automatically and displays them as subtitles over album art.

```bash
mpv "/Volumes/DwRugged/Music/Library/Artist/Album/track.flac"
```

Or just tell Ash "pull up [song] by [artist]" and it'll find the path and launch mpv.

### lrc-editor — visual timestamp editor

Full browser-based waveform editor for adjusting lyric timestamps.

```bash
lrc-editor "/Volumes/DwRugged/Music/Library/Artist/Album/track.flac"
```

- Looks for a `.lrc` sidecar alongside the FLAC automatically
- If no `.lrc` exists, a working copy is created on open; Commit writes the new file
- Opens in the browser with a waveform and draggable timestamp markers
- Drag markers left/right to adjust timing
- Toolbar: **Save Draft** → **Preview in mpv** → **Commit** (overwrites/creates `.lrc`)

#### Editing lyrics

| Action | How |
|--------|-----|
| Select line | Click row or marker; use `[`/`]` to step through |
| Edit text | `e` or `Enter` on selected line, or double-click |
| Add line | `a` — inserts at current playback position, opens text editor |
| Delete line | `Delete`/`Backspace` — confirm dialog, then gone |
| Adjust timestamp | Drag marker on waveform, or nudge with `,`/`.` |
| Undo | `Ctrl+Z` — all edits (add, delete, text change, nudge, drag) |

#### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Space` | Play / Pause |
| `m` | Mute toggle |
| `Home` | Jump to start |
| `←` / `→` | Seek ±1s |
| `Ctrl+←` / `Ctrl+→` | Seek ±5s |
| `Shift+←` / `Shift+→` | Fine seek ±0.1s |
| `[` / `]` | Prev / Next marker |
| `,` / `.` | Nudge selected marker ±0.1s |
| `Shift+,` / `Shift+.` | Nudge selected marker ±1s |
| `a` | Add line at current position |
| `e` / `Enter` | Edit selected line text |
| `Delete` / `Backspace` | Delete selected line |
| `Ctrl+Z` | Undo |
| `?` | Keyboard shortcut reference |

**In confirm dialogs:** `Enter`/`y` to confirm, `Escape`/`n` to cancel.  
**In help modal:** `Escape`, `Space`, `Enter`, or `?` to close.
