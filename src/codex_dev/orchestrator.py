"""Deterministic Codex Dev workflow orchestrator."""

from __future__ import annotations

import hashlib
import os
import signal
import time
from pathlib import Path
from typing import Any

from .codex_runner import (
    CodexRunOutcome,
    build_codex_prompt,
    classify_environment_exception,
    resolve_builder_writable_dirs,
    run_codex_exec,
)
from .config import (
    ENFORCED_SANDBOX_MODE,
    ensure_default_global_config,
    get_codex_dev_home,
    load_config,
    read_local_codex_hints,
    resolve_codex_timeouts,
    validate_sandbox_mode,
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
    promote_policy_for_plan_scope,
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
    base = dict(
        current
        or {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "available": True,
            "source": delta.get("source") or "codex_jsonl",
            "rounds_with_usage": 0,
            "rounds_missing_usage": 0,
            "partial": False,
            "estimated_missing_tokens": 0,
        }
    )
    base.update(
        {
            "input_tokens": int(base.get("input_tokens", 0)) + int(delta.get("input_tokens", 0)),
            "cached_input_tokens": int(base.get("cached_input_tokens", 0)) + int(delta.get("cached_input_tokens", 0)),
            "output_tokens": int(base.get("output_tokens", 0)) + int(delta.get("output_tokens", 0)),
            "total_tokens": int(base.get("total_tokens", 0)) + int(delta.get("total_tokens", 0)),
            "available": True,
            "source": delta.get("source") or base.get("source") or "codex_jsonl",
        }
    )
    return base


def _record_attempt_token_accounting(
    current: dict[str, Any] | None,
    *,
    exact_usage: dict[str, Any] | None,
    estimated_usage: dict[str, Any] | None,
) -> dict[str, Any]:
    """Finalize per-attempt token completeness metadata without polluting exact totals."""
    tok = dict(
        current
        or {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "available": False,
            "source": None,
        }
    )
    tok.setdefault("rounds_with_usage", 0)
    tok.setdefault("rounds_missing_usage", 0)
    tok.setdefault("partial", False)
    tok.setdefault("estimated_missing_tokens", 0)
    if exact_usage is not None:
        tok["rounds_with_usage"] = int(tok["rounds_with_usage"]) + 1
    else:
        tok["rounds_missing_usage"] = int(tok["rounds_missing_usage"]) + 1
        tok["partial"] = True
        if isinstance(estimated_usage, dict):
            tok["estimated_missing_tokens"] = int(tok["estimated_missing_tokens"]) + int(
                estimated_usage.get("total_tokens", 0)
            )
    return tok


def _hash_worktree_files(worktree_path: Path, rel_paths: list[str]) -> dict[str, str]:
    """Capture SHA-256 digests of worktree files touched during pre-Builder preparation."""
    digests: dict[str, str] = {}
    for rel in rel_paths:
        fpath = worktree_path / rel
        if fpath.is_file():
            try:
                digests[rel] = hashlib.sha256(fpath.read_bytes()).hexdigest()
            except OSError:
                digests[rel] = "unreadable"
        else:
            digests[rel] = "missing"
    return digests


def _count_builder_delta_files(
    worktree_path: Path,
    diff_stats: dict[str, Any] | None,
    pre_builder_hashes: dict[str, str] | None,
) -> int:
    """Count worktree file changes attributable to Builder (excluding unchanged pre-prepare artifacts)."""
    raw_count = int((diff_stats or {}).get("files_changed_count", 0))
    if not pre_builder_hashes:
        return raw_count

    current_files = [
        str(item["path"])
        for item in ((diff_stats or {}).get("files") or [])
        if isinstance(item, dict) and item.get("path")
    ]
    current_set = set(current_files)
    delta = 0
    for rel in current_files:
        if rel not in pre_builder_hashes:
            delta += 1
            continue
        cur_hash = _hash_worktree_files(worktree_path, [rel]).get(rel)
        if cur_hash != pre_builder_hashes[rel]:
            delta += 1
    for pre_rel in pre_builder_hashes:
        if pre_rel not in current_set:
            delta += 1
    return delta


def _check_post_execution_invariants(
    structured_result: dict[str, Any] | None,
    diff_stats: dict[str, Any] | None,
    *,
    allow_empty_diff: bool = False,
    effective_files_changed: int | None = None,
) -> tuple[str | None, str | None]:
    """Enforce deterministic post-execution invariants before entering host verification.

    Prevents incomplete (`status == 'partial'`) or zero-change (`files_changed_count == 0`)
    Builder exits from silently passing host verification against an unmodified baseline.
    """
    files_changed = (
        int(effective_files_changed)
        if effective_files_changed is not None
        else int((diff_stats or {}).get("files_changed_count", 0))
    )
    status = str((structured_result or {}).get("status") or "").strip().lower()
    summary = str((structured_result or {}).get("summary") or "").strip()

    if status == "partial":
        detail = f" ({summary[:240]})" if summary else ""
        return (
            "builder_partial",
            f"Builder exited with status='partial' ({files_changed} file(s) changed){detail}; "
            "complete implementation is required before verification.",
        )

    if not allow_empty_diff and files_changed == 0:
        detail = f" (Builder summary: {summary[:240]})" if summary else ""
        return (
            "empty_diff",
            f"Builder exited with zero worktree file changes relative to prepared baseline{detail}; "
            "refusing to run verification against an unchanged baseline.",
        )

    return (None, None)


def _format_env_retry_feedback(
    env_exc: dict[str, Any],
    *,
    changed_files: list[str],
    writable_dirs: list[str],
    network_access: bool,
) -> str:
    """Format concise, factual context for the single non-escalating environment retry."""
    lines = [
        f"- Previous attempt failed due to environment/sandbox constraint (`{env_exc.get('kind')}`): {env_exc.get('detail')}",
        f"- Sandbox permissions remain locked (`{ENFORCED_SANDBOX_MODE}`, network_access={'enabled' if network_access else 'disabled (offline)'}).",
        "- Writable directories: " + ", ".join(f"`{d}`" for d in writable_dirs),
    ]
    failed_cmds = env_exc.get("failed_commands") or []
    if failed_cmds:
        lines.append("- Failed command(s) from previous attempt:")
        for item in failed_cmds[:5]:
            lines.append(
                f"  - `{item.get('command')}` (exit={item.get('exit_code')}): {str(item.get('summary') or '')[:200]}"
            )
    if changed_files:
        lines.append(
            "- Existing worktree edits were preserved: "
            + ", ".join(f"`{p}`" for p in changed_files[:20])
        )
    lines.extend(
        [
            "- Keep valid existing edits in the worktree.",
            "- Do NOT repeat unpermitted operations outside your writable directories or network boundary.",
            "- If the PLAN and specified Builder Checks can be completed cleanly within these boundaries, finish and return `status: \"ready_for_verify\"` or `status: \"completed\"` with `error_type: \"none\"`.",
            "- If a required dependency or specified Builder Check genuinely cannot execute inside this sandbox, stop immediately and return `status: \"blocked\"` with the exact `error_type` and `error_detail`.",
        ]
    )
    return "\n".join(lines)


def _format_execution_invariant_feedback(
    failure_category: str,
    reason: str,
    *,
    structured_result: dict[str, Any] | None,
    target_files: list[str] | None,
    repair_round: int,
) -> str:
    lines = [
        f"# Execution Invariant Feedback (Before Repair Round {repair_round})",
        "",
        f"The previous Builder round was rejected before verification (`{failure_category}`): {reason}",
        "",
        "## Required Action",
        "- Implement the complete immutable PLAN.md contract inside the isolated worktree in this round.",
        "- Do NOT stop early, do NOT return `status: \"partial\"`, and do NOT exit without editing the required files.",
        "- Run the sandbox-safe Builder Checks after completing your edits, then return `status: \"ready_for_verify\"` or `status: \"completed\"`.",
        "",
    ]
    unmet = [
        item
        for item in ((structured_result or {}).get("acceptance_criteria_status") or [])
        if isinstance(item, dict) and item.get("met") is False
    ]
    if unmet:
        lines.append("## Unmet Acceptance Criteria From Previous Attempt")
        for item in unmet[:15]:
            crit = str(item.get("criterion") or "").strip()
            ev = str(item.get("evidence") or "").strip()
            if crit:
                lines.append(f"- {crit}" + (f" — {ev}" if ev else ""))
        lines.append("")
    if target_files:
        lines.append("## Declared Target Files")
        lines.extend(f"- `{tf}`" for tf in target_files[:30])
        lines.append("")
    return "\n".join(lines).strip() + "\n"


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
            log_refs: list[str] = []
            if chk.get("stdout_log_path"):
                log_refs.append(f"stdout=`{chk['stdout_log_path']}`")
            if chk.get("stderr_log_path"):
                log_refs.append(f"stderr=`{chk['stderr_log_path']}`")
            if log_refs:
                lines.append(f"- Full logs: {', '.join(log_refs)}")
            stdout_body = str(chk.get("stdout_excerpt") or (chk.get("stdout_tail") or "")[-1500:]).strip()
            stderr_body = str(chk.get("stderr_excerpt") or (chk.get("stderr_tail") or "")[-1500:]).strip()
            if stdout_body:
                lines.extend(["```text", stdout_body, "```"])
            if stderr_body:
                lines.extend(["```text", stderr_body, "```"])
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
        max_env_retries = min(1, max(0, int(cfg.get("max_env_retries", 1))))
        builder_cfg = cfg.get("builder") if isinstance(cfg.get("builder"), dict) else {}
        _, _, init_writable_dirs = resolve_builder_writable_dirs(
            tdir,
            Path(wt_info["worktree_path"]),
            cfg.get("builder_add_dirs") if isinstance(cfg.get("builder_add_dirs"), list) else None,
        )
        initial_boundary = {
            "sandbox_mode": validate_sandbox_mode(cfg.get("sandbox_mode")),
            "network_access": bool(builder_cfg.get("network_access", False)),
            "writable_dirs": init_writable_dirs,
            "builder_isolation": str(cfg.get("builder_isolation") or "strict"),
        }

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
            "env_prepared": False,
            "env_retry_count": 0,
            "max_env_retries": max_env_retries,
            "environment_error": None,
            "sandbox_boundary": initial_boundary,
            "locked_sandbox_boundary": None,
            "pre_builder_file_hashes": {},
            "pre_prepare_allowed_files": [],
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
                "rounds_with_usage": 0,
                "rounds_missing_usage": 0,
                "partial": False,
                "estimated_missing_tokens": 0,
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
            state["environment_requirements"] = contract.get("environment_requirements") or []
            state["env_prepared"] = False

            cfg = load_config(repo_path=Path(meta["repo_path"]), home_dir=self.home_dir)
            hints = read_local_codex_hints()
            promotion = promote_policy_for_plan_scope(
                state["model_policy"],
                cfg,
                contract,
                cached_models=hints.get("cached_models") or None,
            )

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
                    "environment_requirements": contract.get("environment_requirements") or [],
                },
            )
            if promotion.get("promoted"):
                self.store.append_event(
                    tid,
                    phase="plan",
                    agent="Orchestrator",
                    event_type="plan_scope_promoted",
                    message=str(promotion.get("reason") or "Promoted task model policy based on PLAN.md scope."),
                    details=promotion,
                )
            env_reqs = contract.get("environment_requirements") or []
            self.store.append_execution_log(
                tid,
                "Plan Contract Locked",
                f"- SHA-256: `{contract['plan_sha256']}`\n"
                f"- Acceptance criteria: {len(contract['acceptance_criteria'])}\n"
                f"- Target files: {', '.join(contract['target_files']) or '(unspecified)'}\n"
                + (f"- Environment requirements: {'; '.join(env_reqs)}\n" if env_reqs else "")
                + (
                    f"- Scope auto-promotion: {promotion['reason']}\n"
                    if promotion.get("promoted")
                    else ""
                ),
            )
            res: dict[str, Any] = {"task_id": tid, "contract": contract, "state": state}
            if promotion.get("promoted"):
                res["plan_scope_promotion"] = promotion
            return res

    def _compute_config_sandbox_boundary(
        self,
        tdir: Path,
        worktree_path: Path,
        cfg: dict[str, Any],
    ) -> dict[str, Any]:
        sandbox_mode = validate_sandbox_mode(cfg.get("sandbox_mode"))
        builder_cfg = cfg.get("builder") if isinstance(cfg.get("builder"), dict) else {}
        extra_add_dirs = [
            str(Path(d).expanduser().resolve())
            for d in (cfg.get("builder_add_dirs") if isinstance(cfg.get("builder_add_dirs"), list) else [])
            if isinstance(d, str) and d.strip()
        ]
        eff_isolation = str(cfg.get("builder_isolation") or "strict")
        if eff_isolation not in ("strict", "inherit"):
            eff_isolation = "strict"
        _, _, writable_dirs = resolve_builder_writable_dirs(tdir, worktree_path, extra_add_dirs)
        return {
            "sandbox_mode": sandbox_mode,
            "network_access": bool(builder_cfg.get("network_access", False)),
            "extra_add_dirs": extra_add_dirs,
            "writable_dirs": writable_dirs,
            "builder_isolation": eff_isolation,
        }

    def _run_pre_prepare_locked(
        self,
        tid: str,
        tdir: Path,
        meta: dict[str, Any],
        state: dict[str, Any],
        cfg: dict[str, Any],
        *,
        prepare_commands: list[str] | None = None,
        explicit_env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        refresh_boundary_on_explicit_prepare: bool = False,
    ) -> dict[str, Any]:
        """Run pre-Builder environment & dependency preparation while holding the task write lock."""
        worktree_path = Path(meta["worktree_path"])
        effective_prep = (
            prepare_commands
            if prepare_commands is not None
            else (meta.get("explicit_prepare_commands") or None)
        )
        effective_env = {**dict(meta.get("explicit_verify_env") or {}), **dict(explicit_env or {})}
        codex_bin = str(cfg.get("codex_binary") or "codex")

        prep_report = run_dependency_prepare(
            task_dir=tdir,
            worktree_path=worktree_path,
            base_commit=meta["base_commit"],
            config=cfg,
            explicit_commands=effective_prep,
            explicit_env=effective_env or None,
            timeout_sec=timeout_sec,
            phase="pre",
            check_codex_binary=codex_bin,
        )

        if prep_report["ran"]:
            self.store.append_event(
                tid,
                phase="plan" if state.get("repair_round", 0) == 0 else "repair",
                agent="Orchestrator (Environment Prepare)",
                event_type="prepare_completed" if prep_report["passed"] else "prepare_failed",
                message=(
                    f"Pre-Builder environment preparation {'passed' if prep_report['passed'] else 'failed'} "
                    f"({prep_report['commands_passed']}/{prep_report['commands_total']} checks, source={prep_report['command_source']})."
                ),
                details={
                    "phase": "pre",
                    "commands_passed": prep_report["commands_passed"],
                    "commands_total": prep_report["commands_total"],
                    "source": prep_report["command_source"],
                },
            )
            self.store.append_execution_log(
                tid,
                f"Pre-Builder Environment Preparation ({'PASSED' if prep_report['passed'] else 'FAILED'})",
                f"- Source: `{prep_report['command_source']}` | Passed: `{prep_report['commands_passed']}/{prep_report['commands_total']}`\n"
                + (
                    "- Failures:\n" + "\n".join(f"  - {r}" for r in prep_report["failure_reasons"]) + "\n"
                    if prep_report["failure_reasons"]
                    else ""
                ),
            )

        if not prep_report["passed"]:
            state["env_prepared"] = False
            err_msg = "; ".join(prep_report["failure_reasons"])[:800] or "Pre-Builder environment preparation failed."
            self.store.transition(
                tid,
                state,
                STATE_BLOCKED,
                event_type="dependency_prepare_blocked",
                message=f"Environment preparation failed before starting Builder (repair round not consumed): {err_msg}",
                failure_category="dependency_prepare_failure",
                last_error=err_msg,
                details={"stage": "pre_execute", "prepare": prep_report},
            )
            return {
                "ok": False,
                "next_action": "blocked_dependency_prepare",
                "prepare": prep_report,
            }

        post_prep_files = list(prep_report.get("post_prepare_changed_files") or [])
        if int(state.get("execution_attempts", 0)) == 0:
            state["pre_builder_file_hashes"] = _hash_worktree_files(worktree_path, post_prep_files)
        merged_allowed = sorted(
            set(state.get("pre_prepare_allowed_files") or [])
            | set(prep_report.get("generated_allowed_files") or [])
        )
        state["pre_prepare_allowed_files"] = merged_allowed
        state["env_prepared"] = True

        if refresh_boundary_on_explicit_prepare and state.get("locked_sandbox_boundary") is not None:
            # Explicit upper-layer prepare_task refreshes configured boundary (still strictly workspace-write)
            refreshed = self._compute_config_sandbox_boundary(tdir, worktree_path, cfg)
            state["locked_sandbox_boundary"] = refreshed
            state["sandbox_boundary"] = {k: v for k, v in refreshed.items() if k != "extra_add_dirs"}

        if state["status"] == STATE_BLOCKED and state.get("failure_category") in {
            "dependency_prepare_failure",
            "environment_failure",
        }:
            state["env_retry_count"] = 0
            state["environment_error"] = None
            resume_state = STATE_REVISING if int(state.get("repair_round", 0)) > 0 else STATE_PLANNED
            self.store.transition(
                tid,
                state,
                resume_state,
                event_type="environment_unblocked",
                message="Environment preparation succeeded; task unblocked and ready for Builder execution.",
                details={"resumed_to": resume_state},
            )
        else:
            self.store.save_state(tid, state)

        return {"ok": True, "prepare": prep_report}

    def execute_round(
        self,
        task_id: str | None,
        *,
        override_model: str | None = None,
        override_effort: str | None = None,
        timeout_sec: int | None = None,
        idle_timeout_sec: int | None = None,
        escalate_model: bool = False,
        prepare_commands: list[str] | None = None,
        prepare_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run pre-Builder readiness gate, then execute Codex CLI Builder inside the isolated worktree."""
        tid = self.resolve_task_id(task_id)
        tdir = self.store.task_dir(tid)

        with self.store.acquire_write_lock(tid):
            meta = self.store.load_task_meta(tid)
            state = self.store.load_state(tid)
            cfg = load_config(repo_path=Path(meta["repo_path"]), home_dir=self.home_dir)
            hints = read_local_codex_hints()
            worktree_path = Path(meta["worktree_path"])

            # 1. Verify immutable plan contract before anything else
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

            # 2. Enforce pre-Builder environment & dependency readiness gate (NEVER start Builder if unprepared)
            if not state.get("env_prepared", False) or prepare_commands is not None:
                prep_res = self._run_pre_prepare_locked(
                    tid,
                    tdir,
                    meta,
                    state,
                    cfg,
                    prepare_commands=prepare_commands,
                    explicit_env=prepare_env,
                )
                if not prep_res["ok"]:
                    return {
                        "ok": False,
                        "stage": "prepare",
                        "next_action": "blocked_dependency_prepare",
                        "task_id": tid,
                        "prepare": prep_res["prepare"],
                        "state": state,
                    }

            # 3. Lock immutable sandbox boundary on initial execution; never expand permissions on retries
            configured_boundary = self._compute_config_sandbox_boundary(tdir, worktree_path, cfg)
            locked_raw = state.get("locked_sandbox_boundary")
            if not isinstance(locked_raw, dict):
                locked_raw = configured_boundary
                state["locked_sandbox_boundary"] = locked_raw
            sandbox_mode = validate_sandbox_mode(locked_raw.get("sandbox_mode"))
            locked_network_access = bool(locked_raw.get("network_access", False))
            locked_extra_add_dirs = list(locked_raw.get("extra_add_dirs") or [])
            locked_isolation = str(locked_raw.get("builder_isolation") or "strict")
            _, _, locked_writable_dirs = resolve_builder_writable_dirs(tdir, worktree_path, locked_extra_add_dirs)
            public_boundary = {
                "sandbox_mode": sandbox_mode,
                "network_access": locked_network_access,
                "writable_dirs": locked_writable_dirs,
                "builder_isolation": locked_isolation,
            }
            state["sandbox_boundary"] = public_boundary

            repair_round = int(state.get("repair_round", 0))
            prev_failure_category = state.get("failure_category")
            non_escalating_prev = prev_failure_category in {"environment_failure", "dependency_prepare_failure"}
            should_escalate = escalate_model or (
                repair_round > 0
                and state["model_policy"].get("mode") == "auto"
                and not non_escalating_prev
            )

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

            next_attempt_preview = int(state.get("execution_attempts", 0)) + 1
            self.store.transition(
                tid,
                state,
                STATE_EXECUTING,
                event_type="builder_started",
                message=(
                    f"Handing off to Codex CLI Builder (round {repair_round}, attempt {next_attempt_preview}, "
                    f"model={selection.model}, effort={selection.reasoning_effort or 'default'}): {selection.reason}"
                ),
                details={
                    "round": repair_round,
                    "attempt": next_attempt_preview,
                    "model": selection.model,
                    "reasoning_effort": selection.reasoning_effort,
                    "mode": selection.mode,
                    "locked": selection.locked,
                    "effort_locked": selection.effort_locked,
                    "escalated_from": selection.escalated_from,
                    "effort_escalated_from": selection.effort_escalated_from,
                    "sandbox_boundary": public_boundary,
                },
            )

            plan_text = (tdir / "PLAN.md").read_text(encoding="utf-8")
            contract_doc = read_json(tdir / "contract.json") or {}
            builder_checks = list(contract_doc.get("builder_checks") or [])
            builder_cfg = cfg.get("builder") if isinstance(cfg.get("builder"), dict) else {}
            feedback_path = tdir / "FEEDBACK.md"
            feedback_text = feedback_path.read_text(encoding="utf-8") if feedback_path.is_file() else None

            eff_timeout, eff_idle_timeout = resolve_codex_timeouts(
                cfg,
                explicit_timeout_sec=timeout_sec,
                explicit_idle_timeout_sec=idle_timeout_sec,
                complexity=state["model_policy"].get("complexity"),
                reasoning_effort=selection.reasoning_effort,
            )
            codex_bin = str(cfg.get("codex_binary") or "codex")

            def _live_usage_cb(usage: dict[str, Any]) -> None:
                state["token_usage"] = _merge_token_totals(state.get("token_usage"), usage)
                self.store.save_state(tid, state)

            def _run_single_builder_attempt(
                *,
                env_retry_feedback: str | None = None,
                is_env_retry: bool = False,
            ) -> CodexRunOutcome:
                attempt_num = int(state.get("execution_attempts", 0)) + 1
                state["execution_attempts"] = attempt_num
                self.store.save_state(tid, state)

                prompt = build_codex_prompt(
                    task_name=meta["name"],
                    plan_text=plan_text,
                    feedback_text=feedback_text,
                    repair_round=repair_round,
                    worktree_path=worktree_path,
                    writable_dirs=locked_writable_dirs,
                    builder_checks=builder_checks or None,
                    builder_network_access=locked_network_access,
                    env_retry_feedback=env_retry_feedback,
                )

                att_outcome = run_codex_exec(
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
                    active_grace_sec=int(cfg.get("codex_active_grace_sec", 300)),
                    active_grace_window_sec=int(cfg.get("codex_active_grace_window_sec", 60)),
                    max_active_extensions=int(cfg.get("codex_max_active_extensions", 2)),
                    builder_env=cfg.get("builder_env") if isinstance(cfg.get("builder_env"), dict) else None,
                    builder_add_dirs=locked_extra_add_dirs or None,
                    builder_isolation=locked_isolation,
                    builder_network_access=locked_network_access,
                    on_token_usage=_live_usage_cb,
                )

                state["token_usage"] = _record_attempt_token_accounting(
                    state.get("token_usage"),
                    exact_usage=att_outcome.token_usage,
                    estimated_usage=att_outcome.estimated_token_usage,
                )

                if is_env_retry:
                    history_entry = make_model_history_entry(
                        round_num=repair_round,
                        role="builder",
                        model_name=selection.model,
                        mode=selection.mode,
                        reason=(
                            f"Environment retry {state.get('env_retry_count', 1)}/{state.get('max_env_retries', 1)} "
                            f"retaining model '{selection.model}' and effort '{selection.reasoning_effort}' (no escalation, permissions unchanged)."
                        ),
                        token_usage=att_outcome.token_usage,
                    )
                    history_entry["locked"] = selection.locked
                    history_entry["reasoning_effort"] = selection.reasoning_effort
                    history_entry["env_retry"] = True
                else:
                    history_entry = make_model_history_entry(
                        round_num=repair_round,
                        role="builder",
                        selection=selection,
                        token_usage=att_outcome.token_usage,
                    )
                    history_entry["env_retry"] = False
                history_entry["attempt"] = attempt_num
                if att_outcome.token_usage is None and att_outcome.estimated_token_usage is not None:
                    history_entry["estimated_token_usage"] = att_outcome.estimated_token_usage
                state.setdefault("model_history", []).append(history_entry)

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

                retry_tag = " [Environment Retry]" if is_env_retry else ""
                self.store.append_execution_log(
                    tid,
                    f"Builder Round {repair_round} Attempt {attempt_num}{retry_tag} ({selection.model})",
                    f"- Exit code: `{att_outcome.exit_code}` | Duration: `{att_outcome.duration_sec}s` "
                    f"(timeout={eff_timeout}s, idle_timeout={eff_idle_timeout}s)\n"
                    f"- Sandbox: `{sandbox_mode}` (network_access=`{locked_network_access}`)\n"
                    f"- Timed out: `{att_outcome.timed_out}` ({att_outcome.timeout_reason or 'n/a'}) | Cancelled: `{att_outcome.cancelled}`\n"
                    f"- JSONL log: `{att_outcome.raw_jsonl_path}`\n"
                    f"- Selection rationale: {history_entry['reason']}\n"
                    f"- Token usage: `{att_outcome.token_usage or 'unavailable'}`\n"
                    f"- Summary: {(att_outcome.structured_result or {}).get('summary') or att_outcome.error_message or 'N/A'}\n",
                )
                return att_outcome

            outcome = _run_single_builder_attempt(is_env_retry=False)

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

            # 4. Separate Environment Exception Handling (max 1 automatic retry, no model escalation, no permission expansion, no repair_round consumed)
            env_exc = classify_environment_exception(outcome)
            if env_exc is not None:
                first_env_exc = env_exc
                jsonl_paths = [outcome.raw_jsonl_path]
                env_retry_count = int(state.get("env_retry_count", 0))
                max_env_retries = min(1, max(0, int(state.get("max_env_retries", 1))))

                self.store.append_event(
                    tid,
                    phase="execute",
                    agent="Orchestrator",
                    event_type="environment_exception_detected",
                    message=f"Detected Builder environment exception ({first_env_exc['kind']}): {first_env_exc['detail'][:240]}",
                    details={
                        "kind": first_env_exc["kind"],
                        "source": first_env_exc["source"],
                        "env_retry_count": env_retry_count,
                        "max_env_retries": max_env_retries,
                        "repair_round": repair_round,
                    },
                )

                final_env_exc: dict[str, Any] | None = first_env_exc
                if env_retry_count < max_env_retries:
                    state["env_retry_count"] = env_retry_count + 1
                    changed_paths = [
                        str(f["path"])
                        for f in (state.get("diff_stats") or {}).get("files", [])
                        if isinstance(f, dict) and f.get("path")
                    ]
                    env_fb = _format_env_retry_feedback(
                        first_env_exc,
                        changed_files=changed_paths,
                        writable_dirs=locked_writable_dirs,
                        network_access=locked_network_access,
                    )
                    atomic_write_text(tdir / "ENV_RETRY_CONTEXT.md", env_fb + "\n")
                    self.store.append_event(
                        tid,
                        phase="execute",
                        agent="Orchestrator",
                        event_type="environment_retry_started",
                        message=(
                            f"Starting environment retry {state['env_retry_count']}/{max_env_retries} "
                            f"(model={selection.model}, effort={selection.reasoning_effort}, repair_round={repair_round} unchanged, permissions unchanged)."
                        ),
                        details={
                            "env_retry_count": state["env_retry_count"],
                            "max_env_retries": max_env_retries,
                            "repair_round": repair_round,
                            "model": selection.model,
                            "reasoning_effort": selection.reasoning_effort,
                            "sandbox_boundary": public_boundary,
                            "first_error": first_env_exc,
                        },
                    )

                    outcome = _run_single_builder_attempt(env_retry_feedback=env_fb, is_env_retry=True)
                    jsonl_paths.append(outcome.raw_jsonl_path)

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

                    retry_env_exc = classify_environment_exception(outcome)
                    if retry_env_exc is None and outcome.ok:
                        final_env_exc = None
                        state["environment_error"] = None
                        self.store.append_event(
                            tid,
                            phase="execute",
                            agent="Orchestrator",
                            event_type="environment_retry_succeeded",
                            message=(
                                f"Environment retry {state['env_retry_count']}/{max_env_retries} succeeded "
                                f"without model escalation or permission expansion."
                            ),
                            details={
                                "env_retry_count": state["env_retry_count"],
                                "repair_round": repair_round,
                            },
                        )
                    else:
                        final_env_exc = retry_env_exc or {
                            "kind": first_env_exc["kind"],
                            "detail": (outcome.error_message or first_env_exc["detail"])[:800],
                            "source": "retry_failed",
                            "failed_commands": [],
                        }

                if final_env_exc is not None:
                    changed_paths = [
                        str(f["path"])
                        for f in (state.get("diff_stats") or {}).get("files", [])
                        if isinstance(f, dict) and f.get("path")
                    ]
                    env_report = {
                        "reported_at": utc_now(),
                        "kind": final_env_exc["kind"],
                        "detail": final_env_exc["detail"],
                        "first_error": first_env_exc,
                        "retry_error": final_env_exc if len(jsonl_paths) > 1 else None,
                        "env_retry_count": int(state.get("env_retry_count", 0)),
                        "max_env_retries": max_env_retries,
                        "repair_round": repair_round,
                        "repair_round_consumed": False,
                        "model": selection.model,
                        "reasoning_effort": selection.reasoning_effort,
                        "model_escalated": False,
                        "permission_expanded": False,
                        "sandbox_boundary": public_boundary,
                        "worktree_path": str(worktree_path),
                        "worktree_preserved": worktree_path.is_dir(),
                        "files_changed_count": int((state.get("diff_stats") or {}).get("files_changed_count", 0)),
                        "files_changed": changed_paths,
                        "raw_jsonl_paths": jsonl_paths,
                        "result_file_path": outcome.result_file_path,
                        "execution_log_path": str(tdir / "EXECUTION_LOG.md"),
                    }
                    atomic_write_json(tdir / "ENVIRONMENT_REPORT.json", env_report)
                    state["environment_error"] = env_report
                    err_headline = f"[{final_env_exc['kind']}] {final_env_exc['detail']}"
                    self.store.append_execution_log(
                        tid,
                        "Environment Exception Escalated to Antigravity",
                        f"- Kind: `{final_env_exc['kind']}`\n"
                        f"- Environment retries used: `{state['env_retry_count']}/{max_env_retries}` (Repair round preserved at `{repair_round}`)\n"
                        f"- Worktree preserved: `{worktree_path}` ({len(changed_paths)} changed file(s))\n"
                        f"- First error: {first_env_exc['detail']}\n"
                        + (f"- Retry error: {final_env_exc['detail']}\n" if len(jsonl_paths) > 1 else ""),
                    )
                    self.store.transition(
                        tid,
                        state,
                        STATE_BLOCKED,
                        event_type="environment_blocked",
                        message=(
                            f"Builder blocked by environment exception after {state['env_retry_count']}/{max_env_retries} "
                            f"retry(s); escalating to Antigravity: {err_headline[:300]}"
                        ),
                        agent="Antigravity (Architect)",
                        failure_category="environment_failure",
                        last_error=err_headline[:800],
                        details={
                            "kind": final_env_exc["kind"],
                            "env_retry_count": state["env_retry_count"],
                            "max_env_retries": max_env_retries,
                            "repair_round": repair_round,
                            "worktree_preserved": True,
                        },
                    )
                    return {
                        "ok": False,
                        "next_action": "blocked_environment",
                        "task_id": tid,
                        "outcome": outcome.__dict__,
                        "environment_report": env_report,
                        "state": state,
                    }

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

            # 5. Deterministic post-execution invariant guard: reject partial/no-op exits before verify
            allow_empty_diff = bool(builder_cfg.get("allow_empty_diff", False))
            builder_delta_count = _count_builder_delta_files(
                worktree_path,
                state.get("diff_stats"),
                state.get("pre_builder_file_hashes") if isinstance(state.get("pre_builder_file_hashes"), dict) else None,
            )
            inv_category, inv_reason = _check_post_execution_invariants(
                outcome.structured_result,
                state.get("diff_stats"),
                allow_empty_diff=allow_empty_diff,
                effective_files_changed=builder_delta_count,
            )
            if inv_category is not None and inv_reason is not None:
                max_repairs = int(state.get("max_repair_rounds", 3))
                if repair_round < max_repairs:
                    next_round = repair_round + 1
                    state["repair_round"] = next_round
                    fb_text = _format_execution_invariant_feedback(
                        inv_category,
                        inv_reason,
                        structured_result=outcome.structured_result,
                        target_files=list(contract_doc.get("target_files") or []),
                        repair_round=next_round,
                    )
                    atomic_write_text(tdir / "FEEDBACK.md", fb_text)
                    self.store.transition(
                        tid,
                        state,
                        STATE_REVISING,
                        event_type="execution_invariant_failed",
                        message=(
                            f"Post-execution invariant failed ({inv_category}): {inv_reason} "
                            f"Prepared repair round {next_round}/{max_repairs}."
                        ),
                        failure_category=inv_category,
                        last_error=inv_reason,
                        details={
                            "invariant": inv_category,
                            "files_changed": state["diff_stats"]["files_changed_count"],
                            "repair_round": next_round,
                            "max_repair_rounds": max_repairs,
                        },
                    )
                    return {
                        "ok": False,
                        "next_action": "repair_available",
                        "task_id": tid,
                        "outcome": outcome.__dict__,
                        "state": state,
                    }

                self.store.transition(
                    tid,
                    state,
                    STATE_BLOCKED,
                    event_type="retry_limit_exceeded",
                    message=f"Post-execution invariant failed ({inv_category}) and max repair rounds ({max_repairs}) reached: {inv_reason}",
                    failure_category="retry_limit_exceeded",
                    last_error=inv_reason,
                    details={
                        "invariant": inv_category,
                        "files_changed": state["diff_stats"]["files_changed_count"],
                        "repair_round": repair_round,
                        "max_repair_rounds": max_repairs,
                    },
                )
                return {
                    "ok": False,
                    "next_action": "blocked_retry_limit",
                    "task_id": tid,
                    "outcome": outcome.__dict__,
                    "state": state,
                }

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
        """Run pre-Builder environment & dependency preparation explicitly without burning a repair round."""
        tid = self.resolve_task_id(task_id)
        tdir = self.store.task_dir(tid)
        with self.store.acquire_write_lock(tid):
            meta = self.store.load_task_meta(tid)
            state = self.store.load_state(tid)
            cfg = load_config(repo_path=Path(meta["repo_path"]), home_dir=self.home_dir)
            prep_res = self._run_pre_prepare_locked(
                tid,
                tdir,
                meta,
                state,
                cfg,
                prepare_commands=prepare_commands,
                timeout_sec=timeout_sec,
                refresh_boundary_on_explicit_prepare=True,
            )
            return {
                "ok": prep_res["ok"],
                "next_action": "ready_to_execute" if prep_res["ok"] else "blocked_dependency_prepare",
                "task_id": tid,
                "report": prep_res["prepare"],
                "state": state,
            }

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

            # Post-Builder dependency tidy (e.g. go mod tidy when go.mod changed)
            prep_report: dict[str, Any] | None = None
            extra_allowed_files: list[str] = list(state.get("pre_prepare_allowed_files") or [])
            if not skip_prepare:
                prep_report = run_dependency_prepare(
                    task_dir=tdir,
                    worktree_path=Path(meta["worktree_path"]),
                    base_commit=meta["base_commit"],
                    config=cfg,
                    explicit_commands=prepare_commands,
                    explicit_env=effective_verify_env or None,
                    phase="post",
                )
                if prep_report["ran"]:
                    extra_allowed_files = sorted(
                        set(extra_allowed_files) | set(prep_report.get("generated_allowed_files") or [])
                    )
                    self.store.append_event(
                        tid,
                        phase="verify",
                        agent="Orchestrator (Verifier)",
                        event_type="prepare_completed" if prep_report["passed"] else "prepare_failed",
                        message=(
                            f"Post-Builder dependency tidy {'passed' if prep_report['passed'] else 'failed'} "
                            f"({prep_report['commands_passed']}/{prep_report['commands_total']}, source={prep_report['command_source']})."
                        ),
                        details={
                            "phase": "post",
                            "source": prep_report["command_source"],
                            "commands_passed": prep_report["commands_passed"],
                            "commands_total": prep_report["commands_total"],
                        },
                    )
                    self.store.append_execution_log(
                        tid,
                        f"Post-Builder Dependency Tidy ({'PASSED' if prep_report['passed'] else 'FAILED'})",
                        f"- Source: `{prep_report['command_source']}` | Passed: `{prep_report['commands_passed']}/{prep_report['commands_total']}`\n"
                        + (
                            "- Failures:\n" + "\n".join(f"  - {r}" for r in prep_report["failure_reasons"]) + "\n"
                            if prep_report["failure_reasons"]
                            else ""
                        ),
                    )
                    if not prep_report["passed"]:
                        err_msg = "; ".join(prep_report["failure_reasons"])[:800] or "Post-Builder dependency tidy failed."
                        # Do NOT burn a code-repair round on external dependency/network preparation errors
                        self.store.transition(
                            tid,
                            state,
                            STATE_BLOCKED,
                            event_type="dependency_prepare_blocked",
                            message=(
                                f"Post-Builder dependency tidy failed outside sandbox (repair round not consumed): {err_msg}"
                            ),
                            failure_category="dependency_prepare_failure",
                            last_error=err_msg,
                            details={"stage": "post_execute", "prepare": prep_report},
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
            skip_detection = str(report.get("skip_detection") or "heuristic")
            raw_skipped_total = report.get("skipped_tests_total")
            skipped_total: int | None = None if raw_skipped_total is None else int(raw_skipped_total)
            skipped_names = list(report.get("skipped_tests") or [])
            state["verification_summary"] = {
                "verified_at": report["verified_at"],
                "passed": report["passed"],
                "command_source": report["command_source"],
                "commands_total": report["commands_total"],
                "commands_passed": report["commands_passed"],
                "commands_failed": report["commands_failed"],
                "skip_detection": skip_detection,
                "skipped_tests_total": skipped_total,
                "skipped_tests": skipped_names,
                "contract_valid": report["contract_integrity"]["valid"],
                "blocking_audit_issues": len(report["diff_audit"]["blocking_issues"]),
                "warnings": len(report["diff_audit"]["warnings"]),
                "same_package_companions": list(report["diff_audit"].get("same_package_companions") or []),
                "checks": [
                    {
                        "name": c["name"],
                        "kind": c["kind"],
                        "command": c["command"],
                        "passed": c["passed"],
                        "exit_code": c["exit_code"],
                        "duration_sec": c["duration_sec"],
                        "skip_detection": str(c.get("skip_detection") or "not_applicable"),
                        "skipped_count": None if c.get("skipped_count") is None else int(c["skipped_count"]),
                        **(
                            {
                                k: c[k]
                                for k in ("stdout_log_path", "stderr_log_path")
                                if c.get(k)
                            }
                            if not c["passed"]
                            else {}
                        ),
                    }
                    for c in report["checks"]
                ],
                "failure_reasons": report["failure_reasons"],
            }

            if skipped_total is None:
                skip_log_part = f" | Skipped tests: `unobservable` ({skip_detection})"
            elif skipped_total > 0:
                skip_log_part = f" | Skipped tests: `{skipped_total}` ({skip_detection})"
            else:
                skip_log_part = ""

            self.store.append_execution_log(
                tid,
                f"Independent Verification ({'PASSED' if report['passed'] else 'FAILED'})",
                f"- Source: `{report['command_source']}` | Passed: `{report['commands_passed']}/{report['commands_total']}`"
                + skip_log_part
                + "\n"
                f"- Contract integrity: `{report['contract_integrity']['valid']}`\n"
                f"- Audit blocking issues: `{len(report['diff_audit']['blocking_issues'])}` | Warnings: `{len(report['diff_audit']['warnings'])}`\n"
                f"- Changed files: `{report['diff_stats']['files_changed_count']}` "
                f"(+{report['diff_stats']['total_insertions']}/-{report['diff_stats']['total_deletions']})\n",
            )

            if report["passed"]:
                if skipped_total is None:
                    skip_note = ", skips unobservable"
                elif skipped_total > 0:
                    skip_note = f", {skipped_total} test(s) skipped"
                else:
                    skip_note = ""
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
                        "skip_detection": skip_detection,
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
        """Run one deterministic Pre-Prepare -> Execute -> Post-Tidy -> Verify cycle."""
        parsed_verify_env = parse_env_assignments(verify_env)
        exec_res = self.execute_round(
            task_id,
            override_model=override_model,
            override_effort=override_effort,
            timeout_sec=codex_timeout_sec,
            idle_timeout_sec=codex_idle_timeout_sec,
            prepare_commands=prepare_commands,
            prepare_env=parsed_verify_env or None,
        )
        if not exec_res["ok"]:
            stage = str(exec_res.get("stage") or "execute")
            next_action = exec_res.get("next_action") or (
                "blocked" if exec_res["state"]["status"] == STATE_BLOCKED else "failed"
            )
            out: dict[str, Any] = {
                "ok": False,
                "stage": stage,
                "next_action": next_action,
                "task_id": exec_res["task_id"],
                "execution": None if stage == "prepare" else exec_res,
                "verification": None,
                "state": exec_res["state"],
            }
            if stage == "prepare" and "prepare" in exec_res:
                out["prepare"] = exec_res["prepare"]
            if "environment_report" in exec_res:
                out["environment_report"] = exec_res["environment_report"]
            return out

        ver_res = self.verify_task(
            exec_res["task_id"],
            verify_commands=verify_commands,
            verify_env=parsed_verify_env or None,
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
        acknowledged_warnings: list[str] | None = None,
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
            ack_list = [w.strip() for w in (acknowledged_warnings or []) if w.strip()]

            review_entry = {
                "round": repair_round,
                "verdict": verdict_norm,
                "summary": summary.strip(),
                "issues": [i.strip() for i in (issues or []) if i.strip()],
                "acknowledged_warnings": ack_list,
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
            if ack_list and isinstance(state.get("verification_summary"), dict):
                state["verification_summary"]["acknowledged_warnings"] = ack_list
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
                )
                + (
                    "- Acknowledged Warnings:\n"
                    + "\n".join(f"  - {item}" for item in ack_list)
                    + "\n"
                    if ack_list
                    else ""
                ),
            )

            if verdict_norm == "approve":
                approve_details: dict[str, Any] = {"verdict": "approve", "round": repair_round}
                if ack_list:
                    approve_details["acknowledged_warnings"] = ack_list
                self.store.transition(
                    tid,
                    state,
                    STATE_COMPLETED,
                    event_type="review_approved",
                    message=f"Code Review approved: {review_entry['summary']}",
                    details=approve_details,
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
        environment_report = read_json(tdir / "ENVIRONMENT_REPORT.json")
        reviews = read_json(tdir / "reviews.json", default=[])
        return {
            "task": meta,
            "state": state,
            "events": events,
            "verification": verification,
            "result": result,
            "environment_report": environment_report,
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
