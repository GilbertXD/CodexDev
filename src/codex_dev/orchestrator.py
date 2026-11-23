"""Deterministic Codex Dev workflow orchestrator."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

from .codex_runner import build_codex_prompt, run_codex_exec
from .config import (
    ensure_default_global_config,
    get_codex_dev_home,
    load_config,
    read_local_codex_hints,
    resolve_codex_timeouts,
)
from .contract import (
    save_immutable_contract,
    verify_contract_integrity,
)
from .models import (
    ALLOWED_EFFORTS,
    DISALLOWED_EFFORTS,
    InvalidModelError,
    make_model_history_entry,
    resolve_task_model_policy,
    select_model_for_round,
)
from .state import (
    AGENT_BY_STATE,
    STATE_BLOCKED,
    STATE_COMPLETED,
    STATE_CREATED,
    STATE_EXECUTING,
    STATE_FAILED,
    STATE_PLANNED,
    STATE_REVIEWING,
    STATE_REVISING,
    STATE_VERIFYING,
    StateTransitionError,
    TaskStore,
    atomic_write_json,
    atomic_write_text,
    generate_task_id,
    read_json,
    utc_now,
)
from .verifier import parse_env_assignments, run_dependency_prepare, run_independent_verification
from .worktree import (
    commit_worktree_changes,
    create_isolated_worktree,
    inspect_repo,
    inspect_worktree_changes,
)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _merge_token_totals(current: dict[str, Any] | None, delta: dict[str, Any] | None) -> dict[str, Any] | None:
    if delta is None:
        return current
    base = current or {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "available": True,
        "source": delta.get("source") or "codex_jsonl",
    }
    return {
        "input_tokens": int(base.get("input_tokens", 0)) + int(delta.get("input_tokens", 0)),
        "cached_input_tokens": int(base.get("cached_input_tokens", 0)) + int(delta.get("cached_input_tokens", 0)),
        "output_tokens": int(base.get("output_tokens", 0)) + int(delta.get("output_tokens", 0)),
        "total_tokens": int(base.get("total_tokens", 0)) + int(delta.get("total_tokens", 0)),
        "available": True,
        "source": delta.get("source") or base.get("source") or "codex_jsonl",
    }


def _format_verification_feedback(report: dict[str, Any], repair_round: int) -> str:
    lines = [
        f"# Verification Feedback (Before Repair Round {repair_round})",
        "",
        "Independent verification failed. Fix all issues below without weakening tests or altering the immutable plan contract.",
        "",
    ]
    if not report.get("contract_integrity", {}).get("valid", True):
        lines.append(f"- **Contract Integrity Violation**: {report['contract_integrity'].get('reason')}")
    for issue in report.get("diff_audit", {}).get("blocking_issues", []):
        lines.append(f"- **Integrity Audit Blocking Issue** (`{issue.get('file')}`): {issue.get('message')}")
    for chk in report.get("checks", []):
        if not chk.get("passed"):
            lines.append(f"## Failed Check: `{chk['name']}` (`{chk['command']}`, exit={chk['exit_code']})")
            if chk.get("stdout_tail"):
                lines.extend(["```text", chk["stdout_tail"][-1500:].strip(), "```"])
            if chk.get("stderr_tail"):
                lines.extend(["```text", chk["stderr_tail"][-1500:].strip(), "```"])
            lines.append("")
    return "\n".join(lines).strip() + "\n"


class Orchestrator:
    """High-level deterministic coordinator for Codex Dev tasks."""

    def __init__(self, home_dir: Path | None = None) -> None:
        self.home_dir = (home_dir or get_codex_dev_home()).resolve()
        self.store = TaskStore(self.home_dir)

    def resolve_task_id(self, task_id: str | None) -> str:
        if task_id:
            if not self.store.task_exists(task_id):
                raise FileNotFoundError(f"Task '{task_id}' does not exist in {self.store.tasks_dir}")
            return task_id
        active = self.store.get_active_task_id()
        if not active:
            raise FileNotFoundError("No active task found. Specify --task-id or initialize a new task.")
        return active

    def list_models(self, repo_path: Path | None = None) -> dict[str, Any]:
        cfg = load_config(repo_path=repo_path, home_dir=self.home_dir)
        hints = read_local_codex_hints()
        policy = cfg.get("model_policy") or {}
        return {
            "config_home": str(self.home_dir),
            "authorized_models": policy.get("authorized_models") or [],
            "default_model": policy.get("default_model"),
            "mode": policy.get("mode", "auto"),
            "locked": bool(policy.get("locked", False)),
            "allow_escalation": bool(policy.get("allow_escalation", True)),
            "complexity_routing": policy.get("complexity_routing") or {},
            "escalation_order": policy.get("escalation_order") or [],
            "reasoning_effort": policy.get("reasoning_effort", "medium"),
            "effort_locked": bool(policy.get("effort_locked", False)),
            "effort_by_complexity": policy.get("effort_by_complexity") or {},
            "effort_escalation_order": policy.get("effort_escalation_order") or list(ALLOWED_EFFORTS),
            "max_auto_effort": policy.get("max_auto_effort", "xhigh"),
            "allowed_efforts": list(ALLOWED_EFFORTS),
            "disallowed_efforts": sorted(DISALLOWED_EFFORTS),
            "reviewer": cfg.get("reviewer") or {},
            "local_codex_default": hints.get("default_model"),
            "local_codex_cached_models": hints.get("cached_models") or [],
        }

    def init_task(
        self,
        *,
        name: str,
        repo_path: Path,
        description: str = "",
        model_mode: str | None = None,
        model: str | None = None,
        lock_model: bool | None = None,
        complexity: str = "medium",
        reasoning_effort: str | None = None,
        lock_effort: bool | None = None,
        max_repair_rounds: int | None = None,
        verify_commands: list[str] | None = None,
        verify_env: list[str] | dict[str, str] | None = None,
        prepare_commands: list[str] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a new task, validate model policy, and provision an isolated Git worktree."""
        self.store.ensure_dirs()
        repo_info = inspect_repo(repo_path)
        repo_root = Path(repo_info["repo_root"])
        cfg = load_config(repo_path=repo_root, home_dir=self.home_dir)
        parsed_verify_env = parse_env_assignments(verify_env)
        hints = read_local_codex_hints()

        # Validate model & effort policy BEFORE creating worktree so invalid models/efforts fail cleanly
        task_model_policy = resolve_task_model_policy(
            cfg,
            mode=model_mode,
            requested_model=model,
            locked=lock_model,
            complexity=complexity,
            reasoning_effort=reasoning_effort,
            lock_effort=lock_effort,
            cached_models=hints.get("cached_models") or None,
        )

        tid = task_id or generate_task_id()
        tdir = self.store.task_dir(tid)
        if tdir.exists():
            raise FileExistsError(f"Task directory already exists: {tdir}")

        branch_prefix = str((cfg.get("git") or {}).get("branch_prefix") or "codex-dev/")
        branch_name = f"{branch_prefix}{tid}"
        worktree_path = self.store.worktrees_dir / tid

        wt_info = create_isolated_worktree(
            repo_root=repo_root,
            worktree_path=worktree_path,
            branch_name=branch_name,
            base_commit=repo_info["base_commit"],
        )

        created_at = utc_now()
        max_repairs = int(max_repair_rounds if max_repair_rounds is not None else cfg.get("max_repair_rounds", 3))

        task_meta = {
            "task_id": tid,
            "name": name.strip() or tid,
            "description": description.strip(),
            "repo_path": str(repo_root),
            "base_branch": repo_info["base_branch"],
            "base_commit": repo_info["base_commit"],
            "main_repo_dirty_at_init": repo_info["main_repo_dirty"],
            "main_repo_dirty_count_at_init": repo_info["main_repo_dirty_count"],
            "branch": wt_info["branch"],
            "worktree_path": wt_info["worktree_path"],
            "explicit_verify_commands": verify_commands or [],
            "explicit_verify_env": parsed_verify_env,
            "explicit_prepare_commands": prepare_commands or [],
            "created_at": created_at,
        }

        initial_state = {
            "task_id": tid,
            "name": task_meta["name"],
            "status": STATE_CREATED,
            "previous_status": None,
            "current_phase": "plan",
            "current_agent": AGENT_BY_STATE[STATE_CREATED],
            "phases": {
                "plan": "running",
                "execute": "pending",
                "verify": "pending",
                "review": "pending",
            },
            "model_policy": task_model_policy,
            "model_history": [],
            "repair_round": 0,
            "max_repair_rounds": max_repairs,
            "execution_attempts": 0,
            "failure_category": None,
            "last_error": None,
            "token_usage": {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "available": False,
                "source": None,
            },
            "verification_summary": None,
            "diff_stats": {
                "files_changed_count": 0,
                "total_insertions": 0,
                "total_deletions": 0,
                "added": [],
                "modified": [],
                "deleted": [],
                "renamed": [],
                "untracked": [],
                "files": [],
            },
            "latest_review": None,
            "delivery": {
                "committed": False,
                "commit": None,
                "branch": wt_info["branch"],
            },
            "created_at": created_at,
            "updated_at": created_at,
        }

        with self.store.acquire_write_lock(tid):
            atomic_write_json(tdir / "task.json", task_meta)
            atomic_write_json(tdir / "state.json", initial_state)
            atomic_write_json(tdir / "reviews.json", [])
            atomic_write_text(
                tdir / "EXECUTION_LOG.md",
                f"# Execution Log — {task_meta['name']} (`{tid}`)\n\n"
                f"- Repository: `{repo_root}`\n"
                f"- Base branch / commit: `{repo_info['base_branch']}` @ `{repo_info['base_commit'][:12]}`\n"
                f"- Isolated worktree: `{wt_info['worktree_path']}` (branch `{wt_info['branch']}`)\n"
                f"- Builder model policy: `{task_model_policy['mode']}` -> `{task_model_policy['current_model']}` "
                f"(locked={task_model_policy['locked']})\n",
            )
            self.store.append_event(
                tid,
                phase="plan",
                agent=AGENT_BY_STATE[STATE_CREATED],
                event_type="task_created",
                message=f"Initialized task '{task_meta['name']}' in isolated worktree {wt_info['branch']}",
                details={
                    "repo_path": str(repo_root),
                    "worktree_path": wt_info["worktree_path"],
                    "model": task_model_policy["current_model"],
                    "model_mode": task_model_policy["mode"],
                    "locked": task_model_policy["locked"],
                },
            )
            self.store.set_active_task(tid, str(repo_root), task_meta["name"])

        return {"task": task_meta, "state": initial_state}

    def save_plan(
        self,
        task_id: str | None,
        plan_text: str,
        *,
        verify_commands: list[str] | None = None,
        verify_env: list[str] | dict[str, str] | None = None,
        prepare_commands: list[str] | None = None,
        target_files: list[str] | None = None,
        allow_replan: bool = False,
    ) -> dict[str, Any]:
        """Save immutable PLAN.md contract and transition CREATED -> PLANNED."""
        tid = self.resolve_task_id(task_id)
        tdir = self.store.task_dir(tid)
        parsed_verify_env = parse_env_assignments(verify_env)
        with self.store.acquire_write_lock(tid):
            state = self.store.load_state(tid)
            meta = self.store.load_task_meta(tid)
            if parsed_verify_env:
                merged_env = dict(meta.get("explicit_verify_env") or {})
                merged_env.update(parsed_verify_env)
                meta["explicit_verify_env"] = merged_env
                atomic_write_json(tdir / "task.json", meta)
            effective_verify = verify_commands if verify_commands is not None else (meta.get("explicit_verify_commands") or None)
            effective_prepare = prepare_commands if prepare_commands is not None else (meta.get("explicit_prepare_commands") or None)
            contract = save_immutable_contract(
                tdir,
                plan_text,
                explicit_verify_commands=effective_verify,
                explicit_prepare_commands=effective_prepare,
                explicit_target_files=target_files,
                allow_replan=allow_replan,
            )
            state["contract_sha256"] = contract["plan_sha256"]
            state["acceptance_criteria"] = contract["acceptance_criteria"]
            self.store.transition(
                tid,
                state,
                STATE_PLANNED,
                event_type="plan_saved",
                message=f"Saved immutable PLAN.md contract ({len(contract['acceptance_criteria'])} acceptance criteria)",
                details={
                    "plan_sha256": contract["plan_sha256"],
                    "acceptance_criteria_count": len(contract["acceptance_criteria"]),
                    "target_files": contract["target_files"],
                },
            )
            self.store.append_execution_log(
                tid,
                "Plan Contract Locked",
                f"- SHA-256: `{contract['plan_sha256']}`\n"
                f"- Acceptance criteria: {len(contract['acceptance_criteria'])}\n"
                f"- Target files: {', '.join(contract['target_files']) or '(unspecified)'}\n",
            )
            return {"task_id": tid, "contract": contract, "state": state}

    def execute_round(
        self,
        task_id: str | None,
        *,
        override_model: str | None = None,
        override_effort: str | None = None,
        timeout_sec: int | None = None,
        idle_timeout_sec: int | None = None,
        escalate_model: bool = False,
    ) -> dict[str, Any]:
        """Run Codex CLI Builder inside the isolated worktree for the current round."""
        tid = self.resolve_task_id(task_id)
        tdir = self.store.task_dir(tid)

        with self.store.acquire_write_lock(tid):
            meta = self.store.load_task_meta(tid)
            state = self.store.load_state(tid)
            cfg = load_config(repo_path=Path(meta["repo_path"]), home_dir=self.home_dir)
            hints = read_local_codex_hints()

            # Verify immutable plan contract before invoking Builder
            contract_check = verify_contract_integrity(tdir)
            if not contract_check["valid"]:
                self.store.transition(
                    tid,
                    state,
                    STATE_FAILED,
                    event_type="contract_violation",
                    message=contract_check["reason"],
                    failure_category="contract_violation",
                    last_error=contract_check["reason"],
                )
                return {"ok": False, "task_id": tid, "state": state, "error": contract_check["reason"]}

            repair_round = int(state.get("repair_round", 0))
            should_escalate = escalate_model or (repair_round > 0 and state["model_policy"].get("mode") == "auto")
            prev_failure_category = state.get("failure_category")

            try:
                selection = select_model_for_round(
                    state["model_policy"],
                    cfg,
                    repair_round=repair_round,
                    override_model=override_model,
                    override_effort=override_effort,
                    escalate=should_escalate,
                    failure_category=prev_failure_category,
                    cached_models=hints.get("cached_models") or None,
                )
            except InvalidModelError as exc:
                self.store.transition(
                    tid,
                    state,
                    STATE_FAILED,
                    event_type="invalid_model",
                    message=str(exc),
                    failure_category="invalid_model",
                    last_error=str(exc),
                )
                raise

            state["model_policy"]["current_model"] = selection.model
            if selection.reasoning_effort:
                state["model_policy"]["current_reasoning_effort"] = selection.reasoning_effort
                state["model_policy"]["reasoning_effort"] = selection.reasoning_effort
            attempt_num = int(state.get("execution_attempts", 0)) + 1
            state["execution_attempts"] = attempt_num

            self.store.transition(
                tid,
                state,
                STATE_EXECUTING,
                event_type="builder_started",
                message=(
                    f"Handing off to Codex CLI Builder (round {repair_round}, attempt {attempt_num}, "
                    f"model={selection.model}, effort={selection.reasoning_effort or 'default'}): {selection.reason}"
                ),
                details={
                    "round": repair_round,
                    "attempt": attempt_num,
                    "model": selection.model,
                    "reasoning_effort": selection.reasoning_effort,
                    "mode": selection.mode,
                    "locked": selection.locked,
                    "effort_locked": selection.effort_locked,
                    "escalated_from": selection.escalated_from,
                    "effort_escalated_from": selection.effort_escalated_from,
                },
            )

            plan_text = (tdir / "PLAN.md").read_text(encoding="utf-8")
            contract_doc = read_json(tdir / "contract.json") or {}
            builder_checks = list(contract_doc.get("builder_checks") or [])
            builder_cfg = cfg.get("builder") if isinstance(cfg.get("builder"), dict) else {}
            builder_network_access = bool(builder_cfg.get("network_access", False))
            feedback_path = tdir / "FEEDBACK.md"
            feedback_text = feedback_path.read_text(encoding="utf-8") if feedback_path.is_file() else None
            worktree_path = Path(meta["worktree_path"])

            prompt = build_codex_prompt(
                task_name=meta["name"],
                plan_text=plan_text,
                feedback_text=feedback_text,
                repair_round=repair_round,
                worktree_path=worktree_path,
                builder_checks=builder_checks or None,
                builder_network_access=builder_network_access,
            )

            eff_timeout, eff_idle_timeout = resolve_codex_timeouts(
                cfg,
                explicit_timeout_sec=timeout_sec,
                explicit_idle_timeout_sec=idle_timeout_sec,
                complexity=state["model_policy"].get("complexity"),
                reasoning_effort=selection.reasoning_effort,
            )
            codex_bin = str(cfg.get("codex_binary") or "codex")
            sandbox_mode = str(cfg.get("sandbox_mode") or "workspace-write")

            def _live_usage_cb(usage: dict[str, Any]) -> None:
                state["token_usage"] = _merge_token_totals(state.get("token_usage"), usage)
                self.store.save_state(tid, state)

            outcome = run_codex_exec(
                store=self.store,
                task_id=tid,
                worktree_path=worktree_path,
                codex_binary=codex_bin,
                model=selection.model,
                sandbox_mode=sandbox_mode,
                reasoning_effort=selection.reasoning_effort,
                prompt=prompt,
                timeout_sec=eff_timeout,
                repair_round=repair_round,
                attempt=attempt_num,
                idle_timeout_sec=eff_idle_timeout,
                post_turn_grace_sec=float(cfg.get("codex_post_turn_grace_sec", 4)),
                builder_env=cfg.get("builder_env") if isinstance(cfg.get("builder_env"), dict) else None,
                builder_add_dirs=cfg.get("builder_add_dirs") if isinstance(cfg.get("builder_add_dirs"), list) else None,
                builder_isolation=str(cfg.get("builder_isolation") or "strict"),
                builder_network_access=builder_network_access,
                on_token_usage=_live_usage_cb,
            )

            # Record model history entry with final round token usage
            history_entry = make_model_history_entry(
                round_num=repair_round,
                role="builder",
                selection=selection,
                token_usage=outcome.token_usage,
            )
            history_entry["attempt"] = attempt_num
            state.setdefault("model_history", []).append(history_entry)

            # Update diff stats snapshot after Builder run
            try:
                diff_info = inspect_worktree_changes(worktree_path, meta["base_commit"], include_diff=False)
                state["diff_stats"] = {
                    "files_changed_count": diff_info["files_changed_count"],
                    "total_insertions": diff_info["total_insertions"],
                    "total_deletions": diff_info["total_deletions"],
                    "added": diff_info["added"],
                    "modified": diff_info["modified"],
                    "deleted": diff_info["deleted"],
                    "renamed": diff_info["renamed"],
                    "untracked": diff_info["untracked"],
                    "files": diff_info["files"],
                }
            except Exception:
                pass

            self.store.append_execution_log(
                tid,
                f"Builder Round {repair_round} Attempt {attempt_num} ({selection.model})",
                f"- Exit code: `{outcome.exit_code}` | Duration: `{outcome.duration_sec}s` "
                f"(timeout={eff_timeout}s, idle_timeout={eff_idle_timeout}s)\n"
                f"- Timed out: `{outcome.timed_out}` ({outcome.timeout_reason or 'n/a'}) | Cancelled: `{outcome.cancelled}`\n"
                f"- JSONL log: `{outcome.raw_jsonl_path}`\n"
                f"- Selection rationale: {selection.reason}\n"
                f"- Token usage: `{outcome.token_usage or 'unavailable'}`\n"
                f"- Summary: {(outcome.structured_result or {}).get('summary') or outcome.error_message or 'N/A'}\n",
            )

            if outcome.cancelled:
                self.store.transition(
                    tid,
                    state,
                    STATE_BLOCKED,
                    event_type="builder_cancelled",
                    message=outcome.error_message or "Builder execution cancelled.",
                    failure_category="cancelled",
                    last_error=outcome.error_message,
                )
                return {"ok": False, "task_id": tid, "outcome": outcome.__dict__, "state": state}

            if not outcome.ok:
                target_st = STATE_BLOCKED if outcome.failure_category == "builder_blocked" else STATE_FAILED
                self.store.transition(
                    tid,
                    state,
                    target_st,
                    event_type="builder_blocked" if target_st == STATE_BLOCKED else "builder_failed",
                    message=outcome.error_message or f"Builder failed (exit={outcome.exit_code})",
                    failure_category=outcome.failure_category or "execution_failure",
                    last_error=outcome.error_message,
                    details={"exit_code": outcome.exit_code, "timed_out": outcome.timed_out},
                )
                return {"ok": False, "task_id": tid, "outcome": outcome.__dict__, "state": state}

            # Builder completed cleanly -> move to VERIFYING
            self.store.transition(
                tid,
                state,
                STATE_VERIFYING,
                event_type="builder_completed",
                message=f"Codex CLI finished round {repair_round} in {outcome.duration_sec}s; ready for independent verification.",
                details={
                    "duration_sec": outcome.duration_sec,
                    "files_changed": state["diff_stats"]["files_changed_count"],
                },
            )
            return {"ok": True, "task_id": tid, "outcome": outcome.__dict__, "state": state}

    def prepare_task(
        self,
        task_id: str | None,
        *,
        prepare_commands: list[str] | None = None,
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        """Run non-sandbox dependency preparation (e.g. go mod tidy) explicitly without burning a repair round."""
        tid = self.resolve_task_id(task_id)
        tdir = self.store.task_dir(tid)
        with self.store.acquire_write_lock(tid):
            meta = self.store.load_task_meta(tid)
            state = self.store.load_state(tid)
            cfg = load_config(repo_path=Path(meta["repo_path"]), home_dir=self.home_dir)
            explicit_prep = (
                prepare_commands
                if prepare_commands is not None
                else (meta.get("explicit_prepare_commands") or None)
            )
            prep_report = run_dependency_prepare(
                task_dir=tdir,
                worktree_path=Path(meta["worktree_path"]),
                base_commit=meta["base_commit"],
                config=cfg,
                explicit_commands=explicit_prep,
                timeout_sec=timeout_sec,
            )
            if prep_report["ran"]:
                self.store.append_event(
                    tid,
                    phase="verify",
                    agent="Orchestrator (Verifier)",
                    event_type="prepare_completed" if prep_report["passed"] else "prepare_failed",
                    message=(
                        f"Dependency preparation {'passed' if prep_report['passed'] else 'failed'} "
                        f"({prep_report['commands_passed']}/{prep_report['commands_total']} commands, source={prep_report['command_source']})."
                    ),
                    details={
                        "commands_passed": prep_report["commands_passed"],
                        "commands_total": prep_report["commands_total"],
                        "source": prep_report["command_source"],
                    },
                )
                self.store.append_execution_log(
                    tid,
                    f"Dependency Preparation ({'PASSED' if prep_report['passed'] else 'FAILED'})",
                    f"- Source: `{prep_report['command_source']}` | Passed: `{prep_report['commands_passed']}/{prep_report['commands_total']}`\n"
                    + (
                        "- Failures:\n" + "\n".join(f"  - {r}" for r in prep_report["failure_reasons"]) + "\n"
                        if prep_report["failure_reasons"]
                        else ""
                    ),
                )
            return {"ok": prep_report["passed"], "task_id": tid, "report": prep_report, "state": state}

    def verify_task(
        self,
        task_id: str | None,
        *,
        verify_commands: list[str] | None = None,
        verify_env: list[str] | dict[str, str] | None = None,
        prepare_commands: list[str] | None = None,
        skip_prepare: bool = False,
        timeout_sec: int | None = None,
        from_failed: bool = False,
    ) -> dict[str, Any]:
        """Run non-sandbox dependency preparation (if needed) and independent verification."""
        tid = self.resolve_task_id(task_id)
        tdir = self.store.task_dir(tid)
        call_verify_env = parse_env_assignments(verify_env)

        with self.store.acquire_write_lock(tid):
            meta = self.store.load_task_meta(tid)
            state = self.store.load_state(tid)
            cfg = load_config(repo_path=Path(meta["repo_path"]), home_dir=self.home_dir)
            effective_verify_env = {**dict(meta.get("explicit_verify_env") or {}), **call_verify_env}

            if state["status"] != STATE_VERIFYING:
                allow_from_failed = False
                ev_type = "verification_started"
                ev_msg = "Starting independent verification."
                if state["status"] == STATE_FAILED and from_failed:
                    if state.get("failure_category") != "timeout":
                        raise StateTransitionError(
                            "verify --from-failed is only permitted when failure_category == 'timeout' "
                            f"(current failure_category={state.get('failure_category')!r})."
                        )
                    wt_diff = inspect_worktree_changes(
                        Path(meta["worktree_path"]), meta["base_commit"], include_diff=False
                    )
                    if wt_diff["files_changed_count"] <= 0:
                        raise StateTransitionError(
                            "verify --from-failed refused: isolated worktree has no modified or untracked files to verify."
                        )
                    allow_from_failed = True
                    ev_type = "verification_started_from_failed"
                    ev_msg = (
                        "Architect explicitly invoked verification on worktree artifacts after timeout "
                        f"({wt_diff['files_changed_count']} changed files)."
                    )
                self.store.transition(
                    tid,
                    state,
                    STATE_VERIFYING,
                    event_type=ev_type,
                    message=ev_msg,
                    allow_failed_to_verify=allow_from_failed,
                )
            else:
                state["failure_category"] = None
                state["last_error"] = None
                state["verification_summary"] = None
                self.store.save_state(tid, state)
                self.store.append_event(
                    tid,
                    phase="verify",
                    agent="Orchestrator (Verifier)",
                    event_type="verification_started",
                    message="Running independent verification commands and diff integrity audit.",
                )

            # Remove stale verification.json from any previous round so readers never render outdated checks
            stale_ver = tdir / "verification.json"
            if stale_ver.is_file():
                try:
                    stale_ver.unlink()
                except OSError:
                    pass

            # Non-sandbox dependency preparation (e.g. go mod tidy when go.mod changed)
            prep_report: dict[str, Any] | None = None
            extra_allowed_files: list[str] = []
            if not skip_prepare:
                explicit_prep = (
                    prepare_commands
                    if prepare_commands is not None
                    else (meta.get("explicit_prepare_commands") or None)
                )
                prep_report = run_dependency_prepare(
                    task_dir=tdir,
                    worktree_path=Path(meta["worktree_path"]),
                    base_commit=meta["base_commit"],
                    config=cfg,
                    explicit_commands=explicit_prep,
                    explicit_env=effective_verify_env or None,
                )
                if prep_report["ran"]:
                    extra_allowed_files = list(prep_report.get("generated_allowed_files") or [])
                    self.store.append_event(
                        tid,
                        phase="verify",
                        agent="Orchestrator (Verifier)",
                        event_type="prepare_completed" if prep_report["passed"] else "prepare_failed",
                        message=(
                            f"Non-sandbox dependency preparation {'passed' if prep_report['passed'] else 'failed'} "
                            f"({prep_report['commands_passed']}/{prep_report['commands_total']}, source={prep_report['command_source']})."
                        ),
                        details={
                            "source": prep_report["command_source"],
                            "commands_passed": prep_report["commands_passed"],
                            "commands_total": prep_report["commands_total"],
                        },
                    )
                    self.store.append_execution_log(
                        tid,
                        f"Dependency Preparation ({'PASSED' if prep_report['passed'] else 'FAILED'})",
                        f"- Source: `{prep_report['command_source']}` | Passed: `{prep_report['commands_passed']}/{prep_report['commands_total']}`\n"
                        + (
                            "- Failures:\n" + "\n".join(f"  - {r}" for r in prep_report["failure_reasons"]) + "\n"
                            if prep_report["failure_reasons"]
                            else ""
                        ),
                    )
                    if not prep_report["passed"]:
                        err_msg = "; ".join(prep_report["failure_reasons"])[:800] or "Dependency preparation failed."
                        # Do NOT burn a code-repair round on external dependency/network preparation errors
                        self.store.transition(
                            tid,
                            state,
                            STATE_BLOCKED,
                            event_type="dependency_prepare_blocked",
                            message=(
                                f"Dependency preparation failed outside sandbox (repair round not consumed): {err_msg}"
                            ),
                            failure_category="dependency_prepare_failure",
                            last_error=err_msg,
                            details={"prepare": prep_report},
                        )
                        return {
                            "ok": False,
                            "next_action": "blocked_dependency_prepare",
                            "task_id": tid,
                            "prepare": prep_report,
                            "report": None,
                            "state": state,
                        }

            eff_timeout = int(timeout_sec if timeout_sec is not None else cfg.get("verify_timeout_sec", 300))
            explicit_cmds = verify_commands if verify_commands is not None else (meta.get("explicit_verify_commands") or None)

            report = run_independent_verification(
                task_dir=tdir,
                worktree_path=Path(meta["worktree_path"]),
                base_commit=meta["base_commit"],
                config=cfg,
                explicit_commands=explicit_cmds,
                explicit_env=effective_verify_env or None,
                timeout_sec=eff_timeout,
                extra_allowed_files=extra_allowed_files,
                prepare_report=prep_report if (prep_report and prep_report["ran"]) else None,
            )

            state["diff_stats"] = report["diff_stats"]
            skipped_total = int(report.get("skipped_tests_total", 0))
            skipped_names = list(report.get("skipped_tests") or [])
            state["verification_summary"] = {
                "verified_at": report["verified_at"],
                "passed": report["passed"],
                "command_source": report["command_source"],
                "commands_total": report["commands_total"],
                "commands_passed": report["commands_passed"],
                "commands_failed": report["commands_failed"],
                "skipped_tests_total": skipped_total,
                "skipped_tests": skipped_names,
                "contract_valid": report["contract_integrity"]["valid"],
                "blocking_audit_issues": len(report["diff_audit"]["blocking_issues"]),
                "warnings": len(report["diff_audit"]["warnings"]),
                "checks": [
                    {
                        "name": c["name"],
                        "kind": c["kind"],
                        "command": c["command"],
                        "passed": c["passed"],
                        "exit_code": c["exit_code"],
                        "duration_sec": c["duration_sec"],
                        "skipped_count": int(c.get("skipped_count", 0)),
                    }
                    for c in report["checks"]
                ],
                "failure_reasons": report["failure_reasons"],
            }

            self.store.append_execution_log(
                tid,
                f"Independent Verification ({'PASSED' if report['passed'] else 'FAILED'})",
                f"- Source: `{report['command_source']}` | Passed: `{report['commands_passed']}/{report['commands_total']}`"
                + (f" | Skipped tests: `{skipped_total}`" if skipped_total > 0 else "")
                + "\n"
                f"- Contract integrity: `{report['contract_integrity']['valid']}`\n"
                f"- Audit blocking issues: `{len(report['diff_audit']['blocking_issues'])}` | Warnings: `{len(report['diff_audit']['warnings'])}`\n"
                f"- Changed files: `{report['diff_stats']['files_changed_count']}` "
                f"(+{report['diff_stats']['total_insertions']}/-{report['diff_stats']['total_deletions']})\n",
            )

            if report["passed"]:
                skip_note = f", {skipped_total} test(s) skipped" if skipped_total > 0 else ""
                self.store.transition(
                    tid,
                    state,
                    STATE_REVIEWING,
                    event_type="verification_passed",
                    message=(
                        f"Independent verification passed ({report['commands_passed']}/{report['commands_total']} checks{skip_note}); "
                        "handing off to Antigravity Reviewer."
                    ),
                    details={
                        "commands_passed": report["commands_passed"],
                        "commands_total": report["commands_total"],
                        "skipped_tests_total": skipped_total,
                    },
                )
                return {"ok": True, "next_action": "review_required", "task_id": tid, "report": report, "state": state}

            # Verification failed -> determine repair vs retry limit blocked
            repair_round = int(state.get("repair_round", 0))
            max_repairs = int(state.get("max_repair_rounds", 3))
            err_summary = "; ".join(report["failure_reasons"])[:800] or "Independent verification failed."

            if repair_round < max_repairs:
                next_round = repair_round + 1
                state["repair_round"] = next_round
                fb_text = _format_verification_feedback(report, next_round)
                atomic_write_text(tdir / "FEEDBACK.md", fb_text)
                self.store.transition(
                    tid,
                    state,
                    STATE_REVISING,
                    event_type="verification_failed",
                    message=f"Verification failed ({err_summary}). Prepared repair round {next_round}/{max_repairs}.",
                    failure_category="test_failure",
                    last_error=err_summary,
                    details={"repair_round": next_round, "max_repair_rounds": max_repairs},
                )
                return {
                    "ok": False,
                    "next_action": "repair_available",
                    "task_id": tid,
                    "report": report,
                    "state": state,
                }

            # Retry cap reached -> BLOCKED
            self.store.transition(
                tid,
                state,
                STATE_BLOCKED,
                event_type="retry_limit_exceeded",
                message=f"Verification failed and max repair rounds ({max_repairs}) reached: {err_summary}",
                failure_category="retry_limit_exceeded",
                last_error=err_summary,
                details={"repair_round": repair_round, "max_repair_rounds": max_repairs},
            )
            return {
                "ok": False,
                "next_action": "blocked_retry_limit",
                "task_id": tid,
                "report": report,
                "state": state,
            }

    def run_round(
        self,
        task_id: str | None,
        *,
        override_model: str | None = None,
        override_effort: str | None = None,
        codex_timeout_sec: int | None = None,
        codex_idle_timeout_sec: int | None = None,
        verify_commands: list[str] | None = None,
        verify_env: list[str] | dict[str, str] | None = None,
        prepare_commands: list[str] | None = None,
        skip_prepare: bool = False,
        verify_timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        """Run one deterministic Execute -> Prepare -> Verify cycle."""
        parsed_verify_env = parse_env_assignments(verify_env)
        exec_res = self.execute_round(
            task_id,
            override_model=override_model,
            override_effort=override_effort,
            timeout_sec=codex_timeout_sec,
            idle_timeout_sec=codex_idle_timeout_sec,
        )
        if not exec_res["ok"]:
            return {
                "ok": False,
                "stage": "execute",
                "next_action": "blocked" if exec_res["state"]["status"] == STATE_BLOCKED else "failed",
                "task_id": exec_res["task_id"],
                "execution": exec_res,
                "verification": None,
                "state": exec_res["state"],
            }

        ver_res = self.verify_task(
            exec_res["task_id"],
            verify_commands=verify_commands,
            verify_env=parsed_verify_env or None,
            prepare_commands=prepare_commands,
            skip_prepare=skip_prepare,
            timeout_sec=verify_timeout_sec,
        )
        return {
            "ok": ver_res["ok"],
            "stage": "verify",
            "next_action": ver_res["next_action"],
            "task_id": exec_res["task_id"],
            "execution": exec_res,
            "verification": ver_res,
            "state": ver_res["state"],
        }

    def record_review(
        self,
        task_id: str | None,
        *,
        verdict: str,
        summary: str,
        feedback: str = "",
        issues: list[str] | None = None,
        reviewer_model: str | None = None,
    ) -> dict[str, Any]:
        """Record Antigravity's semantic Code Review verdict and drive Completed/Revising/Blocked."""
        tid = self.resolve_task_id(task_id)
        tdir = self.store.task_dir(tid)
        verdict_norm = verdict.strip().lower()
        if verdict_norm not in {"approve", "request_changes", "block"}:
            raise ValueError("verdict must be one of: approve, request_changes, block")

        with self.store.acquire_write_lock(tid):
            state = self.store.load_state(tid)
            if state["status"] != STATE_REVIEWING:
                raise RuntimeError(
                    f"Cannot record review while task is in state '{state['status']}' (expected '{STATE_REVIEWING}')."
                )

            policy = state.get("model_policy") or {}
            eff_reviewer_model = (
                reviewer_model
                if reviewer_model and policy.get("reviewer_routing_supported")
                else "Antigravity (session model)"
            )
            repair_round = int(state.get("repair_round", 0))
            max_repairs = int(state.get("max_repair_rounds", 3))

            review_entry = {
                "round": repair_round,
                "verdict": verdict_norm,
                "summary": summary.strip(),
                "issues": [i.strip() for i in (issues or []) if i.strip()],
                "feedback": feedback.strip(),
                "reviewer_model": eff_reviewer_model,
                "timestamp": utc_now(),
            }

            reviews = read_json(tdir / "reviews.json", default=[])
            if not isinstance(reviews, list):
                reviews = []
            reviews.append(review_entry)
            atomic_write_json(tdir / "reviews.json", reviews)

            state["latest_review"] = review_entry
            state.setdefault("model_history", []).append(
                make_model_history_entry(
                    round_num=repair_round,
                    role="reviewer",
                    model_name=eff_reviewer_model,
                    mode=policy.get("reviewer_mode", "inherit_session"),
                    reason=f"Semantic code review verdict: {verdict_norm}",
                    token_usage=None,
                )
            )

            self.store.append_execution_log(
                tid,
                f"Antigravity Code Review — {verdict_norm.upper()} (round {repair_round})",
                f"- Reviewer: `{eff_reviewer_model}`\n"
                f"- Summary: {review_entry['summary']}\n"
                + (
                    "- Issues:\n" + "\n".join(f"  - {item}" for item in review_entry["issues"]) + "\n"
                    if review_entry["issues"]
                    else ""
                ),
            )

            if verdict_norm == "approve":
                self.store.transition(
                    tid,
                    state,
                    STATE_COMPLETED,
                    event_type="review_approved",
                    message=f"Code Review approved: {review_entry['summary']}",
                    details={"verdict": "approve", "round": repair_round},
                )
                return {"ok": True, "next_action": "ready_to_deliver", "task_id": tid, "review": review_entry, "state": state}

            if verdict_norm == "block":
                self.store.transition(
                    tid,
                    state,
                    STATE_BLOCKED,
                    event_type="review_blocked",
                    message=f"Code Review blocked task: {review_entry['summary']}",
                    failure_category="review_failure",
                    last_error=review_entry["summary"],
                    details={"verdict": "block", "round": repair_round},
                )
                return {"ok": False, "next_action": "blocked", "task_id": tid, "review": review_entry, "state": state}

            # request_changes
            fb_parts = [f"# Antigravity Code Review Feedback (Round {repair_round})", "", review_entry["summary"], ""]
            if review_entry["issues"]:
                fb_parts.append("## Required Fixes")
                fb_parts.extend(f"- {item}" for item in review_entry["issues"])
                fb_parts.append("")
            if review_entry["feedback"]:
                fb_parts.append("## Detailed Notes")
                fb_parts.append(review_entry["feedback"])
                fb_parts.append("")
            atomic_write_text(tdir / "FEEDBACK.md", "\n".join(fb_parts))

            if repair_round < max_repairs:
                next_round = repair_round + 1
                state["repair_round"] = next_round
                self.store.transition(
                    tid,
                    state,
                    STATE_REVISING,
                    event_type="review_changes_requested",
                    message=f"Review requested changes ({review_entry['summary']}). Starting repair round {next_round}/{max_repairs}.",
                    failure_category="review_failure",
                    last_error=review_entry["summary"],
                    details={"repair_round": next_round, "max_repair_rounds": max_repairs},
                )
                return {"ok": False, "next_action": "repair_available", "task_id": tid, "review": review_entry, "state": state}

            self.store.transition(
                tid,
                state,
                STATE_BLOCKED,
                event_type="retry_limit_exceeded",
                message=f"Review requested changes, but max repair rounds ({max_repairs}) reached: {review_entry['summary']}",
                failure_category="retry_limit_exceeded",
                last_error=review_entry["summary"],
                details={"repair_round": repair_round, "max_repair_rounds": max_repairs},
            )
            return {"ok": False, "next_action": "blocked_retry_limit", "task_id": tid, "review": review_entry, "state": state}

    def cancel_task(self, task_id: str | None, reason: str = "Cancelled by user") -> dict[str, Any]:
        """Gracefully cancel an active or paused task without destructive git cleanup."""
        tid = self.resolve_task_id(task_id)
        tdir = self.store.task_dir(tid)
        atomic_write_text(tdir / ".cancel", f"{utc_now()} {reason}\n")

        run_pid_info = self.store.read_run_pid(tid)
        terminated_pid: int | None = None
        if run_pid_info:
            pid = int(run_pid_info.get("pid") or 0)
            pgid = int(run_pid_info.get("pgid") or pid)
            if _pid_alive(pid):
                terminated_pid = pid
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and _pid_alive(pid):
                    time.sleep(0.1)
                if _pid_alive(pid):
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass

        # Wait briefly for writer to release lock if it was handling cancellation
        for _ in range(15):
            try:
                with self.store.acquire_write_lock(tid):
                    state = self.store.load_state(tid)
                    if state["status"] != STATE_BLOCKED or state.get("failure_category") != "cancelled":
                        if state["status"] != STATE_COMPLETED:
                            self.store.transition(
                                tid,
                                state,
                                STATE_BLOCKED,
                                event_type="task_cancelled",
                                message=reason,
                                failure_category="cancelled",
                                last_error=reason,
                            )
                    self.store.clear_run_pid(tid)
                    return {"cancelled": True, "task_id": tid, "terminated_pid": terminated_pid, "state": state}
            except Exception:
                time.sleep(0.2)

        return {"cancelled": True, "task_id": tid, "terminated_pid": terminated_pid, "state": self.store.load_state(tid)}

    def recover_task(self, task_id: str | None) -> dict[str, Any]:
        """Recover a task interrupted by an unexpected process crash."""
        tid = self.resolve_task_id(task_id)
        with self.store.acquire_write_lock(tid):
            state = self.store.load_state(tid)
            run_pid_info = self.store.read_run_pid(tid)
            if state["status"] in {STATE_EXECUTING, STATE_VERIFYING}:
                alive = bool(run_pid_info and _pid_alive(int(run_pid_info.get("pid") or 0)))
                if not alive:
                    self.store.clear_run_pid(tid)
                    self.store.transition(
                        tid,
                        state,
                        STATE_FAILED,
                        event_type="task_recovered",
                        message="Recovered interrupted task after unexpected worker termination.",
                        failure_category="execution_failure",
                        last_error="Worker process terminated unexpectedly before completing phase.",
                    )
                    return {"recovered": True, "task_id": tid, "state": state}
            return {"recovered": False, "task_id": tid, "state": state}

    def deliver_task(
        self,
        task_id: str | None,
        *,
        confirm_commit: bool = False,
        commit_message: str | None = None,
    ) -> dict[str, Any]:
        """Inspect delivery readiness and optionally commit inside the worktree ONLY when explicitly confirmed."""
        tid = self.resolve_task_id(task_id)
        meta = self.store.load_task_meta(tid)
        state = self.store.load_state(tid)
        wt = Path(meta["worktree_path"])
        diff_info = inspect_worktree_changes(wt, meta["base_commit"], include_diff=False)

        if not confirm_commit:
            return {
                "task_id": tid,
                "status": state["status"],
                "committed": False,
                "requires_confirmation": True,
                "repo_path": meta["repo_path"],
                "worktree_path": meta["worktree_path"],
                "branch": meta["branch"],
                "base_branch": meta["base_branch"],
                "diff_stats": {
                    "files_changed_count": diff_info["files_changed_count"],
                    "total_insertions": diff_info["total_insertions"],
                    "total_deletions": diff_info["total_deletions"],
                    "files": diff_info["files"],
                },
                "note": "No commit, push, or merge was performed. Pass --confirm-commit after explicit user confirmation.",
            }

        if state["status"] != STATE_COMPLETED:
            raise RuntimeError(
                f"Refusing to commit task in state '{state['status']}'. Complete verification and review first."
            )

        with self.store.acquire_write_lock(tid):
            msg = (commit_message or f"codex-dev: {meta['name']}").strip()
            commit_res = commit_worktree_changes(wt, msg)
            state["delivery"] = {
                "committed": commit_res["committed"],
                "commit": commit_res.get("commit"),
                "branch": meta["branch"],
                "commit_message": msg,
                "committed_at": utc_now(),
            }
            self.store.save_state(tid, state)
            self.store.append_event(
                tid,
                phase="deliver",
                agent="Antigravity (Delivered)",
                event_type="task_committed",
                message=f"Committed worktree changes on branch {meta['branch']} ({commit_res.get('commit', '')[:12]}) after human confirmation.",
                details=commit_res,
            )
            self.store.append_execution_log(
                tid,
                "Delivered (Human-Confirmed Commit)",
                f"- Branch: `{meta['branch']}`\n- Commit: `{commit_res.get('commit')}`\n- Message: `{msg}`\n",
            )
            return {"task_id": tid, **commit_res, "worktree_path": meta["worktree_path"], "state": state}

    def get_status(self, task_id: str | None = None, *, events_limit: int = 50) -> dict[str, Any]:
        """Read-only full status snapshot for CLI and Monitor inspection."""
        tid = self.resolve_task_id(task_id)
        tdir = self.store.task_dir(tid)
        meta = self.store.load_task_meta(tid)
        state = self.store.load_state(tid)
        events = self.store.read_events(tid, limit=events_limit)
        verification = read_json(tdir / "verification.json")
        result = read_json(tdir / "RESULT.json")
        reviews = read_json(tdir / "reviews.json", default=[])
        return {
            "task": meta,
            "state": state,
            "events": events,
            "verification": verification,
            "result": result,
            "reviews": reviews,
        }

    def get_diff(self, task_id: str | None = None, *, include_diff: bool = True) -> dict[str, Any]:
        """Read-only diff inspection inside task worktree (summary-only when include_diff=False)."""
        tid = self.resolve_task_id(task_id)
        meta = self.store.load_task_meta(tid)
        return inspect_worktree_changes(
            Path(meta["worktree_path"]),
            meta["base_commit"],
            include_diff=include_diff,
        )

    def ensure_config(self, overwrite: bool = False) -> Path:
        return ensure_default_global_config(home_dir=self.home_dir, overwrite=overwrite)
