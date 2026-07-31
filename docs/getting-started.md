# Getting started

Setting SpindleBot up on a fresh Mac, from clone to first import.

## Prerequisites

- macOS on Apple Silicon
- Homebrew, with `brew install python beets fswatch mpv`
- XLD, for actual CD ripping — not needed to exercise the pipeline on existing
  files
- Python 3.11+ (`setup.sh` installs `tomli` automatically if you're on an older
  3.x)

## 1. Clone and run setup

```bash
git clone https://github.com/strangestlens/SpindleBot.git ~/Music/music-pipeline
cd ~/Music/music-pipeline
./setup.sh
```

`setup.sh` is idempotent and does the following:

1. Creates `~/.config/spindlebot/` and copies `config.toml.example` →
   `config.toml` and `secrets.toml.example` → `secrets.toml`, skipping either if
   it already exists.
2. Writes `~/.config/spindlebot/bootstrap.sh` with your Python path and pipeline
   dir baked in — every shell script sources this to get its `$SPINDLEBOT_*`
   vars.
3. Creates the Import / Processing / Pending / Archive working directories.
4. Installs `music-watcher.sh` → `~/.local/bin/`.
5. Generates and loads the two launchd agents into `~/Library/LaunchAgents/`.
   The home dir, log dir, and the retention volume to watch all come from your
   config — nothing is baked into a checked-in plist.
6. Runs `python3 -m spindlebot check`.

Re-run it any time you move the pipeline directory.

> **If you're upgrading rather than installing fresh**, step 3 also runs
> `migrate_work_dirs`: it relocates anything still sitting in the pre-rename
> defaults `~/Music/Staging` and `~/Music/Library` into the new Import and
> Pending areas, and rewrites the matching paths in the beets DB. It's
> idempotent and never deletes the old top-level directories, but it does move
> files — worth knowing before you run it. The old archive default
> (`~/Music/All Discs`) is deliberately *not* migrated.

## 2. Fill in config and secrets

```bash
$EDITOR ~/.config/spindlebot/config.toml    # paths, tool locations, destinations
$EDITOR ~/.config/spindlebot/secrets.toml   # Telegram token, Genius API key
```

At minimum you need to set `[[destinations]]` to point at your own retention
drive, and check that the `[tools]` paths match your Homebrew install.
`config.toml.example` is heavily commented; [configuration](configuration.md)
covers the parts the comments can't.

## 3. Validate

```bash
cd ~/Music/music-pipeline
python3 -m spindlebot check
```

`check` verifies that the Import / Pending / log / Archive dirs exist (not
Processing or Duplicates), the tool binaries are executable, the beets DB is
present, each enabled destination is reachable, and the credentials are set —
printing a concrete fix suggestion for anything that fails.

Two caveats on reading the output. It checks the Telegram and Genius
credentials unconditionally, so if you aren't using Telegram you'll see it fail
those lines even with `notifications.telegram_enabled = false`; that's cosmetic.
And `check` is a config-and-environment probe, not a smoke test — a clean run
means nothing is obviously misconfigured, not that an import will succeed.

## 4. Point beets at the Pending area

beets does the MusicBrainz matching and owns its own item DB. Its `directory:`
must match `core.pending_dir`, or the import will tag correctly and then file
the album somewhere SpindleBot isn't looking. `beets-config.yaml` in the repo is
a working reference.

## 5. Run the tests

```bash
python3 -m pytest tests/ --ignore=tests/shell    # Python suite
bats tests/shell/                                 # shell suite (needs bats + shellcheck)
```

Both must be green. CI runs the same two jobs on every push and PR — see
[development](development.md).

## 6. First import

Drop an album folder into the Import area, or rip a CD with XLD configured to
write its `.log` there. The fswatch daemon picks it up within seconds and runs
the pipeline; you'll get a macOS notification (and a Telegram message, if
configured) when it lands.

To drive it by hand instead:

```bash
python3 -m spindlebot import-staging --dry-run    # what would be imported
python3 -m spindlebot import "~/Library/Application Support/SpindleBot/Import/Some Album/"
```

Watch it work with `tail -f ~/.config/beets/watcher.log`.

If the album stops in Processing rather than reaching Pending, lyrics are
incomplete for at least one track — that's the designed behaviour, not a
failure. `python3 -m spindlebot finalize` retries and promotes. See
[operations](operations.md#when-an-album-is-stuck-in-processing).

## 7. Inventory the retention drive once

Before the first sync, the destination needs one manual scan — the reconciler
refuses to plan against a location it has never seen, and `music-sync.sh`
doesn't scan the target itself:

```bash
python3 -m spindlebot inventory --location "MyDrive"    # your destination's name
```

Do this once per destination. Skip it and the first sync aborts with
`review failed`.

## Where to go next

- [Commands](commands.md) — the full CLI surface
- [Architecture](architecture.md) — what the areas are and why Processing exists
- [Operations](operations.md) — daemons, logs, sync, and troubleshooting
- [`CLAUDE.md`](../CLAUDE.md) — read this before changing any code

---

## Appendix: the author's machine

Reference values, for reading examples elsewhere in the docs. Everything here
is configuration — substitute your own.

- macOS, Apple Silicon
- Python: `/opt/homebrew/bin/python3` (3.11+; currently resolves to 3.14)
- beet: `/opt/homebrew/bin/beet`; mpv: `/opt/homebrew/bin/mpv`
- beets DB: `~/.config/beets/library.db`; beets config: `~/.config/beets/config.yaml`
- SpindleBot DB: `~/.config/spindlebot/spindlebot.db`
- SpindleBot config: `~/.config/spindlebot/config.toml` + `secrets.toml`
- Logs: `~/.config/beets/watcher.log` (import), `~/.config/beets/music-sync.log` (sync)
- Retention: `/Volumes/DwRugged/Music/Library` — examples that name `DwRugged`
  mean this specific external drive
- launchd agents: `com.strangestlens.music-watcher`, `com.strangestlens.music-sync`
- AI venv: `~/.local/share/spindlebot/ai-venv`; editor state:
  `~/.config/spindlebot/lrc-editor-state.json`
