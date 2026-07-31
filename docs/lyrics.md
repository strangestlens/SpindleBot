# Lyrics

SpindleBot writes synced lyrics as `.lrc` **sidecar files** next to the audio,
not as FLAC tags. FLAC lyrics tags are lowercase and silently ignored by some
DAPs (the Snowsky, for one); a sidecar is read by everything, including mpv and
Navidrome.

Every track ends up with one of two terminal markers:

| Marker | Meaning |
|--------|---------|
| `<track>.lrc` | Synced lyrics were found and written |
| `<track>.nolrc` | Looked, found nothing — don't look again |

An album is **lyric-complete** once every track has one or the other, and only
then is it promoted from Processing to Pending. That gate is what makes Pending
safe to sync and prune from. See [architecture](architecture.md#why-processing-exists).

lrclib is currently the only source (`[lyrics] sources = ["lrclib"]`).

## Quick playback check

`mpv` picks up `.lrc` sidecars automatically and shows them as subtitles over
the album art:

```bash
mpv "/Volumes/<RetentionDrive>/Music/Library/Artist/Album/track.flac"
```

## Re-fetching

```bash
python3 -m spindlebot fetch-lyrics <album_dir> [--dry-run] [--force]
```

`--force` re-fetches tracks that already have a terminal marker; without it,
existing `.lrc` and `.nolrc` files are left alone. To sweep every album still
stuck in Processing on a transient lyric error, use
[`spindlebot finalize`](commands.md#import).

## lrc-editor — visual timestamp editor

A standalone browser-based waveform editor for adjusting lyric timing:

```bash
./lrc-editor "/Volumes/<RetentionDrive>/Music/Library/Artist/Album/track.flac"
```

- Loads the `.lrc` sidecar alongside the FLAC automatically (creates a working
  copy if none exists; **Commit** writes the file).
- Opens in the browser with a waveform and draggable timestamp markers.
- Toolbar flow: **Save Draft** → **Preview in mpv** → **Commit**.
- **Audit** and **AI Arrange** drive the optional AI timing subsystem — see
  [AI lyric timing](ai-lyric-timing.md).

Like [`collection-browser`](collection-audit.md#collection-browser--click-to-ignore-ui),
it lives outside `spindlebot/` on purpose, so the pipeline package never takes a
Flask dependency.

### Editing

| Action | How |
|--------|-----|
| Select line | Click row or marker; `[`/`]` to step |
| Edit text | `e` or `Enter`, or double-click |
| Add line | `a` — inserts at current playback position |
| Delete line | `Delete`/`Backspace` — confirm, then gone |
| Adjust timestamp | Drag the marker, or nudge with `,`/`.` |
| Undo | `Ctrl+Z` |

### Keyboard shortcuts

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

## Bidirectional lyric sync (in progress)

Once a `.lrc` exists in more than one place — Pending, the retention drive, a
DAP — edits can happen anywhere, and "which copy is newest" stops being obvious.
Phase 4 of the content-addressed epic builds causal lineage per lyric document
so the reconciler can tell a linear edit from a genuine concurrent conflict.
The substrate (schema v7, `services/lyrics_sync.py`) is merged; automatic
propagation and a `conflicts` CLI are not. See
[`CLAUDE.md`](../CLAUDE.md) → "Content-addressed library refactor" for status.
