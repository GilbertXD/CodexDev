---
name: codex-dev
description: >-
  Antigravity x Codex CLI automated development workflow (Plan -> Prepare -> Execute -> Verify -> Review -> Repair -> Deliver).
  Use when the user invokes /codex-dev or asks to develop, refactor, or fix code in any Git repository with Antigravity as Architect/Reviewer, Codex CLI as Builder in an isolated Git worktree, deterministic Python orchestration, and the read-only Codex Dev Monitor side panel.
---

# Codex Dev — Antigravity × Codex CLI Workflow

You are the **Architect** and **Reviewer**. **Codex CLI** is the **Builder**. The **Python Orchestrator** (`codex-dev`) manages the deterministic state machine, isolated Git worktrees, environment readiness gates, independent verification, and persistent artifacts under `~/.codex-dev/`. The **Codex Dev Monitor** MCP App side panel is strictly read-only.

**Antigravity chat is the sole control surface.** Never ask the user to manually copy `PLAN.md` or run orchestrator shell commands themselves.

## Operating Rules

1. **Orchestrator binary**: Run `codex-dev` (installed at `~/.local/bin/codex-dev`, fallback `python3 ~/.codex-dev/runtime/run_cli.py`). See [references/workflow-cli.md](./references/workflow-cli.md) for full flags.
2. **Immutable security & sandbox boundary (strict)**:
   - Builder **always** runs inside `workspace-write` within an isolated Git worktree (`~/.codex-dev/worktrees/<task_id>`), with `approval_policy="never"` and `CODEX_HOME` isolation (`~/.codex-dev/codex_home`, `builder_isolation: "strict"` by default).
   - Never configure, pass, or downgrade to `danger-full-access`.
   - Network access (`builder.network_access`), extra writable directories (`builder_add_dirs`), and environment variables (`builder_env`) are controlled solely by explicit upper-layer configuration and **never** expand automatically on retries.
   - Never overwrite uncommitted changes in the user's main repository, and never auto-commit, auto-push, auto-merge, or run destructive Git cleanups (`git clean -fdx`, `git reset --hard`) without explicit user confirmation in chat.
3. **Model & reasoning effort policy (strict)**:
   - Run `codex-dev models --repo <repo>` first to inspect verified `authorized_models`, `complexity_routing`, `effort_by_complexity`, and policy (`auto` vs `manual`).
   - **Auto mode (default)**: Select complexity (`low` | `medium` | `high`) based on scope and risk (prefer `high` for cross-layer features spanning DB migrations + API/service + tests or `>= 8` target files); the orchestrator routes `(model, reasoning_effort)`, promotes unlocked `auto` tasks upward at `codex-dev plan` when `PLAN.md` scope warrants it, and escalates within `authorized_models` / `effort_escalation_order` (up to `max_auto_effort`, default `xhigh`) **only** on code/verification failures — never on environment retries.
   - **Manual / Locked mode**: If the user specifies a Codex model or effort, pass `--model-mode manual --model <id> --lock-model` and/or `--reasoning-effort <low|medium|high|xhigh> --lock-effort`.
   - `max` and `ultra` reasoning efforts are **permanently disabled** and rejected. Never guess unverified model IDs.
   - **Reviewer model**: Inherits your current Antigravity session model by default.
4. **Read-only Monitor**: After initializing a task, call `open_monitor` on `codex-dev_monitor` (when connected) to open the read-only side panel (`aux_pane`).

## Intent Routing

- **New development task**: Follow Steps 1–6 below.
- **Status / progress check**: Run `codex-dev status` (or `codex-dev list`) and summarize state, phase, model, tests, diff stats, and latest review; optionally call `open_monitor`.
- **Stop / cancel task**: Run `codex-dev cancel --reason "<reason>"` and confirm the task is `BLOCKED` (`cancelled`).
- **Modify plan**: Update plan via `codex-dev plan --allow-replan --plan-file -` only when explicitly requested or when adjusting environment/check contracts, then resume execution.
- **Recover crashed run**: Run `codex-dev recover`.
- **Confirm commit / delivery**: Only after explicit user confirmation in chat, run `codex-dev deliver --confirm-commit --commit-message "<msg>"`.

## Full Workflow (Plan → Prepare → Execute → Verify → Review → Repair → Deliver)

### Step 1 — Inspect Repo & Initialize Task
1. Inspect the target Git repository (default: active workspace root) to understand architecture, toolchain, existing dependencies, test stack, and sandbox config (`builder.network_access` in `~/.codex-dev/config.json` / `<repo>/.codex-dev*.json`).
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
4. If `open_monitor` is available, call it with `{"task_id": "<task_id>"}`.

### Step 2 — Lock `PLAN.md` & Prepare Environment Before Builder (`PLANNED`)
1. Author a concise, testable `PLAN.md` containing:
   - `## Goal`
   - `## Environment Requirements` (concise bullets listing required toolchains, existing dependencies, and any new dependencies to pre-stage so they are available inside Builder's `workspace-write` sandbox)
   - `## Target Files` (explicit expected files/directories in backticks, including any deployment/config/migration companions)
   - `## Implementation Plan`
   - `## Acceptance Criteria` (concrete, verifiable checklist items)
   - `## Builder Checks` (explicit checks executed verbatim inside Builder's `workspace-write` sandbox — **no automatic test filtering or command rewriting is performed**. When `builder.network_access` is `false`, specify offline/loopback-free checks such as `go build ./...`, `go vet ./...`, typechecks, or pure unit packages, and keep loopback/DB/integration tests in `## Verification Commands`)
   - `## Verification Commands` (authoritative host-level full test, build, lint, typecheck commands in backticks; caching runners are normalized with `-count=1` / `--no-cache` unless `verify.no_cache: false`)
   - `## Out of Scope`
2. Lock the contract via stdin:
   ```bash
   codex-dev plan --task-id <task_id> --plan-file - <<'EOF'
   <PLAN.md contents>
   EOF
   ```
3. **Pre-Builder Environment Check & Dependency Preparation**:
   - Inspect toolchains, existing worktree dependencies (e.g. `node_modules`, `.venv`, Go module cache), and any newly introduced dependencies from `PLAN.md`.
   - When host preparation is needed before Builder starts (e.g. `go mod download`, `go get <pkg>@<ver>`, `npm ci`), run `codex-dev prepare --task-id <task_id> --prepare-cmd "<cmd>"` (or pass `--prepare-cmd` on `init`/`plan`/`run-round`).
   - **Never start Builder if environment preparation fails** — resolve the host toolchain/dependency issue first.

### Step 3 — Execute Builder & Run Independent Verification
Run one deterministic Pre-Prepare → Execute → Post-Tidy → Verify cycle:
```bash
codex-dev run-round --task-id <task_id>
```
Check the returned JSON `next_action`:
- `review_required` → Proceed to **Step 4 (Code Review)**.
- `blocked_dependency_prepare` → Pre-Builder environment preparation or post-Builder dependency tidy (e.g. `go mod tidy`) failed (`failure_category == "dependency_prepare_failure"`). Builder was **not** started if pre-prepare failed, and `repair_round` was **not** consumed. Fix the host dependency/network/toolchain issue and re-run `codex-dev prepare` / `codex-dev run-round` (or `codex-dev verify` if post-tidy).
- `blocked_environment` → Builder hit a sandbox permission, dependency, or environment exception (`failure_category == "environment_failure"`). The Orchestrator already executed at most **1 automatic environment retry** (no model escalation, no permission expansion, worktree changes preserved, `repair_round` unchanged) and persisted `ENVIRONMENT_REPORT.json`. Inspect `codex-dev status --task-id <task_id>`, resolve the missing dependency on the host via `codex-dev prepare --task-id <task_id> --prepare-cmd "<cmd>"` (or replan `Builder Checks` if a host-only check was mistakenly placed in `Builder Checks`, or ask the user before changing sandbox config), then resume `codex-dev run-round --task-id <task_id>`.
- `repair_available` → Code quality / invariant / host verification failure (`builder_partial`, `empty_diff`, or `test_failure`); `FEEDBACK.md` was generated with failure excerpts and `verify_logs/*.log` paths. Inspect logs, enrich `FEEDBACK.md` if helpful, and re-run `codex-dev run-round --task-id <task_id>` (up to 3 code repair rounds).
- `blocked_retry_limit` → Max code repair rounds (3) reached. Stop and report diagnostics clearly to the user.
- `failed` or `blocked` → Inspect `state.failure_category`. If `failure_category == "timeout"` and `codex-dev diff --summary-only` shows substantial worktree edits, you may run `codex-dev verify --task-id <task_id> --from-failed`; otherwise report clearly to the user.

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
   - Check `verification_summary.skip_detection`, `skipped_tests_total`, and `skipped_tests` (newly added/modified skipped tests automatically fail verification via `changed_tests_skipped`; report any pre-existing skipped tests explicitly).
   - No unintended or out-of-scope edits exist, and edge cases/error handling/security are solid.
4. Record the verdict:
   - Approve:
     ```bash
     codex-dev review --task-id <task_id> --verdict approve --summary "<concise approval conclusion>" [--ack-warning "<justified audit warning note>"]
     ```
   - Request changes:
     ```bash
     codex-dev review --task-id <task_id> \
       --verdict request_changes \
       --summary "<concise summary>" \
       --issue "<concrete issue 1>" \
       --feedback "<actionable fix guidance>"
     ```

### Step 5 — Automatic Code Repair Loop (Max 3 Rounds)
If `review` returns `next_action: "repair_available"`:
- Run `codex-dev run-round --task-id <task_id>` for the incremental code fix.
- Repeat Steps 3–4 until `approve` (`COMPLETED`) or `blocked_retry_limit` (`BLOCKED`).

### Step 6 — Deliver (Human-Confirmed Commit Only)
Once `COMPLETED`:
1. Run `codex-dev deliver --task-id <task_id>` (read-only summary — does NOT commit).
2. Present changed files + diff stats, verification & review results, model(s)/rounds/tokens, worktree path, and branch (`codex-dev/<task_id>`), and ask explicitly whether to commit on the worktree branch.
3. Only after explicit user confirmation in chat, run:
   ```bash
   codex-dev deliver --task-id <task_id> --confirm-commit --commit-message "<confirmed message>"
   ```
