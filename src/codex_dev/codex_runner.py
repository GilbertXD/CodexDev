"""Non-interactive Codex CLI (`codex exec`) runner with JSONL streaming and timeout control."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import signal
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import ENFORCED_SANDBOX_MODE, get_codex_home, validate_sandbox_mode
from .state import TaskStore, atomic_write_json, atomic_write_text, utc_now


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


def _seed_fresh_models_cache(src_path: Path, dst_path: Path) -> None:
    """Copy local models_cache.json into isolated CODEX_HOME with a fresh fetched_at timestamp.

    Prevents offline Codex CLI sessions from blocking on startup remote model-refresh timeouts
    while keeping the user's original ~/.codex/models_cache.json strictly read-only.
    """
    if not src_path.is_file():
        if dst_path.is_symlink() or dst_path.is_file():
            try:
                dst_path.unlink()
            except OSError:
                pass
        return

    # Always remove legacy symlinks before writing so we never write through to ~/.codex/
    if dst_path.is_symlink():
        try:
            dst_path.unlink()
        except OSError:
            pass

    try:
        raw = src_path.read_text(encoding="utf-8")
    except OSError:
        return

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        atomic_write_text(dst_path, raw)
        return

    if isinstance(parsed, dict):
        parsed["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        atomic_write_json(dst_path, parsed)
    else:
        atomic_write_text(dst_path, raw)


def prepare_isolated_codex_home(
    codex_dev_home: Path,
    source_codex_home: Path | None = None,
) -> Path:
    """Provision a clean, plugin/skill-free CODEX_HOME under ~/.codex-dev/codex_home.

    Symlinks auth.json read-only, seeds a fresh local copy of models_cache.json
    (never modifying ~/.codex), and writes a minimal config.toml with no third-party
    plugins, skills, MCP servers, AGENTS.md, or desktop notify hooks.
    """
    isolated_dir = (codex_dev_home / "codex_home").resolve()
    isolated_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(isolated_dir, 0o700)
    except OSError:
        pass

    src_home = (source_codex_home or get_codex_home()).resolve()
    if src_home != isolated_dir:
        _sync_symlink(isolated_dir / "auth.json", src_home / "auth.json")
        _seed_fresh_models_cache(src_home / "models_cache.json", isolated_dir / "models_cache.json")

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


ENV_ERROR_TYPES = frozenset({"permission_denied", "missing_dependency", "environment_error"})

_PERMISSION_SIG_RE = re.compile(
    r"(?:"
    r"\boperation not permitted\b"
    r"|\bEPERM\b"
    r"|\bEACCES\b"
    r"|\bread-only file system\b"
    r"|\bEROFS\b"
    r"|\bsandbox-exec\b"
    r"|\blandlock\b"
    r"|\brejected by execution policy\b"
    r"|\bpermission denied\b"
    r")",
    re.IGNORECASE,
)

_DEPENDENCY_SIG_RE = re.compile(
    r"(?:"
    r"\bmissing go\.sum entry\b"
    r"|\bcannot load module\b"
    r"|\bno required module provides package\b"
    r"|\bModuleNotFoundError:\s*No module named\b"
    r"|\bCannot find module\s+['\"][^./]"
    r"|\bERR_MODULE_NOT_FOUND\b"
    r"|\bdial tcp(?:\s+\S+)?:\s*lookup\b"
    r"|\bproxyconnect tcp\b"
    r"|\btemporary failure in name resolution\b"
    r"|\bENOTFOUND\b"
    r"|\bEAI_AGAIN\b"
    r"|\bnetwork is unreachable\b"
    r")",
    re.IGNORECASE,
)

_ENV_TOOL_SIG_RE = re.compile(
    r"(?:"
    r"\bcommand not found\b"
    r"|\bexecutable file not found in \$PATH\b"
    r"|\bCodex binary not found\b"
    r")",
    re.IGNORECASE,
)


def _match_env_signature_kind(text: str) -> str | None:
    if not text or not text.strip():
        return None
    if _PERMISSION_SIG_RE.search(text):
        return "permission_denied"
    if _DEPENDENCY_SIG_RE.search(text):
        return "missing_dependency"
    if _ENV_TOOL_SIG_RE.search(text):
        return "environment_error"
    return None


def classify_environment_exception(outcome: CodexRunOutcome) -> dict[str, Any] | None:
    """Classify whether a Builder run failed due to sandbox permission, dependency, or environment issues.

    Strictly separates environment/sandbox/dependency blockers from normal code/test/compile failures.
    """
    if outcome.cancelled or outcome.timed_out:
        return None

    sr = outcome.structured_result if isinstance(outcome.structured_result, dict) else None
    status = str((sr or {}).get("status") or "").strip().lower()
    err_type = str((sr or {}).get("error_type") or "none").strip().lower()
    err_detail = str((sr or {}).get("error_detail") or "").strip()
    summary = str((sr or {}).get("summary") or "").strip()
    failed_cmds = [
        {
            "command": str(c.get("command") or ""),
            "exit_code": int(c.get("exit_code", 0) or 0),
            "summary": str(c.get("summary") or ""),
        }
        for c in ((sr or {}).get("commands_run") or [])
        if isinstance(c, dict) and int(c.get("exit_code", 0) or 0) != 0
    ]

    # 1) Explicit structured environment error reported by Builder
    if err_type in ENV_ERROR_TYPES and (not outcome.ok or status in {"blocked", "failed", "partial"}):
        detail = err_detail or summary or outcome.error_message or f"Builder reported {err_type}."
        return {
            "kind": err_type,
            "detail": detail[:800],
            "source": "structured_result",
            "failed_commands": failed_cmds[:10],
        }

    if status == "blocked" and err_type != "code_error":
        combined_blocked = "\n".join(
            part
            for part in (
                err_detail,
                summary,
                *(c["summary"] for c in failed_cmds),
                outcome.stderr_tail,
            )
            if part
        )
        inferred = _match_env_signature_kind(combined_blocked) or "environment_error"
        detail = err_detail or summary or outcome.error_message or "Builder blocked by environment constraint."
        return {
            "kind": inferred,
            "detail": detail[:800],
            "source": "structured_blocked",
            "failed_commands": failed_cmds[:10],
        }

    # 2) Respect explicit code_error or clean completion
    if sr is not None and err_type == "code_error" and status != "blocked":
        return None
    if outcome.ok and status in {"completed", "ready_for_verify"}:
        return None

    # 3) High-confidence deterministic CLI / command environment signatures on failed runs
    if not outcome.ok:
        if any(c["exit_code"] == 127 for c in failed_cmds):
            cmd_127 = next(c for c in failed_cmds if c["exit_code"] == 127)
            detail = err_detail or cmd_127["summary"] or f"Command not found (exit=127): {cmd_127['command']}"
            return {
                "kind": "environment_error",
                "detail": detail[:800],
                "source": "command_exit_127",
                "failed_commands": failed_cmds[:10],
            }

        combined = "\n".join(
            part
            for part in (
                err_detail,
                outcome.error_message or "",
                outcome.stderr_tail or "",
                summary,
                *(f"{c['command']}: {c['summary']}" for c in failed_cmds),
            )
            if part
        )
        sig_kind = _match_env_signature_kind(combined)
        if sig_kind is not None:
            detail = err_detail or summary or outcome.error_message or outcome.stderr_tail or f"Detected {sig_kind}."
            return {
                "kind": sig_kind,
                "detail": detail.strip()[:800],
                "source": "signature_match",
                "failed_commands": failed_cmds[:10],
            }

    return None


def resolve_builder_writable_dirs(
    task_dir: Path,
    worktree_path: Path,
    builder_add_dirs: list[str] | None = None,
) -> tuple[dict[str, str], list[str], list[str]]:
    """Provision task-scoped writable cache/temp directories and return (default_env, add_dirs, writable_dirs)."""
    gocache_dir = (task_dir / "gocache").resolve()
    gotmp_dir = (task_dir / "gotmp").resolve()
    tmp_dir = (task_dir / "tmp").resolve()
    gocache_dir.mkdir(parents=True, exist_ok=True)
    gotmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    default_env = {
        "GOCACHE": str(gocache_dir),
        "GOTMPDIR": str(gotmp_dir),
        "TMPDIR": str(tmp_dir),
    }
    add_dirs: list[str] = [str(gocache_dir), str(gotmp_dir), str(tmp_dir)]
    if builder_add_dirs:
        for d in builder_add_dirs:
            if isinstance(d, str) and d.strip():
                resolved_d = str(Path(d).expanduser().resolve())
                if resolved_d not in add_dirs:
                    add_dirs.append(resolved_d)

    resolved_wt = str(worktree_path.resolve())
    writable_dirs = [resolved_wt, *(d for d in add_dirs if d != resolved_wt)]
    return default_env, add_dirs, writable_dirs


def build_codex_prompt(
    *,
    task_name: str,
    plan_text: str,
    feedback_text: str | None,
    repair_round: int,
    worktree_path: Path,
    writable_dirs: list[str] | None = None,
    builder_checks: list[str] | None = None,
    builder_network_access: bool = False,
    env_retry_feedback: str | None = None,
) -> str:
    """Build concise, language-agnostic Builder instructions for Codex CLI."""
    eff_writable = writable_dirs or [str(worktree_path)]
    writable_desc = ", ".join(f"`{d}`" for d in eff_writable)
    network_desc = "enabled" if builder_network_access else "disabled (offline)"

    parts = [
        f"You are the Builder agent executing task '{task_name}' inside an isolated Git worktree.",
        "",
        "=== SANDBOX & PERMISSIONS ===",
        f"- Sandbox mode: `{ENFORCED_SANDBOX_MODE}` (immutable; permission expansion is prohibited).",
        f"- Working directory: `{worktree_path}`",
        f"- Writable directories: {writable_desc}",
        f"- Network access: {network_desc}",
        "- Environment & dependencies: Prepared upstream by the upper layer.",
        "=== END SANDBOX & PERMISSIONS ===",
        "",
        "CORE CONSTRAINTS:",
        "1. Complete the FULL implementation strictly according to the immutable PLAN across all declared Target Files; do NOT expand task scope or exit with partial/zero edits when unblocked.",
        "2. Environment and dependencies are prepared upstream. Do NOT request expanded permissions, attempt to bypass sandbox boundaries, or loop on blocked workarounds.",
        "3. If you encounter a permission denial or missing dependency/tool that blocks required edits or specified Builder Checks, stop retrying the blocked operation and report it truthfully (`status: \"blocked\"`, `error_type: \"permission_denied\" | \"missing_dependency\" | \"environment_error\"`, with the exact command and error in `error_detail`).",
        "4. Execute the specified Builder Checks (if any); full acceptance verification is handled independently on the host by the Verifier. Return `status: \"ready_for_verify\"` or `status: \"completed\"` with `error_type: \"none\"` when finished.",
        "5. Do NOT weaken, delete, or skip tests, linters, or type checks, and do NOT run `git commit`, `git push`, `git merge`, or destructive git clean commands.",
        "6. Return your final response strictly conforming to the provided JSON output schema.",
        "",
        "=== IMMUTABLE PLAN CONTRACT ===",
        plan_text.strip(),
        "=== END PLAN CONTRACT ===",
    ]
    if builder_checks:
        parts.extend(
            [
                "",
                "=== BUILDER CHECKS ===",
                *(f"- `{cmd}`" for cmd in builder_checks),
                "=== END BUILDER CHECKS ===",
            ]
        )
    if env_retry_feedback and env_retry_feedback.strip():
        parts.extend(
            [
                "",
                "=== ENVIRONMENT RETRY CONTEXT (MAX 1 RETRY — PERMISSIONS UNCHANGED) ===",
                env_retry_feedback.strip(),
                "=== END ENVIRONMENT RETRY CONTEXT ===",
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

DEFAULT_PREFLIGHT_URL = "https://chatgpt.com"

UPSTREAM_TRANSPORT_ERROR_PATTERNS = (
    re.compile(r"workspace routing discovery failed", re.IGNORECASE),
    re.compile(r"Falling back from WebSockets to HTTPS transport", re.IGNORECASE),
    re.compile(r"Reconnecting\.\.\.\s*\d+\s*/\s*\d+", re.IGNORECASE),
    re.compile(r"SSL_ERROR_SYSCALL", re.IGNORECASE),
    re.compile(r"unexpected EOF while reading", re.IGNORECASE),
    re.compile(r"connection reset by peer", re.IGNORECASE),
    re.compile(r"failed to connect to", re.IGNORECASE),
    re.compile(r"error sending request for url", re.IGNORECASE),
    re.compile(r"dns error", re.IGNORECASE),
    re.compile(r"network is unreachable", re.IGNORECASE),
    re.compile(r"timed out while connecting", re.IGNORECASE),
)

_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def is_upstream_transport_error(text: str | None) -> bool:
    """Return True if an error line matches known Codex CLI upstream transport/connectivity failures."""
    if not text:
        return False
    return any(pat.search(text) for pat in UPSTREAM_TRANSPORT_ERROR_PATTERNS)


def resolve_preflight_target_url(
    builder_cfg: dict[str, Any] | None = None,
    builder_env: dict[str, str] | None = None,
) -> str:
    """Resolve the upstream Codex endpoint URL to probe before launching Builder."""
    if isinstance(builder_cfg, dict):
        explicit = str(builder_cfg.get("preflight_url") or "").strip()
        if explicit:
            return explicit
    if isinstance(builder_env, dict):
        from_benv = str(builder_env.get("OPENAI_BASE_URL") or "").strip()
        if from_benv:
            return from_benv
    from_env = str(os.environ.get("OPENAI_BASE_URL") or "").strip()
    if from_env:
        return from_env
    return DEFAULT_PREFLIGHT_URL


def _host_bypassed_by_no_proxy(host: str, no_proxy: str) -> bool:
    host_l = host.lower().strip(".")
    if not no_proxy:
        return False
    for raw in no_proxy.split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        # Strip optional :port suffix
        if ":" in entry and not entry.startswith("["):
            entry = entry.split(":", 1)[0]
        entry = entry.lstrip(".")
        if host_l == entry or host_l.endswith("." + entry):
            return True
    return False


def _select_proxy_for_url(scheme: str, host: str, env: dict[str, str]) -> str | None:
    if host.lower() in {"localhost", "127.0.0.1", "::1"}:
        return None
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
    if _host_bypassed_by_no_proxy(host, no_proxy):
        return None
    keys = (
        ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
        if scheme == "https"
        else ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    )
    for key in keys:
        val = str(env.get(key) or "").strip()
        if val:
            return val
    return None


def _has_fake_ip(resolved_ips: list[str]) -> bool:
    for ip_str in resolved_ips:
        try:
            addr = ipaddress.ip_address(ip_str)
            if isinstance(addr, ipaddress.IPv4Address) and addr in _FAKE_IP_NET:
                return True
        except ValueError:
            continue
    return False


def check_upstream_connectivity(
    target_url: str,
    *,
    timeout_sec: float = 5.0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Perform a fast, host-side TCP/TLS (or proxy HTTP) reachability probe before starting Builder.

    Runs strictly on the host side so Builder's immutable `workspace-write` sandbox (`network_access: false`)
    is never weakened.
    """
    start_ts = time.monotonic()
    eff_timeout = max(0.5, float(timeout_sec or 5.0))
    eff_env = dict(os.environ) if env is None else dict(env)

    parsed = urllib.parse.urlparse(target_url)
    scheme = (parsed.scheme or "").lower()
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)

    if scheme not in {"http", "https"} or not host:
        return {
            "ok": False,
            "checked_at": utc_now(),
            "target_url": target_url,
            "host": host,
            "port": port,
            "scheme": scheme,
            "resolved_ips": [],
            "proxy_used": None,
            "timeout_sec": eff_timeout,
            "duration_sec": round(time.monotonic() - start_ts, 2),
            "error": f"Invalid preflight URL: {target_url!r} (expected http:// or https:// with hostname)",
            "hint": "Check builder.preflight_url or OPENAI_BASE_URL configuration.",
        }

    proxy_url = _select_proxy_for_url(scheme, host, eff_env)
    resolved_ips: list[str] = []
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        seen: set[str] = set()
        for info in infos:
            ip = str(info[4][0])
            if ip not in seen:
                seen.add(ip)
                resolved_ips.append(ip)
    except OSError as exc:
        if not proxy_url:
            return {
                "ok": False,
                "checked_at": utc_now(),
                "target_url": target_url,
                "host": host,
                "port": port,
                "scheme": scheme,
                "resolved_ips": [],
                "proxy_used": None,
                "timeout_sec": eff_timeout,
                "duration_sec": round(time.monotonic() - start_ts, 2),
                "error": f"DNS resolution failed for {host}: {exc}",
                "hint": "Check network connectivity, DNS, VPN/TUN proxy settings, or OPENAI_BASE_URL before resuming.",
            }

    err_msg: str | None = None
    if proxy_url and proxy_url.lower().startswith(("http://", "https://")):
        # Test end-to-end reachability through explicit HTTP/HTTPS proxy; any HTTP status proves transport works.
        proxies = {"http": proxy_url, "https": proxy_url}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        req = urllib.request.Request(target_url, method="HEAD", headers={"User-Agent": "codex-dev-preflight/1"})
        try:
            with opener.open(req, timeout=eff_timeout):
                pass
        except urllib.error.HTTPError:
            # HTTP 401/403/404/405 still confirms TLS + HTTP reachability to the upstream endpoint.
            pass
        except Exception as exc:
            err_msg = f"Proxy connectivity check via {proxy_url} failed: {exc}"
    else:
        # Direct or TUN-mode probe: TCP connect + TLS handshake (for https) to catch Fake-IP/TLS drops fast.
        raw_sock: socket.socket | None = None
        tls_sock: ssl.SSLSocket | None = None
        try:
            raw_sock = socket.create_connection((host, port), timeout=eff_timeout)
            raw_sock.settimeout(eff_timeout)
            if scheme == "https":
                ctx = ssl.create_default_context()
                tls_sock = ctx.wrap_socket(raw_sock, server_hostname=host)
                tls_sock.settimeout(eff_timeout)
                tls_sock.do_handshake()
        except Exception as exc:
            err_msg = f"{'TLS' if scheme == 'https' else 'TCP'} connection to {host}:{port} failed: {exc}"
        finally:
            for s in (tls_sock, raw_sock):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    duration = round(time.monotonic() - start_ts, 2)
    if err_msg is None:
        return {
            "ok": True,
            "checked_at": utc_now(),
            "target_url": target_url,
            "host": host,
            "port": port,
            "scheme": scheme,
            "resolved_ips": resolved_ips[:4],
            "proxy_used": proxy_url,
            "timeout_sec": eff_timeout,
            "duration_sec": duration,
            "error": None,
            "hint": None,
        }

    if _has_fake_ip(resolved_ips):
        hint = (
            f"Resolved IP ({', '.join(resolved_ips[:2])}) is in the local TUN/Fake-IP range (198.18.0.0/15); "
            "switch proxy node/route or disable broken TUN proxy, then resume `codex-dev run-round`."
        )
    elif proxy_url:
        hint = f"Check proxy server ({proxy_url}) health or switch proxy route, then resume `codex-dev run-round`."
    else:
        hint = "Check network connectivity, VPN/proxy settings, or OPENAI_BASE_URL, then resume `codex-dev run-round`."

    return {
        "ok": False,
        "checked_at": utc_now(),
        "target_url": target_url,
        "host": host,
        "port": port,
        "scheme": scheme,
        "resolved_ips": resolved_ips[:4],
        "proxy_used": proxy_url,
        "timeout_sec": eff_timeout,
        "duration_sec": duration,
        "error": err_msg[:500],
        "hint": hint,
    }


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
    max_consecutive_transport_errors: int = 5,
    on_token_usage: Callable[[dict[str, Any]], None] | None = None,
) -> CodexRunOutcome:
    """Execute `codex exec` non-interactively, streaming JSONL events into the task store."""
    enforced_sandbox = validate_sandbox_mode(sandbox_mode)
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

    default_env, add_dirs, writable_dirs = resolve_builder_writable_dirs(
        tdir,
        worktree_path,
        builder_add_dirs,
    )
    injected_env: dict[str, str] = dict(default_env)
    if builder_env:
        for k, v in builder_env.items():
            if isinstance(k, str) and k.strip() and v is not None:
                injected_env[k.strip()] = str(v)

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
        enforced_sandbox,
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
    if builder_network_access:
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

    sandbox_boundary = {
        "sandbox_mode": enforced_sandbox,
        "network_access": bool(builder_network_access),
        "writable_dirs": writable_dirs,
        "builder_isolation": eff_isolation,
    }

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
            f"Starting Codex CLI (model={model}, sandbox={enforced_sandbox}, "
            f"round={repair_round}, attempt={attempt}, timeout={timeout_sec}s"
            + (f", idle_timeout={idle_timeout_sec}s" if idle_timeout_sec > 0 else "")
            + ")"
        ),
        details={
            "model": model,
            "sandbox": enforced_sandbox,
            "sandbox_boundary": sandbox_boundary,
            "builder_isolation": eff_isolation,
            "builder_network_access": bool(builder_network_access),
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
    has_model_progress = False
    consecutive_transport_errors = 0
    transport_fail_fast = False
    transport_fail_msg: str | None = None
    eff_max_transport_errors = max(0, int(max_consecutive_transport_errors))
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
                        has_model_progress = True
                        consecutive_transport_errors = 0
                        latest_usage, delta_usage = _accumulate_attempt_usage(latest_usage, usage)
                        if on_token_usage and any(
                            delta_usage.get(k, 0) > 0 for k in ("input_tokens", "output_tokens", "total_tokens")
                        ):
                            on_token_usage(delta_usage)

                    if ev.get("type") == "turn.completed":
                        has_model_progress = True
                        consecutive_transport_errors = 0
                        _mark_turn_completed()

                    ev_item = ev.get("item")
                    if isinstance(ev_item, dict) and str(ev_item.get("type") or "") in {
                        "agent_message",
                        "file_change",
                        "command_execution",
                        "reasoning",
                    }:
                        has_model_progress = True
                        consecutive_transport_errors = 0

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
                        if ev_type == "codex_error" and not has_model_progress and is_upstream_transport_error(ev_msg):
                            consecutive_transport_errors += 1
                            transport_fail_msg = ev_msg
                            if eff_max_transport_errors > 0 and consecutive_transport_errors >= eff_max_transport_errors:
                                transport_fail_fast = True
                                store.append_event(
                                    task_id,
                                    phase="execute",
                                    agent="Orchestrator (Watchdog)",
                                    event_type="codex_transport_fail_fast",
                                    message=(
                                        f"Aborting Codex CLI early after {consecutive_transport_errors} consecutive "
                                        f"upstream transport errors with no model progress: {ev_msg[:200]}"
                                    ),
                                    details={
                                        "round": repair_round,
                                        "attempt": attempt,
                                        "consecutive_transport_errors": consecutive_transport_errors,
                                        "threshold": eff_max_transport_errors,
                                    },
                                )
                                _terminate_process_group(proc)
                                break
                        elif ev_type == "codex_error":
                            consecutive_transport_errors = 0

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
        "sandbox_boundary": sandbox_boundary,
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

    if transport_fail_fast:
        err = (
            f"Codex CLI aborted early after {consecutive_transport_errors} consecutive upstream transport "
            f"error(s) with no model progress: {transport_fail_msg or last_error_msg or 'upstream connection failed'}"
        )
        return CodexRunOutcome(
            ok=False,
            exit_code=effective_exit_code,
            timed_out=False,
            cancelled=False,
            failure_category="upstream_connectivity_failure",
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

    if effective_exit_code != 0:
        err = last_error_msg or stderr_tail or f"Codex CLI exited abnormally with code {effective_exit_code}"
        fail_cat = (
            "upstream_connectivity_failure"
            if (not has_model_progress and (is_upstream_transport_error(last_error_msg) or is_upstream_transport_error(stderr_tail)))
            else "execution_failure"
        )
        return CodexRunOutcome(
            ok=False,
            exit_code=effective_exit_code,
            timed_out=False,
            cancelled=False,
            failure_category=fail_cat,
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

    if structured_result and (
        structured_result.get("status") in ("failed", "blocked")
        or str(structured_result.get("error_type") or "").strip().lower() in ENV_ERROR_TYPES
    ):
        st_val = str(structured_result.get("status") or "blocked")
        err_type_val = str(structured_result.get("error_type") or "none").strip().lower()
        err_detail_val = str(structured_result.get("error_detail") or "").strip()
        summary_val = str(structured_result.get("summary") or "").strip()
        err_msg = err_detail_val or summary_val or f"Codex reported status={st_val}"
        fail_cat = (
            "environment_failure"
            if err_type_val in ENV_ERROR_TYPES
            else ("builder_blocked" if st_val == "blocked" else "execution_failure")
        )
        return CodexRunOutcome(
            ok=False,
            exit_code=exit_code,
            timed_out=False,
            cancelled=False,
            failure_category=fail_cat,
            error_message=err_msg,
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
