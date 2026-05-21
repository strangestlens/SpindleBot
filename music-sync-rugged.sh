#!/bin/bash
# music-sync-rugged.sh — fires when DWRugged mounts (or run manually)
# Moves ~/Music/Library to /Volumes/DwRugged/Music/Library, updates beets DB paths.
# Lyrics are fetched on DwRugged after sync so there's no race with the import pipeline.

# ── Load SpindleBot config ────────────────────────────────────────────────────
# shellcheck source=/dev/null
source "$HOME/.config/spindlebot/bootstrap.sh" 2>/dev/null || {
  echo "ERROR: SpindleBot not configured. Run setup.sh from the pipeline directory." >&2
  exit 1
}
export PYTHONPATH="$SPINDLEBOT_PIPELINE_DIR"

LOCAL="$SPINDLEBOT_LIBRARY_DIR"
REMOTE="$SPINDLEBOT_DESTINATION_PATH"
LOGFILE="$SPINDLEBOT_LOG_DIR/rugged-sync.log"
LOCKFILE="/tmp/music-sync-rugged.lock"
PYTHON="$SPINDLEBOT_PYTHON"
NOTIFY="$SPINDLEBOT_PIPELINE_DIR/music-notify.sh"
FETCH_ART="$SPINDLEBOT_PIPELINE_DIR/music-fetch-art.py"
PRETAG="$SPINDLEBOT_PIPELINE_DIR/music-pretag.py"
FETCH_LYRICS="$SPINDLEBOT_PIPELINE_DIR/music-fetch-lyrics.py"
DB="$SPINDLEBOT_BEETS_DB"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOGFILE"
}

# Prevent concurrent runs
if [ -f "$LOCKFILE" ]; then
  log "Already running (lockfile exists), skipping."
  exit 0
fi
touch "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

# Confirm drive is actually mounted (launchd can fire spuriously)
if [ ! -d "$REMOTE" ]; then
  mkdir -p "$REMOTE" 2>/dev/null || { log "Drive not ready, skipping."; exit 0; }
fi

# Check if there's anything to move
if [ -z "$(find "$LOCAL" -type f 2>/dev/null)" ]; then
  log "Nothing to sync."
  exit 0
fi

log "DWRugged mounted — syncing $LOCAL → $REMOTE"

# Fetch missing album art before files leave local Library
log "Fetching missing album art"
$PYTHON "$FETCH_ART" "$LOCAL" >> "$LOGFILE" 2>&1

# Final tag cleanup before files leave local Library
log "Running posttag before sync"
find "$LOCAL" -name "*.flac" | $PYTHON "$PRETAG" --post >> "$LOGFILE" 2>&1

# Move files (rsync preserves structure, removes source files after success)
rsync -av --remove-source-files "$LOCAL/" "$REMOTE/" >> "$LOGFILE" 2>&1
RSYNC_STATUS=$?

# Clean up empty directories left behind
find "$LOCAL" -mindepth 1 -type d -empty -delete 2>/dev/null

REMAINING=$(find "$LOCAL" -type f 2>/dev/null | wc -l | tr -d ' ')
if [ "$REMAINING" -eq 0 ]; then
  log "Sync complete."

  # Update beets DB paths: local Library → DwRugged
  if sqlite3 "$DB" \
      "UPDATE items SET path = replace(path, '${LOCAL}', '${REMOTE}') WHERE path LIKE '${LOCAL}/%';" 2>/dev/null; then
    log "Beets DB paths updated to DwRugged"
  else
    log "WARNING: beets DB path update failed"
  fi

  # Fetch lyrics for any albums on DwRugged missing .lrc files.
  # Runs here (not in import script) to avoid race condition with rsync.
  LYRICS_DIRS=$(find "$REMOTE" -name "*.flac" | while IFS= read -r f; do
    dir=$(dirname "$f")
    lrc="${f%.flac}.lrc"
    # Skip albums marked as having no available lyrics
    [ -f "$dir/.nolrc" ] && continue
    [ ! -f "$lrc" ] && echo "$dir"
  done | sort -u)
  if [ -n "$LYRICS_DIRS" ]; then
    log "Fetching missing lyrics on DwRugged"
    while IFS= read -r dir; do
      log "Fetching lyrics for: $dir"
      $PYTHON "$FETCH_LYRICS" "$dir" >> "$LOGFILE" 2>&1
    done <<< "$LYRICS_DIRS"
  fi

  "$NOTIFY" "Sync complete" "All files moved to DwRugged ✓"
else
  log "rsync FAILED (exit $RSYNC_STATUS) — $REMAINING files still in Library."
  "$NOTIFY" "Sync failed" "$REMAINING files still in Library — check rugged-sync.log"
fi
