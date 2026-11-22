# Codex Dev — Antigravity × Codex CLI Automated Development Workflow

`Codex Dev` is a globally installed, deterministic **Antigravity × Codex CLI** automated development workflow.

```text
User (Antigravity Chat: /codex-dev)
   │
   ▼
Antigravity (Architect & Reviewer)
   ├── Writes immutable PLAN.md contract
   ├── Invokes deterministic Python Orchestrator (`codex-dev`)
   │      ├── Creates isolated Git Worktree (`~/.codex-dev/worktrees/<id>`)
   │      ├── Runs non-interactive `codex exec` (Builder, `-s workspace-write`)
   │      ├── Streams JSONL events, tracks models & token usage
   │      └── Runs stack-agnostic Independent Verification & Integrity Audit
   ├── Performs Semantic Code Review (up to 3 auto-repair rounds)
   └── Delivers & commits ONLY after explicit user confirmation in chat
   │
   ▼ (read-only)
Codex Dev Monitor (MCP App Side Panel · Minimal Monochrome)
```

## Architecture & Responsibilities

| Layer | Component | Responsibility |
| :--- | :--- | :--- |
| **Control & Architecture** | Antigravity (`/codex-dev` Skill) | Only control surface; writes immutable `PLAN.md`, decides model policy, performs semantic Code Review, confirms delivery with user. |
| **Builder** | Codex CLI (`codex exec`) | Executes plans non-interactively in an isolated Git Worktree with `-s workspace-write`, `--json`, and `--output-schema`. |
| **Orchestrator** | Python (`src/codex_dev/`, `~/.local/bin/codex-dev`) | 9-state deterministic state machine (`CREATED`, `PLANNED`, `EXECUTING`, `VERIFYING`, `REVIEWING`, `REVISING`, `COMPLETED`, `BLOCKED`, `FAILED`), atomic persistence, writer locks, independent verification, and diff/contract auditing. |
| **Monitor UI** | MCP App (`mcp-app/`, `codex-dev_monitor`) | Strictly read-only Antigravity `aux_pane` & `global` side panel (`@modelcontextprotocol/ext-apps`) in Minimal Monochrome theme with live refresh. |

## Installation

```bash
chmod +x scripts/install.sh scripts/uninstall.sh scripts/verify_install.sh
./scripts/install.sh
./scripts/verify_install.sh
```

Installed locations:
- **Global CLI**: `~/.local/bin/codex-dev`
- **Python Runtime**: `~/.codex-dev/runtime/codex_dev`
- **Global Configuration**: `~/.codex-dev/config.json`
- **Task Persistence & Worktrees**: `~/.codex-dev/tasks/` & `~/.codex-dev/worktrees/`
- **Antigravity Plugin (Global Skill + MCP App)**: `~/.gemini/config/plugins/codex-dev/`

## Usage in Antigravity Chat

In **any** Git repository open in Antigravity:

- Start a task (auto model routing):
  ```text
  /codex-dev Add rate limiting middleware with full unit tests
  ```
- Start a task with a specific locked Builder model:
  ```text
  /codex-dev Use locked model gpt-6-luna to fix the CSV parser edge cases
  ```
- Query status or open the monitor:
  ```text
  /codex-dev Show current task status and open Codex Dev Monitor
  ```
- Stop a running task:
  ```text
  /codex-dev Stop the current task
  ```
- Confirm delivery commit:
  After review passes (`COMPLETED`), Antigravity presents the diff summary, branch, and worktree path. Confirm in chat to commit on the isolated branch (`codex-dev/<task_id>`). No automatic `commit`, `push`, or `merge` is ever performed.

## Model Policy Configuration

Edit `~/.codex-dev/config.json` (global) or `<repo>/.codex-dev.json` (repository override):

```json
{
  "model_policy": {
    "mode": "auto",
    "locked": false,
    "allow_escalation": true,
    "default_model": "gpt-6-luna",
    "authorized_models": ["gpt-6-luna", "gpt-6.2-sol"],
    "complexity_routing": {
      "low": "gpt-6-luna",
      "medium": "gpt-6-luna",
      "high": "gpt-6.2-sol"
    },
    "escalation_order": ["gpt-6-luna", "gpt-6.2-sol"],
    "reasoning_effort": "low"
  }
}
```

- Only models listed in `authorized_models` are accepted; unverified or unauthorized model IDs are rejected immediately.
- Reviewer inherits the active Antigravity session model by default.

## Verification & Testing

```bash
# Run Mock Codex orchestrator test suite (all 8 test suites / 11+ scenarios)
python3 -m unittest tests/test_orchestrator.py -v

# Run MCP App stdio protocol & live read-only snapshot test
node tests/test_mcp_server.mjs

# Verify global installation and Antigravity plugin registration
./scripts/verify_install.sh
```
