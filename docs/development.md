# Development

Read [`CLAUDE.md`](../CLAUDE.md) first. It's the contract — layering rules,
naming conventions, the active epic, and the full gotcha list. This page covers
the mechanics: how to run things, what CI enforces, and how work lands.

## Running the suites

```bash
python3 -m pytest tests/ --ignore=tests/shell    # Python suite
bats tests/shell/                                 # shellcheck + shell integration
python3 -m ruff check .                           # lint
```

Both suites must be green before anything lands. Before every commit, run all
of these plus the floor syntax gate:

```bash
python3 -m pytest tests/ --ignore=tests/shell -q
python3 -m ruff check .
shellcheck ./*.sh
bats tests/shell/
/opt/homebrew/bin/python3.10 -m compileall -q spindlebot/ tests/ lyric_timing/
```

If ruff fails on code your change didn't touch, suspect version drift first
(check which ruff CI downloaded) before "fixing" the code.

## Testing philosophy

**Tests are the contract, not the implementation.** A failing test after a code
change means: investigate whether the behaviour broke, first. Only change a test
if the intended behaviour intentionally changed — and that should be an explicit
decision, not a response to friction. Never silently update a test to make CI
green.

Practical consequences:

- Every new module in `spindlebot/` or `lyric_timing/` needs tests in `tests/`.
- Anything touching FLAC identity uses the controllable-STREAMINFO-md5 fake-FLAC
  fixture (`_write_flac` in `tests/test_identity.py` / `tests/test_inventory.py`),
  so hashes are deterministic without shipping binaries.
- External calls are injected, never live. The Discogs client takes a fetcher;
  the alignment backend sits behind a `Protocol` with a deterministic mock;
  `ImportRunner` takes a `sync_runner`. The standard `pytest` run touches no
  network and needs none of the AI dependencies.
- `tests/test_lyric_timing_torchaudio.py` is the one exception — it exercises
  the real model stack and is skipped unless `LYRIC_TIMING_IT_AUDIO` and
  `LYRIC_TIMING_IT_LRC` point at a real track. It never runs in CI.
- Patch `time.sleep` through the module's own namespace (e.g.
  `spindlebot.pipeline.stages.fetch_lyrics.time.sleep`), so retries run instantly.
- Fixture names describe the role a location plays in the test
  (`RetentionDrive`, `PortablePlayer`), never a real device.

### Proving a regression test

A test written alongside a fix isn't known to cover the bug until it has failed
against the unfixed code:

```bash
backup=$(mktemp)                                             # per-run: agents run this concurrently
cp spindlebot/<module>.py "$backup"                          # keep the fix
git show origin/main:spindlebot/<module>.py > spindlebot/<module>.py
python3 -m pytest tests/<new_tests>.py -q                    # expect failures
cp "$backup" spindlebot/<module>.py && rm "$backup"          # restore
python3 -m pytest tests/<new_tests>.py -q                    # expect green
```

Say in the commit or PR which tests failed without the fix and which pass either
way. Guard assertions that hold in both directions are fine; they just aren't
regression tests.

### Filesystem-sensitive bugs

The retention drive is case-insensitive HFS+, and CI runs on a case-sensitive
Linux filesystem, where two directories differing only in case never collide.
A test for a case-collision bug has to run on a case-insensitive path (a default
macOS temp dir is one) and skip with a stated reason elsewhere. The same applies
to storage-class bugs in SQLite: gotcha 4a's fix is only covered by asserting
`typeof(path)='blob'`, not the value.

## CI

`.github/workflows/ci.yml`, on every push and PR. Two jobs, both required:

| Job | Runs |
|-----|------|
| `python` | `ruff check` + `pytest tests/ --ignore=tests/shell` on a **3.11 + 3.14 matrix** |
| `shell` | `shellcheck ./*.sh` (every script, not a list that drifts) + `bats tests/shell/` |

**Why both Python versions.** 3.11 is the floor the code claims to support
(`StrEnum`, `tomllib`); 3.14 is what the dev machine runs. Testing only the
floor lets dev-machine-only syntax through — a 3.12+ f-string did exactly that.
Testing only 3.14 would let the documented floor rot.

Locally, `python3.10 -m compileall` is a cheap pre-push syntax gate for the
floor.

## Code quality non-negotiables

- **shellcheck clean** on all `.sh` files. `# shellcheck disable=SC####` needs a
  comment explaining why — no blanket suppressions. beets template vars
  (`$albumartist`, `$path`) must be single-quoted in shell, with
  `# shellcheck disable=SC2016` and the comment `"beet template var, not a bash var"`.
- **ruff is pinned** (`ruff==0.15.20` in `requirements.txt`). An unpinned linter
  drifts and breaks unrelated PRs on new default rules; bumping it is a
  deliberate cleanup, not a side effect of someone else's change.
- **No print-driven side effects in library code.** `print()` belongs in
  `cli.py` and the standalone web tools, nowhere else. Services return typed
  results; the CLI renders them.
- **Nothing heavy in `spindlebot/`.** torch, torchaudio, demucs, and Flask stay
  out of the core package — that boundary is what keeps the pipeline and CI
  light. Heavy AI deps go in `lyric_timing/` with its own venv; Flask tools
  (`lrc-editor`, `collection-browser`) live at the repo root as standalone
  executables.
- **Schema is append-only.** Never edit a shipped `schema_v*.sql`; add a new
  version and register it in `migrations.py`.

## Branch and PR workflow

One feature branch per phase off the latest `main`; sub-tasks are ordered
commits on that branch, each one green. Push and open a PR only when asked. PRs
squash-merge to `main`; pull `main` before starting the next branch.

Branch names follow conventional prefixes — `feat/`, `fix/`, `docs/`, `chore/`,
`ci/`, `refactor/` — plus a description of the work. Commit messages are
conventional too (`fix(lyrics): …`, `feat(cli): …`), with a body that says why.

### Worktrees

Every task gets its own worktree off a freshly fetched `main`:

```bash
git -C ~/Music/music-pipeline fetch
git -C ~/Music/music-pipeline worktree add .claude/worktrees/<short-name> -b <type>/<what> origin/main
cd ~/Music/music-pipeline/.claude/worktrees/<short-name>
git branch --show-current        # before the first commit, every time
```

Never run `git checkout -b` in the main checkout while editing in a worktree:
the edits stay on the worktree's branch, and commits have landed on the wrong
branch that way twice. The main checkout stays on `main` because production
runs from it.

### Review loop

Copilot reviews every push to an open PR. After each push, wait about six
minutes, then read both places it writes:

```bash
gh api repos/strangestlens/SpindleBot/pulls/<N>/comments \
  --jq '.[] | {path, line, original_commit_id, body}'
gh pr view <N> --json reviews --jq '.reviews[] | {author: .author.login, state, body}'
```

Low-confidence findings are folded into the **review body**, not posted inline,
and several real bugs have turned up there. Comments often refer to an earlier
commit (`original_commit_id`); one already fixed is stale. Verify each finding
against the code, then fix it with a test or reply on it with the reason it
doesn't apply, push, and repeat until nothing is left open.

PR descriptions say what changed, why, how it was verified (including the
fail-without-the-fix split), and what was deliberately left out.

### Working alongside other agents

Several agents may have branches open at once.

- Pick work whose files no open branch is already changing, or say in your PR
  that you stacked on that branch.
- Only one open branch may add a schema migration. Claim the version in the PR
  title (`… (schema v9)`) and renumber if `main` gets one first.
- `cli.py`, `CLAUDE.md`, `CHANGELOG.md`, and `migrations.py` are shared; keep
  your edits to your own lines and expect to resolve conflicts on rebase.
- A ruff version bump rewrites every file; do it only when nothing else is open.

## Environment quirks

- **TCC on external volumes.** Reading `/Volumes/*` from an agent process fails
  with `Operation not permitted` unless that process's own signed binary has
  Full Disk Access. Claude Code, for example, is signed separately from the
  Claude desktop app, so a grant on the desktop app does nothing for it. The
  owner has to grant it and relaunch; don't try to work around it.
- **The installed beets config isn't the template.** `beets-config.yaml` in the
  repo is what `setup.sh` installs on a fresh machine. The live
  `~/.config/beets/config.yaml` has since diverged; read it before reasoning
  about paths or track numbering.
- **AI venv.** `lyric_timing` heavy work runs in `~/.local/share/spindlebot/ai-venv`
  (Python 3.13), from the repo root (gotcha 9). Demucs on MPS isn't
  reproducible run to run; see [AI lyric timing](ai-lyric-timing.md) before
  benchmarking.

## Repo layout

`CLAUDE.md` carries the annotated file map and the layering rules that go with
it. The short version:

```
spindlebot/       the package — core/ (pure) → db/ (SQL only in repositories/)
                  → services/ (orchestration) → cli.py (the only place that prints)
lyric_timing/     optional AI subsystem, peer package, heavy deps
tests/            pytest suite + tests/shell/ (bats)
*.sh              thin shims that source bootstrap.sh
lrc-editor        standalone Flask apps, outside the package on purpose
collection-browser
```
