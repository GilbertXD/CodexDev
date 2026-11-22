#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CODEX_DEV_HOME="${CODEX_DEV_HOME:-$HOME/.codex-dev}"
RUNTIME_DIR="$CODEX_DEV_HOME/runtime"
LOCAL_BIN="${LOCAL_BIN:-$HOME/.local/bin}"
PLUGIN_DIR="${ANTIGRAVITY_PLUGIN_DIR:-$HOME/.gemini/config/plugins/codex-dev}"

echo "==> [1/5] Building Codex Dev Monitor MCP App..."
(
  cd "$ROOT_DIR/mcp-app"
  if [ ! -d node_modules ]; then
    npm install --no-audit --no-fund
  fi
  npm run build
)

echo "==> [2/5] Installing Python Orchestrator runtime to $RUNTIME_DIR..."
mkdir -p "$RUNTIME_DIR" "$LOCAL_BIN"
rm -rf "$RUNTIME_DIR/codex_dev"
cp -R "$ROOT_DIR/src/codex_dev" "$RUNTIME_DIR/codex_dev"
cat > "$RUNTIME_DIR/run_cli.py" <<'EOF'
#!/usr/bin/env python3
import sys
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from codex_dev.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
EOF
chmod +x "$RUNTIME_DIR/run_cli.py"

cat > "$LOCAL_BIN/codex-dev" <<EOF
#!/usr/bin/env bash
exec python3 "$RUNTIME_DIR/run_cli.py" "\$@"
EOF
chmod +x "$LOCAL_BIN/codex-dev"

echo "==> [3/5] Initializing global configuration at $CODEX_DEV_HOME/config.json..."
"$LOCAL_BIN/codex-dev" --home "$CODEX_DEV_HOME" config-init

echo "==> [4/5] Installing global Antigravity plugin (Skill + MCP App) to $PLUGIN_DIR..."
mkdir -p "$PLUGIN_DIR/mcp-app/dist" "$PLUGIN_DIR/assets" "$PLUGIN_DIR/skills/codex-dev/references"
cp "$ROOT_DIR/plugin/plugin.json" "$PLUGIN_DIR/plugin.json"
cp "$ROOT_DIR/plugin/mcp_config.json" "$PLUGIN_DIR/mcp_config.json"
cp "$ROOT_DIR/plugin/assets/logo.svg" "$PLUGIN_DIR/assets/logo.svg"
cp "$ROOT_DIR/plugin/skills/codex-dev/SKILL.md" "$PLUGIN_DIR/skills/codex-dev/SKILL.md"
cp "$ROOT_DIR/plugin/skills/codex-dev/references/workflow-cli.md" "$PLUGIN_DIR/skills/codex-dev/references/workflow-cli.md"
cp "$ROOT_DIR/mcp-app/dist/server.mjs" "$PLUGIN_DIR/mcp-app/dist/server.mjs"
cp "$ROOT_DIR/mcp-app/dist/monitor.html" "$PLUGIN_DIR/mcp-app/dist/monitor.html"

echo "==> [5/5] Installation complete."
echo "  Orchestrator CLI : $LOCAL_BIN/codex-dev"
echo "  Runtime package  : $RUNTIME_DIR/codex_dev"
echo "  Global config    : $CODEX_DEV_HOME/config.json"
echo "  Antigravity plugin: $PLUGIN_DIR"
echo "  Global Skill     : /codex-dev (bundled in plugin)"
echo "  MCP App Server   : codex-dev_monitor (aux_pane + global)"
