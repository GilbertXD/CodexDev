"""Stack-agnostic independent verification, diff audit, and anti-weakening checks."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .contract import verify_contract_integrity
from .state import atomic_write_json, read_json, utc_now
from .worktree import inspect_worktree_changes

TEST_FILE_PATTERNS = (
    re.compile(r"(^|/)tests?/", re.I),
    re.compile(r"(^|/)__tests__/", re.I),
    re.compile(r"test_[^/]+\.py$", re.I),
    re.compile(r"_test\.(py|go|rs|ts|tsx|js|jsx)$", re.I),
    re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|mjs|cjs)$", re.I),
)

CONFIG_FILE_PATTERNS = (
    re.compile(r"(^|/)(pytest\.ini|setup\.cfg|pyproject\.toml|tox\.ini)$"),
    re.compile(r"(^|/)(tsconfig.*\.json|package\.json|biome\.json)$"),
    re.compile(r"(^|/)(\.?eslint.*|ruff\.toml|\.flake8|\.mypy\.ini)$"),
)

WEAKENING_ADDITION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pytest_skip", re.compile(r"@pytest\.mark\.(skip|xfail)")),
    ("unittest_skip", re.compile(r"@unittest\.skip|self\.skipTest\(")),
    ("js_test_skip", re.compile(r"\b(it|test|describe)\.skip\(|\b(xit|xdescribe)\(")),
    ("go_test_skip", re.compile(r"\bt\.Skip(f|Now)?\(")),
    ("rust_test_ignore", re.compile(r"#\[ignore\]")),
    ("ts_ignore", re.compile(r"@ts-ignore|@ts-nocheck")),
    ("eslint_disable", re.compile(r"eslint-disable")),
    ("noqa_suppression", re.compile(r"#\s*noqa\b|#\s*type:\s*ignore\b")),
]

ASSERTION_LINE_RE = re.compile(r"\b(assert|expect|assertEqual|assertTrue|assertFalse|assertRaises|t\.Error|t\.Fatal)\b")

_GO_SKIP_RE = re.compile(r"^\s*---\s+SKIP:\s+(\S+)", re.MULTILINE)
_PY_UNITTEST_SKIP_LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+(?:\s*\([^)]+\))?)\s+\.\.\.\s+skipped\b", re.MULTILINE)
_PY_UNITTEST_SUMMARY_RE = re.compile(r"^(?:OK|FAILED)\s+\([^)]*\bskipped=(\d+)\b", re.MULTILINE)
_PYTEST_SKIP_LINE_RE = re.compile(r"^\s*([^\s]+::[^\s]+)\s+SKIPPED\b", re.MULTILINE)
_PYTEST_SUMMARY_RE = re.compile(r"(?:^=+[^\n]*?\b(\d+)\s+skipped\b[^\n]*?=+\s*$)|(?:^\s*(?:\d+\s+\w+,\s*)*(\d+)\s+skipped\b[^\n]*\bin\s+[\d.]+s)", re.MULTILINE)
_RUST_IGNORED_LINE_RE = re.compile(r"^\s*test\s+(\S+)\s+\.\.\.\s+ignored\s*$", re.MULTILINE)
_RUST_SUMMARY_RE = re.compile(r"^test result:\s+\w+\.\s+\d+\s+passed;\s+\d+\s+failed;\s+(\d+)\s+ignored;", re.MULTILINE)
_NODE_TEST_SKIP_SUMMARY_RE = re.compile(r"^#\s+skipped\s+(\d+)\s*$", re.MULTILINE)
_JS_TESTS_SUMMARY_RE = re.compile(r"^\s*Tests:\s+[^\n]*?\b(\d+)\s+skipped\b", re.MULTILINE)


def detect_skipped_tests(stdout: str, stderr: str = "") -> dict[str, Any]:
    """Parse test runner output across Go/Python/Node/Jest/Vitest/Rust for skipped tests.

    Returns {"skipped_count": int, "skipped_tests": list[str]} where skipped_tests
    holds up to 25 deduplicated sample test names.
    """
    combined = "\n".join(part for part in (stdout or "", stderr or "") if part)
    if not combined.strip():
        return {"skipped_count": 0, "skipped_tests": []}

    names: list[str] = []
    seen: set[str] = set()

    def _add_name(raw: str) -> None:
        cleaned = raw.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            names.append(cleaned)

    go_matches = _GO_SKIP_RE.findall(combined)
    for m in go_matches:
        _add_name(m)

    py_u_lines = _PY_UNITTEST_SKIP_LINE_RE.findall(combined)
    for m in py_u_lines:
        _add_name(m)
    py_u_summary = sum(int(m) for m in _PY_UNITTEST_SUMMARY_RE.findall(combined))

    pytest_lines = _PYTEST_SKIP_LINE_RE.findall(combined)
    for m in pytest_lines:
        _add_name(m)
    pytest_summary = sum(int(a or b) for a, b in _PYTEST_SUMMARY_RE.findall(combined))

    rust_lines = _RUST_IGNORED_LINE_RE.findall(combined)
    for m in rust_lines:
        _add_name(m)
    rust_summary = sum(int(m) for m in _RUST_SUMMARY_RE.findall(combined))

    node_summary = sum(int(m) for m in _NODE_TEST_SKIP_SUMMARY_RE.findall(combined))
    js_summary = sum(int(m) for m in _JS_TESTS_SUMMARY_RE.findall(combined))

    total = (
        len(go_matches)
        + max(len(py_u_lines), py_u_summary)
        + max(len(pytest_lines), pytest_summary)
        + max(len(rust_lines), rust_summary)
        + node_summary
        + js_summary
    )
    total = max(total, len(names))
    return {"skipped_count": total, "skipped_tests": names[:25]}


def is_test_file(path: str) -> bool:
    return any(p.search(path) for p in TEST_FILE_PATTERNS)


def is_config_file(path: str) -> bool:
    return any(p.search(path) for p in CONFIG_FILE_PATTERNS)


def _normalize_cmd_list(cmds: list[Any], default_kind: str = "custom", prefix: str = "check") -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for idx, item in enumerate(cmds, 1):
        if isinstance(item, str) and item.strip():
            out.append({"name": f"{prefix}-{idx}", "command": item.strip(), "kind": default_kind})
        elif isinstance(item, dict) and isinstance(item.get("command"), str) and item["command"].strip():
            out.append(
                {
                    "name": str(item.get("name") or f"{prefix}-{idx}"),
                    "command": item["command"].strip(),
                    "kind": str(item.get("kind") or default_kind),
                }
            )
    return out


def build_stage_env(config: dict[str, Any], stage: str) -> dict[str, str]:
    """Build host subprocess environment with configured verify/prepare overrides (e.g. GOPROXY)."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    verify_env = (config.get("verify") or {}).get("env")
    if isinstance(verify_env, dict):
        for k, v in verify_env.items():
            if isinstance(k, str) and k.strip() and v is not None:
                env[k.strip()] = str(v)
    if stage == "prepare":
        prep_env = (config.get("prepare") or {}).get("env")
        if isinstance(prep_env, dict):
            for k, v in prep_env.items():
                if isinstance(k, str) and k.strip() and v is not None:
                    env[k.strip()] = str(v)
    return env


def detect_prepare_commands(
    worktree_path: Path,
    diff_info: dict[str, Any],
    config: dict[str, Any],
    contract: dict[str, Any] | None = None,
    explicit_commands: list[str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Resolve non-sandbox dependency preparation commands (e.g. go mod tidy when go.mod changed)."""
    prep_cfg = config.get("prepare") or {}
    if not bool(prep_cfg.get("enabled", True)) and not explicit_commands:
        return ("disabled", [])

    if explicit_commands:
        return ("explicit", _normalize_cmd_list(explicit_commands, default_kind="prepare", prefix="prepare"))

    if contract and contract.get("prepare_commands"):
        norm = _normalize_cmd_list(contract["prepare_commands"], default_kind="prepare", prefix="prepare")
        if norm:
            return ("plan_contract", norm)

    if isinstance(prep_cfg, dict) and prep_cfg.get("commands"):
        norm = _normalize_cmd_list(prep_cfg["commands"], default_kind="prepare", prefix="prepare")
        if norm:
            return ("config", norm)

    detected: list[dict[str, str]] = []
    auto_go_tidy = bool(prep_cfg.get("auto_go_mod_tidy", True)) if isinstance(prep_cfg, dict) else True
    changed_files = set(diff_info.get("all_changed_files") or [])
    if auto_go_tidy:
        # Handle root go.mod or nested module go.mod files touched by the Builder
        go_mods = sorted(
            f for f in changed_files if (f == "go.mod" or f.endswith("/go.mod")) and (worktree_path / f).is_file()
        )
        for gm in go_mods:
            mod_dir = str(Path(gm).parent)
            if mod_dir in ("", "."):
                detected.append({"name": "go:mod-tidy", "command": "go mod tidy", "kind": "prepare"})
            else:
                detected.append(
                    {
                        "name": f"go:mod-tidy:{mod_dir}",
                        "command": f"cd {json.dumps(mod_dir)} && go mod tidy",
                        "kind": "prepare",
                    }
                )
    return ("auto_detected" if detected else "none", detected)


def run_dependency_prepare(
    *,
    task_dir: Path,
    worktree_path: Path,
    base_commit: str,
    config: dict[str, Any],
    explicit_commands: list[str] | None = None,
    timeout_sec: int | None = None,
) -> dict[str, Any]:
    """Run non-sandbox dependency preparation commands outside Codex's offline sandbox."""
    contract = read_json(task_dir / "contract.json")
    diff_info = inspect_worktree_changes(worktree_path, base_commit, include_diff=False)
    source, commands = detect_prepare_commands(
        worktree_path,
        diff_info,
        config,
        contract=contract,
        explicit_commands=explicit_commands,
    )

    prep_cfg = config.get("prepare") or {}
    eff_timeout = int(timeout_sec if timeout_sec is not None else prep_cfg.get("timeout_sec", 180))
    stage_env = build_stage_env(config, "prepare")

    results: list[dict[str, Any]] = []
    passed_count = 0
    failed_count = 0
    generated_allowed_files: list[str] = []

    for item in commands:
        cmd_str = item["command"]
        name = item["name"]
        t0 = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(
                cmd_str,
                cwd=str(worktree_path),
                shell=True,
                capture_output=True,
                text=True,
                timeout=eff_timeout,
                env=stage_env,
            )
            exit_code = int(proc.returncode)
            stdout_tail = proc.stdout[-4000:] if proc.stdout else ""
            stderr_tail = proc.stderr[-4000:] if proc.stderr else ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            stdout_tail = (
                exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            )[-2000:]
            stderr_tail = f"Prepare command timed out after {eff_timeout}s"

        duration = round(time.monotonic() - t0, 2)
        ok = (exit_code == 0) and not timed_out
        if ok:
            passed_count += 1
            if name == "go:mod-tidy":
                generated_allowed_files.append("go.sum")
            elif name.startswith("go:mod-tidy:"):
                sub = name.split("go:mod-tidy:", 1)[1]
                generated_allowed_files.append(f"{sub}/go.sum")
        else:
            failed_count += 1

        results.append(
            {
                "name": name,
                "kind": item["kind"],
                "command": cmd_str,
                "passed": ok,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_sec": duration,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            }
        )

    failure_reasons = [
        f"Prepare '{r['name']}' failed (exit={r['exit_code']}): {r['command']}"
        + (f" — {r['stderr_tail'].splitlines()[-1][:200]}" if r["stderr_tail"].strip() else "")
        for r in results
        if not r["passed"]
    ]

    report = {
        "prepared_at": utc_now(),
        "ran": len(results) > 0,
        "passed": failed_count == 0,
        "command_source": source,
        "commands_total": len(results),
        "commands_passed": passed_count,
        "commands_failed": failed_count,
        "checks": results,
        "generated_allowed_files": sorted(set(generated_allowed_files)),
        "failure_reasons": failure_reasons,
    }
    if results:
        atomic_write_json(task_dir / "prepare.json", report)
    return report


def detect_verification_commands(
    worktree_path: Path,
    config: dict[str, Any],
    contract: dict[str, Any] | None = None,
    explicit_commands: list[str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Resolve stack-agnostic verification commands in priority order.

    Priority:
      1. Explicit task-level commands (`--verify-cmd`)
      2. Immutable PLAN.md contract verify_commands
      3. Repository / global config `verify.commands`
      4. Auto-detected stack commands (Node/TS, Python, Go, Rust, Makefile)
    """
    if explicit_commands:
        return ("explicit", _normalize_cmd_list(explicit_commands, default_kind="custom", prefix="check"))

    if contract and contract.get("verify_commands"):
        norm = _normalize_cmd_list(contract["verify_commands"], default_kind="custom", prefix="check")
        if norm:
            return ("plan_contract", norm)

    cfg_verify = config.get("verify") or {}
    if isinstance(cfg_verify, dict) and cfg_verify.get("commands"):
        norm = _normalize_cmd_list(cfg_verify["commands"], default_kind="custom", prefix="check")
        if norm:
            return ("config", norm)

    # Auto-detect project stack without hardcoding a single language
    detected: list[dict[str, str]] = []
    pkg_json = worktree_path / "package.json"
    if pkg_json.is_file():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts") or {}
            if isinstance(scripts, dict):
                for script_name, kind in (
                    ("typecheck", "typecheck"),
                    ("lint", "lint"),
                    ("test", "test"),
                    ("build", "build"),
                ):
                    cmd_str = scripts.get(script_name)
                    if isinstance(cmd_str, str) and cmd_str.strip() and "no test specified" not in cmd_str:
                        detected.append(
                            {
                                "name": f"npm:{script_name}",
                                "command": f"npm run {script_name}",
                                "kind": kind,
                            }
                        )
        except (OSError, json.JSONDecodeError):
            pass

    if (worktree_path / "go.mod").is_file():
        detected.append({"name": "go:vet", "command": "go vet ./...", "kind": "lint"})
        detected.append({"name": "go:test", "command": "go test ./...", "kind": "test"})

    if (worktree_path / "Cargo.toml").is_file():
        detected.append({"name": "cargo:test", "command": "cargo test", "kind": "test"})

    has_python = (
        (worktree_path / "pyproject.toml").is_file()
        or (worktree_path / "setup.py").is_file()
        or any(worktree_path.glob("test_*.py"))
        or (worktree_path / "tests").is_dir()
    )
    if has_python:
        # Prefer pytest if installed and tests exist; fall back cleanly to unittest
        has_pytest = subprocess.run(
            ["python3", "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
        ).returncode == 0
        if has_pytest:
            detected.append({"name": "python:pytest", "command": "python3 -m pytest -q", "kind": "test"})
        else:
            detected.append(
                {
                    "name": "python:unittest",
                    "command": "python3 -m unittest discover -v",
                    "kind": "test",
                }
            )

    return ("auto_detected", detected)


def audit_diff_integrity(
    diff_info: dict[str, Any],
    contract: dict[str, Any] | None,
    extra_allowed_files: list[str] | None = None,
) -> dict[str, Any]:
    """Audit diff for deleted/weakened tests, config tampering, and out-of-scope edits."""
    warnings: list[dict[str, Any]] = []
    blocking_issues: list[dict[str, Any]] = []

    # 1. Deleted test files are suspicious and flagged prominently
    for df in diff_info.get("deleted") or []:
        if is_test_file(df):
            blocking_issues.append(
                {
                    "code": "deleted_test_file",
                    "file": df,
                    "message": f"Test file '{df}' was deleted; verify this was explicitly requested.",
                }
            )
        elif is_config_file(df):
            warnings.append(
                {
                    "code": "deleted_config_file",
                    "file": df,
                    "message": f"Configuration file '{df}' was deleted.",
                }
            )

    # 2. Scan unified diff hunks per file for weakening patterns and net assertion drops
    full_diff = str(diff_info.get("full_diff") or "")
    current_file: str | None = None
    added_asserts: dict[str, int] = {}
    removed_asserts: dict[str, int] = {}

    for line in full_diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            continue
        if not current_file:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            if is_test_file(current_file) or is_config_file(current_file):
                for code, pat in WEAKENING_ADDITION_PATTERNS:
                    if pat.search(content):
                        blocking_issues.append(
                            {
                                "code": f"weakening_{code}",
                                "file": current_file,
                                "message": f"Suspicious test/check weakening ({code}) added in '{current_file}': {content.strip()[:120]}",
                            }
                        )
            if is_test_file(current_file) and ASSERTION_LINE_RE.search(content):
                added_asserts[current_file] = added_asserts.get(current_file, 0) + 1
        elif line.startswith("-") and not line.startswith("---"):
            content = line[1:]
            if is_test_file(current_file) and ASSERTION_LINE_RE.search(content):
                removed_asserts[current_file] = removed_asserts.get(current_file, 0) + 1

    for fpath, rem_count in removed_asserts.items():
        add_count = added_asserts.get(fpath, 0)
        if rem_count > add_count:
            warnings.append(
                {
                    "code": "assertions_reduced",
                    "file": fpath,
                    "message": f"Test file '{fpath}' removed {rem_count} assertion lines while adding {add_count}; inspect carefully during review.",
                }
            )

    # 3. Config modifications warning
    for fpath in diff_info.get("modified") or []:
        if is_config_file(fpath):
            warnings.append(
                {
                    "code": "config_modified",
                    "file": fpath,
                    "message": f"Project config '{fpath}' was modified; confirm rules/checks were not weakened.",
                }
            )

    # 4. Out-of-scope file check if contract declared explicit target_files
    target_files = list((contract or {}).get("target_files") or [])
    if target_files:
        # Automatically treat sibling go.sum as expected when go.mod is targeted or go mod tidy ran
        for tf in list(target_files):
            if tf == "go.mod" and "go.sum" not in target_files:
                target_files.append("go.sum")
            elif tf.endswith("/go.mod"):
                sib = tf[:-len("go.mod")] + "go.sum"
                if sib not in target_files:
                    target_files.append(sib)
        if extra_allowed_files:
            for af in extra_allowed_files:
                if af not in target_files:
                    target_files.append(af)
    unexpected_files: list[str] = []
    if target_files:
        for fpath in diff_info.get("all_changed_files") or []:
            matched = False
            for scope in target_files:
                s = scope.rstrip("/")
                if fpath == s or fpath.startswith(s + "/") or (s.endswith("*") and fpath.startswith(s[:-1])):
                    matched = True
                    break
            if not matched:
                unexpected_files.append(fpath)
        if unexpected_files:
            warnings.append(
                {
                    "code": "unexpected_files",
                    "files": unexpected_files,
                    "message": f"Files modified outside declared plan scope: {', '.join(unexpected_files[:8])}",
                }
            )

    return {
        "passed": len(blocking_issues) == 0,
        "blocking_issues": blocking_issues,
        "warnings": warnings,
        "unexpected_files": unexpected_files,
    }


def run_independent_verification(
    *,
    task_dir: Path,
    worktree_path: Path,
    base_commit: str,
    config: dict[str, Any],
    explicit_commands: list[str] | None = None,
    timeout_sec: int = 300,
    extra_allowed_files: list[str] | None = None,
    prepare_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute independent verification commands and full git/contract/anti-weakening audit."""
    contract = read_json(task_dir / "contract.json")
    contract_check = verify_contract_integrity(task_dir)
    diff_info = inspect_worktree_changes(worktree_path, base_commit)
    integrity_audit = audit_diff_integrity(diff_info, contract, extra_allowed_files=extra_allowed_files)

    source, commands = detect_verification_commands(
        worktree_path,
        config,
        contract=contract,
        explicit_commands=explicit_commands,
    )

    checks: list[dict[str, Any]] = []
    passed_count = 0
    failed_count = 0
    skipped_tests_total = 0
    all_skipped_tests: list[str] = []
    seen_skipped_tests: set[str] = set()
    verify_env = build_stage_env(config, "verify")

    for item in commands:
        cmd_str = item["command"]
        name = item["name"]
        kind = item["kind"]
        t0 = time.monotonic()
        timed_out = False
        full_stdout = ""
        full_stderr = ""
        try:
            proc = subprocess.run(
                cmd_str,
                cwd=str(worktree_path),
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                env=verify_env,
            )
            exit_code = int(proc.returncode)
            full_stdout = proc.stdout or ""
            full_stderr = proc.stderr or ""
            stdout_tail = full_stdout[-4000:] if full_stdout else ""
            stderr_tail = full_stderr[-4000:] if full_stderr else ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            full_stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stdout_tail = full_stdout[-2000:]
            stderr_tail = f"Verification command timed out after {timeout_sec}s"

        duration = round(time.monotonic() - t0, 2)
        ok = (exit_code == 0) and not timed_out
        if ok:
            passed_count += 1
        else:
            failed_count += 1

        skip_info = detect_skipped_tests(full_stdout, full_stderr)
        skipped_tests_total += int(skip_info["skipped_count"])
        for tname in skip_info["skipped_tests"]:
            if tname not in seen_skipped_tests:
                seen_skipped_tests.add(tname)
                all_skipped_tests.append(tname)

        checks.append(
            {
                "name": name,
                "kind": kind,
                "command": cmd_str,
                "passed": ok,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_sec": duration,
                "skipped_count": int(skip_info["skipped_count"]),
                "skipped_tests": skip_info["skipped_tests"],
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            }
        )

    if skipped_tests_total > 0:
        sample = ", ".join(all_skipped_tests[:5])
        suffix = "..." if len(all_skipped_tests) > 5 else ""
        detail = f" ({sample}{suffix})" if sample else ""
        integrity_audit["warnings"].append(
            {
                "code": "skipped_tests",
                "count": skipped_tests_total,
                "tests": all_skipped_tests[:25],
                "message": (
                    f"{skipped_tests_total} test(s) were skipped during verification{detail}; "
                    "verify required test environment variables or backing services are configured."
                ),
            }
        )

    # Overall pass requires: contract valid, zero failed verification checks, zero integrity blocking issues
    overall_passed = (
        contract_check["valid"]
        and failed_count == 0
        and integrity_audit["passed"]
    )

    failure_reasons: list[str] = []
    if not contract_check["valid"]:
        failure_reasons.append(contract_check["reason"])
    for chk in checks:
        if not chk["passed"]:
            failure_reasons.append(f"Check '{chk['name']}' failed (exit={chk['exit_code']}): {chk['command']}")
    for issue in integrity_audit["blocking_issues"]:
        failure_reasons.append(issue["message"])

    report = {
        "verified_at": utc_now(),
        "passed": overall_passed,
        "command_source": source,
        "commands_total": len(checks),
        "commands_passed": passed_count,
        "commands_failed": failed_count,
        "skipped_tests_total": skipped_tests_total,
        "skipped_tests": all_skipped_tests[:25],
        "checks": checks,
        "prepare": prepare_report,
        "contract_integrity": contract_check,
        "diff_audit": integrity_audit,
        "diff_stats": {
            "files_changed_count": diff_info["files_changed_count"],
            "total_insertions": diff_info["total_insertions"],
            "total_deletions": diff_info["total_deletions"],
            "added": diff_info["added"],
            "modified": diff_info["modified"],
            "deleted": diff_info["deleted"],
            "renamed": diff_info["renamed"],
            "untracked": diff_info["untracked"],
            "files": diff_info["files"],
        },
        "failure_reasons": failure_reasons,
    }
    atomic_write_json(task_dir / "verification.json", report)
    return report
