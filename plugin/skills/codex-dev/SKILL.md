---
name: codex-dev
description: >-
  Antigravity x Codex CLI automated development workflow (Plan -> Execute -> Verify -> Review -> Repair -> Deliver).
  Use when the user invokes /codex-dev or asks to develop, refactor, or fix code in any Git repository with Antigravity as Architect/Reviewer, Codex CLI as Builder in an isolated Git worktree, deterministic Python orchestration, and the read-only Codex Dev Monitor side panel.
---

# Codex Dev — Antigravity × Codex CLI Workflow

You are the **Architect** and **Reviewer**. **Codex CLI** is the **Builder**. The **Python Orchestrator** (`codex-dev`) manages the deterministic state machine, isolated Git worktrees, independent verification, and persistent artifacts under `~/.codex-dev/`. The **Codex Dev Monitor** MCP App side panel is strictly read-only.

**Antigravity chat is the sole control surface.** Never ask the user to manually copy `PLAN.md` or run orchestrator shell commands themselves.

## Operating Rules

1. **Orchestrator binary**: Run `codex-dev` (installed at `~/.local/bin/codex-dev`, fallback `python3 ~/.codex-dev/runtime/run_cli.py`). See [references/workflow-cli.md](./references/workflow-cli.md) for full flags.
2. **Model & reasoning effort policy (strict)**:
   - Run `codex-dev models --repo <repo>` first to inspect the user's verified `authorized_models`, `complexity_routing`, `effort_by_complexity`, and policy (`auto` vs `manual`).
   - **Auto mode (default)**: Select complexity (`low` | `medium` | `high`) based on scope and risk; the orchestrator routes both `(model, reasoning_effort)` (`low→low`, `medium→medium`, `high→high`) and may escalate within `authorized_models` / `effort_escalation_order` (up to `max_auto_effort`, default `xhigh`) on failed verification/execution rounds while retaining effort on targeted review-fix rounds.
   - **Manual / Locked mode**: If the user specifies a Codex model or effort (e.g. "use `gpt-6.2-sol`" or "use effort `high`"), pass `--model-mode manual --model <id> --lock-model` and/or `--reasoning-effort <low|medium|high|xhigh> --lock-effort`.
   - `max` and `ultra` reasoning efforts are **permanently disabled** and rejected by the orchestrator.
   - Never guess or substitute unverified model IDs. If a requested model is unauthorized, report the verified list and ask the user.
   - **Reviewer model**: Inherits your current Antigravity session model by default.
   - **Builder isolation**: By default (`builder_isolation: "strict"`), Codex CLI runs with an isolated `CODEX_HOME` (`~/.codex-dev/codex_home`) and disables personal plugins, skills, apps, hooks, and desktop notifications without modifying `~/.codex/`.
3. **Read-only Monitor**: After initializing a task, call the `open_monitor` tool on the `codex-dev_monitor` MCP server (when connected) to open the **Codex Dev Monitor** side panel (`aux_pane`) so the user can observe live progress.
4. **Git safety**:
   - Work always happens in an isolated Git worktree (`~/.codex-dev/worktrees/<task_id>`). Never overwrite uncommitted changes in the user's main repository.
   - Never auto-commit, auto-push, auto-merge, or run destructive Git cleanups (`git clean -fdx`, `git reset --hard`) without explicit user confirmation in chat.

## Intent Routing

- **New development task**: Follow Steps 1–6 below.
- **Status / progress check**: Run `codex-dev status` (or `codex-dev list`) and summarize state, phase, model, tests, diff stats, and latest review; optionally call `open_monitor`.
- **Stop / cancel task**: Run `codex-dev cancel --reason "<reason>"` and confirm the task is `BLOCKED` (`cancelled`).
- **Modify plan**: Update plan via `codex-dev plan --allow-replan --plan-file -` only when explicitly requested by the user, then resume execution.
- **Recover crashed run**: Run `codex-dev recover`.
- **Confirm commit / delivery**: Only after explicit user confirmation in chat, run `codex-dev deliver --confirm-commit --commit-message "<msg>"`.

## Full Workflow (Plan → Execute → Verify → Review → Repair → Deliver)

### Step 1 — Inspect Repo & Initialize Task
1. Inspect the target Git repository (default: active workspace root) and read relevant files to understand architecture, test stack, and conventions.
2. Run `codex-dev models --repo <repo>` to verify authorized Builder models.
3. Initialize the task:
   ```bash
   codex-dev init \
     --name "<concise task title>" \
     --repo "<repo_root>" \
     --description "<user request summary>" \
     --complexity <low|medium|high> \
     [--model-mode manual --model <id> --lock-model]
   ```
4. If the `open_monitor` MCP tool is available, call it with `{"task_id": "<task_id>"}` to open the read-only side panel.

### Step 2 — Author Immutable Plan Contract (`PLANNED`)
Write a clear, testable `PLAN.md` containing:
- `## Goal`
- `## Target Files` (explicit expected files/directories in backticks; whenever a task introduces new runtime CLI/binary dependencies, config keys, environment variables, or database migrations/schema changes, audit and include deployment/config files such as `Dockerfile`, `docker-compose.yml`, `.env.example`, config templates, and existing migration/rollback integration tests up front to avoid avoidable `unexpected_files` audit warnings or regression failures)
- `## Implementation Plan`
- `## Acceptance Criteria` (concrete, verifiable checklist items)
- `## Builder Checks` (optional sandbox-safe offline checks for Codex CLI to run inside `workspace-write`, e.g., `go vet ./...`, `go test -short ./...`, `make build`)
- `## Verification Commands` (authoritative host-level test, build, lint, typecheck commands in backticks; if a task requires task-specific env vars such as live smoke test targets, pass repeatable `--verify-env KEY=VALUE` on `init`/`plan`/`verify`/`run-round`)
- `## Out of Scope`

Lock the contract via stdin so the user never copies anything:
```bash
codex-dev plan --task-id <task_id> --plan-file - <<'EOF'
<PLAN.md contents>
EOF
```

### Step 3 — Execute Builder & Run Independent Verification
Run one deterministic Execute → Verify cycle:
```bash
codex-dev run-round --task-id <task_id>
```
Check the returned JSON `next_action`:
- `review_required` → Proceed to **Step 4 (Code Review)**.
- `repair_available` → Verification failed; `FEEDBACK.md` was generated automatically. Inspect the failure, enrich `FEEDBACK.md` if needed, and re-run `codex-dev run-round --task-id <task_id>` (up to 3 total repair rounds).
- `blocked_retry_limit` → Max repair rounds (3) reached. Stop and report diagnostics clearly to the user.
- `blocked_dependency_prepare` → Non-sandbox dependency preparation (e.g., `go mod tidy` after `go.mod` edits) failed (`failure_category == "dependency_prepare_failure"`). Repair rounds were **not** consumed; inspect network/proxy config (e.g., `verify.env.GOPROXY` in `~/.codex-dev/config.json`) or dependency versions and re-run `codex-dev verify` once resolved.
- `failed` or `blocked` → Inspect `state.failure_category` (`execution_failure`, `builder_blocked`, `timeout`, `cancelled`, `invalid_model`, `contract_violation`, `dependency_prepare_failure`). If `failure_category == "timeout"` and `codex-dev diff --summary-only` shows the Builder already wrote substantial changes to the worktree, you may explicitly run `codex-dev verify --task-id <task_id> --from-failed` to verify existing worktree artifacts without re-running the Builder; otherwise report clearly to the user.

### Step 4 — Semantic Code Review (Antigravity as Reviewer)
When state is `REVIEWING`:
1. Inspect the full diff and untracked files:
   ```bash
   codex-dev diff --task-id <task_id>
   ```
2. Inspect `verification.json` and `RESULT.json` via `codex-dev status --task-id <task_id>`.
3. Verify semantically against the immutable `PLAN.md`:
   - Every acceptance criterion is genuinely satisfied.
   - No tests, linters, type checks, or configs were weakened or bypassed.
   - Check `verification_summary.skipped_tests_total` and `skipped_tests`: if tests were skipped (e.g., DB integration tests waiting on `TEST_DATABASE_URL`), verify whether it is an environment prerequisite (which can be configured via `repo_overrides.<repo_path>.verify.env` in `~/.codex-dev/config.json` or `<repo>/.codex-dev.local.json`) and report skipped coverage explicitly to the user.
   - No unintended or out-of-scope edits exist.
   - Edge cases, error handling, readability, and security are solid.
4. Record the verdict:
   - If satisfied:
     ```bash
     codex-dev review --task-id <task_id> --verdict approve --summary "<concise approval conclusion>"
     ```
   - If issues found:
     ```bash
     codex-dev review --task-id <task_id> \
       --verdict request_changes \
       --summary "<concise summary>" \
       --issue "<concrete issue 1>" \
       --issue "<concrete issue 2>" \
       --feedback "<actionable fix guidance>"
     ```

### Step 5 — Automatic Repair Loop (Max 3 Rounds)
If `review` returns `next_action: "repair_available"`:
- Immediately run `codex-dev run-round --task-id <task_id>` to dispatch Codex CLI for the incremental fix (in `auto` mode, the orchestrator may escalate within authorized models).
- Repeat Steps 3–4 until `approve` (`COMPLETED`) or `blocked_retry_limit` (`BLOCKED`).

### Step 6 — Deliver (Human-Confirmed Commit Only)
Once `COMPLETED`:
1. Run `codex-dev deliver --task-id <task_id>` (read-only summary — does NOT commit).
2. Present to the user:
   - What changed (files + diff stats)
   - Verification results & Code Review conclusion
   - Builder model(s) used, repair rounds, and token usage (if available)
   - Worktree path and Git branch (`codex-dev/<task_id>`)
   - Ask explicitly whether they would like to commit on the worktree branch (and propose a commit message), or inspect/merge manually.
3. Only after the user confirms in chat, run:
   ```bash
   codex-dev deliver --task-id <task_id> --confirm-commit --commit-message "<confirmed message>"
   ```
