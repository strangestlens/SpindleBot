#!/usr/bin/env bats
# Tests for music-import.sh:
#   - argument parsing (--force placement, log vs directory input)
#   - disc-check hold and --force bypass
#   - double-fire guard (missing .log file)
#   - directory input mode (digital downloads / Bandcamp)
#   - unrecognised input exits cleanly

SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)/music-import.sh"
FIXTURES="$(cd "$(dirname "$BATS_TEST_FILENAME")/fixtures" && pwd)"

setup() {
  export BATS_TMPDIR
  BATS_TMPDIR="$(mktemp -d)"

  mkdir -p \
    "$BATS_TMPDIR/Staging" \
    "$BATS_TMPDIR/AllDiscs" \
    "$BATS_TMPDIR/logs" \
    "$BATS_TMPDIR/Library" \
    "$BATS_TMPDIR/bin" \
    "$BATS_TMPDIR/pipeline"

  cp "$FIXTURES/bin/beet"   "$BATS_TMPDIR/bin/beet"
  cp "$FIXTURES/bin/python" "$BATS_TMPDIR/bin/python"
  cp "$FIXTURES/bin/sleep"  "$BATS_TMPDIR/bin/sleep"

  cp "$FIXTURES/pipeline/music-pretag.py"       "$BATS_TMPDIR/pipeline/"
  cp "$FIXTURES/pipeline/music-notify.sh"        "$BATS_TMPDIR/pipeline/"
  cp "$FIXTURES/pipeline/music-fetch-lyrics.py"  "$BATS_TMPDIR/pipeline/"

  export REAL_HOME="$HOME"
  export HOME="$BATS_TMPDIR/home"
  mkdir -p "$HOME/.config/spindlebot"
  sed "s|\${BATS_TMPDIR}|$BATS_TMPDIR|g" "$FIXTURES/bootstrap.sh" \
    > "$HOME/.config/spindlebot/bootstrap.sh"

  export PATH="$BATS_TMPDIR/bin:$PATH"

  sqlite3 "$BATS_TMPDIR/library.db" \
    "CREATE TABLE IF NOT EXISTS items
       (id INTEGER PRIMARY KEY, path TEXT, added INTEGER);
     CREATE TABLE IF NOT EXISTS item_attributes
       (entity_id INTEGER, key TEXT, value TEXT);" 2>/dev/null || true

  export MOCK_LOG="$BATS_TMPDIR/mock.log"
  export MOCK_BEET_EXIT=0
}

teardown() {
  export HOME="$REAL_HOME"
  rm -rf "$BATS_TMPDIR"
}

# ── helpers ───────────────────────────────────────────────────────────────────

log_contains() {
  grep -qF "$1" "$BATS_TMPDIR/logs/watcher.log" 2>/dev/null
}

# ── argument parsing ──────────────────────────────────────────────────────────

@test "non-.log non-directory argument exits immediately" {
  run bash "$SCRIPT" "$BATS_TMPDIR/Staging/somefile.flac"
  [ "$status" -eq 0 ]
  ! log_contains "Detected completed rip"
  ! log_contains "Directory import"
}

@test "missing .log file exits cleanly (double-fire guard)" {
  run bash "$SCRIPT" "$BATS_TMPDIR/Staging/nonexistent.log"
  [ "$status" -eq 0 ]
  ! log_contains "Detected completed rip"
}

# ── disc-check (log-file mode) ────────────────────────────────────────────────

@test "disc check holds on WAIT when no --force (log mode)" {
  touch "$BATS_TMPDIR/Staging/Album.log"
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

@test "--force before log path skips disc check" {
  touch "$BATS_TMPDIR/Staging/Album.log"
  export MOCK_DISC_WAIT="WAIT:1:2"

  bash "$SCRIPT" --force "$BATS_TMPDIR/Staging/Album.log"
  log_contains "Disc check skipped (--force)"
  log_contains "Running pretag"
}

@test "--force after log path skips disc check" {
  touch "$BATS_TMPDIR/Staging/Album.log"
  export MOCK_DISC_WAIT="WAIT:1:2"

  bash "$SCRIPT" "$BATS_TMPDIR/Staging/Album.log" --force
  log_contains "Disc check skipped (--force)"
}

@test "single disc passes check and proceeds to import (log mode)" {
  touch "$BATS_TMPDIR/Staging/Album.log"
  # MOCK_DISC_WAIT unset → empty output → no WAIT → proceeds

  bash "$SCRIPT" "$BATS_TMPDIR/Staging/Album.log"
  log_contains "Disc check passed"
  log_contains "Running pretag"
}

# ── directory input mode ──────────────────────────────────────────────────────

@test "directory argument enters directory import mode" {
  album_dir="$BATS_TMPDIR/Staging/CLANN - Seelie"
  mkdir -p "$album_dir"
  touch "$album_dir/01 Caves.flac"

  bash "$SCRIPT" "$album_dir"
  log_contains "Directory import"
}

@test "directory mode runs disc check" {
  album_dir="$BATS_TMPDIR/Staging/Album"
  mkdir -p "$album_dir"
  touch "$album_dir/01.flac"
  export MOCK_DISC_WAIT=""  # empty = pass

  bash "$SCRIPT" "$album_dir"
  log_contains "Disc check passed"
}

@test "directory mode holds on WAIT without --force" {
  album_dir="$BATS_TMPDIR/Staging/Album"
  mkdir -p "$album_dir"
  touch "$album_dir/01.flac"
  export MOCK_DISC_WAIT="WAIT:1:2"

  bash "$SCRIPT" "$album_dir"
  log_contains "waiting for remaining discs"
  ! log_contains "Running pretag"
}

@test "--force bypasses disc check in directory mode" {
  album_dir="$BATS_TMPDIR/Staging/Album"
  mkdir -p "$album_dir"
  touch "$album_dir/01.flac"
  export MOCK_DISC_WAIT="WAIT:1:2"

  bash "$SCRIPT" --force "$album_dir"
  log_contains "Disc check skipped (--force)"
  log_contains "Running pretag"
}

@test "directory mode proceeds to pretag when disc check passes" {
  album_dir="$BATS_TMPDIR/Staging/Album"
  mkdir -p "$album_dir"
  touch "$album_dir/01.flac"

  bash "$SCRIPT" "$album_dir"
  log_contains "Running pretag"
}
