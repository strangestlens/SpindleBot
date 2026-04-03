#!/bin/bash
# music-notify.sh — send a notification via macOS + Telegram
# Usage: music-notify.sh "Title" "Message"

TITLE="$1"
MESSAGE="$2"

# ── Load SpindleBot config ────────────────────────────────────────────────────
# shellcheck source=/dev/null
source "$HOME/.config/spindlebot/bootstrap.sh" 2>/dev/null || {
  echo "ERROR: SpindleBot not configured. Run setup.sh from the pipeline directory." >&2
  exit 1
}

BOT_TOKEN="$SPINDLEBOT_TELEGRAM_TOKEN"
CHAT_ID="$SPINDLEBOT_TELEGRAM_CHAT_ID"

# macOS notification
if [ "${SPINDLEBOT_MACOS_NOTIFY:-1}" = "1" ]; then
  osascript -e "display notification \"$MESSAGE\" with title \"$TITLE\" sound name \"Glass\"" 2>/dev/null
fi

# Telegram
if [ "${SPINDLEBOT_TELEGRAM_ENABLED:-1}" = "1" ] && [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ]; then
  curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d "chat_id=${CHAT_ID}" \
    -d "text=🎵 *${TITLE}*%0A${MESSAGE}" \
    -d "parse_mode=Markdown" > /dev/null
fi
