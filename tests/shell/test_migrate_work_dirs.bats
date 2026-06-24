#!/usr/bin/env bats
# Tests for migrate-work-dirs.sh — the Phase A working-dir migration helpers
# sourced by setup.sh. Everything runs in a sandboxed tmpdir; no real
# ~/Music, ~/Library/Application Support, or beets DB is touched.

LIB="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)/migrate-work-dirs.sh"

setup() {
  BATS_TMPDIR="$(mktemp -d)"
  LEGACY_IMPORT="$BATS_TMPDIR/Music/Staging"
  LEGACY_PENDING="$BATS_TMPDIR/Music/Library"
  NEW_IMPORT="$BATS_TMPDIR/AppSupport/Import"
  NEW_PENDING="$BATS_TMPDIR/AppSupport/Pending"
  BEETS_DB="$BATS_TMPDIR/library.db"
  # shellcheck source=/dev/null
  source "$LIB"
}

teardown() {
  rm -rf "$BATS_TMPDIR"
}

_seed_db() {
  sqlite3 "$BEETS_DB" "CREATE TABLE items (id INTEGER PRIMARY KEY, path TEXT);"
  sqlite3 "$BEETS_DB" \
    "INSERT INTO items (path) VALUES ('$LEGACY_PENDING/Artist/Album/01. Track.flac');"
  # A path outside the pending area must be left untouched.
  sqlite3 "$BEETS_DB" \
    "INSERT INTO items (path) VALUES ('/Volumes/DwRugged/Music/Library/Other/x.flac');"
}

@test "relocates import + pending contents and reconciles beets DB" {
  mkdir -p "$LEGACY_IMPORT" "$LEGACY_PENDING/Artist/Album"
  echo log > "$LEGACY_IMPORT/Album.log"
  echo hidden > "$LEGACY_IMPORT/.dotfile"
  echo flac > "$LEGACY_PENDING/Artist/Album/01. Track.flac"
  _seed_db

  run migrate_work_dirs "$NEW_IMPORT" "$NEW_PENDING" "$BEETS_DB" "$LEGACY_IMPORT" "$LEGACY_PENDING"
  [ "$status" -eq 0 ]

  # Import contents moved (including dotfiles)
  [ -f "$NEW_IMPORT/Album.log" ]
  [ -f "$NEW_IMPORT/.dotfile" ]
  [ -z "$(ls -A "$LEGACY_IMPORT")" ]

  # Pending contents moved, structure preserved
  [ -f "$NEW_PENDING/Artist/Album/01. Track.flac" ]

  # Legacy top-level dirs still exist (never deleted)
  [ -d "$LEGACY_IMPORT" ]
  [ -d "$LEGACY_PENDING" ]

  # beets path under pending rewritten; the DwRugged path untouched
  run sqlite3 "$BEETS_DB" "SELECT path FROM items ORDER BY id;"
  [ "${lines[0]}" = "$NEW_PENDING/Artist/Album/01. Track.flac" ]
  [ "${lines[1]}" = "/Volumes/DwRugged/Music/Library/Other/x.flac" ]
}

@test "is idempotent — second run is a no-op and leaves DB stable" {
  mkdir -p "$LEGACY_PENDING/Artist/Album"
  echo flac > "$LEGACY_PENDING/Artist/Album/01. Track.flac"
  _seed_db

  migrate_work_dirs "$NEW_IMPORT" "$NEW_PENDING" "$BEETS_DB" "$LEGACY_IMPORT" "$LEGACY_PENDING"
  run migrate_work_dirs "$NEW_IMPORT" "$NEW_PENDING" "$BEETS_DB" "$LEGACY_IMPORT" "$LEGACY_PENDING"
  [ "$status" -eq 0 ]

  [ -f "$NEW_PENDING/Artist/Album/01. Track.flac" ]
  run sqlite3 "$BEETS_DB" "SELECT path FROM items WHERE id = 1;"
  [ "$output" = "$NEW_PENDING/Artist/Album/01. Track.flac" ]
}

@test "creates new dirs and does nothing when legacy dirs are absent" {
  run migrate_work_dirs "$NEW_IMPORT" "$NEW_PENDING" "" "$LEGACY_IMPORT" "$LEGACY_PENDING"
  [ "$status" -eq 0 ]
  [ -d "$NEW_IMPORT" ]
  [ -d "$NEW_PENDING" ]
}

@test "move_contents is a no-op when source equals destination" {
  mkdir -p "$NEW_PENDING/Album"
  echo x > "$NEW_PENDING/keep.flac"
  run move_contents "$NEW_PENDING" "$NEW_PENDING"
  [ "$status" -ne 0 ]
  [ -f "$NEW_PENDING/keep.flac" ]
}

@test "move_contents returns non-zero when there is nothing to move" {
  mkdir -p "$LEGACY_IMPORT"
  run move_contents "$LEGACY_IMPORT" "$NEW_IMPORT"
  [ "$status" -ne 0 ]
}
