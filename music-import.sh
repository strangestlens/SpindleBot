#!/bin/bash
# music-import.sh — triggered when XLD finishes ripping an album to Staging

STAGING="/Users/danielwilliams/Music/Staging"
COMPLETE="/Users/danielwilliams/Music/All Discs"
BEET="/opt/homebrew/bin/beet"
PYTHON="/opt/homebrew/bin/python3"
PRETAG="/Users/danielwilliams/.local/bin/music-pretag.py"
LOGFILE="/Users/danielwilliams/.config/beets/watcher.log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOGFILE"
}

log "Watcher fired: $1"

CHANGED="$1"

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
# Only wait if ALL flac files consistently report disctotal > 1 AND we're missing disc numbers.
/opt/homebrew/bin/python3 - "$ALBUM_DIR" << 'PYEOF'
import sys, glob, mutagen.flac

album_dir = sys.argv[1]
files = glob.glob(album_dir + "/*.flac")
if not files:
    sys.exit(0)

disc_totals = set()
disc_numbers = set()
for path in files:
    try:
        f = mutagen.flac.FLAC(path)
        dt = int((f.tags.get('disctotal') or f.tags.get('totaldiscs') or ['1'])[0])
        dn = int((f.tags.get('discnumber') or f.tags.get('disc') or ['1'])[0])
        disc_totals.add(dt)
        disc_numbers.add(dn)
    except:
        pass

# Only hold if ALL files agree on disctotal > 1 and we're missing discs
if len(disc_totals) == 1:
    disctotal = disc_totals.pop()
    if disctotal > 1 and len(disc_numbers) < disctotal:
        print(f"WAIT:{len(disc_numbers)}:{disctotal}")
        sys.exit(0)
PYEOF

WAIT_CHECK=$(/opt/homebrew/bin/python3 - "$ALBUM_DIR" << 'PYEOF'
import sys, glob, mutagen.flac

album_dir = sys.argv[1]
files = glob.glob(album_dir + "/*.flac")
if not files:
    sys.exit(0)

disc_totals = set()
disc_numbers = set()
for path in files:
    try:
        f = mutagen.flac.FLAC(path)
        dt = int((f.tags.get('disctotal') or f.tags.get('totaldiscs') or ['1'])[0])
        dn = int((f.tags.get('discnumber') or f.tags.get('disc') or ['1'])[0])
        disc_totals.add(dt)
        disc_numbers.add(dn)
    except:
        pass

if len(disc_totals) == 1:
    disctotal = disc_totals.pop()
    if disctotal > 1 and len(disc_numbers) < disctotal:
        print(f"WAIT:{len(disc_numbers)}:{disctotal}")
PYEOF
)

if [[ "$WAIT_CHECK" == WAIT:* ]]; then
  HAVE=$(echo "$WAIT_CHECK" | cut -d: -f2)
  NEED=$(echo "$WAIT_CHECK" | cut -d: -f3)
  log "Multi-disc album: have $HAVE of $NEED discs — waiting for remaining discs before importing"
  exit 0
fi
log "Disc check passed — proceeding with import"

# Step 1: Pre-process tags (feat., compilation, artist normalization)
log "Running pretag on: $ALBUM_DIR"
$PYTHON "$PRETAG" "$ALBUM_DIR" >> "$LOGFILE" 2>&1
if [ $? -ne 0 ]; then
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
  ARTIST_ALBUM=$($BEET ls -f '$albumartist - $album' "added:${TODAY}.." 2>/dev/null | sort -u | head -1)
  if [ -z "$ARTIST_ALBUM" ]; then
    ARTIST_ALBUM=$(basename "$ALBUM_DIR")
  fi
  /Users/danielwilliams/.local/bin/music-notify.sh "Rip complete" "$ARTIST_ALBUM — reply 'sync' to move to DwRugged"

  # Step 3: Post-import fixes
  # Fix multidisc: base it on how many disc numbers were ACTUALLY ripped,
  # not MusicBrainz disctotal (which can be >1 for DualDiscs, deluxe editions, etc.)
  ACTUAL_DISCS=$(/opt/homebrew/bin/python3 - "$ALBUM_DIR" << 'PYEOF'
import sys, glob, mutagen.flac
files = glob.glob(sys.argv[1] + "/*.flac")
discs = set()
for path in files:
    try:
        f = mutagen.flac.FLAC(path)
        dn = int((f.tags.get('discnumber') or f.tags.get('disc') or ['1'])[0])
        discs.add(dn)
    except: pass
print(len(discs))
PYEOF
)
  # Fix disctotal in DB and ensure multidisc flex attr row exists for every new item.
  # MusicBrainz can say disctotal=2 for a single-disc rip (DualDiscs, deluxe editions);
  # we patch disctotal to actual ripped count so the inline computes correctly.
  # beet modify multidisc= DELETES the flex attr row (empty value), leaving $multidisc
  # undefined in templates — which beet renders as the literal string "$multidisc" (truthy).
  # So we INSERT the row directly via sqlite instead of relying on beet modify.
  DB="/Users/danielwilliams/.config/beets/library.db"
  if [ "${ACTUAL_DISCS:-1}" -gt 1 ]; then
    $BEET modify --yes "added:${TODAY}.." "path:/Users/danielwilliams/Music/" disctotal="${ACTUAL_DISCS}" >> "$LOGFILE" 2>&1
    sqlite3 "$DB" "INSERT OR IGNORE INTO item_attributes (entity_id, key, value)
      SELECT id, 'multidisc', '1' FROM items
      WHERE added >= strftime('%s','${TODAY}') AND path LIKE '/Users/danielwilliams/Music/%'
        AND id NOT IN (SELECT entity_id FROM item_attributes WHERE key='multidisc');" 2>/dev/null
    log "Multi-disc rip ($ACTUAL_DISCS discs) — set disctotal=$ACTUAL_DISCS, multidisc=1"
  else
    $BEET modify --yes "added:${TODAY}.." "path:/Users/danielwilliams/Music/" disctotal=1 disc=1 >> "$LOGFILE" 2>&1
    # Ensure multidisc="" row exists so $multidisc is never the undefined literal
    sqlite3 "$DB" "INSERT OR IGNORE INTO item_attributes (entity_id, key, value)
      SELECT id, 'multidisc', '' FROM items
      WHERE added >= strftime('%s','${TODAY}') AND path LIKE '/Users/danielwilliams/Music/%'
        AND id NOT IN (SELECT entity_id FROM item_attributes WHERE key='multidisc');" 2>/dev/null
    log "Single-disc rip — patched disctotal=1, ensured multidisc row exists"
  fi
  $BEET move "added:${TODAY}.." "path:/Users/danielwilliams/Music/" >> "$LOGFILE" 2>&1
  log "Moved files to correct paths"

  # Step 4: posttag — strip beets alias tags and truncate DATE to year only.
  # Runs after beet move (beet is fully done writing at this point).
  # Also runs in sync script as a no-op safety net for any pre-existing files.
  IMPORT_FILES=$($BEET ls -f '$path' "added:${TODAY}.." 2>/dev/null | grep -v '^/Volumes/')
  if [ -n "$IMPORT_FILES" ]; then
    log "Running posttag on imported files"
    echo "$IMPORT_FILES" | /opt/homebrew/bin/python3 /Users/danielwilliams/.local/bin/music-pretag.py --post >> "$LOGFILE" 2>&1
  fi

  # Step 5: Fetch synced lyrics (.lrc sidecar files)
  # Filter to local Library only — DwRugged may not be mounted, and today's query
  # returns all modified items including previously synced DwRugged paths.
  ALBUM_DIRS=$(echo "$IMPORT_FILES" | while IFS= read -r p; do dirname "$p"; done | sort -u)
  if [ -n "$ALBUM_DIRS" ]; then
    while IFS= read -r dir; do
      log "Fetching lyrics for: $dir"
      /opt/homebrew/bin/python3 /Users/danielwilliams/.local/bin/music-fetch-lyrics.py "$dir" >> "$LOGFILE" 2>&1
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
