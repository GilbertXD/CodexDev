#!/usr/bin/env bash
set -euo pipefail

CODEX_DEV_HOME="${CODEX_DEV_HOME:-$HOME/.codex-dev}"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"
PLUGIN_DIR="${ANTIGRAVITY_PLUGIN_DIR:-$HOME/.gemini/config/plugins/codex-dev}"

echo "Removing Codex Dev global installation..."
rm -f "$LOCAL_BIN/codex-dev"
rm -rf "$CODEX_DEV_HOME/runtime"
rm -rf "$PLUGIN_DIR"

echo "Removed CLI, runtime, and Antigravity plugin."
echo "Preserved task history and config in $CODEX_DEV_HOME (remove manually if desired)."
