#!/bin/bash
# music-notify.sh — send a notification via macOS + Telegram
# Usage: music-notify.sh "Title" "Message"

TITLE="$1"
MESSAGE="$2"
BOT_TOKEN="8632337845:AAEB2cqVduKANm97DzZRKs6xl8vPeArHhlg"
CHAT_ID="6418395024"

# macOS notification
osascript -e "display notification \"$MESSAGE\" with title \"$TITLE\" sound name \"Glass\"" 2>/dev/null

# Telegram
curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
  -d "chat_id=${CHAT_ID}" \
  -d "text=🎵 *${TITLE}*%0A${MESSAGE}" \
  -d "parse_mode=Markdown" > /dev/null
