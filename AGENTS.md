# AGENTS.md

Instructions for any coding agent working in this repository.

1. Read [`CLAUDE.md`](CLAUDE.md). Despite the name, it is the binding contract
   for every agent: layering, conventions, schema history, the agent rules
   (including what needs the owner's approval), and the numbered gotcha list.
   Where anything else disagrees with it, CLAUDE.md wins.
2. Read [`docs/development.md`](docs/development.md) for the mechanics: test and
   lint commands, worktrees, the review loop, working alongside other agents,
   and environment quirks.
3. Read the [`docs/`](docs/README.md) page for whatever area you're changing.
4. Current status, open PRs, and the work queue are kept on a `handoff` branch
   that is never merged, because they go stale as work lands:
   `git show handoff:docs/handoff.md` and `git show handoff:ROADMAP.md`.

The short version: work in your own worktree on a `feat/`/`fix/`/`docs/` branch
off the latest `origin/main`; keep `pytest`, `ruff check`, `shellcheck`, and
`bats` green; no network in tests; no secrets or personal paths (the repo is
public). Never run `sync`, `prune --execute`, `delete --execute`, or anything
else against the real library or drives without the owner's explicit go.
