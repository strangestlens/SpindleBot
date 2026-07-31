# Development

Read [`CLAUDE.md`](../CLAUDE.md) first. It's the contract — layering rules,
naming conventions, the active epic, and the full gotcha list. This page covers
the mechanics: how to run things, what CI enforces, and how work lands.

## Running the suites

```bash
python3 -m pytest tests/ --ignore=tests/shell    # 43 Python test modules
bats tests/shell/                                 # shellcheck + shell integration
python3 -m ruff check .                           # lint
```

Both suites must be green before anything lands.

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

## CI

`.github/workflows/ci.yml`, on every push and PR. Two jobs, both required:

| Job | Runs |
|-----|------|
| `python` | `ruff check` + `pytest tests/ --ignore=tests/shell` on a **3.11 + 3.14 matrix** |
| `shell` | `shellcheck` on the shell scripts + `bats tests/shell/` |

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

Branch names follow conventional prefixes — `feat/`, `fix/`, `docs/`.

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
