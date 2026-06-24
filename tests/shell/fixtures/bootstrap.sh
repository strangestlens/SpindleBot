#!/bin/bash
# Test fixture: minimal bootstrap.sh sourced in place of the real one.
# Callers must set BATS_TMPDIR before sourcing.

export SPINDLEBOT_IMPORT_DIR="${BATS_TMPDIR}/Import"
export SPINDLEBOT_ARCHIVE_DIR="${BATS_TMPDIR}/AllDiscs"
export SPINDLEBOT_LOG_DIR="${BATS_TMPDIR}/logs"
export SPINDLEBOT_PENDING_DIR="${BATS_TMPDIR}/Pending"
export SPINDLEBOT_BEET="${BATS_TMPDIR}/bin/beet"
export SPINDLEBOT_PYTHON="${BATS_TMPDIR}/bin/python"
export SPINDLEBOT_BEETS_DB="${BATS_TMPDIR}/library.db"
export SPINDLEBOT_BEETS_CONFIG="${BATS_TMPDIR}/beets-config.yaml"
export SPINDLEBOT_PIPELINE_DIR="${BATS_TMPDIR}/pipeline"
export SPINDLEBOT_DESTINATION_PATH="${BATS_TMPDIR}/DwRugged"
export SPINDLEBOT_TELEGRAM_TOKEN="fake-token"
export SPINDLEBOT_TELEGRAM_CHAT_ID="fake-chat"
export SPINDLEBOT_LYRICS_DELAY="0"
export SPINDLEBOT_MACOS_NOTIFY="0"
export SPINDLEBOT_TELEGRAM_ENABLED="0"
