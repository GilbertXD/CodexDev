# Codex Dev Orchestrator CLI Reference

Binary: `codex-dev` (installed globally at `~/.local/bin/codex-dev`; fallback `python3 ~/.codex-dev/runtime/run_cli.py`).
All commands output structured JSON to stdout.

## Configuration & Models

- `codex-dev config-init [--overwrite]` — Initialize `~/.codex-dev/config.json` from verified local Codex configuration.
- `codex-dev models [--repo <path>]` — Show authorized models, routing policy, escalation order, and local Codex hints.

## Task Lifecycle

- `codex-dev init --name <title> --repo <path> [--description <text>] [--complexity low|medium|high] [--model-mode auto|manual] [--model <id>] [--lock-model] [--max-repair-rounds 3] [--verify-cmd <cmd>]`
- `codex-dev plan [--task-id <id>] (--plan-file <path|-> | --plan-text <md>) [--verify-cmd <cmd>] [--target-file <path>] [--allow-replan]`
- `codex-dev execute [--task-id <id>] [--model <id>] [--timeout <sec>] [--escalate]`
- `codex-dev verify [--task-id <id>] [--verify-cmd <cmd>] [--timeout <sec>]`
- `codex-dev run-round [--task-id <id>] [--model <id>] [--codex-timeout <sec>] [--verify-cmd <cmd>] [--verify-timeout <sec>]`
- `codex-dev review [--task-id <id>] --verdict <approve|request_changes|block> --summary <text> [--issue <item>]... [--feedback <text>]`

## Inspection, Control & Delivery

- `codex-dev status [--task-id <id>] [--events-limit 50]` — Full read-only state snapshot.
- `codex-dev list [--limit 20]` — List recent tasks across all repositories.
- `codex-dev diff [--task-id <id>] [--summary-only]` — Full Git diff + untracked files in worktree.
- `codex-dev cancel [--task-id <id>] [--reason <text>]` — Safely terminate active Builder process and mark task `BLOCKED` (`cancelled`).
- `codex-dev recover [--task-id <id>]` — Recover a task interrupted by unexpected process termination.
- `codex-dev deliver [--task-id <id>] [--confirm-commit --commit-message <msg>]` — Inspect delivery summary; commits inside worktree branch ONLY with `--confirm-commit`.
