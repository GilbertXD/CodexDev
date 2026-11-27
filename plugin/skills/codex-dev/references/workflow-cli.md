# Codex Dev Orchestrator CLI Reference

Binary: `codex-dev` (installed globally at `~/.local/bin/codex-dev`; fallback `python3 ~/.codex-dev/runtime/run_cli.py`).
All commands output structured JSON to stdout.

## Configuration & Models

- `codex-dev config-init [--overwrite]` — Initialize `~/.codex-dev/config.json` from verified local Codex configuration.
- `codex-dev models [--repo <path>]` — Show authorized models, routing policy, escalation order, and local Codex hints.

## Task Lifecycle

- `codex-dev init --name <title> --repo <path> [--description <text>] [--complexity low|medium|high] [--model-mode auto|manual] [--model <id>] [--lock-model] [--reasoning-effort low|medium|high|xhigh] [--lock-effort] [--max-repair-rounds 3] [--verify-cmd <cmd>] [--verify-env KEY=VAL] [--prepare-cmd <cmd>]`
- `codex-dev plan [--task-id <id>] (--plan-file <path|-> | --plan-text <md>) [--verify-cmd <cmd>] [--verify-env KEY=VAL] [--prepare-cmd <cmd>] [--target-file <path>] [--allow-replan]`
- `codex-dev prepare [--task-id <id>] [--prepare-cmd <cmd>] [--timeout <sec>]` — Run pre-Builder host environment & dependency preparation (unblocks `dependency_prepare_failure` / `environment_failure` once resolved without consuming code repair rounds).
- `codex-dev execute [--task-id <id>] [--model <id>] [--reasoning-effort low|medium|high|xhigh] [--timeout <sec>] [--idle-timeout <sec>] [--escalate]` — Enforce pre-Builder readiness gate and run Codex CLI inside the immutable `workspace-write` sandbox (includes max 1 non-escalating environment retry on permission/dependency/environment errors).
- `codex-dev verify [--task-id <id>] [--verify-cmd <cmd>] [--verify-env KEY=VAL] [--prepare-cmd <cmd>] [--skip-prepare] [--timeout <sec>] [--from-failed]` — Run post-Builder dependency tidy (e.g. `go mod tidy`) and authoritative host-level independent verification.
- `codex-dev run-round [--task-id <id>] [--model <id>] [--reasoning-effort low|medium|high|xhigh] [--codex-timeout <sec>] [--codex-idle-timeout <sec>] [--verify-cmd <cmd>] [--verify-env KEY=VAL] [--prepare-cmd <cmd>] [--skip-prepare] [--verify-timeout <sec>]` — Run full deterministic Pre-Prepare -> Execute -> Post-Tidy -> Verify cycle.
- `codex-dev review [--task-id <id>] --verdict <approve|request_changes|block> --summary <text> [--issue <item>]... [--ack-warning <note>]... [--feedback <text>]`

## Inspection, Control & Delivery

- `codex-dev status [--task-id <id>] [--events-limit 50]` — Full read-only state snapshot (including `environment_report` when blocked on environment).
- `codex-dev list [--limit 20]` — List recent tasks across all repositories.
- `codex-dev diff [--task-id <id>] [--summary-only]` — Full Git diff + untracked files in worktree.
- `codex-dev cancel [--task-id <id>] [--reason <text>]` — Safely terminate active Builder process and mark task `BLOCKED` (`cancelled`).
- `codex-dev recover [--task-id <id>]` — Recover a task interrupted by unexpected process termination.
- `codex-dev deliver [--task-id <id>] [--confirm-commit --commit-message <msg>]` — Inspect delivery summary; commits inside worktree branch ONLY with `--confirm-commit`.
