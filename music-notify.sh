#!/bin/bash
# music-notify.sh — shim. Logic lives in spindlebot/pipeline/stages/notify.py.

# shellcheck source=/dev/null
source "$HOME/.config/spindlebot/bootstrap.sh" 2>/dev/null || {
  echo "ERROR: SpindleBot not configured. Run setup.sh from the pipeline directory." >&2
  exit 1
}

exec "$SPINDLEBOT_PYTHON" -m spindlebot notify "$@"
