# Configuration

Two files, both under `~/.config/spindlebot/`:

| File | Holds | Created by |
|------|-------|------------|
| `config.toml` | Paths, tool locations, behaviour, destinations | `setup.sh`, from `config.toml.example` |
| `secrets.toml` | Telegram token, Genius key, Discogs token | `setup.sh`, from `secrets.toml.example` |

**`config.toml.example` is the per-key reference** — it's heavily commented and
kept current. This page covers what the comments can't: how the pieces relate,
what's load-bearing, and the rules that aren't visible from a single key.

Apply changes with `python3 -m spindlebot check`, then `spindlebot restart` if
you changed anything the daemons read.

## How config reaches the shell scripts

Nothing hardcodes a path. `setup.sh` writes `~/.config/spindlebot/bootstrap.sh`,
every shell script sources it, and it evals `python3 -m spindlebot config shell`
to export the `$SPINDLEBOT_*` variables.

This is load-bearing: **if Python or the config is broken, every `$SPINDLEBOT_*`
var comes back empty.** Scripts fail loudly by design rather than operating on
empty paths. To see what they'll get:

```bash
python3 -m spindlebot config shell
python3 -m spindlebot config get core.pending_dir
```

Note the env var names carry a `_DIR` suffix: `SPINDLEBOT_IMPORT_DIR`, not
`SPINDLEBOT_IMPORT`. The short form silently resolves to empty.

## `[core]` — the working areas

`import_dir`, `processing_dir`, `pending_dir`, `duplicates_dir`, `archive_dir`,
`log_dir`. What each area is for and why there are three of them is covered in
[architecture](architecture.md#the-working-areas).

The legacy keys `staging_dir` and `library_dir` are still honored if present —
they're the pre-rename names for `import_dir` and `pending_dir`.

`auto_sync_on_import` (default `false`) decides what a successful import does
next. Left off, the run finishes fast and logs a hint to sync manually. Turned
on, the import checks whether the retention destination is mounted and invokes
`music-sync.sh` if so. A sync failure there is logged, never promoted to an
import failure.

## `[tools]`

Absolute paths to `beet`, `python`, and `mpv`, plus the beets DB and beets
config locations. These are absolute on purpose — launchd agents don't inherit
your interactive shell's `PATH`.

beets' own `directory:` must point at `core.pending_dir`. If they disagree, the
import tags correctly and then files the album where SpindleBot isn't looking.

## `[notifications]`

`macos_notify` and `telegram_enabled` toggle independently, so you can turn
Telegram off without deleting your token.

## `[lyrics]` and `[art]`

Source order and tuning. `lyrics.sources` is `["lrclib"]` — that's currently the
only implemented source, despite the `# future: add "shazam"` comment in the
example. `art.sources = ["caa", "itunes"]` tries the Cover Art Archive first and
falls back to the iTunes Store.

## `[[destinations]]` — sync targets

One block per target, as many as you like:

```toml
[[destinations]]
name    = "MyDrive"
type    = "local_drive"      # rsync to a mounted volume
path    = "/Volumes/MyDrive/Music/Library"
enabled = true

[[destinations]]
name    = "Backblaze"
type    = "rclone"           # any configured rclone remote
path    = "b2:my-music-bucket/Library"
enabled = false
```

Two rules that matter more than they look:

1. **The first enabled `local_drive` destination is the retention target.** It's
   what `music-sync.sh` syncs to, what `prune` checks before releasing a Pending
   copy, and what `delete` counts copies against.
2. **It's also the mount probe.** `setup.sh` generates the sync agent's launchd
   `WatchPaths` from it, and `auto_sync_on_import` tests it for existence before
   invoking sync. An rclone path can never serve as a mount check, which is why
   the rule names `local_drive` specifically.

Reorder the blocks and you change which drive is authoritative for retention.

## Secrets and env-var precedence

`secrets.toml` holds `[telegram] token` / `chat_id`, `[genius] key`, and the
optional `[discogs] token`. Environment variables **override the file**:

| Variable | Overrides |
|----------|-----------|
| `SPINDLEBOT_TELEGRAM_TOKEN` | `[telegram] token` |
| `SPINDLEBOT_TELEGRAM_CHAT_ID` | `[telegram] chat_id` |
| `SPINDLEBOT_GENIUS_KEY` | `[genius] key` |

Path and behaviour keys have env overrides too, following the same pattern:
`SPINDLEBOT_IMPORT_DIR`, `SPINDLEBOT_PROCESSING_DIR`, `SPINDLEBOT_PENDING_DIR`,
`SPINDLEBOT_DUPLICATES_DIR`, `SPINDLEBOT_AUTO_SYNC_ON_IMPORT`. Useful for a
one-off run against a scratch area without editing the file.

## `[collection]` — optional

The whole block can be deleted; nothing else depends on it. It configures the
[collection audit](collection-audit.md): `source`, `account`, `media`, `index`,
`cache_ttl_hours`, and `ignore_path`.
