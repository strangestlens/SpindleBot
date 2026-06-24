#!/usr/bin/env bats
# Tests for music-import.sh shell shim.
#
# music-import.sh is now a one-liner that sources bootstrap.sh and execs
# `python -m spindlebot import "$@"`. These tests verify the shim correctly
# sources bootstrap and forwards arguments to Python.
#
# All pipeline behaviour (disc check, pretag, beet import, etc.) is tested
# in tests/test_runner.py via pytest.

SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)/music-import.sh"
FIXTURES="$(cd "$(dirname "$BATS_TEST_FILENAME")/fixtures" && pwd)"

setup() {
  export BATS_TMPDIR
  BATS_TMPDIR="$(mktemp -d)"

  mkdir -p \
    "$BATS_TMPDIR/Import" \
    "$BATS_TMPDIR/bin"

  cp "$FIXTURES/bin/python" "$BATS_TMPDIR/bin/python"

  export REAL_HOME="$HOME"
  export HOME="$BATS_TMPDIR/home"
  mkdir -p "$HOME/.config/spindlebot"
  sed "s|\${BATS_TMPDIR}|$BATS_TMPDIR|g" "$FIXTURES/bootstrap.sh" \
    > "$HOME/.config/spindlebot/bootstrap.sh"

  export PATH="$BATS_TMPDIR/bin:$PATH"
  export MOCK_LOG="$BATS_TMPDIR/mock.log"
}

teardown() {
  export HOME="$REAL_HOME"
  rm -rf "$BATS_TMPDIR"
}

@test "script forwards trigger path to python -m spindlebot import" {
  run bash "$SCRIPT" "$BATS_TMPDIR/Import/Album.log"
  [ "$status" -eq 0 ]
  grep -qF "spindlebot import" "$MOCK_LOG"
  grep -qF "Album.log" "$MOCK_LOG"
}

@test "script forwards --force flag to python" {
  run bash "$SCRIPT" --force "$BATS_TMPDIR/Import/Album.log"
  [ "$status" -eq 0 ]
  grep -qF -- "--force" "$MOCK_LOG"
}

@test "script exits non-zero when bootstrap missing" {
  rm "$HOME/.config/spindlebot/bootstrap.sh"
  run bash "$SCRIPT" "$BATS_TMPDIR/Import/Album.log"
  [ "$status" -ne 0 ]
}

@test "script sets PYTHONPATH when calling python -m spindlebot" {
  # The mock python exits 1 if PYTHONPATH is unset for -m spindlebot calls.
  # This guards against the shim omitting PYTHONPATH="$SPINDLEBOT_PIPELINE_DIR",
  # which causes "No module named spindlebot" in the watcher's stripped environment.
  run bash "$SCRIPT" "$BATS_TMPDIR/Import/Album.log"
  [ "$status" -eq 0 ]
}
