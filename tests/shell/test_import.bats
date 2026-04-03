#!/usr/bin/env bats
# Tests for music-import.sh argument handling and disc-check behaviour.

SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)/music-import.sh"
FIXTURES="$(cd "$(dirname "$BATS_TEST_FILENAME")/fixtures" && pwd)"

setup() {
  # Isolated temp dirs per test
  export BATS_TMPDIR
  BATS_TMPDIR="$(mktemp -d)"

  # Directory layout the script expects
  mkdir -p \
    "$BATS_TMPDIR/Staging" \
    "$BATS_TMPDIR/AllDiscs" \
    "$BATS_TMPDIR/logs" \
    "$BATS_TMPDIR/Library" \
    "$BATS_TMPDIR/bin" \
    "$BATS_TMPDIR/pipeline"

  # Copy mock executables into per-test bin dir (so we can customise per test)
  cp "$FIXTURES/bin/beet"   "$BATS_TMPDIR/bin/beet"
  cp "$FIXTURES/bin/python" "$BATS_TMPDIR/bin/python"
  cp "$FIXTURES/bin/sleep"  "$BATS_TMPDIR/bin/sleep"

  # Copy mock pipeline scripts
  cp "$FIXTURES/pipeline/music-pretag.py"    "$BATS_TMPDIR/pipeline/"
  cp "$FIXTURES/pipeline/music-notify.sh"    "$BATS_TMPDIR/pipeline/"
  cp "$FIXTURES/pipeline/music-fetch-lyrics.py" "$BATS_TMPDIR/pipeline/"

  # Wire HOME so the script sources our mock bootstrap
  export REAL_HOME="$HOME"
  export HOME="$BATS_TMPDIR/home"
  mkdir -p "$HOME/.config/spindlebot"
  sed "s|\${BATS_TMPDIR}|$BATS_TMPDIR|g" "$FIXTURES/bootstrap.sh" > "$HOME/.config/spindlebot/bootstrap.sh"

  # Put mock bin ahead of real PATH
  export PATH="$BATS_TMPDIR/bin:$PATH"

  # Minimal sqlite DB so posttag sql calls don't error
  sqlite3 "$BATS_TMPDIR/library.db" \
    "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, path TEXT, added INTEGER);
     CREATE TABLE IF NOT EXISTS item_attributes (entity_id INTEGER, key TEXT, value TEXT);" 2>/dev/null || true

  export MOCK_LOG="$BATS_TMPDIR/mock.log"
  export MOCK_BEET_EXIT=0
}

teardown() {
  export HOME="$REAL_HOME"
  rm -rf "$BATS_TMPDIR"
}

# ── helper ────────────────────────────────────────────────────────────────────

log_contains() {
  grep -qF "$1" "$BATS_TMPDIR/logs/watcher.log" 2>/dev/null
}

# ── tests ─────────────────────────────────────────────────────────────────────

@test "non-.log file exits immediately without logging" {
  run bash "$SCRIPT" "$BATS_TMPDIR/Staging/somefile.flac"
  [ "$status" -eq 0 ]
  ! log_contains "Detected completed rip"
}

@test "missing .log file exits cleanly (double-fire guard)" {
  run bash "$SCRIPT" "$BATS_TMPDIR/Staging/nonexistent.log"
  [ "$status" -eq 0 ]
  ! log_contains "Detected completed rip"
}

@test "disc check holds when WAIT returned (no --force)" {
  # Create a real .log file in Staging
  touch "$BATS_TMPDIR/Staging/Album.log"

  # Mock python returns WAIT:1:2 for disc check
  export MOCK_DISC_WAIT="WAIT:1:2"

  run bash "$SCRIPT" "$BATS_TMPDIR/Staging/Album.log"
  [ "$status" -eq 0 ]
  log_contains "waiting for remaining discs"
  ! log_contains "Running pretag"
}

@test "disc check hold message includes --force hint" {
  touch "$BATS_TMPDIR/Staging/Album.log"
  export MOCK_DISC_WAIT="WAIT:1:2"

  bash "$SCRIPT" "$BATS_TMPDIR/Staging/Album.log"
  log_contains "run with --force"
}

@test "--force skips disc check and proceeds to import" {
  touch "$BATS_TMPDIR/Staging/Album.log"

  # Even with WAIT signal set, --force should bypass it
  export MOCK_DISC_WAIT="WAIT:1:2"
  export MOCK_BEET_EXIT=0

  bash "$SCRIPT" --force "$BATS_TMPDIR/Staging/Album.log"
  log_contains "Disc check skipped (--force)"
  log_contains "Running pretag"
}

@test "--force works when flag comes after log path" {
  touch "$BATS_TMPDIR/Staging/Album.log"
  export MOCK_DISC_WAIT="WAIT:1:2"

  bash "$SCRIPT" "$BATS_TMPDIR/Staging/Album.log" --force
  log_contains "Disc check skipped (--force)"
}

@test "single disc passes check and proceeds to import" {
  touch "$BATS_TMPDIR/Staging/Album.log"
  # MOCK_DISC_WAIT unset → empty → no WAIT output → import proceeds

  bash "$SCRIPT" "$BATS_TMPDIR/Staging/Album.log"
  log_contains "Disc check passed"
  log_contains "Running pretag"
}
