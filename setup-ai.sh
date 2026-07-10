#!/bin/bash
# Sets up the OPTIONAL AI lyric-timing environment (lyric_timing/ real backend).
# Heavy deps — torch, torchaudio, demucs — go into a dedicated venv so core
# spindlebot stays light. Safe to re-run; a previously working venv is
# restored if every install attempt fails.
#
# Venv location: ~/.local/share/spindlebot/ai-venv (override: SPINDLEBOT_AI_VENV).
# Models (~2-3 GB: Demucs htdemucs + wav2vec2 alignment) download to ~/.cache
# on first alignment run, not here.
set -euo pipefail

VENV_DIR="${SPINDLEBOT_AI_VENV:-$HOME/.local/share/spindlebot/ai-venv}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS="$SCRIPT_DIR/requirements-ai.txt"

# torch wheels: try newest supported first; fall back if
# dependency resolution fails (e.g. a transitive dep lacks wheels for that version).
CANDIDATES=(python3.13 python3.12 python3.11 python3.10)

# Venvs embed absolute paths, so always build at the real location; keep any
# existing venv aside and restore it if no candidate succeeds.
BACKUP=""
if [ -d "$VENV_DIR" ]; then
    BACKUP="$VENV_DIR.bak"
    rm -rf "$BACKUP"
    mv "$VENV_DIR" "$BACKUP"
fi

for py in "${CANDIDATES[@]}"; do
    if ! command -v "$py" >/dev/null 2>&1; then
        continue
    fi
    echo "==> Trying $py ($(command -v "$py"))"
    rm -rf "$VENV_DIR"
    "$py" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
    if "$VENV_DIR/bin/pip" install -r "$REQUIREMENTS"; then
        [ -n "$BACKUP" ] && rm -rf "$BACKUP"
        echo "==> AI venv ready: $VENV_DIR (python: $py)"
        echo "==> Verify with: $VENV_DIR/bin/python -c 'import torch, torchaudio, demucs; print(torch.backends.mps.is_available())'"
        exit 0
    fi
    echo "==> Install failed under $py; trying next candidate" >&2
done

rm -rf "$VENV_DIR"
if [ -n "$BACKUP" ]; then
    mv "$BACKUP" "$VENV_DIR"
    echo "==> All candidates failed; previous venv restored untouched" >&2
fi
echo "ERROR: no Python in ${CANDIDATES[*]} could install requirements-ai.txt" >&2
exit 1
