# Music Pipeline

Automated CD ripping pipeline for Daniel's music library.

## Flow

```
CD → XLD → ~/Music/Staging/
                    │
              [XLD writes .log]
                    │
              fswatch detects .log
                    │
          music-import.sh fires
                    │
          ┌─────────┴──────────┐
          │  1. pretag         │  music-pretag.py (feat., artist cleanup)
          │  2. beet import    │  MusicBrainz match → ~/Music/Library/
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
          │  4. update DB      │  sqlite: ~/Music/Library paths → DwRugged paths
          │  5. fetch lyrics   │  catch anything import missed (race condition safety net)
          │  6. notify         │  macOS + Telegram
          └────────────────────┘
```

## Key paths

| Path | Purpose |
|------|---------|
| `~/Music/Staging/` | XLD rips here; cleared after import |
| `~/Music/Library/` | Beet's canonical library; transient (emptied after sync) |
| `~/Music/All Discs/` | Archived XLD .log files |
| `/Volumes/DwRugged/Music/Library/` | Permanent library on external drive |
| `~/.config/beets/config.yaml` | Beet config (directory: ~/Music/Library) |
| `~/.config/beets/watcher.log` | Import pipeline log |
| `~/.config/beets/rugged-sync.log` | Sync pipeline log |

## Scripts

| Script | Role |
|--------|------|
| `music-import.sh` | Main import pipeline; triggered by fswatch on Staging |
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
- **Partial disc imports** (only have disc 1 of 2): patch `disctotal=1` in Staging FLACs before pipeline runs, otherwise it waits forever for disc 2
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
- Opens in the browser with a waveform and draggable timestamp markers
- Drag markers left/right to adjust timing
- Toolbar: **Save Draft** → **Preview in mpv** → **Commit** (overwrites original `.lrc`)

#### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Space` | Play / Pause |
| `m` | Mute toggle |
| `Home` | Jump to start |
| `←` / `→` | Seek ±1s |
| `Ctrl+←` / `Ctrl+→` | Seek ±5s |
| `Shift+←` / `Shift+→` | Fine seek ±0.1s |
| `[` / `]` | Prev / Next marker (selects it) |
| `,` / `.` | Nudge selected marker ±0.1s |
| `Shift+,` / `Shift+.` | Nudge selected marker ±1s |
| `Ctrl+Z` | Undo |
| `?` | Keyboard shortcut reference |

Click a marker or lyric row to select it (cyan highlight), then nudge with `,`/`.`.
