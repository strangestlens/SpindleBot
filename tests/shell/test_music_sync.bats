#!/usr/bin/env bats
# Tests for music-sync.sh — the content-addressed mount-sync.
#
# The script orchestrates the tested spindlebot commands; these verify the
# shell glue: the guard conditions, the command sequence, and the safety
# ordering (prune only after a clean sync). The commands themselves are covered
# by pytest (test_sync.py / test_prune.py / test_reconciler.py).

SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)/music-sync.sh"
FIXTURES="$(cd "$(dirname "$BATS_TEST_FILENAME")/fixtures" && pwd)"

setup() {
  export BATS_TMPDIR
  BATS_TMPDIR="$(mktemp -d)"
  mkdir -p "$BATS_TMPDIR/bin" "$BATS_TMPDIR/pipeline" "$BATS_TMPDIR/logs" \
           "$BATS_TMPDIR/Pending" "$BATS_TMPDIR/RetentionDrive"
  cp "$FIXTURES/bin/python" "$BATS_TMPDIR/bin/python"
  cp "$FIXTURES/pipeline/music-notify.sh" "$BATS_TMPDIR/pipeline/music-notify.sh"
  chmod +x "$BATS_TMPDIR/pipeline/music-notify.sh"

  export REAL_HOME="$HOME"
  export HOME="$BATS_TMPDIR/home"
  mkdir -p "$HOME/.config/spindlebot"
  sed "s|\${BATS_TMPDIR}|$BATS_TMPDIR|g" "$FIXTURES/bootstrap.sh" \
    > "$HOME/.config/spindlebot/bootstrap.sh"

  export PATH="$BATS_TMPDIR/bin:$PATH"
  export MOCK_LOG="$BATS_TMPDIR/mock.log"
  # isolate the lockfile to the temp dir — never touch /tmp / a real agent's lock
  export SPINDLEBOT_SYNC_LOCKFILE="$BATS_TMPDIR/music-sync.lock"
}

teardown() {
  export HOME="$REAL_HOME"
  rm -rf "$BATS_TMPDIR"
}

@test "skips (no spindlebot calls) when nothing is pending" {
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [ ! -f "$MOCK_LOG" ]
}

@test "skips when the drive is not mounted" {
  echo x > "$BATS_TMPDIR/Pending/track.flac"
  rmdir "$BATS_TMPDIR/RetentionDrive"
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [ ! -f "$MOCK_LOG" ]
}

@test "runs inventory → review --yes → sync → prune, in that order" {
  echo x > "$BATS_TMPDIR/Pending/track.flac"
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  grep -qF "spindlebot inventory" "$MOCK_LOG"
  grep -qF "spindlebot review --location RetentionDrive --yes" "$MOCK_LOG"
  grep -qF "spindlebot sync --location RetentionDrive" "$MOCK_LOG"
  grep -qF "spindlebot prune --execute" "$MOCK_LOG"
  sync_line=$(grep -n "spindlebot sync" "$MOCK_LOG" | head -1 | cut -d: -f1)
  prune_line=$(grep -n "spindlebot prune" "$MOCK_LOG" | head -1 | cut -d: -f1)
  [ "$sync_line" -lt "$prune_line" ]
}

@test "a stray .DS_Store alone does not trigger a run" {
  touch "$BATS_TMPDIR/Pending/.DS_Store"
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [ ! -f "$MOCK_LOG" ]   # no spindlebot calls — treated as nothing pending
}

@test "a per-file inventory error is non-fatal — sync + prune still run" {
  echo x > "$BATS_TMPDIR/Pending/track.flac"
  cat > "$BATS_TMPDIR/bin/python" <<'MOCK'
#!/bin/bash
echo "python $*" >> "${MOCK_LOG:-/dev/null}"
for a in "$@"; do [ "$a" = "inventory" ] && exit 1; done   # inventory exits nonzero
exit 0
MOCK
  chmod +x "$BATS_TMPDIR/bin/python"
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]                       # not wedged
  grep -qF "spindlebot sync" "$MOCK_LOG"    # pressed on to sync
  grep -qF "spindlebot prune" "$MOCK_LOG"
}

@test "prune failure yields a warning notification, not a false success" {
  echo x > "$BATS_TMPDIR/Pending/track.flac"
  # a notify mock that records its title argument
  cat > "$BATS_TMPDIR/pipeline/music-notify.sh" <<'NOTIFY'
#!/bin/bash
echo "$1" >> "${NOTIFY_LOG}"
exit 0
NOTIFY
  chmod +x "$BATS_TMPDIR/pipeline/music-notify.sh"
  export NOTIFY_LOG="$BATS_TMPDIR/notify.log"
  # python mock that fails only on prune
  cat > "$BATS_TMPDIR/bin/python" <<'MOCK'
#!/bin/bash
echo "python $*" >> "${MOCK_LOG:-/dev/null}"
for a in "$@"; do [ "$a" = "prune" ] && exit 1; done
exit 0
MOCK
  chmod +x "$BATS_TMPDIR/bin/python"
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]                                  # prune failure is non-fatal
  grep -qF "warnings" "$NOTIFY_LOG"
  ! grep -qF "Sync complete" "$NOTIFY_LOG"
}

@test "does NOT prune when sync fails" {
  echo x > "$BATS_TMPDIR/Pending/track.flac"
  cat > "$BATS_TMPDIR/bin/python" <<'MOCK'
#!/bin/bash
echo "python $*" >> "${MOCK_LOG:-/dev/null}"
for a in "$@"; do [ "$a" = "sync" ] && exit 1; done
exit 0
MOCK
  chmod +x "$BATS_TMPDIR/bin/python"
  run bash "$SCRIPT"
  [ "$status" -eq 1 ]
  grep -qF "spindlebot sync" "$MOCK_LOG"
  ! grep -qF "spindlebot prune" "$MOCK_LOG"
}

@test "the old rsync MOVE is gone — no rsync command in the script" {
  # the phrase may appear in a comment; assert no actual rsync command runs
  ! grep -qE '^[[:space:]]*rsync' "$SCRIPT"
}

@test "hard-fails (no inventory/sync/prune) when no destination is configured" {
  echo x > "$BATS_TMPDIR/Pending/track.flac"        # there IS work to sync
  # Blank the configured destination name; a later export overrides the fixture's.
  echo 'export SPINDLEBOT_DESTINATION_NAME=""' >> "$HOME/.config/spindlebot/bootstrap.sh"
  run bash "$SCRIPT"
  [ "$status" -eq 1 ]        # fail loud rather than prune against an empty target
  [ ! -f "$MOCK_LOG" ]       # bailed before any spindlebot call (no prune)
}

# ── beets DB path rewrite ─────────────────────────────────────────────────────
#
# beets stores items.path as a BLOB, and PathQuery binds its pattern as a BLOB.
# SQLite never compares TEXT equal to BLOB, so if the rewrite drops a row to
# TEXT then `beet ls path:...` stops matching it — and `beet move path:<dir>/`,
# which the promote step depends on, fails with "No matching items found".
# These assert the storage class survives, not just the string value.

_seed_beets_db() {
  # One item row whose path is a BLOB under Pending, as beets would store it.
  sqlite3 "$BATS_TMPDIR/library.db" \
    "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB);
     INSERT INTO items (path) VALUES (CAST('$BATS_TMPDIR/Pending/artist/album/01. t.flac' AS BLOB));"
}

@test "beets path rewrite repoints Pending paths at the destination" {
  command -v sqlite3 >/dev/null || skip "sqlite3 not installed"
  echo x > "$BATS_TMPDIR/Pending/track.flac"
  _seed_beets_db

  run bash "$SCRIPT"
  [ "$status" -eq 0 ]

  got="$(sqlite3 "$BATS_TMPDIR/library.db" "SELECT CAST(path AS TEXT) FROM items;")"
  [ "$got" = "$BATS_TMPDIR/RetentionDrive/artist/album/01. t.flac" ]
}

@test "beets path rewrite keeps items.path a BLOB" {
  command -v sqlite3 >/dev/null || skip "sqlite3 not installed"
  echo x > "$BATS_TMPDIR/Pending/track.flac"
  _seed_beets_db

  run bash "$SCRIPT"
  [ "$status" -eq 0 ]

  # replace() returns TEXT; without the CAST back to BLOB this row reads 'text'
  # and every `path:` query in the library silently stops matching.
  types="$(sqlite3 "$BATS_TMPDIR/library.db" "SELECT DISTINCT typeof(path) FROM items;")"
  [ "$types" = "blob" ]
}

@test "beets path rewrite leaves rows outside Pending untouched" {
  command -v sqlite3 >/dev/null || skip "sqlite3 not installed"
  echo x > "$BATS_TMPDIR/Pending/track.flac"
  _seed_beets_db
  # a row already on the retention drive must not be rewritten or retyped
  sqlite3 "$BATS_TMPDIR/library.db" \
    "INSERT INTO items (path) VALUES (CAST('$BATS_TMPDIR/RetentionDrive/other/album/02. u.flac' AS BLOB));"

  run bash "$SCRIPT"
  [ "$status" -eq 0 ]

  n="$(sqlite3 "$BATS_TMPDIR/library.db" \
        "SELECT count(*) FROM items WHERE CAST(path AS TEXT) LIKE '%/RetentionDrive/%' AND typeof(path)='blob';")"
  [ "$n" -eq 2 ]
}
