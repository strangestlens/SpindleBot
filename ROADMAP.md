# SpindleBot — Roadmap

*Last updated: 2026-11-11*

Where the project stands and what comes next. What's already shipped is in
[`CHANGELOG.md`](CHANGELOG.md); the rules for changing code are in
[`CLAUDE.md`](CLAUDE.md); how to pick up a piece of this and work it is in
[`docs/handoff.md`](docs/handoff.md).

## Vision

A personal music intelligence system. Near term: a clean, well-tested,
configurable pipeline that anyone can run. Longer term: a local-first music
platform that knows every copy of every track on every device, keeps lyrics and
notes in sync between them, and serves the library remotely, organized,
resilient, and extensible.

---

## Status snapshot

| Track | State | Notes |
|-------|-------|-------|
| Import pipeline | **Shipped, hardened** | XLD/folder drop → Processing → Pending (lyric-complete only). Three hardening passes since July: #67 (silent whole-rip loss), #69 (albums stranding in Processing; `finalize` now runs on every mount), #71 (disc count read before `beet import` moves the rip). |
| Content-addressed DB | **Phases A–3 + 4.0 merged** | Schema **v8** on `main` (v8 = listening notes, #72). Identity, locations, sidecars, reconciler, copy→verify→presence, prune, gated delete, lyric lineage substrate. |
| Mount-sync to the retention drive | **Live** | `music-sync.sh`: finalize → inventory → review+ack → sync → prune → beets path reconciliation → notify. |
| `spindlebot push` | **Merged (#75)** | Fill one location in one pass. Never prunes (CLAUDE.md gotcha 14). |
| Lyric sync (Phase 4.1 / 4.2) | **Not started** | 4.0 lineage is merged; nothing moves `.lrc` bytes between copies yet. |
| Listening notes | **N0 merged, N1+N2 in review** | #72 merged (schema v8, repos, markdown). #74 (the `note` CLI, import/export) is green and waiting. |
| AI lyric timing | **Iteration 2 merged (#70)** | Star-token alignment, interpolation over sung time, 0.2 s global lead. Mean error 8.9 s → 0.95 s on the benchmark set. |
| Collection audit | **Shipped** | Discogs → library gap report, ignore list, `collection-browser`. |
| Original roadmap phases | 1–2 done, 3 partial | `music-sync.sh` is now shell orchestration over Python commands, not a shim over one service. See "Fold music-sync.sh into a service" below. |

Test suite: 1034 Python tests on `main`, plus bats. CI runs Python 3.11 + 3.14.

### Waiting to merge

All three are green, mergeable, and have been idle since September/October.

| PR | What | Why it matters |
|----|------|----------------|
| [#76](https://github.com/strangestlens/SpindleBot/pull/76) | lrclib's 3600 s duration cap no longer strands an album | **Production still has this bug.** Any track over an hour (single-track ambient CDs) strands its album in Processing forever. Adds gotcha 7b. |
| [#77](https://github.com/strangestlens/SpindleBot/pull/77) | `fetch_art` test stops calling iTunes | One test hit the network; flaky CI. |
| [#74](https://github.com/strangestlens/SpindleBot/pull/74) | Notes N1+N2 — resolution, service, `note` CLI, import/export | Branch predates #70/#73/#75. Rebase on `main` and let CI re-run before merging. Adds the "Listening notes (schema v8)" rules section to CLAUDE.md and `docs/notes.md`. |

---

## P0 — Data integrity

Live defects in the paths that move or delete bytes. Each one has either already
lost data or can feed a wrong "safe to delete" signal into prune/delete. These
come before any feature work.

1. **Sync verifies the copy, not the source.** `services/sync.py:119` hashes the
   source *at copy time* (`expected = file_sha256(src)`) instead of checking it
   against the `file_sha256` the action was planned on. If the source was
   replaced between plan and execute, sync records the replacement as a verified
   copy of the *original* content id — and prune then releases Pending on that
   false signal. Happened for real (M.I.A. *MAYA*, sync run 74).
   **Fix:** verify the source against the planned content before copying; fail
   the action on mismatch. Do not "fix" it by re-hashing and updating the action
   — that launders the substitution.

2. **Import writes into a directory that already holds a different album.** On a
   case-insensitive volume (the retention drive is HFS+), `Artist/Maya/` and
   `Artist/MAYA/` are one directory; a later import overwrote an earlier rip
   file by file, unrecoverably. Same failure for any name collision (Unicode
   normalization, trailing dots, `replace:` rules).
   **Fix:** a pre-write guard wherever bytes first land at a canonical path
   (`beet move` into Processing, the promote into Pending). "Different" is by
   content identity, not name — a re-import of the *same* album must still take
   the existing duplicate path. Test on a case-insensitive path; a test on a
   case-sensitive filesystem passes against the bug.

3. **A retention-flagged DAP can authorize prune/delete on its own.** The DAP
   location is `is_retention = true`, so it counts toward `min_copies`, and
   prune fires at the first retention copy. `spindlebot prune`/`delete` run by
   hand can release or remove the retention-drive copy of anything that's also
   on the DAP. `push` sidesteps this by never pruning; the automated mount-sync
   only prunes after syncing the retention drive. **Fix:** split `is_retention`
   into "content flows here" vs "counts toward the floor" (see Destination model,
   P1). A config-only stopgap is a decision for Daniel.

4. **A file deleted from a destination is never restored.** Nothing executes
   `UPDATE_PRESENCE` (only `reconciler.py` emits it). The stale `present=1` row
   stays, the reconciler keeps proposing UPDATE_PRESENCE instead of COPY, and the
   run reports `missing=1, copied=0, ok=True` forever.
   **Fix:** have the reconciler propose COPY directly for a confirmed absence
   (one pass), or add an executor that records the absence (two passes).
   Schedule alongside the retention split — same underlying problem.

5. **One path per identity per location.** `audio_presence` and
   `sidecar_presence` key on `(content, location)`, so two paths holding
   identical content at one location collapse to one row. The invisible path is
   fully re-identified and re-hashed on every scan (89 files on the retention
   drive), and a per-disc `cover.jpg` at an untracked path is only safe from
   prune because prune verifies exact paths. **Fix:** schema migration to track
   every path, plus reconciler changes. Prerequisite for the dedup report.

6. **Metadata never propagates between copies.** Identity is the decoded-audio
   MD5, so a retag or new embedded art doesn't change identity and the
   reconciler correctly copies nothing. But nothing else will ever carry the fix
   to other copies, and a stale copy's old tags fork a *second* album row for
   the same tracks. **Fix:** a distinct "metadata refresh" action kind with an
   explicit authoritative copy. Must not be inferred from a sha256 difference —
   that's also what corruption looks like. Design pass first.

7. **A retried push queues duplicate COPY rows.** An interrupted push leaves
   acknowledged-unexecuted COPY rows; the next push plans fresh ones for the
   same files, so each is transferred and hashed twice. Non-destructive, wasted
   I/O on flaky USB. **Fix:** dedupe in the reconciler/`action_repo` against
   acknowledged-unexecuted actions for the same (dest, content, rel_path).

Fixed since the last roadmap, for reference: the sync/fetch-lyrics race and the
orphan-sidecar gap (Processing area, #45; #47), albums stranding on a transient
lrclib error (#69), and the lrclib duration cap (in #76, unmerged).

---

## P1 — Finish the content-addressed epic

### Phase 4.1 — lyric auto-propagation
Consume the 4.0 lineage: a location *behind* the head gets the head's bytes
(reuse the sidecar-copy path, extended to "stale on target → overwrite");
concurrent edits keep both, write the loser as
`<track>.conflict-<date>-<location>.lrc`, and open a `conflict` row. Clean wins
propagate with no acknowledge gate (Daniel's call); conflicts always need a
human. Fold in the small #48 follow-up: store each location's real
`sidecar_presence.observed_utc` in `lyric_version_presence`, not the reconcile
run time.

### Phase 4.2 — `spindlebot conflicts list|resolve [--json]`
Resolve = pick a winner → bump its vclock to dominate → propagate → clean up
loser files. Key conflict dedup on the version pair, not on open status, or a
resolved conflict re-opens on the next reconcile.

### Destination model (design-gated)
Daniel wants every mountable destination automated the way the retention drive
is, with a per-location sync mode (working names: `full` mirror vs `partial`
curated). Three tangled problems:

- `[[destinations]]` and `[[locations]]` overlap — the retention drive is in
  both, the DAP only in locations, which is why the DAP has no automation.
- The reconciler treats every location as a full mirror, so a curated device
  gets "copy the whole library" plans and a deliberate deletion looks like loss.
- `is_retention` conflates "content flows here" with "counts toward
  `min_copies`" (P0 #3).

Order: split retention from the floor first (independently valuable, and must
land before any mount automation makes P0 #3 fire unattended), then sync modes,
then per-destination mount triggers. `partial` needs a **DELETE planner** — only
the delete *executor* exists today. **The open design questions are not
settled; agree them with Daniel before code.** The original long-form design
document is not available; re-derive the questions from the three problems
above.

### Fold `music-sync.sh` into a service
The mount-sync is a sequence of CLI calls in shell. `services/push.py` was put in
`services/` precisely so the sync script could become a shim over it. Remaining
work: a mount-sync service (finalize → push to the retention location → prune →
beets path reconciliation → notify), with prune staying a separate, explicit
step outside `push_location`. This is the unfinished legacy Phase 3.

### Phase 5 — plugins and the AI re-timer in the pipeline
A plugin hook (e.g. post-fetch-lyrics) so `lyric_timing retime` can run on
crammed/unsynced `.lrc` files automatically. Its output reaches the DB as an
ordinary out-of-band edit on the next scan, so Phase 4 lineage handles it with
no special case.

### Phase 6 — DB snapshots and durability
Moved up in importance by listening notes: notes are the **only authored,
un-regenerable data** in `spindlebot.db`; everything else rebuilds from
`inventory`. Scheduled snapshots of the DB (SQLite online backup) to a retention
location, plus a restore path.

### Phase 7 — daemon
One long-running process replacing both launchd agents and the Full Disk Access
wrapper app: DiskArbitration mount events, an import queue, a status surface.
Natural precursor to the Mac app.

---

## P2 — Features

### Import
- **Disc-metadata fix command** — unstick an album whose disc tags are wrong
  (box-set MBIDs give `disctotal=4` for a 1-CD rip) without hand-editing tags.
  `--force` is not the answer: it releases *every* batch in a flat Import area,
  including half-ripped albums. **Design discussion with Daniel first**; options
  on the table are a scoped tag-repair command, per-album force, or a
  reply-to-resolve notification.
- **Discogs preflight** — consult the owner's Discogs collection before trusting
  rip disc counts (it had the Tom Petty *Wildflowers & All the Rest* 2-CD count
  right when MusicBrainz said 4). The client and matcher exist in
  `collections/`; the constraint is that a Discogs outage or cache miss must
  never block an import.
- **Duplicate confirm-to-delete** — duplicates already land in
  `duplicates_dir`; add a "reply `delete`" action to the Telegram notification.
  Never auto-delete.
- **Digital downloads** — a "gentle" pretag mode for downloads that arrive with
  good metadata: normalize instead of strip; consistent ALBUM/ARTIST/DATE means
  a complete album; sparse metadata falls back to a MusicBrainz search.

### Lyrics
- **`format_lrc` strips LRC metadata tags** (`[ar:]`, `[ti:]`, `[re:]` …) in
  *both* `lyric_timing/lrc.py` and lrc-editor's own copy. Small standalone bug;
  precondition for provenance.
- **Provenance + contribute-back** — populate the existing
  `lyric_version.source` (`scan` / `edit` / `ai-retimer`) by writing a `[re:]`
  tag and reading it at inventory; then a dry-run-default command to publish
  hand-timed lyrics to lrclib (the publish API is a SHA-256 proof-of-work
  challenge, verified to exist).
- **A plain-text lyric source** — lrclib is the only source. Shazam/Genius have
  unsynced text; fetch it and hand it to `retime`. The alignment half is already
  solved. (`retime` re-times text already in the `.lrc`; it is not a source.)
- **AI timing iteration 3** — repetition census: liner-note lyrics print a
  chorus once that's sung three times, and nothing acoustic says which
  repetition the text owns. Needs per-line CTC keyword spotting against the
  posterior (greedy decode was tried and is unusable on sung audio).
- **`.nolrc` re-check** — a definitive miss is never retried without `--force`.
  If misses commonly gain lyrics on lrclib later, add an age-based re-check.

### Listening notes (after #74)
- N3: a notes panel in `collection-browser`.
- Note lineage/sync across locations — a port of `services/lyrics_sync.py`.
- Attachments / rich content.

### Library hygiene
- **Dedup report** — list every path sharing an identity at a location so
  duplicate directories can be reclaimed (needs P0 #5).
- **Non-audio extras** — booklet scans, `.cue`, `.log`, `.pdf`, extra images
  are invisible to inventory and never sync. They're a *set* of
  content-addressed files per album, not a singular slot like `cover`; model
  them separately.
- **Readable `review` output** — sidecar actions print as `sidecar <id>`; show
  role and the track/album they belong to. Render-only change in `cli.py`.
- **Normalize disc directory names** on the retention drive (`[Disc 1]`,
  `[Disk 1]`, `[Disk 01]` all exist; five albums beets doesn't track).

### Resilience
- One `retry_with_backoff` used by every external call (lrclib already has its
  own; iTunes, CAA, Telegram don't), honouring `Retry-After`.
- A failure journal (`failed.jsonl`) with `spindlebot retry-failed` and a count
  in `spindlebot status`.
- Pre-flight checks before import (Import readable, `beet` present) and sync
  (destination reachable, enough free space).

### Collection audit
- **Sorting (uncommitted WIP).** A `feat/collection-sort` worktree holds ~230
  uncommitted lines across the CLI, providers, report, and tests. Its intent and
  completeness are unknown; ask Daniel before resuming or discarding it.
- More providers (MusicBrainz collections would bring release MBIDs and skip
  string matching; CSV; Last.fm) — one module plus a registry entry each.
- `--media vinyl` already works; a vinyl-not-digital report is a flag away.
- Transliteration stays out of `spindlebot/` (heavy dependency); the ignore list
  is the intended answer.

---

## Chores

- **ruff 0.16 bump** — ~145 findings across the tree. Its own PR, run when no
  other branch is open (it touches everything).
- **lrc-editor mutating routes** — `/save` and `/commit` accept simple
  cross-origin POSTs. Add the Host/Origin check `collection-browser` already has.
- **Branch and worktree cleanup** — ~20 stale `claude/*` and `worktree-agent-*`
  branches and several dead worktrees under `.claude/worktrees/`. Merged-branch
  worktrees (`push-command`) can go.
- **SQL `LIKE` wildcard hardening** for user-supplied paths containing `_`/`%`
  in the beets path rewrite (not a bug on the current paths).
- **CLAUDE.md drift** — keep the schema line, file map, and epic status current
  as PRs land; several sections lagged a merge behind.

---

## Later

### Cloud backup destination
`[[destinations]]` already accepts `type = "rclone"`; nothing exercises it. For a
1–2 TB FLAC library, Backblaze B2 + rclone is the pragmatic default ($6/TB/month,
checksum verification, dry-run). Wasabi if restores are frequent (no egress
fees); S3 Glacier Instant Retrieval if cost matters most. iCloud is unsuitable —
200 MB file limit and AAC transcoding for unmatched tracks. Depends on the
destination model: a cloud bucket is a retention location with no mount event.

### Remote access — use Navidrome, don't build it
A single Go binary serving the Subsonic/OpenSubsonic API to 20+ existing
clients, reading a static music directory (exactly what the pipeline produces),
and supporting `.lrc` sidecars natively. Run it against the retention drive
behind Tailscale. Symfonium/Substreamer on iOS and macOS. Jellyfin or
Plex + Plexamp if it's ever not enough.

### A Mac app
A SwiftUI shell around the Python core: config editor, status dashboard, import
queue, notification preferences; Navidrome as a bundled service; launchd agents
and the Full Disk Access wrapper become app internals. The core doesn't change,
only its surface. Phase 7 is the stepping stone.

### Distribution
`git clone` → working Mac. The plists are already generated per machine by
`setup.sh`. Remaining: an interactive `spindlebot setup` for first config, a
dependency preflight, the Full Disk Access wrapper app built from source in the
repo, and guided prompts for what can't be scripted (TCC grants, XLD output
directory). Partial-sync destinations become necessary before anyone with a
small device uses this.

---

## Dependencies at a glance

```
P0 #1 verify-source ──┐
P0 #3 retention split ├─► prune/delete safe to automate further
P0 #4 restore deletes ┘          │
                                 ▼
                     Destination model ──► per-destination mount automation
                                 │                 │
                     DELETE planner ──► partial sync mode
                                                   ▼
                                       Cloud backup destination

P0 #5 all-paths presence ──► Dedup report
format_lrc fix ──► Provenance ──► lrclib contribute-back
Phase 4.1 ──► Phase 4.2 ──► Notes lineage (same pattern)
Phase 5 plugin hook ──► Auto-retime on import
Phase 7 daemon ──► Mac app
#74 merged ──► Notes N3+
```

---

The original 6-phase roadmap is archived at
[`docs/archive/original-roadmap.md`](docs/archive/original-roadmap.md).
