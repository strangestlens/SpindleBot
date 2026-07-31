# Command reference

Every command is invoked as `python3 -m spindlebot <command>` — there is no
`spindlebot` executable on `PATH`, and `setup.sh` doesn't install one. The
examples below spell out the full form so they can be pasted as-is. If you use
this often, alias it:

```bash
alias spindlebot='python3 -m spindlebot'
```

The DB and sync commands (`finalize`, `inventory`, `review`, `sync`, `prune`,
`delete`) support `--json` for structured output. The others print
human-readable text only.

> **Destructive ops default to dry-run.** `prune` and `delete` only touch bytes
> with `--execute`. See [gates](#destructive-op-gates) below.

## Inspection and setup

```bash
python3 -m spindlebot check                          # validate config + environment
python3 -m spindlebot config shell                   # emit config as shell exports (what bootstrap.sh evals)
python3 -m spindlebot config get core.pending_dir    # print a single value
python3 -m spindlebot restart                        # restart the launchd agents
python3 -m spindlebot notify <title> <message>       # send a test notification on all channels
```

## Import

```bash
python3 -m spindlebot import <trigger> [--force]     # run the import pipeline for one album
                                                     #   trigger = an album dir OR an XLD .log
                                                     #   --force skips the multi-disc wait
python3 -m spindlebot import-staging [--dry-run]     # import everything currently in the Import area
python3 -m spindlebot finalize [--dry-run] [--json]  # retry lyrics + promote lyric-complete albums
                                                     #   out of Processing → Pending
```

`import` is what the fswatch daemon fires on a `.log` write or folder drop; you
rarely need to run it by hand. `finalize` is the sweep for albums that stalled
in Processing on a transient lyric error — it re-fetches and promotes anything
that has since become lyric-complete.

## Content-addressed DB and sync

```bash
python3 -m spindlebot inventory [--location <name>] [--rehash]   # scan a location into the DB
python3 -m spindlebot review --location <name> [--yes]           # plan reconciliation; --yes acknowledges
python3 -m spindlebot review --acknowledge-run <run_id>          # acknowledge every action in a run
python3 -m spindlebot review --acknowledge <id[,id...]>          # acknowledge specific actions
python3 -m spindlebot sync [--location <name>]                   # execute acknowledged copies
python3 -m spindlebot prune [--execute]                          # release Pending copies verified on retention
python3 -m spindlebot delete [--execute]                         # execute acknowledged retention-copy deletes
```

Each of these takes `--json`, plus `-v`/`--verbose` for scrolling per-file
output or `--quiet`/`--no-progress` to silence the progress display.
`music-sync.sh` passes `--quiet`, which is why the sync log reads as milestones
rather than per-file chatter.

The pipeline is deliberately staged, and each stage is inert with respect to the
next:

| Stage | Touches bytes? | What it does |
|-------|----------------|--------------|
| `inventory` | No | Hashes what's actually on disk at a location; upserts content, albums, sidecars, presence |
| `review` | No | Diffs the DB against what was observed and writes `pending_action` rows — a plan, nothing more |
| `sync` | Yes (writes) | Executes *acknowledged* copy actions: copy → verify dest sha256 → record presence |
| `prune` | Yes (deletes) | Releases the non-retention authoring copy once retention is verified |
| `delete` | Yes (deletes) | Removes a *retention* copy — the gated one |

`review` requires a fresh scan of the target location; it refuses to plan
against stale observations. Nothing in `sync`, `prune`, or `delete` acts on an
action that hasn't been acknowledged.

### Destructive op gates

The two destructive operations are genuinely different and are gated
differently:

- **`prune`** releases a *non-retention* copy (the authoring copy in Pending)
  once a retention copy is verified present. Releasing a non-retention copy
  can't lower the retention count, so `min_copies` is only a **warning** here
  (`below_floor`), not a gate. It fires at the *first* verified retention copy.
- **`delete`** removes a *retention* copy, and **is** gated: it never drops the
  retention count below `min_copies`.

Both are dry-run unless you pass `--execute`.

## Per-album utilities

```bash
python3 -m spindlebot fetch-lyrics <dir> [--dry-run] [--force]
python3 -m spindlebot fetch-art <dir> [--dry-run] [--force]
```

`--force` overwrites what's already there; without it, existing `.lrc`/`.nolrc`
markers and embedded art are left alone.

## Collection audit (optional)

```bash
python3 -m spindlebot collection-audit [--handle <name>] [--media cd,vinyl]
                            [--index auto|beets|db] [--refresh]
                            [--strict] [--all] [--show-ignored]
                            [--html <file>] [--json]
python3 -m spindlebot collection-ignore <id...> [--reason <text>]
python3 -m spindlebot collection-ignore --list | --remove <id...> | --clear --yes
```

Full reference in [collection audit](collection-audit.md).

## lyric_timing (optional peer package)

```bash
# audit — pure text heuristics, NO heavy deps, runs on the system python3
python3 -m lyric_timing audit <dir-or-.lrc ...> [--json]

# retime — forced alignment; must run from the repo root, via the AI venv
~/.local/share/spindlebot/ai-venv/bin/python -m lyric_timing retime \
    <audio> <lrc> [--overwrite] [--json] [--no-vocal-sep]
```

`retime` is non-destructive by default — the new LRC goes to stdout unless you
pass `--overwrite`. Full reference in [AI lyric timing](ai-lyric-timing.md).

## Standalone web tools

```bash
./lrc-editor <track.flac>                 # waveform lyric-timing editor
./collection-browser [--handle <name>]    # collection audit with click-to-ignore
```

Both are single-file Flask apps living outside the `spindlebot/` package, bound
to `127.0.0.1`.
