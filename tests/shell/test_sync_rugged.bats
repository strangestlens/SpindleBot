#!/usr/bin/env bats
# Tests for music-sync-rugged.sh — the content-addressed mount-sync.
#
# The script orchestrates the tested spindlebot commands; these verify the
# shell glue: the guard conditions, the command sequence, and the safety
# ordering (prune only after a clean sync). The commands themselves are covered
# by pytest (test_sync.py / test_prune.py / test_reconciler.py).

SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)/music-sync-rugged.sh"
FIXTURES="$(cd "$(dirname "$BATS_TEST_FILENAME")/fixtures" && pwd)"

setup() {
  export BATS_TMPDIR
  BATS_TMPDIR="$(mktemp -d)"
  mkdir -p "$BATS_TMPDIR/bin" "$BATS_TMPDIR/pipeline" "$BATS_TMPDIR/logs" \
           "$BATS_TMPDIR/Pending" "$BATS_TMPDIR/DwRugged"
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
  rm -f /tmp/music-sync-rugged.lock
}

teardown() {
  export HOME="$REAL_HOME"
  rm -rf "$BATS_TMPDIR"
  rm -f /tmp/music-sync-rugged.lock
}

@test "skips (no spindlebot calls) when nothing is pending" {
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [ ! -f "$MOCK_LOG" ]
}

@test "skips when the drive is not mounted" {
  echo x > "$BATS_TMPDIR/Pending/track.flac"
  rmdir "$BATS_TMPDIR/DwRugged"
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [ ! -f "$MOCK_LOG" ]
}

@test "runs inventory → review --yes → sync → prune, in that order" {
  echo x > "$BATS_TMPDIR/Pending/track.flac"
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  grep -qF "spindlebot inventory" "$MOCK_LOG"
  grep -qF "spindlebot review --location DwRugged --yes" "$MOCK_LOG"
  grep -qF "spindlebot sync" "$MOCK_LOG"
  grep -qF "spindlebot prune --execute" "$MOCK_LOG"
  sync_line=$(grep -n "spindlebot sync" "$MOCK_LOG" | head -1 | cut -d: -f1)
  prune_line=$(grep -n "spindlebot prune" "$MOCK_LOG" | head -1 | cut -d: -f1)
  [ "$sync_line" -lt "$prune_line" ]
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
