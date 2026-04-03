#!/bin/bash
# music-import.sh — triggered when XLD finishes ripping an album to Staging

# ── Load SpindleBot config ────────────────────────────────────────────────────
# shellcheck source=/dev/null
source "$HOME/.config/spindlebot/bootstrap.sh" 2>/dev/null || {
  echo "ERROR: SpindleBot not configured. Run setup.sh from the pipeline directory." >&2
  exit 1
}

STAGING="$SPINDLEBOT_STAGING_DIR"
COMPLETE="$SPINDLEBOT_ARCHIVE_DIR"
BEET="$SPINDLEBOT_BEET"
PYTHON="$SPINDLEBOT_PYTHON"
PRETAG="$SPINDLEBOT_PIPELINE_DIR/music-pretag.py"
NOTIFY="$SPINDLEBOT_PIPELINE_DIR/music-notify.sh"
FETCH_LYRICS="$SPINDLEBOT_PIPELINE_DIR/music-fetch-lyrics.py"
LIBRARY="$SPINDLEBOT_LIBRARY_DIR"
LOGFILE="$SPINDLEBOT_LOG_DIR/watcher.log"
DB="$SPINDLEBOT_BEETS_DB"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOGFILE"
}

FORCE=0
CHANGED=""
for arg in "$@"; do
  if [[ "$arg" == "--force" ]]; then
    FORCE=1
  else
    CHANGED="$arg"
  fi
done

log "Watcher fired: $CHANGED${FORCE:+ (--force)}"

# Only care about .log files (XLD rip log = album complete)
if [[ "$CHANGED" != *.log ]]; then
  exit 0
fi

# Guard against double-fire (e.g. fswatch sees mv as an update before deletion)
if [ ! -f "$CHANGED" ]; then
  exit 0
fi

ALBUM_DIR="$(dirname "$CHANGED")"
log "Detected completed rip: $ALBUM_DIR"

# Brief pause to let XLD finish any final writes
sleep 3

# Check if all discs are present for multi-disc albums.
# Skip with --force to import immediately regardless of disctotal tags.
if [[ "$FORCE" -eq 0 ]]; then
  WAIT_CHECK=$(PYTHONPATH="$SPINDLEBOT_PIPELINE_DIR" $PYTHON -m spindlebot.disc check "$ALBUM_DIR")

  if [[ "$WAIT_CHECK" == WAIT:* ]]; then
    HAVE=$(echo "$WAIT_CHECK" | cut -d: -f2)
    NEED=$(echo "$WAIT_CHECK" | cut -d: -f3)
    log "Multi-disc album: have $HAVE of $NEED discs — waiting for remaining discs before importing"
    log "  (run with --force to import immediately)"
    exit 0
  fi
  log "Disc check passed — proceeding with import"
else
  log "Disc check skipped (--force)"
fi

# Step 1: Pre-process tags (feat., compilation, artist normalization)
log "Running pretag on: $ALBUM_DIR"
if ! $PYTHON "$PRETAG" "$ALBUM_DIR" >> "$LOGFILE" 2>&1; then
  log "pretag failed — aborting import"
  exit 1
fi

# Step 2: beets import
log "Starting beet import on: $ALBUM_DIR"
$BEET import "$ALBUM_DIR" >> "$LOGFILE" 2>&1
STATUS=$?

if [ $STATUS -eq 0 ]; then
  log "Import complete: $ALBUM_DIR"
  TODAY=$(date +%Y-%m-%d)
  # shellcheck disable=SC2016  # $albumartist/$album are beet template vars, not bash vars
  ARTIST_ALBUM=$($BEET ls -f '$albumartist - $album' "added:${TODAY}.." 2>/dev/null | sort -u | head -1)
  if [ -z "$ARTIST_ALBUM" ]; then
    ARTIST_ALBUM=$(basename "$ALBUM_DIR")
  fi
  "$NOTIFY" "Rip complete" "$ARTIST_ALBUM — reply 'sync' to move to DwRugged"

  # Step 3: Post-import fixes
  # Fix multidisc: base it on how many disc numbers were ACTUALLY ripped,
  # not MusicBrainz disctotal (which can be >1 for DualDiscs, deluxe editions, etc.)
  ACTUAL_DISCS=$(PYTHONPATH="$SPINDLEBOT_PIPELINE_DIR" $PYTHON -m spindlebot.disc count "$ALBUM_DIR")
  # Fix disctotal in DB and ensure multidisc flex attr row exists for every new item.
  # MusicBrainz can say disctotal=2 for a single-disc rip (DualDiscs, deluxe editions);
  # we patch disctotal to actual ripped count so the inline computes correctly.
  # beet modify multidisc= DELETES the flex attr row (empty value), leaving $multidisc
  # undefined in templates — which beet renders as the literal string "$multidisc" (truthy).
  # So we INSERT the row directly via sqlite instead of relying on beet modify.
  if [ "${ACTUAL_DISCS:-1}" -gt 1 ]; then
    $BEET modify --yes "added:${TODAY}.." "path:${LIBRARY}/" disctotal="${ACTUAL_DISCS}" >> "$LOGFILE" 2>&1
    sqlite3 "$DB" "INSERT OR IGNORE INTO item_attributes (entity_id, key, value)
      SELECT id, 'multidisc', '1' FROM items
      WHERE added >= strftime('%s','${TODAY}') AND path LIKE '${LIBRARY}/%'
        AND id NOT IN (SELECT entity_id FROM item_attributes WHERE key='multidisc');" 2>/dev/null
    log "Multi-disc rip ($ACTUAL_DISCS discs) — set disctotal=$ACTUAL_DISCS, multidisc=1"
  else
    $BEET modify --yes "added:${TODAY}.." "path:${LIBRARY}/" disctotal=1 disc=1 >> "$LOGFILE" 2>&1
    # Ensure multidisc="" row exists so $multidisc is never the undefined literal
    sqlite3 "$DB" "INSERT OR IGNORE INTO item_attributes (entity_id, key, value)
      SELECT id, 'multidisc', '' FROM items
      WHERE added >= strftime('%s','${TODAY}') AND path LIKE '${LIBRARY}/%'
        AND id NOT IN (SELECT entity_id FROM item_attributes WHERE key='multidisc');" 2>/dev/null
    log "Single-disc rip — patched disctotal=1, ensured multidisc row exists"
  fi
  $BEET move "added:${TODAY}.." "path:${LIBRARY}/" >> "$LOGFILE" 2>&1
  log "Moved files to correct paths"

  # Step 4: posttag — strip beets alias tags and truncate DATE to year only.
  # Runs after beet move (beet is fully done writing at this point).
  # Also runs in sync script as a no-op safety net for any pre-existing files.
  # shellcheck disable=SC2016  # $path is a beet template var, not a bash var
  IMPORT_FILES=$($BEET ls -f '$path' "added:${TODAY}.." 2>/dev/null | grep -v "^/Volumes/")
  if [ -n "$IMPORT_FILES" ]; then
    log "Running posttag on imported files"
    echo "$IMPORT_FILES" | $PYTHON "$PRETAG" --post >> "$LOGFILE" 2>&1
  fi

  # Step 5: Fetch synced lyrics (.lrc sidecar files)
  # Filter to local Library only — DwRugged may not be mounted, and today's query
  # returns all modified items including previously synced DwRugged paths.
  ALBUM_DIRS=$(echo "$IMPORT_FILES" | while IFS= read -r p; do dirname "$p"; done | sort -u)
  if [ -n "$ALBUM_DIRS" ]; then
    while IFS= read -r dir; do
      log "Fetching lyrics for: $dir"
      $PYTHON "$FETCH_LYRICS" "$dir" >> "$LOGFILE" 2>&1
    done <<< "$ALBUM_DIRS"
  fi

  # Archive all XLD logs from Staging (covers multi-disc albums where earlier disc logs remain)
  mkdir -p "$COMPLETE"
  find "$STAGING" -maxdepth 1 -name "*.log" | while read -r logfile; do
    mv "$logfile" "$COMPLETE/"
    log "Archived XLD log to: $COMPLETE/$(basename "$logfile")"
  done
else
  log "Import FAILED (exit $STATUS): $ALBUM_DIR"
fi
