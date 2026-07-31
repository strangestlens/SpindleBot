# AI lyric timing

*Optional. Not part of the import pipeline, and not required to run SpindleBot.*

lrclib doesn't always have *synced* lyrics. When it only has plain text, the
fetch stage still writes an `.lrc` — but every line carries `[00:00.00]`, which
is useless for playback. The `lyric_timing/` package fixes those files: it finds
them (`audit`) and re-times them against the audio by forced alignment
(`retime`).

It re-times lyric text that is **already in the `.lrc` file**. It is not a
lyrics source and cannot help when lrclib returns nothing.

**Why it's a peer package to `spindlebot/`, not part of it.** The real alignment
backend needs torch, torchaudio, and demucs. Those never enter core SpindleBot
or CI — they live in a dedicated venv, and the backend sits behind a `Protocol`
so every timing rule is unit-tested offline against a mock.

## Setup

```bash
./setup-ai.sh    # → ~/.local/share/spindlebot/ai-venv  (override: $SPINDLEBOT_AI_VENV)
```

Idempotent, and it restores your previous venv if every install attempt fails.
It tries Python 3.13 down to 3.10 and uses the first that can resolve
`requirements-ai.txt`. Models (~700 MB: Demucs `htdemucs` ~300 MB + wav2vec2
~360 MB) download to `~/.cache` on the first alignment run, not during setup.
Verify with the command `setup-ai.sh` prints on success.

`audit` needs none of this — it's pure text heuristics and runs on a bare
`python3`.

## audit — find the mistimed files

```bash
python3 -m lyric_timing audit <dir-or-.lrc ...> [--json]
```

Recurses directories for `.lrc` files and flags the suspicious ones:

| Reason | Meaning |
|--------|---------|
| `all-timestamps-identical` | every line at the same time (the `[00:00.00]` case) |
| `low-distinct-timestamps` | under 30% distinct times — bulk-stamped, not individually timed |
| `timestamps-crammed-early` | all lyrics land in the first half of the track *and* are tightly bunched |
| `non-monotonic` | timestamps go backwards in file order |
| `no-timed-lines` | has lyric content but no parseable timestamps |

The heuristics are tuned to avoid false-positiving hand-timed songs: a
well-spread file that simply ends early (long instrumental tail, a lone
`[Instrumental]` marker) is left alone. Track duration comes from the sibling
audio file via mutagen when present; without it the crammed-early check is
skipped.

## retime — fix one file

```bash
~/.local/share/spindlebot/ai-venv/bin/python -m lyric_timing retime \
    <audio> <lrc> [--overwrite] [--json] [--no-vocal-sep]
```

Run it **from the repo root** so `lyric_timing` is importable by the venv's
Python — the venv doesn't have the package installed, it resolves off the CWD.
Run it from elsewhere and you get `No module named lyric_timing`.

It keeps the lyric *text* exactly as-is and only recomputes timestamps:

1. Demucs (`htdemucs`) isolates the vocal stem — skip with `--no-vocal-sep`
   (faster, notably worse on dense mixes).
2. wav2vec2 CTC forced alignment over 30-second windows produces word
   timestamps. Windowing is what keeps peak memory flat in track length.
3. Words are matched to lyric lines positionally, so a repeated chorus line
   resolves to its own occurrence instead of all snapping to the first.
4. Unmatched or low-confidence lines are filled by interpolation between
   confident anchors; times are then forced monotonic and clamped to the track.

Parenthetical ad-libs (`walk away (walk away)`) are stripped for the alignment
pass only — they're backing-vocal echoes that overlap the lead and distort
neighbouring lines. Output keeps the original text.

Non-destructive by default: the new LRC goes to stdout. `--overwrite` writes it
back; `--json` emits `[{time, text, confidence}]` instead. Anything the model
stack prints while working (Demucs progress) goes to stderr, so stdout stays
parseable.

## In lrc-editor

Two toolbar entries drive the same two commands from the browser.

**Audit** opens `/audit`: pick a folder and an output JSON path with the native
macOS pickers, hit **Run Audit**, and get a table of just the suspicious files
with their reasons and line counts. **Edit** on any row loads that track
straight into the editor. Both paths and the last results are remembered in
`~/.config/spindlebot/lrc-editor-state.json`, so reopening the page restores the
previous run.

**AI Arrange** runs `retime` on the currently loaded track, using the lyric text
already in the editor (lay out or import the text first — it aligns text, it
doesn't transcribe). It runs as a `nice`'d, memory-capped background subprocess
of the AI venv and can take a few minutes. When it lands:

- New times are applied through the undo stack — `Ctrl+Z` reverts the whole
  arrangement.
- Deliberate empty-text markers (e.g. one bounding an instrumental outro) keep
  their manual times.
- Markers below 0.5 confidence turn **orange**. That's the model flagging its
  own weak spots — nudge those by hand.
- **Nothing auto-saves.** **Commit** still writes the file.

`lrc-editor` finds the venv via `$SPINDLEBOT_AI_VENV` and the package via
`$SPINDLEBOT_PIPELINE_DIR` (defaulting to the editor's own directory). Without
the venv, **AI Arrange** reports "AI venv not found — run setup-ai.sh first";
the rest of the editor is unaffected.

## Accuracy

Benchmarked against a hand-timed album: mean error 0.35s and 0.59s on two
tracks, 94% and 100% of lines within 1s. Known weak spots — repeated outro
chants, long instrumental interludes, vocals buried in dense mixes — generally
self-report as low confidence rather than failing silently.

## Tests

`tests/test_lyric_timing_*.py` and `tests/test_lrc_editor_{ai,audit}.py` cover
parse/format, the audit heuristics, the aligner, both CLIs, and the editor's job
orchestration — all against the mock backend, so the standard `pytest` run needs
none of the AI dependencies. `tests/test_lyric_timing_torchaudio.py` exercises
the real backend and is skipped unless `LYRIC_TIMING_IT_AUDIO` and
`LYRIC_TIMING_IT_LRC` point at a real track. It never runs in CI.
