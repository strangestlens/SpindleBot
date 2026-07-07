#!/bin/bash
# music-sync-rugged.sh — fires when DwRugged mounts (or run manually).
#
# Content-addressed sync (replaces the old rsync --remove-source-files MOVE):
#   inventory Pending → review + acknowledge → sync (copy → verify hash → record
#   presence on DwRugged) → prune (release the Pending copy, but ONLY files that
#   are hash-verified on DwRugged) → point beets at DwRugged → notify.
#
# Nothing leaves Pending until a verified copy exists on retention, and prune
# only runs after a clean sync — so a copy can never be lost.

# ── Load SpindleBot config ────────────────────────────────────────────────────
# shellcheck source=/dev/null
source "$HOME/.config/spindlebot/bootstrap.sh" 2>/dev/null || {
  echo "ERROR: SpindleBot not configured. Run setup.sh from the pipeline directory." >&2
  exit 1
}
export PYTHONPATH="$SPINDLEBOT_PIPELINE_DIR"

PENDING="$SPINDLEBOT_PENDING_DIR"
REMOTE="$SPINDLEBOT_DESTINATION_PATH"
DEST_NAME="DwRugged"                 # the [[destinations]] name; matches WatchPaths
LOGFILE="$SPINDLEBOT_LOG_DIR/rugged-sync.log"
LOCKFILE="/tmp/music-sync-rugged.lock"
PYTHON="$SPINDLEBOT_PYTHON"
NOTIFY="$SPINDLEBOT_PIPELINE_DIR/music-notify.sh"
DB="$SPINDLEBOT_BEETS_DB"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOGFILE"; }
sb()  { "$PYTHON" -m spindlebot "$@" >> "$LOGFILE" 2>&1; }

# Prevent concurrent runs
if [ -f "$LOCKFILE" ]; then
  log "Already running (lockfile exists), skipping."
  exit 0
fi
touch "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

# Confirm the drive is actually mounted (launchd can fire spuriously)
if [ ! -d "$REMOTE" ]; then
  log "DwRugged not mounted, skipping."
  exit 0
fi

# Anything to sync? (ignore the location marker file)
if [ -z "$(find "$PENDING" -type f ! -name '.spindlebot-location-*' 2>/dev/null)" ]; then
  log "Nothing pending to sync."
  exit 0
fi

log "DwRugged mounted — running content-addressed sync"

# 1. Catalog new imports in the Pending area.
if ! sb inventory --quiet; then
  log "inventory failed — aborting"
  "$NOTIFY" "Sync failed" "inventory error — see rugged-sync.log"
  exit 1
fi

# 2. Plan the copies to DwRugged and acknowledge them (--yes) in one shot.
if ! sb review --location "$DEST_NAME" --yes --quiet; then
  log "review failed — aborting (has DwRugged been inventoried once?)"
  "$NOTIFY" "Sync failed" "review error — see rugged-sync.log"
  exit 1
fi

# 3. Copy → verify → record presence. If this fails, leave Pending intact.
if ! sb sync --quiet; then
  log "sync failed — leaving Pending untouched, NOT pruning"
  "$NOTIFY" "Sync failed" "copy/verify error — Pending untouched, see rugged-sync.log"
  exit 1
fi

# 4. Release Pending files now verified on retention (safe: verify-before-delete).
if ! sb prune --execute --quiet; then
  log "prune reported issues — see rugged-sync.log"
fi

# 5. Point beets at DwRugged for anything that left the Pending area.
if sqlite3 "$DB" \
    "UPDATE items SET path = replace(path, '${PENDING}', '${REMOTE}') WHERE path LIKE '${PENDING}/%';" 2>/dev/null; then
  log "Beets DB paths updated to DwRugged"
else
  log "WARNING: beets DB path update failed"
fi

log "Sync complete."
"$NOTIFY" "Sync complete" "New music copied to DwRugged and released from Pending ✓"
