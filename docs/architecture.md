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
    ├─ sync        copy → verify dest sha256 → record presence
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
sha256 exists too, but it is **integrity**, never identity — it's what `sync`
verifies at the destination after a copy.

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
