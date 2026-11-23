"""Non-interactive Codex CLI (`codex exec`) runner with JSONL streaming and timeout control."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import get_codex_home
from .state import TaskStore, atomic_write_json, atomic_write_text


SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "codex_result.schema.json"

VALID_BUILDER_ISOLATION = {"strict", "inherit"}
STRICT_DISABLED_FEATURES = (
    "api_key_model_discovery",
    "plugins",
    "apps",
    "hooks",
    "skill_search",
)


def _sync_symlink(link_path: Path, target_path: Path) -> None:
    """Create or refresh a symlink without touching the target file."""
    if not target_path.exists():
        if link_path.is_symlink():
            try:
                link_path.unlink()
            except OSError:
                pass
        return
    if link_path.is_symlink():
        try:
            if link_path.resolve() == target_path.resolve():
                return
            link_path.unlink()
        except OSError:
            pass
    elif link_path.exists():
        return
    try:
        link_path.symlink_to(target_path)
    except OSError:
        pass


def prepare_isolated_codex_home(
    codex_dev_home: Path,
    source_codex_home: Path | None = None,
) -> Path:
    """Provision a clean, plugin/skill-free CODEX_HOME under ~/.codex-dev/codex_home.

    Symlinks only auth.json and models_cache.json from the user's real ~/.codex
    (read-only, never modifying ~/.codex) and writes a minimal config.toml with
    no third-party plugins, skills, MCP servers, AGENTS.md, or desktop notify hooks.
    """
    isolated_dir = (codex_dev_home / "codex_home").resolve()
    isolated_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(isolated_dir, 0o700)
    except OSError:
        pass

    src_home = (source_codex_home or get_codex_home()).resolve()
    if src_home != isolated_dir:
        for fname in ("auth.json", "models_cache.json"):
            _sync_symlink(isolated_dir / fname, src_home / fname)

    minimal_toml = (
        "# Managed by codex-dev (strict Builder isolation). Do not place personal plugins/skills here.\n"
        'approval_policy = "never"\n'
        "check_for_update_on_startup = false\n"
        "notify = []\n"
        "\n"
        "[features]\n"
        "api_key_model_discovery = false\n"
        "plugins = false\n"
        "apps = false\n"
        "hooks = false\n"
        "skill_search = false\n"
        "memories = false\n"
        "chronicle = false\n"
    )
    cfg_path = isolated_dir / "config.toml"
    try:
        existing = cfg_path.read_text(encoding="utf-8") if cfg_path.is_file() else None
    except OSError:
        existing = None
    if existing != minimal_toml:
        atomic_write_text(cfg_path, minimal_toml)

    return isolated_dir


@dataclass
class CodexRunOutcome:
    ok: bool
    exit_code: int
    timed_out: bool
    cancelled: bool
    failure_category: str | None
    error_message: str | None
    duration_sec: float
    structured_result: dict[str, Any] | None
    token_usage: dict[str, Any] | None
    events_seen: int
    raw_jsonl_path: str
    result_file_path: str
    stderr_tail: str
    attempt: int = 1
    timeout_reason: str | None = None
    active_extensions_used: int = 0
    effective_timeout_sec: int | None = None
    estimated_token_usage: dict[str, Any] | None = None


def build_codex_prompt(
    *,
    task_name: str,
    plan_text: str,
    feedback_text: str | None,
    repair_round: int,
    worktree_path: Path,
    builder_checks: list[str] | None = None,
    builder_network_access: bool = False,
) -> str:
    """Build deterministic builder instructions for Codex CLI."""
    if builder_network_access:
        sandbox_rule = (
            "6. You are running inside a `workspace-write` sandbox with a pre-configured writable "
            "`GOCACHE`/`GOTMPDIR` outside the worktree and `sandbox_workspace_write.network_access=true` "
            "(so local loopback servers like 127.0.0.1 and local backing services are reachable, while "
            "filesystem writes remain confined to the worktree). Prefer running offline/unit package tests "
            "first and avoid probing `docker info` if Docker is unavailable. The Orchestrator will still "
            "run full independent host verification after you finish."
        )
    else:
        sandbox_rule = (
            "6. You are running inside a `workspace-write` sandbox with a pre-configured writable "
            "`GOCACHE`/`GOTMPDIR` outside the worktree (so local Go vet/build/test can run without "
            "polluting the repo). Prefer running offline/unit package tests that do not require "
            "outbound network access, loopback port binding (e.g. 127.0.0.1), a live database, or the "
            "Docker daemon (do NOT probe `docker info` or retry blocked integration tests). The "
            "`Verification Commands` section in PLAN.md will be executed as independent verification on "
            "the host by the Orchestrator (including non-sandbox dependency preparation such as `go mod tidy`) after "
            "you finish. When your implementation is complete and sandbox-safe checks pass, return "
            "`status: \"ready_for_verify\"` (do NOT mark `status: \"partial\"` merely because host-only "
            "verification commands have not run inside the sandbox)."
        )

    parts = [
        f"You are the Builder agent executing task '{task_name}' inside an isolated Git worktree.",
        f"Working directory: {worktree_path}",
        "",
        "STRICT RULES:",
        "1. Implement the task strictly according to the immutable PLAN below.",
        "2. Do NOT weaken, delete, or skip tests, linters, or type checks to make checks pass.",
        "3. Do NOT run git commit, git push, git merge, or destructive git clean commands.",
        "4. Run relevant sandbox-safe checks before finishing, and report accurate results.",
        "5. Return your final response strictly conforming to the provided JSON output schema.",
        sandbox_rule,
        "",
        "=== IMMUTABLE PLAN CONTRACT ===",
        plan_text.strip(),
        "=== END PLAN CONTRACT ===",
    ]
    if builder_checks:
        parts.extend(
            [
                "",
                "=== SANDBOX-SAFE BUILDER CHECKS (RUN THESE INSIDE THE SANDBOX) ===",
                *(f"- `{cmd}`" for cmd in builder_checks),
                "=== END BUILDER CHECKS ===",
            ]
        )
    if repair_round > 0 and feedback_text and feedback_text.strip():
        parts.extend(
            [
                "",
                f"=== REPAIR ROUND {repair_round} FEEDBACK (ADDRESS EVERY ITEM) ===",
                feedback_text.strip(),
                "=== END FEEDBACK ===",
            ]
        )
    return "\n".join(parts) + "\n"


def _extract_usage(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Extract normalized token usage from Codex JSONL event payloads when available."""
    usage = obj.get("usage")
    cumulative = False
    if not isinstance(usage, dict):
        # Some events nest usage under turn/response/result/metrics/info
        for key in ("turn", "response", "result", "metrics", "info", "payload"):
            nested = obj.get(key)
            if not isinstance(nested, dict):
                continue
            if isinstance(nested.get("usage"), dict):
                usage = nested["usage"]
                break
            if isinstance(nested.get("last_token_usage"), dict):
                usage = nested["last_token_usage"]
                break
            if isinstance(nested.get("total_token_usage"), dict):
                usage = nested["total_token_usage"]
                cumulative = True
                break
    if not isinstance(usage, dict):
        return None

    inp = usage.get("input_tokens")
    cached = usage.get("cached_input_tokens", usage.get("cache_read_input_tokens", 0))
    out = usage.get("output_tokens")
    total = usage.get("total_tokens")
    if inp is None and out is None and total is None:
        return None
    inp_i = int(inp or 0)
    cached_i = int(cached or 0)
    out_i = int(out or 0)
    total_i = int(total) if total is not None else (inp_i + out_i)
    return {
        "input_tokens": inp_i,
        "cached_input_tokens": cached_i,
        "output_tokens": out_i,
        "total_tokens": total_i,
        "source": "codex_jsonl",
        "cumulative": cumulative,
    }


def _accumulate_attempt_usage(
    current: dict[str, Any] | None,
    event_usage: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Update per-attempt usage totals and return (new_attempt_total, delta_for_task_total)."""
    clean_ev = {
        "input_tokens": int(event_usage.get("input_tokens", 0)),
        "cached_input_tokens": int(event_usage.get("cached_input_tokens", 0)),
        "output_tokens": int(event_usage.get("output_tokens", 0)),
        "total_tokens": int(event_usage.get("total_tokens", 0)),
        "source": event_usage.get("source") or "codex_jsonl",
    }
    if current is None:
        return dict(clean_ev), dict(clean_ev)

    if event_usage.get("cumulative"):
        delta = {
            "input_tokens": max(0, clean_ev["input_tokens"] - int(current.get("input_tokens", 0))),
            "cached_input_tokens": max(0, clean_ev["cached_input_tokens"] - int(current.get("cached_input_tokens", 0))),
            "output_tokens": max(0, clean_ev["output_tokens"] - int(current.get("output_tokens", 0))),
            "total_tokens": max(0, clean_ev["total_tokens"] - int(current.get("total_tokens", 0))),
            "source": clean_ev["source"],
        }
        updated = {
            "input_tokens": max(int(current.get("input_tokens", 0)), clean_ev["input_tokens"]),
            "cached_input_tokens": max(int(current.get("cached_input_tokens", 0)), clean_ev["cached_input_tokens"]),
            "output_tokens": max(int(current.get("output_tokens", 0)), clean_ev["output_tokens"]),
            "total_tokens": max(int(current.get("total_tokens", 0)), clean_ev["total_tokens"]),
            "source": clean_ev["source"],
        }
        return updated, delta

    updated = {
        "input_tokens": int(current.get("input_tokens", 0)) + clean_ev["input_tokens"],
        "cached_input_tokens": int(current.get("cached_input_tokens", 0)) + clean_ev["cached_input_tokens"],
        "output_tokens": int(current.get("output_tokens", 0)) + clean_ev["output_tokens"],
        "total_tokens": int(current.get("total_tokens", 0)) + clean_ev["total_tokens"],
        "source": clean_ev["source"],
    }
    return updated, clean_ev


def _summarize_jsonl_event(ev: dict[str, Any]) -> tuple[str, str] | None:
    """Map raw Codex CLI `--json` event into concise (event_type, message)."""
    etype = str(ev.get("type") or "")
    if etype == "thread.started":
        tid = ev.get("thread_id") or ""
        return ("codex_thread", f"Codex thread started ({tid})" if tid else "Codex thread started")
    if etype == "turn.started":
        return ("codex_turn", "Codex turn started")
    if etype == "turn.completed":
        usage = _extract_usage(ev)
        if usage:
            return (
                "codex_turn_completed",
                f"Codex turn completed (in={usage['input_tokens']}, cached={usage['cached_input_tokens']}, out={usage['output_tokens']})",
            )
        return ("codex_turn_completed", "Codex turn completed")
    if etype in ("turn.failed", "error"):
        err = ev.get("error") or ev.get("message") or str(ev)
        if isinstance(err, dict):
            err = err.get("message") or json.dumps(err)
        return ("codex_error", f"Codex error: {str(err)[:240]}")

    item = ev.get("item")
    if isinstance(item, dict):
        itype = str(item.get("type") or "")
        if itype == "command_execution":
            cmd = str(item.get("command") or "").strip()
            status = str(item.get("status") or "")
            code = item.get("exit_code")
            if etype == "item.started":
                return ("codex_command", f"Running: {cmd[:140]}")
            if etype == "item.completed":
                return ("codex_command_done", f"Command finished (exit={code}, {status}): {cmd[:120]}")
        elif itype == "file_change":
            changes = item.get("changes") or []
            if isinstance(changes, list) and changes:
                paths = [str(c.get("path") or c) for c in changes[:4]]
                suffix = f" (+{len(changes) - 4} more)" if len(changes) > 4 else ""
                return ("codex_file_change", f"File changes: {', '.join(paths)}{suffix}")
        elif itype == "agent_message" and etype == "item.completed":
            text = str(item.get("text") or "").strip()
            if text:
                preview = text.replace("\n", " ")[:180]
                return ("codex_message", preview)
        elif itype == "reasoning" and etype == "item.completed":
            text = str(item.get("text") or "").strip()
            if text:
                return ("codex_reasoning", text.replace("\n", " ")[:160])

    return None


def _terminate_process_group(proc: subprocess.Popen[str], grace_sec: float = 3.0) -> None:
    """Terminate a child process group cleanly (SIGTERM -> SIGKILL)."""
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass

    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)

    if proc.poll() is None:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


BENIGN_STDERR_MARKERS = (
    "codex_models_manager::manager: failed to refresh available models",
)


def _is_benign_stderr_line(line: str) -> bool:
    return any(marker in line for marker in BENIGN_STDERR_MARKERS)


def _format_stderr_tail(chunks: list[str], *, max_lines: int = 40) -> str:
    """Collapse repetitive benign model-refresh timeout lines while preserving all real errors."""
    raw_lines = "".join(chunks).splitlines()
    kept: list[str] = []
    suppressed = 0
    for line in raw_lines:
        if _is_benign_stderr_line(line):
            suppressed += 1
            continue
        kept.append(line)
    tail = kept[-max_lines:]
    if suppressed > 0:
        tail.append(f"[suppressed {suppressed} benign codex model-refresh timeout stderr line(s)]")
    return "\n".join(tail).strip()


def _has_valid_result_file(path: Path) -> bool:
    """Return True if the --output-last-message file exists and contains a valid JSON object."""
    if not path.is_file():
        return False
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return False
        parsed = json.loads(raw)
        return isinstance(parsed, dict)
    except (OSError, json.JSONDecodeError):
        return False


def _toml_quote(val: str) -> str:
    return json.dumps(str(val))


def run_codex_exec(
    *,
    store: TaskStore,
    task_id: str,
    worktree_path: Path,
    codex_binary: str,
    model: str,
    sandbox_mode: str,
    reasoning_effort: str | None,
    prompt: str,
    timeout_sec: int,
    repair_round: int,
    attempt: int = 1,
    idle_timeout_sec: int = 0,
    post_turn_grace_sec: float = 4.0,
    active_grace_sec: int = 0,
    active_grace_window_sec: int = 60,
    max_active_extensions: int = 2,
    builder_env: dict[str, str] | None = None,
    builder_add_dirs: list[str] | None = None,
    builder_isolation: str = "strict",
    builder_network_access: bool = False,
    on_token_usage: Callable[[dict[str, Any]], None] | None = None,
) -> CodexRunOutcome:
    """Execute `codex exec` non-interactively, streaming JSONL events into the task store."""
    tdir = store.task_dir(task_id)
    run_tag = f"round_{repair_round}_attempt_{attempt}"
    raw_jsonl_path = tdir / f"codex_{run_tag}.jsonl"
    result_file_path = tdir / f"codex_{run_tag}_result.json"
    cancel_flag = tdir / ".cancel"
    if cancel_flag.exists():
        try:
            cancel_flag.unlink()
        except OSError:
            pass

    # Provision writable Go build/temp cache directories outside the worktree so Go vet/test/build
    # works cleanly inside Codex's workspace-write sandbox without polluting git status.
    gocache_dir = (tdir / "gocache").resolve()
    gotmp_dir = (tdir / "gotmp").resolve()
    gocache_dir.mkdir(parents=True, exist_ok=True)
    gotmp_dir.mkdir(parents=True, exist_ok=True)

    injected_env: dict[str, str] = {
        "GOCACHE": str(gocache_dir),
        "GOTMPDIR": str(gotmp_dir),
    }
    if builder_env:
        for k, v in builder_env.items():
            if isinstance(k, str) and k.strip() and v is not None:
                injected_env[k.strip()] = str(v)

    add_dirs: list[str] = [str(gocache_dir), str(gotmp_dir)]
    if builder_add_dirs:
        for d in builder_add_dirs:
            if isinstance(d, str) and d.strip():
                resolved_d = str(Path(d).expanduser().resolve())
                if resolved_d not in add_dirs:
                    add_dirs.append(resolved_d)

    eff_isolation = builder_isolation if builder_isolation in VALID_BUILDER_ISOLATION else "strict"
    isolated_codex_home: Path | None = None
    if eff_isolation == "strict":
        isolated_codex_home = prepare_isolated_codex_home(store.home_dir)

    # Support either direct executable file path (possibly with spaces) or multi-token command (for mock testing)
    if Path(codex_binary).is_file():
        bin_tokens = [codex_binary]
    else:
        bin_tokens = shlex.split(codex_binary)
    cmd = [
        *bin_tokens,
        "exec",
        "-m",
        model,
        "-s",
        sandbox_mode,
        "-C",
        str(worktree_path),
        "--json",
        "--output-schema",
        str(SCHEMA_PATH),
        "-o",
        str(result_file_path),
        "--ephemeral",
        "-c",
        'approval_policy="never"',
        "-c",
        "check_for_update_on_startup=false",
    ]
    if eff_isolation == "strict":
        for feat in STRICT_DISABLED_FEATURES:
            cmd.extend(["--disable", feat])
        cmd.extend(["-c", "notify=[]"])
    else:
        cmd.extend(["--disable", "api_key_model_discovery"])
    if sandbox_mode == "workspace-write" and builder_network_access:
        cmd.extend(["-c", "sandbox_workspace_write.network_access=true"])
    for extra_dir in add_dirs:
        cmd.extend(["--add-dir", extra_dir])
    for env_k, env_v in injected_env.items():
        cmd.extend(["-c", f"shell_environment_policy.set.{env_k}={_toml_quote(env_v)}"])
    if reasoning_effort:
        cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    cmd.append("-")

    proc_env = {**os.environ, **injected_env}
    if isolated_codex_home is not None:
        proc_env["CODEX_HOME"] = str(isolated_codex_home)

    start_ts = time.monotonic()
    last_activity_ts = start_ts
    activity_lock = threading.Lock()

    def _touch_activity() -> None:
        nonlocal last_activity_ts
        with activity_lock:
            last_activity_ts = time.monotonic()

    def _get_last_activity() -> float:
        with activity_lock:
            return last_activity_ts

    store.append_event(
        task_id,
        phase="execute",
        agent="Codex CLI (Builder)",
        event_type="codex_invoked",
        message=(
            f"Starting Codex CLI (model={model}, sandbox={sandbox_mode}, "
            f"round={repair_round}, attempt={attempt}, timeout={timeout_sec}s"
            + (f", idle_timeout={idle_timeout_sec}s" if idle_timeout_sec > 0 else "")
            + ")"
        ),
        details={
            "model": model,
            "sandbox": sandbox_mode,
            "builder_isolation": eff_isolation,
            "builder_network_access": bool(sandbox_mode == "workspace-write" and builder_network_access),
            "isolated_codex_home": str(isolated_codex_home) if isolated_codex_home else None,
            "round": repair_round,
            "attempt": attempt,
            "timeout_sec": timeout_sec,
            "idle_timeout_sec": idle_timeout_sec,
            "raw_jsonl_path": str(raw_jsonl_path),
        },
    )

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(worktree_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=proc_env,
        )
    except FileNotFoundError as exc:
        return CodexRunOutcome(
            ok=False,
            exit_code=-1,
            timed_out=False,
            cancelled=False,
            failure_category="execution_failure",
            error_message=f"Codex binary not found: {codex_binary} ({exc})",
            duration_sec=time.monotonic() - start_ts,
            structured_result=None,
            token_usage=None,
            events_seen=0,
            raw_jsonl_path=str(raw_jsonl_path),
            result_file_path=str(result_file_path),
            stderr_tail=str(exc),
            attempt=attempt,
        )

    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    store.write_run_pid(task_id, proc.pid, pgid)

    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            if line.strip() and not _is_benign_stderr_line(line):
                _touch_activity()
            stderr_chunks.append(line)
            if len(stderr_chunks) > 200:
                stderr_chunks.pop(0)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    # Feed prompt via stdin and close stdin immediately
    try:
        if proc.stdin is not None:
            proc.stdin.write(prompt)
            proc.stdin.flush()
            proc.stdin.close()
    except BrokenPipeError:
        pass

    timed_out = False
    timeout_reason: str | None = None
    cancelled = False
    graceful_post_turn_exit = False
    turn_completed_seen = False
    events_seen = 0
    streamed_chars = 0
    latest_usage: dict[str, Any] | None = None
    last_error_msg: str | None = None
    active_extensions_used = 0
    effective_timeout_sec = max(1, int(timeout_sec))
    eff_grace_sec = max(0, int(active_grace_sec))
    eff_max_ext = max(0, int(max_active_extensions))
    eff_grace_window = max(0.0, float(active_grace_window_sec))
    if idle_timeout_sec > 0:
        eff_grace_window = min(eff_grace_window, float(idle_timeout_sec) * 0.5)

    def _mark_turn_completed() -> None:
        nonlocal turn_completed_seen
        with activity_lock:
            turn_completed_seen = True

    def _is_turn_completed() -> bool:
        with activity_lock:
            return turn_completed_seen

    def _watchdog() -> None:
        nonlocal timed_out, timeout_reason, cancelled, graceful_post_turn_exit
        nonlocal active_extensions_used, effective_timeout_sec
        deadline = start_ts + max(1, timeout_sec)
        ready_to_exit_ts: float | None = None
        while proc.poll() is None:
            if cancel_flag.exists():
                cancelled = True
                _terminate_process_group(proc)
                return
            now = time.monotonic()
            if post_turn_grace_sec > 0 and _is_turn_completed():
                if ready_to_exit_ts is None:
                    if _has_valid_result_file(result_file_path):
                        ready_to_exit_ts = now
                elif (now - ready_to_exit_ts) >= post_turn_grace_sec:
                    graceful_post_turn_exit = True
                    _terminate_process_group(proc)
                    return
            idle_elapsed = now - _get_last_activity()
            if idle_timeout_sec > 0 and idle_elapsed >= idle_timeout_sec:
                timed_out = True
                timeout_reason = "idle"
                _terminate_process_group(proc)
                return
            if now >= deadline:
                if (
                    eff_grace_sec > 0
                    and eff_max_ext > 0
                    and active_extensions_used < eff_max_ext
                    and idle_elapsed <= eff_grace_window
                ):
                    active_extensions_used += 1
                    deadline = now + eff_grace_sec
                    effective_timeout_sec += eff_grace_sec
                    store.append_event(
                        task_id,
                        phase="execute",
                        agent="Orchestrator (Watchdog)",
                        event_type="codex_timeout_extended",
                        message=(
                            f"Codex CLI still actively streaming (last event {idle_elapsed:.1f}s ago); "
                            f"extended wall-clock deadline by +{eff_grace_sec}s "
                            f"(extension {active_extensions_used}/{eff_max_ext}, new limit {effective_timeout_sec}s)."
                        ),
                        details={
                            "round": repair_round,
                            "attempt": attempt,
                            "extension": active_extensions_used,
                            "max_extensions": eff_max_ext,
                            "added_sec": eff_grace_sec,
                            "effective_timeout_sec": effective_timeout_sec,
                            "idle_since_sec": round(idle_elapsed, 1),
                        },
                    )
                else:
                    timed_out = True
                    timeout_reason = "wall_clock"
                    _terminate_process_group(proc)
                    return
            time.sleep(0.2)

    watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
    watchdog_thread.start()

    try:
        with raw_jsonl_path.open("w", encoding="utf-8") as log_fh:
            if proc.stdout is not None:
                for raw_line in proc.stdout:
                    _touch_activity()
                    log_fh.write(raw_line)
                    log_fh.flush()
                    streamed_chars += len(raw_line)
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    try:
                        ev = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    events_seen += 1

                    usage = _extract_usage(ev)
                    if usage:
                        latest_usage, delta_usage = _accumulate_attempt_usage(latest_usage, usage)
                        if on_token_usage and any(
                            delta_usage.get(k, 0) > 0 for k in ("input_tokens", "output_tokens", "total_tokens")
                        ):
                            on_token_usage(delta_usage)

                    if ev.get("type") == "turn.completed":
                        _mark_turn_completed()

                    summary = _summarize_jsonl_event(ev)
                    if summary:
                        ev_type, ev_msg = summary
                        if ev_type == "codex_error":
                            last_error_msg = ev_msg
                        store.append_event(
                            task_id,
                            phase="execute",
                            agent="Codex CLI (Builder)",
                            event_type=ev_type,
                            message=ev_msg,
                            details={"round": repair_round, "attempt": attempt, "model": model},
                        )

        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        timed_out = True
        timeout_reason = timeout_reason or "wall_clock"
        _terminate_process_group(proc)
        proc.wait(timeout=5)
    finally:
        watchdog_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if proc.stderr is not None:
            try:
                proc.stderr.close()
            except OSError:
                pass
        store.clear_run_pid(task_id)

    duration = round(time.monotonic() - start_ts, 2)
    idle_since = round(max(0.0, time.monotonic() - _get_last_activity()), 1)
    exit_code = int(proc.returncode if proc.returncode is not None else -1)
    stderr_tail = _format_stderr_tail(stderr_chunks)

    structured_result: dict[str, Any] | None = None
    if result_file_path.is_file():
        try:
            raw_res = result_file_path.read_text(encoding="utf-8").strip()
            if raw_res:
                parsed = json.loads(raw_res)
                if isinstance(parsed, dict):
                    structured_result = parsed
        except (OSError, json.JSONDecodeError):
            structured_result = None

    # Save/update canonical RESULT.json snapshot
    effective_exit_code = 0 if (graceful_post_turn_exit and structured_result is not None and not cancelled and not timed_out) else exit_code
    if graceful_post_turn_exit and structured_result is not None and not cancelled and not timed_out:
        store.append_event(
            task_id,
            phase="execute",
            agent="Codex CLI (Builder)",
            event_type="codex_graceful_exit",
            message=(
                f"Codex CLI lingered after turn.completed with output saved; "
                f"terminated process group gracefully after {post_turn_grace_sec}s grace period."
            ),
            details={
                "round": repair_round,
                "attempt": attempt,
                "raw_exit_code": exit_code,
                "post_turn_grace_sec": post_turn_grace_sec,
            },
        )
    estimated_usage: dict[str, Any] | None = None
    if latest_usage is None and (events_seen > 0 or streamed_chars > 0):
        est_in = max(1, len(prompt) // 4)
        est_out = max(1, streamed_chars // 4)
        estimated_usage = {
            "input_tokens": est_in,
            "cached_input_tokens": 0,
            "output_tokens": est_out,
            "total_tokens": est_in + est_out,
            "estimated": True,
            "source": "char_estimate",
        }

    result_doc = {
        "round": repair_round,
        "attempt": attempt,
        "model": model,
        "exit_code": effective_exit_code,
        "timed_out": timed_out,
        "timeout_reason": timeout_reason,
        "timeout_sec": timeout_sec,
        "effective_timeout_sec": effective_timeout_sec,
        "active_extensions_used": active_extensions_used,
        "idle_timeout_sec": idle_timeout_sec,
        "graceful_post_turn_exit": graceful_post_turn_exit,
        "cancelled": cancelled,
        "duration_sec": duration,
        "token_usage": latest_usage,
        "estimated_token_usage": estimated_usage,
        "raw_jsonl_path": str(raw_jsonl_path),
        "result_file_path": str(result_file_path),
        "codex_result": structured_result,
        "stderr_tail": stderr_tail[:2000],
    }
    atomic_write_json(tdir / "RESULT.json", result_doc)

    if cancelled:
        return CodexRunOutcome(
            ok=False,
            exit_code=exit_code,
            timed_out=False,
            cancelled=True,
            failure_category="cancelled",
            error_message="Codex execution was cancelled by user.",
            duration_sec=duration,
            structured_result=structured_result,
            token_usage=latest_usage,
            events_seen=events_seen,
            raw_jsonl_path=str(raw_jsonl_path),
            result_file_path=str(result_file_path),
            stderr_tail=stderr_tail,
            attempt=attempt,
            active_extensions_used=active_extensions_used,
            effective_timeout_sec=effective_timeout_sec,
            estimated_token_usage=estimated_usage,
        )

    if timed_out:
        if timeout_reason == "idle":
            err_msg = (
                f"Codex execution timed out after {idle_timeout_sec}s with no output events "
                f"(idle timeout; total elapsed {duration}s)."
            )
        else:
            ext_note = f", {active_extensions_used} active extension(s)" if active_extensions_used else ""
            err_msg = (
                f"Codex execution timed out after {effective_timeout_sec}s total limit "
                f"(last event {idle_since}s ago{ext_note})."
            )
        return CodexRunOutcome(
            ok=False,
            exit_code=exit_code,
            timed_out=True,
            cancelled=False,
            failure_category="timeout",
            error_message=err_msg,
            duration_sec=duration,
            structured_result=structured_result,
            token_usage=latest_usage,
            events_seen=events_seen,
            raw_jsonl_path=str(raw_jsonl_path),
            result_file_path=str(result_file_path),
            stderr_tail=stderr_tail,
            attempt=attempt,
            timeout_reason=timeout_reason,
            active_extensions_used=active_extensions_used,
            effective_timeout_sec=effective_timeout_sec,
            estimated_token_usage=estimated_usage,
        )

    if effective_exit_code != 0:
        err = last_error_msg or stderr_tail or f"Codex CLI exited abnormally with code {effective_exit_code}"
        return CodexRunOutcome(
            ok=False,
            exit_code=effective_exit_code,
            timed_out=False,
            cancelled=False,
            failure_category="execution_failure",
            error_message=err[:1000],
            duration_sec=duration,
            structured_result=structured_result,
            token_usage=latest_usage,
            events_seen=events_seen,
            raw_jsonl_path=str(raw_jsonl_path),
            result_file_path=str(result_file_path),
            stderr_tail=stderr_tail,
            attempt=attempt,
            active_extensions_used=active_extensions_used,
            effective_timeout_sec=effective_timeout_sec,
            estimated_token_usage=estimated_usage,
        )

    if structured_result and structured_result.get("status") in ("failed", "blocked"):
        st_val = str(structured_result.get("status"))
        return CodexRunOutcome(
            ok=False,
            exit_code=exit_code,
            timed_out=False,
            cancelled=False,
            failure_category="builder_blocked" if st_val == "blocked" else "execution_failure",
            error_message=str(structured_result.get("summary") or f"Codex reported status={st_val}"),
            duration_sec=duration,
            structured_result=structured_result,
            token_usage=latest_usage,
            events_seen=events_seen,
            raw_jsonl_path=str(raw_jsonl_path),
            result_file_path=str(result_file_path),
            stderr_tail=stderr_tail,
            attempt=attempt,
            active_extensions_used=active_extensions_used,
            effective_timeout_sec=effective_timeout_sec,
            estimated_token_usage=estimated_usage,
        )

    return CodexRunOutcome(
        ok=True,
        exit_code=0,
        timed_out=False,
        cancelled=False,
        failure_category=None,
        error_message=None,
        duration_sec=duration,
        structured_result=structured_result,
        token_usage=latest_usage,
        events_seen=events_seen,
        raw_jsonl_path=str(raw_jsonl_path),
        result_file_path=str(result_file_path),
        stderr_tail=stderr_tail,
        attempt=attempt,
        active_extensions_used=active_extensions_used,
        effective_timeout_sec=effective_timeout_sec,
        estimated_token_usage=estimated_usage,
    )
