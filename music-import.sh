#!/bin/bash
# music-import.sh — triggered by fswatch when XLD finishes ripping to Staging.
# All pipeline logic lives in spindlebot/pipeline/runner.py.

# shellcheck source=/dev/null
source "$HOME/.config/spindlebot/bootstrap.sh" 2>/dev/null || {
  echo "ERROR: SpindleBot not configured. Run setup.sh from the pipeline directory." >&2
  exit 1
}

export PYTHONPATH="$SPINDLEBOT_PIPELINE_DIR"
exec "$SPINDLEBOT_PYTHON" -m spindlebot import "$@"
