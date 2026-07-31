# SpindleBot — Roadmap

*Last updated: July 2026*

What's still ahead. For what's already shipped see [`CHANGELOG.md`](CHANGELOG.md);
for per-phase detail on the epic in flight see [`CLAUDE.md`](CLAUDE.md).

## Vision

A personal music intelligence system. Near term: a clean, well-tested,
configurable pipeline that anyone can run. Longer term: a complete local-first
music management platform with remote streaming access — organized, resilient,
and extensible.

## In flight

**Content-addressed library refactor.** Replacing "the library is whatever is at
these known paths" with a SpindleBot-owned SQLite DB that is the system of
record for content identity, every location a copy lives, and eventually version
history. Phases A through 3 are merged — the DB, the reconciler, and the
destructive mount-sync cutover are all in daily use.

The active piece is **Phase 4, bidirectional lyric sync**. Once a `.lrc` exists
in more than one place, edits can happen anywhere and "which copy is newest"
stops being obvious. 4.0 (the causal-lineage substrate) is merged; 4.1
(auto-propagating clean wins, preserving conflict files) and 4.2 (a `conflicts
list|resolve` CLI) are not.

Then: 5 — beets plugins and the AI re-timer wired into the pipeline proper;
6 — DB snapshots; 7 — a daemon.

[`CLAUDE.md`](CLAUDE.md) → "Content-addressed library refactor" is the live
status.

## Next

### Error recovery and resilience

Transient failures shouldn't silently drop data. Partly addressed — the
lyric-completeness gate plus `finalize` means a failed lyric fetch can't strand
an album — but the general case is still open:

- A shared `retry_with_backoff(fn, attempts=3, base_delay=1.0)` around every
  external API call (lrclib, iTunes, CAA, Telegram), respecting `Retry-After` on
  a 429.
- A failure journal at `~/.config/spindlebot/failed.jsonl` — one record per
  failed stage, with `spindlebot retry-failed` to replay it and a count in
  `spindlebot status`. Machine-readable, unlike today's log-and-hope.
- Pre-flight checks: before an import, verify the Import area is readable and
  beet is available; before a sync, verify the destination is reachable and has
  space. Fail fast rather than partway through.

### Input sources

The pipeline takes CD rips and folder drops. What's missing is a dedicated
digital-download path: downloads usually arrive with correct metadata already,
so pretag should act in a "gentle mode" that normalizes rather than strips. A
folder whose audio files carry consistent `ALBUM`, `ARTIST`, and `DATE` can be
treated as a complete album and imported directly; sparse metadata falls back to
a MusicBrainz search.

### A plain-text lyric source

lrclib is the only source, and when it misses there's nothing to fall back on.
Shazam and Genius both have plain (unsynced) text.

The alignment half of this problem is **already solved**: `lyric_timing retime`
does Demucs vocal separation plus wav2vec2 CTC forced alignment, which is
stronger than the timestamp-estimation heuristics originally sketched for this,
and it reports per-line confidence. So the work is only the fetch side — get
plain text from somewhere, hand it to `retime`, done. No "estimated" marker is
needed either: quality is already surfaced by `lyric_timing audit` and by
lrc-editor colouring sub-0.5-confidence markers orange.

To be explicit, because the two get conflated: `retime` re-times lyric text
that's *already in the `.lrc`*. It is not a lyric source and does nothing for a
track lrclib has never heard of.

### Collection audit extensions

Shipped and in use; these are the natural next steps, none scheduled.

- **Other providers.** A provider is one function (account →
  `list[CollectionItem]`), split into an impure client and a pure transformer,
  so MusicBrainz collections, a CSV export, or Last.fm each mean one module and
  a registry entry — no changes to the matcher or the CLI. MusicBrainz is the
  interesting one: it would bring release MBIDs, which short-circuit the string
  matcher entirely. The `mb_release_id` path already exists and is tested, and
  the `fixture` provider reads the field; what's missing is a *remote* source
  that supplies one, since Discogs exposes no MBID.
- **Other media.** `--media vinyl` already works — a "what have I got on vinyl
  but not digitally?" report is a flag away.
- **Transliteration** is the one matching gap left: a library tagged in a
  different script than the collection lists it (`ベック` vs `Beck`). Solving it
  needs a transliteration dependency, which would violate the light-deps
  boundary on `spindlebot/`. The ignore list covers it instead, and that's the
  intended answer rather than a stopgap.

### Cloud backup destination

`[[destinations]]` already supports `type = "rclone"`; what's missing is running
one in anger. For a 1–2 TB FLAC library, Backblaze B2 + rclone is the pragmatic
answer — $6/TB/month, $0.01/GB egress, incremental sync, checksum verification,
and a dry-run mode. Wasabi ($6.99/TB, no egress fees) is better if you'd restore
often; S3 Glacier Instant Retrieval (~$4/TB) is cheapest at scale with slower
restores.

iCloud Drive is **not** suitable. iCloud Music Library imposes a 200 MB per-file
limit and transcodes unmatched tracks to 256 kbps AAC — both fatal for FLAC —
and as a generic file sync target its reliability on directories of large files
at scale is poor.

## Later

### Remote library access — use Navidrome

Don't build this. Navidrome already does exactly what's needed: a lightweight
self-hosted streaming server, single Go binary, ~200 MB RAM for 40,000 tracks,
implementing the Subsonic/OpenSubsonic API so 20+ existing client apps work with
it.

It fits SpindleBot unusually well. It reads from a static music directory, which
is precisely what the pipeline produces; it surfaces the metadata SpindleBot
works to normalize (album artist, year, genre, MusicBrainz IDs); and it supports
`.lrc` sidecars natively — the exact files the lyric fetch writes.

Deployment: run it against the library on the retention drive, expose it via
Tailscale or a reverse proxy, and no data leaves your infrastructure.

Clients worth knowing: **Symfonium** and **Substreamer** on iOS/macOS (CarPlay
support), **Symfonium**/**DSub** on Android, **Sonixd** or the built-in web UI on
desktop. If Navidrome ever isn't enough, **Jellyfin** is heavier but adds video
and official mobile apps, and **Plex + Plexamp** is the premium option at $120
lifetime or $5/month for remote streaming.

What SpindleBot contributes: the library Navidrome indexes is only as good as
the metadata feeding it. Normalized tags, consistent album artist, embedded art,
`.lrc` files — the two systems are naturally complementary.

### A Mac app

macOS-only is fine and true to the platform this was built for. The logical
endpoint is a real app: a SwiftUI shell with an embedded Python runtime, the
pipeline stages running as background processes, and the surface area that's
currently config files and log tails becoming a config editor, a status
dashboard, an import queue, and notification preferences. Navidrome runs as a
bundled service; the launchd plists become internal app infrastructure.

The core Python doesn't change — only the surface around it does. That's a real
path to the App Store.

---

The original 6-phase roadmap (Phases 1–2 delivered, the rest superseded by the
content-addressed epic) is archived at
[`docs/archive/original-roadmap.md`](docs/archive/original-roadmap.md).
