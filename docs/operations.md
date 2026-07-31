# Operations

Running SpindleBot day to day: the daemons, the logs, what sync actually does,
and what to check when something looks wrong.

## The daemons

Two launchd agents, both installed and loaded by `setup.sh`:

| Agent | Fires on | Runs |
|-------|----------|------|
| `com.strangestlens.music-watcher` | fswatch seeing a `.log` write or folder drop in the Import area | `spindlebot import` |
| `com.strangestlens.music-sync` | `WatchPaths` on the retention drive mount point | `music-sync.sh` |

The plists are **generated per machine** by `setup.sh` — home dir, log dir, and
the volume to watch all come from your config, so nothing drive-specific is
checked in. Re-run `setup.sh` after moving the pipeline directory or changing
the retention destination.

```bash
python3 -m spindlebot restart    # bounce both agents
```

## Scripts

| Script | Role |
|--------|------|
| `music-watcher.sh` | fswatch daemon; fires `spindlebot import` (installed to `~/.local/bin/`) |
| `music-sync.sh` | content-addressed sync to the retention drive; fires on drive mount |
| `music-import.sh` | shim → `spindlebot import` |
| `music-notify.sh` | legacy notify shim (superseded by `stages/notify.py`) |
| `setup.sh` | one-time environment setup; installs the watcher and the agents |
| `setup-ai.sh` | optional; builds the AI venv for lyric re-timing |
| `migrate-work-dirs.sh` | sourceable helpers for the Staging→Import / Library→Pending rename |

`music-*.py` at the repo root are legacy standalone scripts, superseded by
`spindlebot/pipeline/stages/` and kept only for reference.

## Logs

| Log | What's in it |
|-----|--------------|
| `~/.config/beets/watcher.log` | Every import run, stage by stage |
| `~/.config/beets/music-sync.log` | Every sync run |

Both live under `core.log_dir`. Follow an import as it happens:

```bash
tail -f ~/.config/beets/watcher.log
```

User-facing milestones are emoji-prefixed, so scanning for the emoji gives you
the run summary without the verbose internals.

## What a sync run does

`music-sync.sh` is self-guarding at every step, and safe to invoke manually or
concurrently with a mount event:

1. **Refuses to run** if no enabled `local_drive` destination is configured —
   rather than pruning against an empty target.
2. **Takes a lockfile** (`/tmp/music-sync.lock`, override with
   `SPINDLEBOT_SYNC_LOCKFILE`) and exits quietly if one exists.
3. **Confirms the drive is actually mounted** — launchd fires spuriously.
4. **Checks Pending has real content**, counting only non-dotfiles, so a stray
   `.DS_Store` or AppleDouble file doesn't trigger a no-op run.
5. `inventory` — per-file errors are **not** fatal. One unreadable FLAC must not
   wedge the pipeline; it's isolated and logged, and the rest is cataloged.
6. `review --yes` — plan and acknowledge the copies in one shot.
7. `sync` — copy → verify → record, scoped to this destination so it doesn't try
   to execute copies queued for some other, possibly unmounted, destination. On
   failure it aborts and **leaves Pending intact, without pruning**.
8. `prune --execute` — release only files hash-verified on retention.
9. Rewrite beets DB paths from Pending to the retention path.
10. Notify.

Nothing leaves Pending until a verified copy exists on retention, and prune only
runs after a clean sync.

### One-time setup per destination

The reconciler won't plan against a location it has never seen — `review`
requires a target scan. `music-sync.sh` never inventories the target itself, so
a fresh install (or a newly added destination) needs one manual pass:

```bash
python3 -m spindlebot inventory --location "MyDrive"
```

Skip it and sync aborts with `review failed`.

## Troubleshooting

### When an album is stuck in Processing

Expected, not broken. An album stays in Processing until every track has a
terminal `.lrc` or `.nolrc` marker; a transient lrclib error leaves one without.

```bash
python3 -m spindlebot finalize --dry-run    # what would promote
python3 -m spindlebot finalize              # re-fetch lyrics, promote what's ready
```

### An import left files in Import

If `beet import` succeeded but added nothing, and the runner couldn't confirm
the album was already in the library, it deliberately leaves the files where
they are rather than moving them somewhere you'd have to go find. Check
`watcher.log` for the warning, then re-run the import by hand.

A genuine duplicate is moved to the Duplicates area instead — see
[architecture](architecture.md#duplicate-handling).

### A multi-disc set waits forever

The disc check waits for every disc in the set before importing. If you only
have disc 1 of 2 and always will:

```bash
python3 -m spindlebot import <trigger> --force
```

The alternative is patching `disctotal` in the FLACs first. Note that
MusicBrainz reports `disctotal=2` for some single-disc releases (DualDiscs,
conceptual A/B sides) — the runner corrects this after import from the actual
ripped disc count.

### Every `$SPINDLEBOT_*` var is empty

`bootstrap.sh` evals `python3 -m spindlebot config shell`. If Python or the
config is broken, everything comes back empty. Check it directly:

```bash
python3 -m spindlebot config shell
python3 -m spindlebot check
```

Also verify you're using `SPINDLEBOT_IMPORT_DIR`, not `SPINDLEBOT_IMPORT` — the
short form resolves to empty, and fswatch will then watch whatever the daemon's
working directory happened to be, with no error. `music-watcher.sh` guards
against this at startup.

### Sync says "review failed"

The destination hasn't been inventoried. See
[one-time setup per destination](#one-time-setup-per-destination).

### Lyrics don't show on a DAP

The pipeline writes `.lrc` sidecars, not FLAC lyrics tags — tags are lowercase
and ignored by some players. If the sidecars are present and still not showing,
the player may need them alongside the audio in the same directory, which is
where sync puts them.

### `lyric_timing retime` fails with `No module named lyric_timing`

It must run from the repo root. The AI venv doesn't have the package installed
— it resolves off the working directory.

---

The full gotcha list, including the ones that only matter when changing code,
is in [`CLAUDE.md`](../CLAUDE.md#known-gotchas).
