# Handoff: current state and work queue

*Last updated: 2026-11-11*

This file and `ROADMAP.md` live on the **`handoff` branch, which is never
merged.** They describe where the work stands right now, and they go stale as
work lands. Everything that stays true across tasks (rules, workflow, test
norms, environment quirks) is on `main`, in `CLAUDE.md` and
`docs/development.md`.

You'll be working in a worktree off `main`, which doesn't have these files.
Read them from the branch:

```bash
git show handoff:docs/handoff.md
git show handoff:ROADMAP.md
```

**Reading order:**

1. [`CLAUDE.md`](../CLAUDE.md) on `main`, especially **Agent rules** (what needs
   the owner's approval) and **Known gotchas**. It binds every agent, whatever
   the name suggests, and it wins any disagreement with this file.
2. [`docs/development.md`](development.md) on `main`: commands, worktrees, the
   review loop, working alongside other agents.
3. This file, then `ROADMAP.md`.
4. The `docs/` page for the area you're touching.

---

## 1. What SpindleBot is, in one screen

An event-driven pipeline that rips, tags, and manages a lossless music library
on macOS, plus a SQLite system of record that tracks every copy of every track
across every drive and device.

- **Import:** XLD rip or folder drop into **Import** → `spindlebot import` →
  pretag → `beet import` → multidisc fix → `beet move` into **Processing** →
  posttag → art → lyrics → promoted to **Pending** only once every track has a
  terminal `.lrc`/`.nolrc` → notify.
- **Sync:** the retention drive mounts → `music-sync.sh` → finalize → inventory →
  review + acknowledge → sync (copy → verify → record presence) → prune → beets
  path reconciliation → notify.
- **The DB** records content identity (decoded-audio MD5), locations, per-copy
  presence, sidecars, planned actions, lyric lineage, and listening notes.

Detail: [`architecture.md`](architecture.md). The owner is **Daniel**, a staff engineer;
write accordingly: answer first, no preamble or recap, no emojis.

---

## 2. Where things live on the owner's machine

| What | Where |
|------|-------|
| Checkout (production runs from it, on `main`) | `~/Music/music-pipeline` |
| Worktrees | `~/Music/music-pipeline/.claude/worktrees/<name>/` |
| SpindleBot config / secrets | `~/.config/spindlebot/config.toml`, `secrets.toml` |
| SpindleBot DB | `~/.config/spindlebot/spindlebot.db` (schema 8) |
| Collection ignore list / lrc-editor state | `~/.config/spindlebot/collection-ignore.json`, `lrc-editor-state.json` |
| beets config / DB | `~/.config/beets/config.yaml` (diverges from the repo template), `~/.config/beets/library.db` |
| Working areas | `~/Library/Application Support/SpindleBot/{Import,Processing,Pending,Duplicates}` |
| Retention drive | First enabled `local_drive` in `[[destinations]]`; library under `Music/Library` on that volume |
| DAP | A `[[locations]]` entry with `is_retention = true` (ROADMAP P0 #3) |
| launchd agents | `com.strangestlens.music-watcher`, `com.strangestlens.music-sync` |
| AI venv | `~/.local/share/spindlebot/ai-venv` (Python 3.13) |

Live config facts that change behaviour:
- `core.min_copies = 2`, with one retention drive plus the DAP counting as
  retention. `review` flags most content below floor; that's expected.
- `core.auto_sync_on_import` is unset (false): imports log a hint, not a sync.
- The only enabled destination is the retention drive; rclone is commented out.

Older history refers to plan files under `~/.claude/plans/`. They no longer
exist. Where a decision they held wasn't captured in the repo, the item is
marked design-gated below.

---

## 3. Open PRs and worktrees

**Open PRs, all green and mergeable, waiting on Daniel:**
- **#76** `fix/lrclib-duration-cap`: a permanent lrclib 400 no longer strands
  an album. Production doesn't have this fix yet.
- **#77** `fix/fetch-art-network-flake`: removes the one test that hit the
  network.
- **#74** `feat/listening-notes-cli`: notes N1+N2. Rebase onto `main` before
  merge (it predates #70/#73/#75).

**Worktrees under `.claude/worktrees/`:**

| Worktree | Branch | State |
|----------|--------|-------|
| `lrclib-duration` | `fix/lrclib-duration-cap` | PR #76 |
| `fetch-art-flake` | `fix/fetch-art-network-flake` | PR #77 |
| `musing-cerf-c07dbd` | `feat/collection-sort` | **Uncommitted WIP** (10 files). Don't touch without asking. |
| `push-command` | `feat/push-command` | Merged as #75; removable |
| `dap-auto-sync-question-28a090` | detached | Content landed in #73; removable |
| `jolly-wu-55785e` | `claude/busy-kepler-07f355` | Stale (July); removable |
| `system-sync-after-merges-1e07f9` | detached at #74 head | Review checkout; removable |
| `roadmap-handoff` | `handoff` | This branch |

Removing worktrees is Daniel's call.

---

## 4. Read these gotchas before touching…

| Area | Gotchas in CLAUDE.md |
|------|----------------------|
| Import runner, beets | 1, 2, 2a, 3, 4b, and the duplicate-handling rules in the stage sequence |
| Sync, prune, delete, push | 4, 4a, 14, plus the PRUNE vs DELETE gates in the epic section |
| Lyrics fetch | 7a, 7b (7b lands with #76) |
| `lyric_timing`, lrc-editor | 9, 10, 11 |
| Collection audit | 12, 13 |

---

## 5. Parallel-work lanes

The rules for working alongside other agents are in `docs/development.md`. These
are the lanes for the current queue, by the files each item touches. Don't take
an item whose lane already has an open branch unless you're told to stack on it.

| Lane | Files | Items |
|------|-------|-------|
| **Sync** | `services/sync.py`, `services/reconciler.py`, `services/push.py`, `db/repositories/action_repo.py`, presence repos | W1, W3, W4, W6; D1, D4 |
| **Import** | `pipeline/runner.py`, `pipeline/stages/*`, `disc.py`, `staging.py` | W2, W9; D2, D7 |
| **Lyrics text** | `lyric_timing/`, `lrc-editor`, `stages/fetch_lyrics.py` | W5, W8 |
| **Notes** | `core/notes*.py`, notes services/repos, `collection-browser` | N3+ after #74 |
| **Collection** | `collections/`, `core/collection*.py`, `services/collection_*` | sorting WIP |
| **Shell / ops** | `*.sh`, `setup.sh`, `tests/shell/` | D6 (also touches Sync) |
| **New module** | `services/backup.py` + its CLI | W10 |

The Sync lane is the bottleneck and must be serialized: W1 → W3 → W4 → W6.
Start with W1; it's the smallest and the most dangerous.

---

## 6. Work queue

One agent, one branch per item. **Gate:** *ready* = go; *design-gated* = agree
the design with Daniel first and write no code until then; *real-data* =
Daniel runs or approves the final validation against the live library. ROADMAP.md
has the background for each.

### Ready

**W1 — Sync verifies the source against the plan** (P0 #1) · Sync · ready; destructive-adjacent
- Before copying in `execute_pending`, check the source's sha256 against the
  sha256 the action was planned on. On mismatch: fail the action, write no
  presence, leave it unexecuted, report it. `pending_action` has no sha column,
  and reading presence at execute time isn't enough (it can be refreshed between
  plan and execute), so this likely means schema v9 adding `expected_sha256`
  captured at plan time. Claim v9.
- Done when: a test replaces the source between plan and execute and proves no
  presence row is written, and that test fails against `main`.

**W2 — Import directory-collision guard** (P0 #2) · Import · ready
- Refuse (or divert to `duplicates_dir`) when the destination directory exists
  and holds audio whose identity isn't the incoming album's. Same-album
  re-imports keep the existing duplicate path.
- Done when: a test on a case-insensitive path shows `Maya` vs `MAYA` is
  refused, and it fails against `main`.

**W3 — Restore files deleted from a destination** (P0 #4) · Sync · ready after W1
- Preferred: the reconciler proposes COPY for a confirmed absence. Done when a
  test deletes a file from a target, re-inventories, reconciles, syncs, and the
  file is back with a verified presence row.

**W4 — Dedupe COPY actions on a retried push** (P0 #7) · Sync · ready after W3

**W5 — `format_lrc` preserves LRC metadata tags** · Lyrics text · ready
- Both `lyric_timing/lrc.py` and lrc-editor's copy. Round-trip test with
  `[ar:]`/`[ti:]`/`[al:]`/`[re:]`/`[offset:]`; keep the two byte-identical.

**W6 — Phase 4.1 lyric auto-propagation** · Sync · ready after W1/W3; destructive (overwrites `.lrc`)
- Includes the `lyric_version_presence.observed_utc` follow-up.

**W7 — Readable `review` output for sidecars** · `cli.py` render only · ready, low priority

**W8 — lrc-editor origin check on mutating routes** · Lyrics text · ready
- Mirror `collection-browser`'s `_same_origin` on every `POST` route (`/open`,
  `/save`, `/commit`, `/ai-arrange`, `/audit/*`, `/load`, `/preview`); test that a
  cross-origin POST is refused.

**W9 — Shared retry + failure journal** · Import · ready
- `retry_with_backoff` honouring `Retry-After` for iTunes, CAA, Telegram. Keep
  lrclib's miss-vs-error semantics exactly (gotchas 7a/7b).

**W10 — Phase 6 DB snapshots** · New module · ready to build; scheduling it live is real-data
- SQLite online backup to a configured location, a retention count, a restore
  command, and a `check` warning when the last snapshot is stale.

### Design-gated
- **D1 — Destination model:** split retention from the floor, sync modes,
  per-destination mount automation. Start with the retention split.
- **D2 — Disc-metadata fix command.** Must not release a half-ripped album from
  a flat Import area.
- **D3 — Metadata propagation** (P0 #6): what's authoritative; how to tell it
  from corruption.
- **D4 — All-paths presence** (P0 #5): schema shape, reconciler impact.
- **D5 — Non-audio extras:** set-of-files model, schema.
- **D6 — Fold `music-sync.sh` into a service:** step boundaries; prune stays
  outside `push_location`.
- **D7 — Discogs preflight at import:** behaviour when Discogs is down.
- **D8 — Phase 5 plugin hook** shape.

### Not for agents
- Merging, reloading launchd agents, granting macOS permissions.
- Importing Daniel's listening-notes corpus (the source needs manual heading
  fixes first).
- Anything that needs a credential.

---

## 7. Keeping this branch current

- When a PR merges or a worktree comes or goes, update §3 and the ROADMAP status
  table, date the header, and commit to `handoff`.
- Rebase `handoff` onto `main` after merges so its links resolve.
- If you find a rule here that should hold beyond the current work, it belongs
  in `CLAUDE.md` on `main` via a normal PR, not here.
