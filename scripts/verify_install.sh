#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_DEV_HOME="${CODEX_DEV_HOME:-$HOME/.codex-dev}"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"
PLUGIN_DIR="${ANTIGRAVITY_PLUGIN_DIR:-$HOME/.gemini/config/plugins/codex-dev}"

echo "==> Verifying CLI binary..."
test -x "$LOCAL_BIN/codex-dev"
"$LOCAL_BIN/codex-dev" --home "$CODEX_DEV_HOME" models >/dev/null
echo "  OK: $LOCAL_BIN/codex-dev works and loads verified models"

echo "==> Verifying plugin files..."
for f in \
  "$PLUGIN_DIR/plugin.json" \
  "$PLUGIN_DIR/mcp_config.json" \
  "$PLUGIN_DIR/assets/logo.svg" \
  "$PLUGIN_DIR/skills/codex-dev/SKILL.md" \
  "$PLUGIN_DIR/mcp-app/dist/server.mjs" \
  "$PLUGIN_DIR/mcp-app/dist/monitor.html"; do
  test -f "$f" || { echo "MISSING: $f"; exit 1; }
done
echo "  OK: All plugin & MCP App artifacts present in $PLUGIN_DIR"

echo "==> Verifying installed MCP App stdio server..."
node "$ROOT_DIR/tests/test_mcp_server.mjs" "$PLUGIN_DIR/mcp-app/dist/server.mjs"

if [ -n "${ANTIGRAVITY_LS_ADDRESS:-}" ] && [ -n "${ANTIGRAVITY_CSRF_TOKEN:-}" ]; then
  echo "==> Verifying Antigravity Language Server plugin discovery..."
  curl -sS -X POST "http://${ANTIGRAVITY_LS_ADDRESS}/exa.language_server_pb.LanguageServerService/GetAllPlugins" \
    -H "Content-Type: application/json" \
    -H "x-codeium-csrf-token: ${ANTIGRAVITY_CSRF_TOKEN}" \
    -d '{"workspaceUris":["file:///Users/hiya/CodexDev"]}' | python3 -c '
import json, sys
data = json.load(sys.stdin)
found = [p for p in data.get("plugins", []) if p.get("name") == "codex-dev"]
if not found:
    print("ERROR: codex-dev plugin not discovered by Antigravity Language Server!")
    sys.exit(1)
p = found[0]
skills = [s.get("name") for s in p.get("skills", [])]
mcps = [m.get("serverName") or m.get("name") or "" for m in p.get("mcpServers", [])]
disabled = p.get("disabled", False)
print(f"  OK: Discovered plugin codex-dev | disabled={disabled} | skills={skills} | mcp={mcps}")
assert "codex-dev" in skills, f"Expected skill codex-dev, got {skills}"
assert any("monitor" in m for m in mcps), f"Expected monitor MCP server, got {mcps}"
'
fi

echo "==> All installation verification checks passed."
