"""Command-line interface for the Codex Dev deterministic orchestrator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import get_codex_dev_home
from .models import InvalidModelError
from .orchestrator import Orchestrator
from .state import StateTransitionError, TaskLockError


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _read_text_arg(text: str | None, file_path: str | None) -> str:
    if file_path:
        if file_path == "-":
            return sys.stdin.read()
        return Path(file_path).read_text(encoding="utf-8")
    if text is not None:
        return text
    raise ValueError("Provide either inline text or a file path.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex-dev",
        description="Codex Dev deterministic workflow orchestrator (Antigravity x Codex CLI).",
    )
    p.add_argument(
        "--home",
        default=None,
        help="Override CODEX_DEV_HOME persistent directory (default: ~/.codex-dev).",
    )

    sub = p.add_subparsers(dest="command", required=True)

    # config-init
    ci = sub.add_parser("config-init", help="Initialize ~/.codex-dev/config.json from verified local Codex models.")
    ci.add_argument("--overwrite", action="store_true", help="Overwrite existing config.json.")

    # models
    mp = sub.add_parser("models", help="Show authorized models, routing policy, and verified local Codex hints.")
    mp.add_argument("--repo", default=None, help="Optional repository path for repo-level config override.")

    # init
    ip = sub.add_parser("init", help="Initialize a new task and isolated Git worktree.")
    ip.add_argument("--name", required=True, help="Human-readable task name.")
    ip.add_argument("--repo", default=".", help="Target Git repository path.")
    ip.add_argument("--description", default="", help="Task description.")
    ip.add_argument("--model-mode", choices=["auto", "manual"], default=None, help="Model selection policy mode.")
    ip.add_argument("--model", default=None, help="Explicit authorized Builder model ID.")
    ip.add_argument("--lock-model", action="store_true", default=None, help="Lock model across all repair rounds.")
    ip.add_argument("--no-lock-model", dest="lock_model", action="store_false", help="Allow authorized model escalation.")
    ip.add_argument("--complexity", choices=["low", "medium", "high"], default="medium", help="Task complexity tier.")
    ip.add_argument("--reasoning-effort", default=None, help="Codex reasoning effort override (low/medium/high/xhigh; max/ultra disabled).")
    ip.add_argument("--lock-effort", action="store_true", default=None, help="Lock reasoning effort across all repair rounds.")
    ip.add_argument("--no-lock-effort", dest="lock_effort", action="store_false", help="Allow automatic reasoning effort escalation.")
    ip.add_argument("--max-repair-rounds", type=int, default=None, help="Maximum automatic repair rounds (default: 3).")
    ip.add_argument("--verify-cmd", action="append", default=None, help="Explicit independent verification command (repeatable).")
    ip.add_argument("--verify-env", action="append", default=None, help="Task-level verification env override KEY=VALUE (repeatable).")
    ip.add_argument("--prepare-cmd", action="append", default=None, help="Explicit non-sandbox dependency preparation command (repeatable).")
    ip.add_argument("--task-id", default=None, help="Optional custom task ID.")

    # plan
    pp = sub.add_parser("plan", help="Save immutable PLAN.md contract for a task.")
    pp.add_argument("--task-id", default=None, help="Task ID (defaults to active task).")
    pp.add_argument("--plan-file", default=None, help="Path to PLAN.md file (or '-' for stdin).")
    pp.add_argument("--plan-text", default=None, help="Inline PLAN.md markdown text.")
    pp.add_argument("--verify-cmd", action="append", default=None, help="Explicit verification command (repeatable).")
    pp.add_argument("--verify-env", action="append", default=None, help="Task-level verification env override KEY=VALUE (repeatable).")
    pp.add_argument("--prepare-cmd", action="append", default=None, help="Explicit non-sandbox prepare command (repeatable).")
    pp.add_argument("--target-file", action="append", default=None, help="Expected file/dir scope (repeatable).")
    pp.add_argument("--allow-replan", action="store_true", help="Explicitly replace existing plan contract.")

    # execute
    ep = sub.add_parser("execute", help="Run Codex CLI Builder for the current round.")
    ep.add_argument("--task-id", default=None, help="Task ID (defaults to active task).")
    ep.add_argument("--model", default=None, help="Optional per-round authorized model override (if unlocked).")
    ep.add_argument("--reasoning-effort", default=None, help="Optional per-round reasoning effort override (low/medium/high/xhigh; max/ultra disabled).")
    ep.add_argument("--timeout", type=int, default=None, help="Total Codex execution timeout in seconds.")
    ep.add_argument("--idle-timeout", type=int, default=None, help="Idle no-event timeout in seconds (0 disables).")
    ep.add_argument("--escalate", action="store_true", help="Request authorized model escalation if unlocked.")

    # prepare
    prp = sub.add_parser("prepare", help="Run pre-Builder environment and dependency preparation on the host.")
    prp.add_argument("--task-id", default=None, help="Task ID (defaults to active task).")
    prp.add_argument("--prepare-cmd", action="append", default=None, help="Override preparation commands (repeatable).")
    prp.add_argument("--timeout", type=int, default=None, help="Per-command timeout in seconds.")

    # verify
    vp = sub.add_parser("verify", help="Run post-Builder dependency tidy (if needed) and independent verification.")
    vp.add_argument("--task-id", default=None, help="Task ID (defaults to active task).")
    vp.add_argument("--verify-cmd", action="append", default=None, help="Override verification commands.")
    vp.add_argument("--verify-env", action="append", default=None, help="Per-run verification env override KEY=VALUE (repeatable).")
    vp.add_argument("--prepare-cmd", action="append", default=None, help="Override post-Builder tidy commands.")
    vp.add_argument("--skip-prepare", action="store_true", help="Skip automatic post-Builder dependency tidy.")
    vp.add_argument("--timeout", type=int, default=None, help="Per-command timeout in seconds.")
    vp.add_argument(
        "--from-failed",
        action="store_true",
        help="Explicitly allow verifying existing worktree changes when FAILED due to timeout.",
    )

    # run-round
    rrp = sub.add_parser("run-round", help="Prepare environment before Builder, execute Builder, tidy dependencies, and verify.")
    rrp.add_argument("--task-id", default=None, help="Task ID (defaults to active task).")
    rrp.add_argument("--model", default=None, help="Optional per-round authorized model override.")
    rrp.add_argument("--reasoning-effort", default=None, help="Optional per-round reasoning effort override (low/medium/high/xhigh; max/ultra disabled).")
    rrp.add_argument("--codex-timeout", type=int, default=None, help="Total Codex timeout in seconds.")
    rrp.add_argument("--codex-idle-timeout", type=int, default=None, help="Codex idle no-event timeout in seconds (0 disables).")
    rrp.add_argument("--verify-cmd", action="append", default=None, help="Explicit verification command.")
    rrp.add_argument("--verify-env", action="append", default=None, help="Per-run verification env override KEY=VALUE (repeatable).")
    rrp.add_argument("--prepare-cmd", action="append", default=None, help="Explicit pre-Builder prepare command.")
    rrp.add_argument("--skip-prepare", action="store_true", help="Skip automatic post-Builder dependency tidy.")
    rrp.add_argument("--verify-timeout", type=int, default=None, help="Verification command timeout in seconds.")

    # review
    rvp = sub.add_parser("review", help="Record Antigravity semantic Code Review verdict.")
    rvp.add_argument("--task-id", default=None, help="Task ID (defaults to active task).")
    rvp.add_argument("--verdict", required=True, choices=["approve", "request_changes", "block"])
    rvp.add_argument("--summary", required=True, help="Concise review conclusion.")
    rvp.add_argument("--feedback", default="", help="Detailed feedback text.")
    rvp.add_argument("--feedback-file", default=None, help="Read detailed feedback from file.")
    rvp.add_argument("--issue", action="append", default=None, help="Specific blocking issue (repeatable).")
    rvp.add_argument(
        "--ack-warning",
        action="append",
        default=None,
        help="Explicitly acknowledge a justified verification/diff-audit warning (repeatable).",
    )
    rvp.add_argument("--reviewer-model", default=None, help="Explicit reviewer model (only used if routing_supported=true).")

    # status
    sp = sub.add_parser("status", help="Inspect read-only status snapshot of a task.")
    sp.add_argument("--task-id", default=None, help="Task ID (defaults to active task).")
    sp.add_argument("--events-limit", type=int, default=50, help="Max events to return.")

    # list
    lp = sub.add_parser("list", help="List recent tasks.")
    lp.add_argument("--limit", type=int, default=20, help="Max tasks to return.")

    # diff
    dp = sub.add_parser("diff", help="Inspect full worktree diff and untracked files.")
    dp.add_argument("--task-id", default=None, help="Task ID (defaults to active task).")
    dp.add_argument("--summary-only", action="store_true", help="Omit full_diff body.")

    # cancel
    cp = sub.add_parser("cancel", help="Stop and block a running task safely.")
    cp.add_argument("--task-id", default=None, help="Task ID (defaults to active task).")
    cp.add_argument("--reason", default="Cancelled by user via Antigravity chat", help="Cancellation reason.")

    # recover
    rcp = sub.add_parser("recover", help="Recover a task interrupted by unexpected process termination.")
    rcp.add_argument("--task-id", default=None, help="Task ID (defaults to active task).")

    # deliver
    dlp = sub.add_parser("deliver", help="Inspect delivery summary or commit worktree after human confirmation.")
    dlp.add_argument("--task-id", default=None, help="Task ID (defaults to active task).")
    dlp.add_argument("--confirm-commit", action="store_true", help="Explicitly confirm committing inside the worktree branch.")
    dlp.add_argument("--commit-message", default=None, help="Commit message when --confirm-commit is set.")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser().resolve() if args.home else get_codex_dev_home()
    orch = Orchestrator(home_dir=home)

    try:
        if args.command == "config-init":
            path = orch.ensure_config(overwrite=args.overwrite)
            _print_json({"ok": True, "config_path": str(path)})
            return 0

        if args.command == "models":
            repo = Path(args.repo).expanduser().resolve() if args.repo else None
            _print_json(orch.list_models(repo_path=repo))
            return 0

        if args.command == "init":
            res = orch.init_task(
                name=args.name,
                repo_path=Path(args.repo),
                description=args.description,
                model_mode=args.model_mode,
                model=args.model,
                lock_model=args.lock_model,
                complexity=args.complexity,
                reasoning_effort=args.reasoning_effort,
                lock_effort=args.lock_effort,
                max_repair_rounds=args.max_repair_rounds,
                verify_commands=args.verify_cmd,
                verify_env=args.verify_env,
                prepare_commands=args.prepare_cmd,
                task_id=args.task_id,
            )
            _print_json(res)
            return 0

        if args.command == "plan":
            plan_text = _read_text_arg(args.plan_text, args.plan_file)
            res = orch.save_plan(
                args.task_id,
                plan_text,
                verify_commands=args.verify_cmd,
                verify_env=args.verify_env,
                prepare_commands=args.prepare_cmd,
                target_files=args.target_file,
                allow_replan=args.allow_replan,
            )
            _print_json(res)
            return 0

        if args.command == "execute":
            res = orch.execute_round(
                args.task_id,
                override_model=args.model,
                override_effort=args.reasoning_effort,
                timeout_sec=args.timeout,
                idle_timeout_sec=args.idle_timeout,
                escalate_model=args.escalate,
            )
            _print_json(res)
            return 0 if res["ok"] else 2

        if args.command == "prepare":
            res = orch.prepare_task(
                args.task_id,
                prepare_commands=args.prepare_cmd,
                timeout_sec=args.timeout,
            )
            _print_json(res)
            return 0 if res["ok"] else 2

        if args.command == "verify":
            res = orch.verify_task(
                args.task_id,
                verify_commands=args.verify_cmd,
                verify_env=args.verify_env,
                prepare_commands=args.prepare_cmd,
                skip_prepare=args.skip_prepare,
                timeout_sec=args.timeout,
                from_failed=args.from_failed,
            )
            _print_json(res)
            return 0 if res["ok"] else 2

        if args.command == "run-round":
            res = orch.run_round(
                args.task_id,
                override_model=args.model,
                override_effort=args.reasoning_effort,
                codex_timeout_sec=args.codex_timeout,
                codex_idle_timeout_sec=args.codex_idle_timeout,
                verify_commands=args.verify_cmd,
                verify_env=args.verify_env,
                prepare_commands=args.prepare_cmd,
                skip_prepare=args.skip_prepare,
                verify_timeout_sec=args.verify_timeout,
            )
            _print_json(res)
            return 0 if res["ok"] else 2

        if args.command == "review":
            fb = args.feedback
            if args.feedback_file:
                fb = Path(args.feedback_file).read_text(encoding="utf-8")
            res = orch.record_review(
                args.task_id,
                verdict=args.verdict,
                summary=args.summary,
                feedback=fb,
                issues=args.issue,
                acknowledged_warnings=args.ack_warning,
                reviewer_model=args.reviewer_model,
            )
            _print_json(res)
            return 0

        if args.command == "status":
            _print_json(orch.get_status(args.task_id, events_limit=args.events_limit))
            return 0

        if args.command == "list":
            _print_json(
                {
                    "active_task_id": orch.store.get_active_task_id(),
                    "tasks": orch.store.list_tasks(limit=args.limit),
                }
            )
            return 0

        if args.command == "diff":
            diff = orch.get_diff(args.task_id, include_diff=not args.summary_only)
            _print_json(diff)
            return 0

        if args.command == "cancel":
            _print_json(orch.cancel_task(args.task_id, reason=args.reason))
            return 0

        if args.command == "recover":
            _print_json(orch.recover_task(args.task_id))
            return 0

        if args.command == "deliver":
            _print_json(
                orch.deliver_task(
                    args.task_id,
                    confirm_commit=args.confirm_commit,
                    commit_message=args.commit_message,
                )
            )
            return 0

        parser.error(f"Unknown command: {args.command}")
        return 1

    except (InvalidModelError, StateTransitionError, TaskLockError, ValueError, FileNotFoundError, RuntimeError) as exc:
        _print_json({"ok": False, "error_type": type(exc).__name__, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
