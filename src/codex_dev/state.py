"""Deterministic task state machine, atomic persistence, and writer locks."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import get_codex_dev_home

# Exact 9 states required by Codex Dev specification
STATE_CREATED = "CREATED"
STATE_PLANNED = "PLANNED"
STATE_EXECUTING = "EXECUTING"
STATE_VERIFYING = "VERIFYING"
STATE_REVIEWING = "REVIEWING"
STATE_REVISING = "REVISING"
STATE_COMPLETED = "COMPLETED"
STATE_BLOCKED = "BLOCKED"
STATE_FAILED = "FAILED"

ALL_STATES = {
    STATE_CREATED,
    STATE_PLANNED,
    STATE_EXECUTING,
    STATE_VERIFYING,
    STATE_REVIEWING,
    STATE_REVISING,
    STATE_COMPLETED,
    STATE_BLOCKED,
    STATE_FAILED,
}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    STATE_CREATED: {STATE_PLANNED, STATE_BLOCKED, STATE_FAILED},
    STATE_PLANNED: {STATE_EXECUTING, STATE_BLOCKED, STATE_FAILED},
    STATE_EXECUTING: {STATE_VERIFYING, STATE_REVISING, STATE_BLOCKED, STATE_FAILED},
    STATE_VERIFYING: {STATE_REVIEWING, STATE_REVISING, STATE_BLOCKED, STATE_FAILED},
    STATE_REVIEWING: {STATE_COMPLETED, STATE_REVISING, STATE_BLOCKED, STATE_FAILED},
    STATE_REVISING: {STATE_EXECUTING, STATE_VERIFYING, STATE_BLOCKED, STATE_FAILED},
    STATE_BLOCKED: {STATE_PLANNED, STATE_REVISING, STATE_EXECUTING, STATE_FAILED},
    STATE_FAILED: {STATE_PLANNED, STATE_REVISING, STATE_EXECUTING, STATE_BLOCKED},
    STATE_COMPLETED: {STATE_REVISING},
}

PHASE_BY_STATE: dict[str, str] = {
    STATE_CREATED: "plan",
    STATE_PLANNED: "plan",
    STATE_EXECUTING: "execute",
    STATE_VERIFYING: "verify",
    STATE_REVIEWING: "review",
    STATE_REVISING: "repair",
    STATE_COMPLETED: "deliver",
    STATE_BLOCKED: "blocked",
    STATE_FAILED: "failed",
}

AGENT_BY_STATE: dict[str, str] = {
    STATE_CREATED: "Antigravity (Architect)",
    STATE_PLANNED: "Antigravity (Architect)",
    STATE_EXECUTING: "Codex CLI (Builder)",
    STATE_VERIFYING: "Orchestrator (Verifier)",
    STATE_REVIEWING: "Antigravity (Reviewer)",
    STATE_REVISING: "Antigravity -> Codex CLI",
    STATE_COMPLETED: "Antigravity (Ready to Deliver)",
    STATE_BLOCKED: "Waiting for User",
    STATE_FAILED: "Orchestrator (Error)",
}


class StateTransitionError(RuntimeError):
    """Raised when an invalid state machine transition is attempted."""


class TaskLockError(RuntimeError):
    """Raised when another writer process already holds the lock for a task."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_task_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(3)
    return f"cd-{ts}-{suffix}"


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically write UTF-8 text via temp file + fsync + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically write pretty JSON via temp file + fsync + os.replace."""
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(path, serialized)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


class TaskStore:
    """Manages persistent directory layout, atomic state updates, and locking for tasks."""

    def __init__(self, home_dir: Path | None = None) -> None:
        self.home_dir = (home_dir or get_codex_dev_home()).resolve()
        self.tasks_dir = self.home_dir / "tasks"
        self.worktrees_dir = self.home_dir / "worktrees"
        self.active_task_file = self.home_dir / "active_task.json"

    def ensure_dirs(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str) -> Path:
        if not task_id or "/" in task_id or "\\" in task_id or ".." in task_id:
            raise ValueError(f"Invalid task_id: {task_id!r}")
        return self.tasks_dir / task_id

    def task_exists(self, task_id: str) -> bool:
        return (self.task_dir(task_id) / "state.json").is_file()

    @contextlib.contextmanager
    def acquire_write_lock(self, task_id: str) -> Iterator[None]:
        """Acquire exclusive non-blocking advisory lock so only one writer runs per task."""
        tdir = self.task_dir(task_id)
        tdir.mkdir(parents=True, exist_ok=True)
        lock_path = tdir / ".lock"
        fh = lock_path.open("a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TaskLockError(
                    f"Task '{task_id}' is currently locked by another active writer process."
                ) from exc
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"pid": os.getpid(), "locked_at": utc_now()}) + "\n")
            fh.flush()
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    def set_active_task(self, task_id: str, repo_path: str, name: str) -> None:
        self.ensure_dirs()
        atomic_write_json(
            self.active_task_file,
            {
                "task_id": task_id,
                "repo_path": repo_path,
                "name": name,
                "updated_at": utc_now(),
            },
        )

    def get_active_task_id(self) -> str | None:
        data = read_json(self.active_task_file)
        if isinstance(data, dict) and isinstance(data.get("task_id"), str):
            tid = data["task_id"]
            if self.task_exists(tid):
                return tid
        return None

    def load_task_meta(self, task_id: str) -> dict[str, Any]:
        path = self.task_dir(task_id) / "task.json"
        data = read_json(path)
        if not isinstance(data, dict):
            raise FileNotFoundError(f"Task metadata not found for '{task_id}'")
        return data

    def load_state(self, task_id: str) -> dict[str, Any]:
        path = self.task_dir(task_id) / "state.json"
        data = read_json(path)
        if not isinstance(data, dict):
            raise FileNotFoundError(f"Task state not found for '{task_id}'")
        return data

    def save_state(self, task_id: str, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        atomic_write_json(self.task_dir(task_id) / "state.json", state)

    def transition(
        self,
        task_id: str,
        state: dict[str, Any],
        target_status: str,
        *,
        event_type: str = "state_transition",
        message: str = "",
        agent: str | None = None,
        details: dict[str, Any] | None = None,
        failure_category: str | None = None,
        last_error: str | None = None,
        allow_failed_to_verify: bool = False,
    ) -> dict[str, Any]:
        """Validate and persist a state machine transition atomically."""
        if target_status not in ALL_STATES:
            raise StateTransitionError(f"Unknown target state: {target_status}")
        current = str(state.get("status") or "")
        if current and target_status != current:
            allowed = set(ALLOWED_TRANSITIONS.get(current, set()))
            if allow_failed_to_verify and current == STATE_FAILED and target_status == STATE_VERIFYING:
                allowed.add(STATE_VERIFYING)
            if target_status not in allowed:
                raise StateTransitionError(
                    f"Illegal state transition: {current} -> {target_status} "
                    f"(allowed: {sorted(allowed)})"
                )

        prev_agent = state.get("current_agent")
        new_agent = agent or AGENT_BY_STATE[target_status]
        state["previous_status"] = current or None
        state["status"] = target_status
        state["current_phase"] = PHASE_BY_STATE[target_status]
        state["current_agent"] = new_agent

        # Update phase progress map
        phases = state.setdefault(
            "phases",
            {
                "plan": "pending",
                "execute": "pending",
                "verify": "pending",
                "review": "pending",
            },
        )
        if target_status == STATE_PLANNED:
            phases["plan"] = "completed"
        elif target_status == STATE_EXECUTING:
            phases["plan"] = "completed"
            phases["execute"] = "running"
            phases["verify"] = "pending"
            phases["review"] = "pending"
        elif target_status == STATE_VERIFYING:
            phases["execute"] = "completed"
            phases["verify"] = "running"
        elif target_status == STATE_REVIEWING:
            phases["verify"] = "completed"
            phases["review"] = "running"
        elif target_status == STATE_REVISING:
            if current == STATE_EXECUTING:
                phases["execute"] = "failed"
            elif current == STATE_VERIFYING:
                phases["verify"] = "failed"
            elif current == STATE_REVIEWING:
                phases["review"] = "changes_requested"
        elif target_status == STATE_COMPLETED:
            phases["plan"] = "completed"
            phases["execute"] = "completed"
            phases["verify"] = "completed"
            phases["review"] = "completed"
        elif target_status == STATE_FAILED:
            cur_phase = PHASE_BY_STATE.get(current, "execute")
            if cur_phase in phases:
                phases[cur_phase] = "failed"
        elif target_status == STATE_BLOCKED:
            cur_phase = PHASE_BY_STATE.get(current, "execute")
            if cur_phase in phases and phases[cur_phase] == "running":
                phases[cur_phase] = "blocked"

        if failure_category is not None:
            state["failure_category"] = failure_category
        elif target_status in {STATE_COMPLETED, STATE_EXECUTING, STATE_VERIFYING, STATE_REVIEWING}:
            state["failure_category"] = None

        if last_error is not None:
            state["last_error"] = last_error
        elif target_status in {STATE_COMPLETED, STATE_EXECUTING, STATE_VERIFYING, STATE_REVIEWING}:
            state["last_error"] = None

        if target_status in {STATE_EXECUTING, STATE_VERIFYING}:
            state["verification_summary"] = None

        self.save_state(task_id, state)
        self.append_event(
            task_id,
            phase=state["current_phase"],
            agent=new_agent,
            event_type=event_type,
            message=message or f"Transitioned {current or 'INIT'} -> {target_status}",
            details={
                "from": current or None,
                "to": target_status,
                "handoff": prev_agent != new_agent and prev_agent is not None,
                "from_agent": prev_agent,
                "to_agent": new_agent,
                **(details or {}),
            },
        )
        return state

    def append_event(
        self,
        task_id: str,
        *,
        phase: str,
        agent: str,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a structured event line to events.jsonl."""
        events_path = self.task_dir(task_id) / "events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        seq = 1
        if events_path.is_file():
            try:
                with events_path.open("r", encoding="utf-8") as fh:
                    seq = sum(1 for _ in fh) + 1
            except OSError:
                seq = 1

        event = {
            "seq": seq,
            "timestamp": utc_now(),
            "phase": phase,
            "agent": agent,
            "type": event_type,
            "message": message,
            "details": details or {},
        }
        with events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return event

    def append_execution_log(self, task_id: str, heading: str, body: str) -> None:
        """Append a human-readable section to EXECUTION_LOG.md."""
        log_path = self.task_dir(task_id) / "EXECUTION_LOG.md"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = f"\n## [{utc_now()}] {heading}\n\n{body.rstrip()}\n"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
            fh.flush()
            os.fsync(fh.fileno())

    def read_events(self, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        events_path = self.task_dir(task_id) / "events.jsonl"
        if not events_path.is_file():
            return []
        lines = events_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def write_run_pid(self, task_id: str, pid: int, pgid: int | None = None) -> None:
        atomic_write_json(
            self.task_dir(task_id) / "run.pid",
            {"pid": pid, "pgid": pgid or pid, "started_at": utc_now()},
        )

    def clear_run_pid(self, task_id: str) -> None:
        p = self.task_dir(task_id) / "run.pid"
        with contextlib.suppress(OSError):
            p.unlink()

    def read_run_pid(self, task_id: str) -> dict[str, Any] | None:
        data = read_json(self.task_dir(task_id) / "run.pid")
        return data if isinstance(data, dict) else None

    def list_tasks(self, limit: int = 30) -> list[dict[str, Any]]:
        if not self.tasks_dir.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for entry in self.tasks_dir.iterdir():
            if not entry.is_dir():
                continue
            state_path = entry / "state.json"
            task_path = entry / "task.json"
            if not state_path.is_file():
                continue
            try:
                state = read_json(state_path) or {}
                meta = read_json(task_path) or {}
                items.append(
                    {
                        "task_id": entry.name,
                        "name": meta.get("name") or state.get("name") or entry.name,
                        "repo_path": meta.get("repo_path"),
                        "branch": meta.get("branch"),
                        "worktree_path": meta.get("worktree_path"),
                        "status": state.get("status"),
                        "current_phase": state.get("current_phase"),
                        "current_agent": state.get("current_agent"),
                        "current_model": (state.get("model_policy") or {}).get("current_model"),
                        "repair_round": state.get("repair_round", 0),
                        "max_repair_rounds": state.get("max_repair_rounds", 3),
                        "env_retry_count": state.get("env_retry_count", 0),
                        "max_env_retries": state.get("max_env_retries", 1),
                        "created_at": meta.get("created_at"),
                        "updated_at": state.get("updated_at"),
                    }
                )
            except (OSError, json.JSONDecodeError):
                continue
        items.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
        return items[:limit]
