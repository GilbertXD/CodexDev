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


def is_test_file(path: str) -> bool:
    return any(p.search(path) for p in TEST_FILE_PATTERNS)


def is_config_file(path: str) -> bool:
    return any(p.search(path) for p in CONFIG_FILE_PATTERNS)


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
    def _normalize(cmds: list[Any]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for idx, item in enumerate(cmds, 1):
            if isinstance(item, str) and item.strip():
                out.append({"name": f"check-{idx}", "command": item.strip(), "kind": "custom"})
            elif isinstance(item, dict) and isinstance(item.get("command"), str) and item["command"].strip():
                out.append(
                    {
                        "name": str(item.get("name") or f"check-{idx}"),
                        "command": item["command"].strip(),
                        "kind": str(item.get("kind") or "custom"),
                    }
                )
        return out

    if explicit_commands:
        return ("explicit", _normalize(explicit_commands))

    if contract and contract.get("verify_commands"):
        norm = _normalize(contract["verify_commands"])
        if norm:
            return ("plan_contract", norm)

    cfg_verify = config.get("verify") or {}
    if isinstance(cfg_verify, dict) and cfg_verify.get("commands"):
        norm = _normalize(cfg_verify["commands"])
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
    target_files = (contract or {}).get("target_files") or []
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
) -> dict[str, Any]:
    """Execute independent verification commands and full git/contract/anti-weakening audit."""
    contract = read_json(task_dir / "contract.json")
    contract_check = verify_contract_integrity(task_dir)
    diff_info = inspect_worktree_changes(worktree_path, base_commit)
    integrity_audit = audit_diff_integrity(diff_info, contract)

    source, commands = detect_verification_commands(
        worktree_path,
        config,
        contract=contract,
        explicit_commands=explicit_commands,
    )

    checks: list[dict[str, Any]] = []
    passed_count = 0
    failed_count = 0

    for item in commands:
        cmd_str = item["command"]
        name = item["name"]
        kind = item["kind"]
        t0 = time.monotonic()
        timed_out = False
        verify_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
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
            stdout_tail = proc.stdout[-4000:] if proc.stdout else ""
            stderr_tail = proc.stderr[-4000:] if proc.stderr else ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            stdout_tail = (exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))[-2000:]
            stderr_tail = f"Verification command timed out after {timeout_sec}s"

        duration = round(time.monotonic() - t0, 2)
        ok = (exit_code == 0) and not timed_out
        if ok:
            passed_count += 1
        else:
            failed_count += 1

        checks.append(
            {
                "name": name,
                "kind": kind,
                "command": cmd_str,
                "passed": ok,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_sec": duration,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
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
        "checks": checks,
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
