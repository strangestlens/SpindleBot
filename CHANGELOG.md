# Changelog

Notable changes to SpindleBot, loosely following [Keep a Changelog](https://keepachangelog.com/).
Versioning is 0.x (pre-1.0, no stability guarantees) until the content-addressed library
refactor epic is complete.

## [Unreleased]

### Added
- Optional collection audit: `spindlebot collection-audit` compares an external collection
  against the digital library and lists what you own but haven't ripped. Purely assistive —
  reads the library, writes only a fetch cache, and stays inert when unconfigured.
  - Pluggable sources behind a one-method provider protocol, each split into an impure client
    and a pure transformer: `discogs` (public collections need no credentials; an optional
    token raises the rate limit from 25 to 60 req/min) and `fixture` (a hand-written JSON
    collection — test double and the way in for anyone not on Discogs)
  - Artist-scoped matcher with three outcomes (`owned` / `uncertain` / `missing`), so a
    normalization miss surfaces for review instead of sending you to re-rip a disc you own
  - `[collection]` config block and `[discogs] token` secret, both optional; `--handle`
    overrides the configured account
  - Library index defaults to `auto`, the union of beets and the SpindleBot DB. Neither is a
    superset (measured on a real library: 67 albums DB-only, 2 beets-only), and either alone
    inflates the missing list. Every run prints which index contributed what; an empty index
    is a hard failure rather than a "your whole collection is missing" report

### Changed
- CI now runs the Python suite on a 3.11 + 3.14 matrix. Testing only the 3.11 floor let
  dev-machine-only syntax through to CI (a 3.12+ f-string), and testing only 3.14 would let
  the documented 3.11 floor rot

## [0.3.0] - 2026-07-30

First tagged release. This entry summarizes current capability rather than itemizing all 86
prior commits (PRs #1-#57) — see `git log --oneline` for the full history.

### Added
- Event-driven import pipeline: XLD rip -> fswatch -> `spindlebot import` -> pretag -> beet
  import (with duplicate detection) -> multidisc fix -> beet move -> posttag -> art/lyrics
  fetch -> per-album promotion to Pending gated on lyric-completeness -> log archive -> notify
- Content-addressed SQLite system-of-record (`spindlebot.db`, schema v1-v7) tracking content
  identity, first-class locations, and per-location presence (#26-#48)
- Reconciler-driven mount-sync CLI (`inventory` / `review` / `prune` / `delete`): copy, verify,
  record presence; retention-floor-gated deletes; safe prune of authoring copies (#31, #35-#39)
- Phase 4.0 causal-lineage substrate for bidirectional `.lrc` sync (#48)
- Standalone `lrc-editor` (Flask/WaveSurfer.js) for manual lyric timing, plus an AI lyric-timing
  subsystem (Demucs vocal separation + wav2vec2 CTC forced alignment) with `audit`/`retime`
  CLIs and an "AI Arrange" button in lrc-editor (#49)
- `spindlebot check` / `config` / `restart` operational commands; full pytest + bats CI

### Changed
- Working areas renamed and relocated under `~/Library/Application Support/SpindleBot/`
  (Import / Processing / Pending), config- and env-var-driven throughout (#26, #45)
- Mount-sync now driven entirely from `config.toml` destinations, no hardcoded drive or paths
  (#53-#55)

### Fixed
- Multidisc flex-attribute, shell `PYTHONPATH`, AppleDouble/orphan-sidecar, and duplicate-import
  edge cases accumulated through the import and sync hardening passes (#40, #44, #47, others)
