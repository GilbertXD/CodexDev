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

### Recommended Antigravity Permission Setup

Because `codex-dev` stores global task metadata, isolated Git worktrees, and runtime code under `~/.codex-dev/` (outside the active repository workspace), Antigravity's terminal sandbox may prompt on each new `codex-dev` subcommand unless allowlisted once. In Antigravity **Settings → Permissions**:

1. Add `codex-dev` to **Command Allowlist** (so orchestrator subcommands run cleanly without repeated prompts).
2. Add a **Permission Grant / Non-Workspace File Access** allowance for `~/.codex-dev` (preserving strict workspace sandboxing everywhere else).

### Builder Isolation, Environment Preparation & Exception Handling

- **Immutable Sandbox Boundary (`workspace-write`)**: Codex CLI always runs in an isolated Git worktree (`~/.codex-dev/worktrees/<task_id>`) with `-s workspace-write` and `approval_policy="never"`. Downgrading to `danger-full-access` is rejected, and permissions (`network_access`, `builder_add_dirs`, `builder_isolation`) never expand on retries.
- **Strict Builder Isolation (`builder_isolation: "strict"`, default)**: Before launching `codex exec`, the Orchestrator provisions an isolated `CODEX_HOME` at `~/.codex-dev/codex_home` (symlinking `auth.json` read-only and seeding a local copy of `models_cache.json` without modifying `~/.codex/`) and disables third-party plugins, apps, hooks, skill search, and notifications.
- **Pre-Builder Environment Preparation & Post-Builder Tidy**: Antigravity inspects toolchains and dependencies from `PLAN.md` (`## Environment Requirements`) and prepares dependencies on the host before starting Builder (`codex-dev prepare` / `run-round` pre-gate). If preparation fails, Builder is never launched. Post-execution dependency tidy (such as `go mod tidy` when `go.mod` changes) is retained before host verification.
- **Verbatim Builder Checks vs Host Verify**: `## Builder Checks` run verbatim inside `workspace-write` (no hidden test-skipping rewrites), while full integration/socket/DB suites execute on the host in `## Verification Commands`.
- **Separated Environment Retries vs Code Repairs**: Permission, dependency, and environment exceptions trigger at most **1 automatic retry** (retaining the same model/effort, permissions, worktree edits, and `repair_round`), then persist `ENVIRONMENT_REPORT.json` and escalate `blocked_environment` to Antigravity — keeping all 3 code repair rounds reserved strictly for compile, test, and review fixes.
- **Post-Turn Grace Period (`codex_post_turn_grace_sec: 4`)**: After `turn.completed` arrives and the structured output file is saved on disk, lingering Codex CLI teardown processes are gracefully terminated after the short grace window.

You can pass environment overrides (such as `GOPROXY`, `TEST_DATABASE_URL`, or task-specific live test targets) to Verify and Prepare stages via:
1. Global `~/.codex-dev/config.json` (`verify.env` / `prepare.env`)
2. Shared repository `<repo>/.codex-dev.json`
3. Non-intrusive per-repository `repo_overrides` inside `~/.codex-dev/config.json`
4. Machine-local `<repo>/.codex-dev.local.json`
5. Task/command-level `--verify-env KEY=VALUE` on `init`, `plan`, `verify`, or `run-round` (highest precedence; persisted per task, with only key names recorded in `verification.json`)

```json
{
  "prepare": {
    "enabled": true,
    "auto_go_mod_tidy": true,
    "commands": [],
    "timeout_sec": 180,
    "env": {}
  },
  "verify": {
    "commands": [],
    "env": {
      "GOPROXY": "https://goproxy.cn,direct"
    }
  },
  "repo_overrides": {
    "/path/to/your-repo": {
      "verify": {
        "env": {
          "TEST_DATABASE_URL": "postgres://user:pass@localhost:5432/app?sslmode=disable"
        }
      }
    }
  }
}
```

### Skipped-Test Visibility

Independent verification parses test runner output across Go (`--- SKIP:`), Python (`unittest` / `pytest`), Rust (`ignored`), Node (`node --test`), and Jest/Vitest. Any skipped tests are recorded in `verification.json` (`skipped_tests_total`, `skipped_tests`), surfaced as a non-blocking `skipped_tests` audit warning, and highlighted in the **Codex Dev Monitor** side panel so silently skipped integration suites never go unnoticed.

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
    "reasoning_effort": "medium",
    "effort_locked": false,
    "effort_by_complexity": {
      "low": "low",
      "medium": "medium",
      "high": "high"
    },
    "effort_escalation_order": ["low", "medium", "high", "xhigh"],
    "max_auto_effort": "xhigh"
  }
}
```

- Only models listed in `authorized_models` are accepted; unverified or unauthorized model IDs are rejected immediately.
- Allowed reasoning efforts are `low`, `medium`, `high`, and `xhigh`. `max` and `ultra` are **permanently disabled** and rejected if requested.
- On repair rounds after test/execution failures, `auto` mode escalates model first and then `reasoning_effort` up to `max_auto_effort` (clamped to the model's `supported_efforts`). On targeted `review_failure` rounds, current effort is retained unless overridden.
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
