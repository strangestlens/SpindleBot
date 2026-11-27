# Command reference

Every command is invoked as `python3 -m spindlebot <command>` — there is no
`spindlebot` executable on `PATH`, and `setup.sh` doesn't install one. The
examples below spell out the full form so they can be pasted as-is. If you use
this often, alias it:

```bash
alias spindlebot='python3 -m spindlebot'
```

The DB and sync commands (`finalize`, `inventory`, `review`, `sync`, `push`,
`prune`, `delete`) support `--json` for structured output. The others print
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
python3 -m spindlebot push --location <name> [--dry-run]         # inventory→review→ack→sync, one location
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
| `sync` | Yes (writes) | Executes *acknowledged* copy actions: verify source against plan → copy → verify dest → record presence |
| `push` | Yes (writes) | The three above, for one location, in one pass (plus a rescan of the authoring library first). Stops there — never prunes |
| `prune` | Yes (deletes) | Releases the non-retention authoring copy once retention is verified |
| `delete` | Yes (deletes) | Removes a *retention* copy — the gated one |

`review` requires a fresh scan of the target location; it refuses to plan
against stale observations. Nothing in `sync`, `prune`, or `delete` acts on an
action that hasn't been acknowledged.

After a destination file is deleted, run inventory → review/acknowledge → sync
to restore its recorded path in one pass, provided another location holds the
content. This includes tracked audio, lyrics, cover art, and `.nolrc` markers.
Inventory confirms the exact path absent; a stale observation or failed scan
alone is insufficient. Restorations preserve and refuse a symlink or different
bytes that reappear at the target; already-correct regular files are verified
without writing. An unreadable or unidentified existing path
is not treated as a deletion.

COPY plans include the source checksum observed at planning time, for audio and
sidecars. A changed source fails before any copy, writes no destination presence,
and leaves the action unexecuted; `sync` reports the action ID and exits nonzero.
Destination verification uses that same planned checksum, including when the
source changes during transfer.

Schema v9 adds this checksum without backfilling older actions from current
presence. A COPY with no planned checksum is refused even if its source is
readable. Re-inventory the source and review again to create a fresh plan.
Old acknowledged actions remain queued and continue reporting failure until
explicitly retired; a fresh plan does not silently replace them.

### `push` — one location, one pass

```bash
python3 -m spindlebot push --location <name> [--dry-run] [--rehash] [--json]
```

`push` is a rescan of the authoring library → `inventory` of the target →
`review` → acknowledge → `sync`, the last of these scoped to the one location. It exists for destinations that `music-sync.sh` can't reach:
`music-sync.sh` reads the single enabled `[[destinations]]` entry, so it only
ever targets the retention drive, while a DAP configured only as a
`[[locations]]` entry had no automation at all and had to be filled by hand.

```bash
# fill a DAP mounted at its configured root
python3 -m spindlebot push --location MyDap

# see what it would copy first — plans, acknowledges nothing, copies nothing
python3 -m spindlebot push --location MyDap --dry-run
```

**`push` never prunes, by design.** `prune` has no per-location scope: it
releases every Pending file verified on *any* retention location. A DAP
configured `is_retention = true` would therefore make a post-push prune release
the authoring copy on the strength of the DAP alone. Stopping at `sync` is what
makes `push` safe to run against a retention DAP today. If you do want to
release Pending, run `prune` separately and deliberately.

The authoring library is rescanned first for the same reason `music-sync.sh`
opens with an unscoped `spindlebot inventory`: nothing in the import pipeline
writes to the SpindleBot DB, so an album ripped since the last scan exists on
disk but in no table the reconciler reads. Without that step a push run straight
after a rip plans nothing and calls it success. If the authoring library isn't
mounted — a legitimate state after a prune — the step is skipped, said so in the
output, and planning continues from recorded state.

`--dry-run` still inventories — a plan built on a stale scan is worthless — but
acknowledges nothing and copies nothing. The exit code is 0 only when every
stage ran cleanly; a dry run that stops before the copies is a success, but a
planner *refusal* under `--dry-run` is not.

Note that the `sync` stage executes every acknowledged-but-unexecuted copy for
that destination, not just the ones this pass planned. A copy an earlier push
never finished (the DAP was unplugged mid-run) is still owed to the location,
and the next push completes it.

An equivalent acknowledged COPY is reused rather than queued a second time.
The planned-copy count reports new proposals only: a retry can plan zero new
copies and still execute its earlier queue. This also applies to `--dry-run`,
which leaves the existing queue untouched. A changed source, checksum, path, or
restoration mode gets a separate proposal; old approvals are never rewritten.
Equivalent duplicate queue rows are not automatically retired. A later-approved
restoration does retire earlier overwrite-capable COPY approvals for
the same destination path, so the older approval cannot bypass its no-overwrite
requirement. Those original plans and approvals remain in history, marked
superseded; deleting replacement history cannot make them executable again.
Actual acknowledgement order governs this rule, with stricter mode winning
same-batch or unknown-order ties. A dry run does not supersede anything.

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

## Listening notes

```bash
python3 -m spindlebot note add [--artist <a>] [--album <b>] [--track <t>] [--mbid <id>] [--new]
                          [-m <text> | -F <file> | -] [--tag <t>] [--session <id>]
                          [--index auto|beets|db] [--json]
python3 -m spindlebot note list [--artist <a>] [--album <b>] [--track <t>]
                          [--kind artist|album|track] [--tag <t>] [--session <id>]
                          [--since <date>] [--all] [--json]
python3 -m spindlebot note show <id> [--history] [--json]
python3 -m spindlebot note edit <id> [-m <text> | -F <file> | -] [--json]
python3 -m spindlebot note rm <id> [--json]
python3 -m spindlebot note restore <id> [--json]
python3 -m spindlebot note tag <id> [--json] [--] <tag>...
python3 -m spindlebot note untag <id> [--json] [--] <tag>
python3 -m spindlebot note session start [--title <text>] [--json]
python3 -m spindlebot note sessions [--since <date>] [--json]
python3 -m spindlebot note import <file> [--dry-run] [--root-level N] [--new] [--skip-unresolved]
                             [--index auto|beets|db] [--session <id>] [--title <text>] [--json]
python3 -m spindlebot note export [--artist <a>] [--album <b>] [--track <t>]
                             [--kind <k>] [--since <d>] [--root-level N] [-o <file>] [--json]
python3 -m spindlebot note export --json [-o <file>]     # archival: tags, uuids, revisions; no JSON restore
```

`add` refuses an ambiguous subject rather than guessing; `--new` writes about
something the library does not have. `import` is atomic and idempotent, and
`--dry-run` prints the resolution table without writing. Full reference in
[listening notes](notes.md).

`-m/--message` and `--tag` are repeatable. Inline values such as
`--message=--message` or `--tag=--json` preserve literal option names. `--`
ends option parsing for positional tags; put output flags before it. Tags are
case-sensitive and surrounding whitespace is trimmed on add, filter, and removal.

## lyric_timing (optional peer package)

```bash
# audit — pure text heuristics, NO heavy deps, runs on the system python3
python3 -m lyric_timing audit <dir-or-.lrc ...> [--json]

# retime — forced alignment; must run from the repo root, via the AI venv
~/.local/share/spindlebot/ai-venv/bin/python -m lyric_timing retime \
    <audio> <lrc> [--overwrite] [--json] [--no-vocal-sep] \
    [--model wav2vec2_en|mms_fa] [--language <iso-639-1>]
```

`retime` is non-destructive by default — the new LRC goes to stdout unless you
pass `--overwrite`. `--model` defaults to the English model, which is the more
accurate one on English singing; `mms_fa` is the multilingual alternative. Full
reference in [AI lyric timing](ai-lyric-timing.md).

## Standalone web tools

```bash
./lrc-editor <track.flac>                 # waveform lyric-timing editor
./collection-browser [--handle <name>]    # collection audit with click-to-ignore
```

Both are single-file Flask apps living outside the `spindlebot/` package, bound
to `127.0.0.1`.
