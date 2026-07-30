#!/bin/bash
# music-sync.sh — fires when the retention drive mounts (or run manually).
#
# Content-addressed sync (replaces the old rsync --remove-source-files MOVE):
#   inventory Pending → review + acknowledge → sync (copy → verify hash → record
#   presence on retention) → prune (release the Pending copy, but ONLY files that
#   are hash-verified on retention) → point beets at the retention path → notify.
#
# Nothing leaves Pending until a verified copy exists on retention, and prune
# only runs after a clean sync — so a copy can never be lost.
#
# The destination is whatever the first enabled [[destinations]] local_drive is
# in config.toml — nothing here is specific to any one drive.

# ── Load SpindleBot config ────────────────────────────────────────────────────
# shellcheck source=/dev/null
source "$HOME/.config/spindlebot/bootstrap.sh" 2>/dev/null || {
  echo "ERROR: SpindleBot not configured. Run setup.sh from the pipeline directory." >&2
  exit 1
}
export PYTHONPATH="$SPINDLEBOT_PIPELINE_DIR"

PENDING="$SPINDLEBOT_PENDING_DIR"
REMOTE="$SPINDLEBOT_DESTINATION_PATH"
DEST_NAME="$SPINDLEBOT_DESTINATION_NAME"   # the enabled local_drive [[destinations]] name
LOGFILE="$SPINDLEBOT_LOG_DIR/music-sync.log"
LOCKFILE="${SPINDLEBOT_SYNC_LOCKFILE:-/tmp/music-sync.lock}"
PYTHON="$SPINDLEBOT_PYTHON"
NOTIFY="$SPINDLEBOT_PIPELINE_DIR/music-notify.sh"
DB="$SPINDLEBOT_BEETS_DB"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOGFILE"; }
sb()  { "$PYTHON" -m spindlebot "$@" >> "$LOGFILE" 2>&1; }

# A destination must be configured, or there is nothing to sync to. Fail loud
# rather than silently pruning against an empty target.
if [ -z "$DEST_NAME" ] || [ -z "$REMOTE" ]; then
  log "No enabled local_drive destination configured — nothing to sync to. Check [[destinations]] in config.toml."
  exit 1
fi

# Prevent concurrent runs
if [ -f "$LOCKFILE" ]; then
  log "Already running (lockfile exists), skipping."
  exit 0
fi
touch "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

# Confirm the drive is actually mounted (launchd can fire spuriously)
if [ ! -d "$REMOTE" ]; then
  log "$DEST_NAME not mounted, skipping."
  exit 0
fi

# Anything to sync? Count only non-dotfiles — skips the location marker, a stray
# .DS_Store, ._ AppleDouble files, and the .nolrc marker, so macOS junk alone
# doesn't trigger a spurious no-op run.
if [ -z "$(find "$PENDING" -type f ! -name '.*' 2>/dev/null)" ]; then
  log "Nothing pending to sync."
  exit 0
fi

log "$DEST_NAME mounted — running content-addressed sync"

# 1. Catalog new imports. Per-file errors (e.g. one unreadable FLAC) are NOT
#    fatal: inventory isolates them and still catalogs everything else, so a
#    single bad file must not wedge the whole pipeline. Press on.
sb inventory --quiet || log "inventory reported per-file errors — continuing"

# 2. Plan the copies to the retention drive and acknowledge them (--yes) in one
#    shot. NOTE: requires the destination to have been inventoried once (the
#    reconciler's target-scan gate); this script never inventories the target.
#    A fresh install must run `spindlebot inventory --location "$DEST_NAME"` once.
if ! sb review --location "$DEST_NAME" --yes --quiet; then
  log "review failed — aborting (has $DEST_NAME been inventoried once?)"
  "$NOTIFY" "Sync failed" "review error — see music-sync.log"
  exit 1
fi

# 3. Copy → verify → record presence, scoped to this destination so we don't try
#    to execute copies queued for some other (possibly unmounted) destination.
#    If this fails, leave Pending intact.
if ! sb sync --location "$DEST_NAME" --quiet; then
  log "sync failed — leaving Pending untouched, NOT pruning"
  "$NOTIFY" "Sync failed" "copy/verify error — Pending untouched, see music-sync.log"
  exit 1
fi

# 4. Release Pending files now verified on retention (safe: verify-before-delete).
PRUNE_OK=1
if ! sb prune --execute --quiet; then
  log "prune reported issues — some files may remain in Pending — see music-sync.log"
  PRUNE_OK=0
fi

# 5. Point beets at the retention path for anything that left the Pending area.
if sqlite3 "$DB" \
    "UPDATE items SET path = replace(path, '${PENDING}', '${REMOTE}') WHERE path LIKE '${PENDING}/%';" 2>/dev/null; then
  log "Beets DB paths updated to $DEST_NAME"
else
  log "WARNING: beets DB path update failed"
fi

if [ "$PRUNE_OK" -eq 1 ]; then
  log "Sync complete."
  "$NOTIFY" "Sync complete" "New music copied to $DEST_NAME and released from Pending ✓"
else
  log "Sync finished with warnings — see music-sync.log"
  "$NOTIFY" "Sync finished with warnings" "Copied to $DEST_NAME; some files not released from Pending"
fi
