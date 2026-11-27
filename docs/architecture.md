# Architecture

SpindleBot is event-driven: nothing polls, and nothing runs on a schedule. Two
events start work — a rip finishing in the Import area, and the retention drive
being mounted.

This page explains what the system does and why it's shaped this way. For the
rules that govern changing the code — layering, conventions, the active epic —
see [`CLAUDE.md`](../CLAUDE.md).

## The two flows

**Import** — XLD writes a `.log` (or you drop a folder) into the Import area:

```
fswatch → music-watcher.sh → spindlebot import
    │
    ├─ pretag          normalize tags before beet sees them
    ├─ beet import     MusicBrainz match (+ duplicate detection)
    ├─ multidisc fix   patch disctotal from the actual disc count
    ├─ beet move       → Processing
    ├─ posttag         strip alias tags, truncate DATE to year
    ├─ fetch art       Cover Art Archive → iTunes fallback
    ├─ fetch lyrics    lrclib → .lrc / .nolrc per track
    ├─ promote         → Pending, only if lyric-complete
    ├─ archive log     → archive dir
    └─ notify          macOS + Telegram
```

**Sync** — launchd sees the retention drive mount:

```
music-sync.sh
    ├─ inventory   scan the location into the DB
    ├─ review      diff DB vs observed → a plan of pending actions
    ├─ acknowledge
    ├─ sync        verify source vs plan → copy → verify dest → record presence
    ├─ prune       release Pending copies verified on retention
    ├─ reconcile   fix up beets DB paths
    └─ notify
```

The launchd `WatchPaths` that trigger this are generated per-machine by
`setup.sh` from the first enabled `local_drive` destination — see
[configuration](configuration.md#destinations--sync-targets).

## The working areas

An album moves through three areas, plus a siding for duplicates:

| Area | Role |
|------|------|
| **Import** | XLD rips and downloads land here. Owned by the watcher. |
| **Processing** | In-flight work. Art and lyrics are fetched here. |
| **Pending** | Lyric-complete albums awaiting distribution. |
| **Duplicates** | Rips already in the library are parked here, not stranded in Import. |

Paths are configurable (`core.import_dir` and friends); the defaults live under
`~/Library/Application Support/SpindleBot/`. Beyond these, the retention drive
holds the permanent library, and the archive dir collects processed XLD `.log`
files.

### Why Processing exists

The obvious design is two areas — import into a staging dir, move to a library
dir when done. SpindleBot had exactly that, and it had a race.

Lyric fetching takes time and can partially fail. With albums landing in the
library area *before* their lyrics were complete, a drive mount could fire a
sync mid-fetch. Sync would copy the audio to retention, prune would then release
the local copy as verified — and the still-running lyric fetch would write
`.lrc` files next to audio that was no longer there. Stranded sidecars, and a
window where an album existed in neither place cleanly.

Processing closes that window. An album is promoted out of it **only once every
track has a terminal `.lrc` or `.nolrc` marker** — `album_lyrics_complete()`.
Pending is therefore complete-by-construction, and sync and prune can trust
anything they find there without knowing what else is running.

The cost is that albums can get stuck in Processing when lyric fetching hits a
transient error. That's what `spindlebot finalize` sweeps up: re-fetch, re-check
completeness, promote what's ready. Promotion is a beets-native
`beet move path:<dir>/`, so the beets DB stays authoritative about where its
items are.

## Content addressing

The older model was "the library is whatever is at these known paths." That
can't answer the questions that matter once copies exist in more than one place:
is this file the same recording as that one, how many copies exist, is it safe
to delete this one.

So SpindleBot owns a SQLite database — `~/.config/spindlebot/spindlebot.db` —
that is the system of record for content **identity** and every **location** a
copy lives at.

**Identity is the decoded-audio MD5** — the FLAC STREAMINFO `md5_signature`,
falling back to a whole-file sha256 when there isn't one. Decoded audio is the
right identity because re-tagging a file changes its bytes but not its music;
two copies that differ only in metadata are the same content. Per-copy file
sha256 exists too, but it is **integrity**, never identity. `review` freezes
the source presence checksum into each COPY action. `sync` verifies the source
against that planned checksum before copying, then verifies the destination
against the same checksum before recording presence. Refreshing inventory after
planning cannot change what an acknowledged action is allowed to copy.

A completed inventory also checks previously recorded paths for absence. Only
an exact-path check that finds no filesystem entry records a confirmed loss;
unreadable files, dangling symlinks, and incomplete scans do not authorize a
restoration. Schema v10 ties each absence to its scan ID, so two scans within
one second cannot reuse each other's evidence. `review` can then propose COPY
directly, restoring the recorded target path from an available source.

When a scan observes a different recording or sidecar identity at an existing
path, it retires the previous identity's path association. Deleting the new
occupant therefore cannot restore the old one. If older records leave several
identities claiming a now-missing path, inventory retires those ambiguous
claims without authorizing a restoration. Rescans checkpoint their running
marker before filesystem setup, including root checks and marker refresh, so a
failed setup or interrupted scan cannot revive older absence evidence when its
connection closes. First-time inventory still validates the root before recording
its initial scan, preserving the existing first-fill planning gate.

Restoration plans freeze source and destination paths separately. Absolute or
parent-traversing queued paths are refused before source access; an invalid
action fails independently while valid queued work continues. Sync verifies
a staged copy, exclusively creates the absent destination, and writes through
that file descriptor. Directory descriptors anchored to the location root reject
symlinked parents and prevent concurrent parent replacement from redirecting writes.
This includes ancestors above the configured root: restoration requires a path
without symlink components. The location marker policy is rechecked on the pinned
root descriptor before copying and after verification; any foreign marker
refuses an ambiguous root, even when the expected marker is also present.
Final verification hashes a no-follow file descriptor and checks that the directory
chain and final path still identify the verified inodes. A symlink or different
bytes that reappear are preserved and the action fails; an already-correct regular file is verified without writing.
Only the final verified path is recorded present. Failed or interrupted
publication can leave an unverified partial file requiring inspection and
cleanup before retry. Presence records describe bytes verified during the operation;
external writers can change a path afterward. Open descriptors or an advisory
lock cannot prevent non-cooperating external pathname changes. Prune retains
its independent path-and-hash verification gate. Normal copies retain their
existing copy path.

**Locations are first-class**, not paths in a config file. Each is identified by
a marker file `.spindlebot-location-<uuid>` at its root, and a root may be a
subfolder of a shared volume rather than a whole disk. Two deliberate rules:
a *missing* marker is never treated as a wiped drive (an unmounted volume looks
identical to an empty one), and a *foreign* marker refuses to resolve rather
than adopting someone else's data.

**beets is an overlay, not the source of truth.** `audio_content.beets_item_id`
is linked by path during inventory, read-only. It's advisory and nullable, and
nothing depends on it — beets knows about local paths, and the whole point of
the DB is to know about copies across devices.

### Planning is separate from acting

`review` is a planner. It diffs the DB against what `inventory` observed and
writes `pending_action` rows. It never touches bytes, and it refuses to plan
against a stale scan. Actions must be acknowledged before `sync`, `prune`, or
`delete` will execute them. The full staging table and the destructive-op gates
are in [commands](commands.md#content-addressed-db-and-sync).

An equivalent COPY already acknowledged and awaiting execution is not proposed
again on a retry. Equivalence includes destination, content kind and identity,
both paths, source location, planned checksum, and restoration mode. The queue
is read once per reconciliation. Changed plans remain separate proposals;
approved plan fields are never rewritten. Unacknowledged previews and executed history
do not suppress new work. Equivalent duplicate rows are not retroactively removed.

A later-approved restoration supersedes earlier, unexecuted generic COPY
approvals writing the same destination path. Schema v11 records when each action
was retired and its replacement ID, preserving original approval and plan values.
An ordinal allocated atomically by each acknowledgement orders approvals; row
creation IDs and run timestamps do not. An acknowledgement batch shares one
ordinal and stricter mode wins ties. Pre-v11 approvals share legacy order zero;
unknown orders from older clients also favor the stricter plan. The retirement timestamp remains authoritative if replacement
history is purged. Planning and dry runs do not retire actions. Acknowledgement
and the executor also normalize older mixed queues, including replacements
already executed. Other destinations remain untouched.

Each COPY rechecks its queue eligibility while acquiring the caller's SQLite
write transaction. That transaction remains held through transfer, verification,
and presence publication until the caller's checkpoint, so a concurrent approval
cannot retire the action between validation and transfer. Other database writers
may wait for a file checkpoint. The executor refuses eligible copies on an
autocommit connection without a caller-owned transaction.

## Import stage sequence

`spindlebot/pipeline/runner.py`, `ImportRunner.run()`:

1. **Trigger validation** — a directory is the album dir; a `.log` means its
   parent is. Anything else is logged and skipped.
2. **Double-fire guard** — `.log` mode only: an already-archived log means this
   is a repeat event, so exit clean.
3. **Disc check** — wait if a multi-disc set is incomplete (`--force` bypasses).
4. **pretag** — normalize before beet sees the files.
5. **beet import** — streams live to the terminal when an echo callback is set.
6. **multidisc fix** — patch `disctotal` and the `multidisc` flex attribute.
7. **beet move** — relocate to canonical paths under Processing.
8. **posttag** — strip beet alias tags, truncate DATE to year. Runs *after* the
   move because beet re-adds alias tags on write.
9. **fetch-art + fetch-lyrics** — embed art, write `.lrc`/`.nolrc` sidecars.
10. **promote** — Processing → Pending, if lyric-complete.
11. **archive** — move the XLD `.log` to the archive dir.
12. **auto-sync-or-hint** — on a fully successful run, either invoke
    `music-sync.sh` or log a hint, per `core.auto_sync_on_import`.

### Duplicate handling

A green `beet import` that adds no new items is ambiguous: it might be a
re-rip of something you already have, or it might be a different no-op failure.
The runner disambiguates by checking the existing library — matching on
`musicbrainz_albumid`, else on **both** `albumartist` and `album` (a one-sided
fallback could false-match an unrelated release).

- Match ⇒ it's a duplicate. Log it, move the rip to
  `duplicates_dir/<artist>/<album>/`, notify, skip the remaining stages.
- No match ⇒ some other failure. Warn, and **leave the files in Import**. Never
  moved, never discarded.
- The verification query itself failed ⇒ the result is *unknown*. Skip duplicate
  handling entirely and leave the files in place, rather than risk moving a real
  import.

### Canonical directory collisions

Before import or relocation writes audio into a canonical directory, the
pipeline loads a small beets guard through a private temporary configuration
overlay. It preserves the effective configured plugins and path rules, and uses
beets' own destination calculation after metadata matching. This protects the
first `beet import` write as well as the later Processing move and Pending
promotion; checking only the explicit moves would be too late because import
can already have written into Pending.

The guard compares decoded-audio identity (with the normal sha256 fallback).
An existing directory may hold a subset of the incoming album, allowing an
interrupted move to resume. A directory containing any foreign audio identity
is refused before copying or moving audio, including audio in nested directories.
AppleDouble `._` resource files are ignored. Same-album imports still follow
beets' duplicate decision and the runner's existing Duplicates handling.

Relocation preflights every selected item's native destination before any item
moves, including multidisc destinations and `move -d` overrides. Different
selected albums cannot reserve overlapping destination trees, including case
aliases on the actual filesystem, even when those directories start empty.
Nested directory symlinks refuse verification rather than hiding uninspected
audio behind a link. Source identity
is scoped to the selected items, so an offline, unselected sibling disc does not
block local promotion. An operation-local identity cache avoids repeated full
file reads and invalidates on device/inode/size/mtime/ctime changes. Per-item move
hooks recheck destination contents and refuse changed source identity.
Native post-move tag writes refresh the participating destination's identity,
so formats using whole-file SHA fallback still import multiple matched tracks.

The validation unit is each actual audio destination directory and its
descendants. It does not infer a broader album root from arbitrary templates:
the shipped `Artist/Album [Disk N]` layout has `Artist` as its common parent,
which also contains unrelated albums. With a custom `Album/Disc 1/track` layout,
audio in the ancestor `Album` or an unselected sibling `Disc 2` is outside that
destination's check. Templates needing that broader boundary require an explicit
album-root contract; guessing from path names would falsely refuse other albums.

Guard-owned command aliases make a missing or broken adapter fail closed rather
than letting beets log a plugin-load error and continue unprotected. Neither the
installed beets configuration nor its secrets are copied to the overlay; only
plugin names/search paths and disabled-plugin settings are carried over. The
temporary overlay is removed after the command, including on failure.

This is a pre-write check, not a cross-process filesystem lock. Independent
imports or external edits racing between verification and movement remain a
time-of-check/time-of-use limitation. Beets can record incoming items in its
database before an import-stage refusal; the source files remain intact at
their recorded paths. The guard does not attempt destructive database cleanup.

## Path template

```
Single disc:  Artist/Album/NN. Title.flac
Multi-disc:   Artist/Album [Disk N]/NN. Title.flac
```

Multi-disc is determined by the **actual ripped disc count**, not MusicBrainz
`disctotal` — MusicBrainz reports `disctotal=2` for single-disc DualDiscs and
conceptual A/B sides, which would otherwise scatter one album across two
folders.

## Package layering

Strictly one-directional, enforced by convention and reviewed as such:

```
core/        pure — hashing, enums, frozen row models. No DB, no IO, no print.
db/          repositories/ is the ONLY layer that issues SQL. Callers own the
             transaction; repos don't commit.
services/    orchestration over repos + core. Returns typed results, prints
             nothing.
cli.py       thin client. The only place that prints. Every command has --json.
```

`CLAUDE.md` carries the annotated file map, the enum and schema conventions, and
the reasoning behind each rule.
