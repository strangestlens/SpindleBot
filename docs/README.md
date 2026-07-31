# SpindleBot documentation

Start at the [project README](../README.md) for the overview and the flow
diagram. These are the topic guides.

## Running it

| Doc | What's in it |
|-----|--------------|
| [Getting started](getting-started.md) | Prerequisites, `setup.sh`, first config, validation, first import |
| [Configuration](configuration.md) | `config.toml` by section, secrets, env-var precedence, sync destinations |
| [Commands](commands.md) | Full CLI reference — every `spindlebot` command and `lyric_timing` |
| [Operations](operations.md) | The launchd daemons, logs, sync/prune/delete semantics, troubleshooting |

## Understanding it

| Doc | What's in it |
|-----|--------------|
| [Architecture](architecture.md) | The two flows, the working areas, content addressing, the import stage sequence |
| [Development](development.md) | Tests, CI, linting, branch and PR workflow |

## Features

| Doc | What's in it |
|-----|--------------|
| [Lyrics](lyrics.md) | `.lrc` sidecars, playback, the lrc-editor timing editor |
| [AI lyric timing](ai-lyric-timing.md) | Optional forced-alignment re-timer: `audit`, `retime`, editor integration |
| [Collection audit](collection-audit.md) | Optional: what you own on disc but haven't ripped, plus collection-browser |

## Elsewhere in the repo

- [`CLAUDE.md`](../CLAUDE.md) — the contract for anyone (human or agent) changing
  the code: layering rules, conventions, the active epic, the full gotcha list.
- [`ROADMAP.md`](../ROADMAP.md) — where it's going.
- [`CHANGELOG.md`](../CHANGELOG.md) — what's shipped.
- [`archive/original-roadmap.md`](archive/original-roadmap.md) — the superseded
  6-phase plan, kept for context.
