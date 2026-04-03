#!/bin/bash
# music-import.sh — import one album from Staging into the beets Library.
#
# Accepts either:
#   music-import.sh /path/to/Staging/Album.log          CD rip (XLD log trigger)
#   music-import.sh /path/to/Staging/Album/             Digital download (directory trigger)
#   music-import.sh /path/to/Staging/Album/ --force     Skip multi-disc hold

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

# ── Argument parsing ──────────────────────────────────────────────────────────
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

# ── Determine album directory ─────────────────────────────────────────────────
#
# Directory mode  — digital download / Bandcamp:
#   $CHANGED is a directory containing audio files.  No .log file exists.
#
# Log-file mode   — CD rip (XLD):
#   $CHANGED is a .log file.  Album dir = dirname($CHANGED).
#
IS_DIR_MODE=0
if [ -d "$CHANGED" ]; then
  IS_DIR_MODE=1
  ALBUM_DIR="$CHANGED"
  log "Directory import: $ALBUM_DIR"
elif [[ "$CHANGED" == *.log ]]; then
  # Guard against double-fire (fswatch can see mv as an update before deletion)
  if [ ! -f "$CHANGED" ]; then
    exit 0
  fi
  ALBUM_DIR="$(dirname "$CHANGED")"
  log "Detected completed rip: $ALBUM_DIR"
else
  # Neither a directory nor a .log file — nothing to do
  exit 0
fi

# Brief pause to let any final writes settle (relevant for CD rips; harmless otherwise)
sleep 3

# ── Disc check ────────────────────────────────────────────────────────────────
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

# ── Step 1: Pre-process tags ──────────────────────────────────────────────────
log "Running pretag on: $ALBUM_DIR"
if ! PYTHONPATH="$SPINDLEBOT_PIPELINE_DIR" $PYTHON "$PRETAG" "$ALBUM_DIR" >> "$LOGFILE" 2>&1; then
  log "pretag failed — aborting import"
  exit 1
fi

# ── Step 2: beets import ──────────────────────────────────────────────────────
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
  "$NOTIFY" "Import complete" "$ARTIST_ALBUM — reply 'sync' to move to DwRugged"

  # ── Step 3: Post-import DB fixes ─────────────────────────────────────────
  # Fix multidisc: base it on how many disc numbers were ACTUALLY ripped,
  # not MusicBrainz disctotal (which can be >1 for DualDiscs, deluxe editions, etc.)
  ACTUAL_DISCS=$(PYTHONPATH="$SPINDLEBOT_PIPELINE_DIR" $PYTHON -m spindlebot.disc count "$ALBUM_DIR")

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

  # ── Step 4: posttag ───────────────────────────────────────────────────────
  # Strip beets alias tags and truncate DATE to year only.
  # Runs after beet move (beet is fully done writing at this point).
  # shellcheck disable=SC2016  # $path is a beet template var, not a bash var
  IMPORT_FILES=$($BEET ls -f '$path' "added:${TODAY}.." 2>/dev/null | grep -v "^/Volumes/")
  if [ -n "$IMPORT_FILES" ]; then
    log "Running posttag on imported files"
    echo "$IMPORT_FILES" | PYTHONPATH="$SPINDLEBOT_PIPELINE_DIR" $PYTHON "$PRETAG" --post >> "$LOGFILE" 2>&1
  fi

  # ── Step 5: Extra assets ──────────────────────────────────────────────────
  # Copy non-audio files (artwork, liner notes, etc.) from the staging album
  # directory to the library album directory.  This preserves bonus artwork
  # like inner sleeve scans that Bandcamp and some download stores include.
  #
  # Rules:
  #   - Only copies from the source album dir (never recurses into subdirs)
  #   - Skips .log files (those are XLD artefacts, not assets)
  #   - Does not overwrite files that already exist in the library
  #   - Only runs when ALBUM_DIR is a real subdirectory (not the Staging root),
  #     because Staging-root imports are CD rips and have no extra assets.
  ALBUM_DIRS=$(echo "$IMPORT_FILES" | while IFS= read -r p; do dirname "$p"; done | sort -u)
  LIB_DEST=$(echo "$ALBUM_DIRS" | head -1)

  if [ -n "$LIB_DEST" ] && [ -d "$LIB_DEST" ] && [ "$ALBUM_DIR" != "$STAGING" ]; then
    AUDIO_PAT='\.(flac|mp3|m4a|aac|ogg|opus|wav|aif|aiff|wv|ape|wma|log)$'
    COPIED=0
    while IFS= read -r asset; do
      dest="$LIB_DEST/$(basename "$asset")"
      if [ ! -f "$dest" ]; then
        if cp "$asset" "$dest" 2>/dev/null; then
          log "Copied asset: $(basename "$asset") → $LIB_DEST"
          COPIED=$((COPIED + 1))
        else
          log "  warning: could not copy asset $(basename "$asset")"
        fi
      fi
    done < <(find "$ALBUM_DIR" -maxdepth 1 -type f | grep -viE "$AUDIO_PAT")
    if [ "$COPIED" -gt 0 ]; then
      log "Copied $COPIED extra asset(s) to library"
    fi
  fi

  # ── Step 6: Fetch synced lyrics ───────────────────────────────────────────
  # Filter to local Library only — DwRugged may not be mounted.
  if [ -n "$ALBUM_DIRS" ]; then
    while IFS= read -r dir; do
      log "Fetching lyrics for: $dir"
      $PYTHON "$FETCH_LYRICS" "$dir" >> "$LOGFILE" 2>&1
    done <<< "$ALBUM_DIRS"
  fi

  # ── Step 7: Archive XLD logs ──────────────────────────────────────────────
  # Only relevant for CD rips.  For directory-mode imports there is no .log
  # file to archive, so this loop is a no-op.
  mkdir -p "$COMPLETE"
  # Archive root-level .log files (standard XLD rip location)
  while IFS= read -r logfile; do
    mv "$logfile" "$COMPLETE/"
    log "Archived XLD log to: $COMPLETE/$(basename "$logfile")"
  done < <(find "$STAGING" -maxdepth 1 -name "*.log" 2>/dev/null)
  # Also archive any .log file that came in a named subdir (less common)
  if [[ "$IS_DIR_MODE" -eq 0 ]] && [ -f "$CHANGED" ]; then
    mv "$CHANGED" "$COMPLETE/" 2>/dev/null && \
      log "Archived XLD log to: $COMPLETE/$(basename "$CHANGED")"
  fi

else
  log "Import FAILED (exit $STATUS): $ALBUM_DIR"
fi
