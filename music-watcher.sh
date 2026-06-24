#!/bin/bash
# music-watcher.sh — long-running daemon, kept alive by launchd.
# Watches the Import area for new items and kicks off the import pipeline:
#   .log file   — XLD rip complete; fire import with the log as trigger
#   directory   — album dropped directly into the Import area; fire import with the dir
#   other files — ignored (individual audio files appearing mid-copy, etc.)
#
# Installed to ~/.local/bin/ by setup.sh.

# shellcheck source=/dev/null
source "$HOME/.config/spindlebot/bootstrap.sh" 2>/dev/null || {
  echo "ERROR: SpindleBot not configured. Run setup.sh from the pipeline directory." >&2
  exit 1
}

IMPORT_SCRIPT="$SPINDLEBOT_PIPELINE_DIR/music-import.sh"
LOGFILE="$HOME/.config/beets/watcher.log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOGFILE"
}

if [[ -z "$SPINDLEBOT_IMPORT_DIR" ]]; then
  echo "ERROR: SPINDLEBOT_IMPORT_DIR is not set. Check bootstrap.sh." >&2
  exit 1
fi

log "Watcher started, monitoring: $SPINDLEBOT_IMPORT_DIR"

/opt/homebrew/bin/fswatch \
  --event=Created \
  --event=Updated \
  --event=Renamed \
  -0 \
  "$SPINDLEBOT_IMPORT_DIR" \
| while IFS= read -r -d "" changed_file; do
    if [[ "$changed_file" == *.log ]]; then
      log "XLD log detected: $changed_file"
      bash "$IMPORT_SCRIPT" "$changed_file"
    elif [[ -d "$changed_file" ]] && [[ "$(dirname "$changed_file")" == "$SPINDLEBOT_IMPORT_DIR" ]]; then
      log "Album directory detected: $changed_file"
      bash "$IMPORT_SCRIPT" "$changed_file"
    fi
  done
