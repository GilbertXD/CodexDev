"""Stack-agnostic independent verification, diff audit, and anti-weakening checks."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .contract import verify_contract_integrity
from .state import atomic_write_json, atomic_write_text, read_json, utc_now
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


_NON_TEST_CMD_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*("
    r"go\s+(?:vet|build|fmt|mod)\b|"
    r"make\s+(?:vet|build|lint|fmt|tidy|docker-\w+)\b|"
    r"docker\s+compose\b|"
    r"(?:npx|pnpm|yarn|bunx)?\s*(?:tsc|eslint|biome|prettier)\b|"
    r"npm\s+run\s+(?:build|lint|typecheck|format)\b|"
    r"(?:python3?\s+-m\s+)?(?:ruff|flake8|mypy|black|isort)\b|"
    r"cargo\s+(?:check|clippy|build|fmt)\b"
    r")"
)
_TEST_CMD_RE = re.compile(
    r"(?:^|\s)(?:"
    r"go\s+test\b|"
    r"(?:python3?\s+-m\s+)?(?:pytest|unittest)\b|"
    r"(?:npx|pnpm|yarn|bunx)?\s*(?:jest|vitest)\b|"
    r"node\s+--test\b|"
    r"cargo\s+test\b|"
    r"npm\s+(?:test|run\s+test\b)|"
    r"(?:pnpm|yarn)\s+(?:test|run\s+test\b)|"
    r"make\s+test(?:[-_]\w+)?\b|"
    r"(?:mvn|gradle|./gradlew)\s+test\b|"
    r"ctest\b"
    r")"
)
_OBSERVABLE_TEST_SUMMARY_RES = (
    re.compile(r"^\s*Ran\s+\d+\s+tests?\s+in\b", re.MULTILINE),
    re.compile(r"^test result:\s+(?:ok|FAILED)\.", re.MULTILINE),
    re.compile(r"^#\s+tests\s+\d+\s*$", re.MULTILINE),
    re.compile(r"^\s*Tests:\s+.*\btotal\b", re.MULTILINE),
    re.compile(r"(?:^=+[^\n]*?\b(?:passed|failed|skipped)\b[^\n]*?=+\s*$)|(?:^\s*\d+\s+(?:passed|failed|skipped)\b[^\n]*\bin\s+[\d.]+s)", re.MULTILINE),
    re.compile(r"^\s*---\s+(?:PASS|FAIL|SKIP):\s+\S+", re.MULTILINE),
)


def _has_shell_chaining(cmd: str) -> bool:
    """Return True if shell chaining/piping operators appear outside quotes."""
    in_single = False
    in_double = False
    escaped = False
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\" and not in_single:
            escaped = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch in (";", "|", "\n"):
                return True
            if ch == "&" and i + 1 < n and cmd[i + 1] == "&":
                return True
        i += 1
    return False


def _is_test_command(cmd: str, kind: str = "custom") -> bool:
    s = cmd.strip()
    if kind in ("lint", "build", "typecheck", "prepare"):
        return False
    if _NON_TEST_CMD_RE.match(s):
        return False
    if kind == "test":
        return True
    return bool(_TEST_CMD_RE.search(s))


def instrument_verify_command(
    cmd: str,
    report_dir: Path,
    check_idx: int,
    *,
    kind: str = "custom",
    structured_reporting: bool = True,
) -> dict[str, Any]:
    """Optionally attach machine-readable structured output flags for supported test runners.

    Preserves command semantics without altering the original display command.
    """
    s = cmd.strip()
    is_test = _is_test_command(s, kind=kind)
    if not structured_reporting or not s or _has_shell_chaining(s):
        return {
            "executed_command": s,
            "adapter": None,
            "report_path": None,
            "is_test_command": is_test,
        }

    # 1) Go test: inject -json unless compile-only (-c / -o)
    if re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*go\s+test\b", s):
        if not re.search(r"(?:^|\s)-(?:c|o)(?:=|\s|$)", s):
            if re.search(r"(?:^|\s)-json\b", s):
                return {
                    "executed_command": s,
                    "adapter": "go_json",
                    "report_path": None,
                    "is_test_command": True,
                }
            return {
                "executed_command": re.sub(r"(\bgo\s+test\b)", r"\1 -json", s, count=1),
                "adapter": "go_json",
                "report_path": None,
                "is_test_command": True,
            }

    # 2) Pytest: inject --junitxml=<tmp>/junit_<idx>.xml
    if re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:python3?\s+-m\s+)?pytest\b", s):
        if not re.search(r"(?:^|\s)--junitxml(?:=|\s)", s):
            rpath = report_dir / f"junit_{check_idx}.xml"
            return {
                "executed_command": f"{s} --junitxml={shlex.quote(str(rpath))}",
                "adapter": "junit_xml",
                "report_path": rpath,
                "is_test_command": True,
            }

    # 3) Jest: inject --json --outputFile=<tmp>/jest_<idx>.json
    if re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:(?:npx|pnpm|yarn|bunx)\s+)?jest\b", s):
        if not re.search(r"(?:^|\s)(?:--json|--outputFile)(?:=|\s|$)", s):
            rpath = report_dir / f"jest_{check_idx}.json"
            return {
                "executed_command": f"{s} --json --outputFile={shlex.quote(str(rpath))}",
                "adapter": "js_json",
                "report_path": rpath,
                "is_test_command": True,
            }

    # 4) Vitest: inject --reporter=json --outputFile=<tmp>/vitest_<idx>.json
    if re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:(?:npx|pnpm|yarn|bunx)\s+)?vitest\s+run\b", s):
        if not re.search(r"(?:^|\s)(?:--reporter|--outputFile)(?:=|\s|$)", s):
            rpath = report_dir / f"vitest_{check_idx}.json"
            return {
                "executed_command": f"{s} --reporter=json --outputFile={shlex.quote(str(rpath))}",
                "adapter": "js_json",
                "report_path": rpath,
                "is_test_command": True,
            }

    return {
        "executed_command": s,
        "adapter": None,
        "report_path": None,
        "is_test_command": is_test,
    }


def parse_go_test_json(stdout: str) -> dict[str, Any] | None:
    """Parse `go test -json` NDJSON output for exact per-test skip counts, reasons, and readable logs."""
    if not stdout or not stdout.strip():
        return None
    events_parsed = 0
    last_output: dict[tuple[str, str], str] = {}
    executed: set[tuple[str, str]] = set()
    skipped_details: list[dict[str, str]] = []
    skipped_seen: set[tuple[str, str]] = set()
    display_lines: list[str] = []

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            display_lines.append(raw_line)
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            display_lines.append(raw_line)
            continue
        if not isinstance(ev, dict) or "Action" not in ev:
            display_lines.append(raw_line)
            continue
        events_parsed += 1
        action = str(ev.get("Action") or "")
        pkg = str(ev.get("Package") or "")
        test_name = str(ev.get("Test") or "")
        output = str(ev.get("Output") or "")

        if action == "output" and output:
            cleaned = output.strip()
            if test_name:
                if cleaned and not cleaned.startswith(("=== RUN", "=== PAUSE", "=== CONT", "--- SKIP", "--- PASS", "--- FAIL")):
                    last_output[(pkg, test_name)] = cleaned[:240]
                if cleaned.startswith(("--- FAIL:", "--- SKIP:", "FAIL")) or "FAIL" in cleaned:
                    display_lines.append(output.rstrip("\n"))
            else:
                # Package-level summary line (ok / ? / FAIL)
                display_lines.append(output.rstrip("\n"))
        elif test_name and action in ("pass", "fail", "skip"):
            executed.add((pkg, test_name))
            if action == "skip" and (pkg, test_name) not in skipped_seen:
                skipped_seen.add((pkg, test_name))
                skipped_details.append(
                    {
                        "name": test_name,
                        "package": pkg,
                        "reason": last_output.get((pkg, test_name), ""),
                    }
                )

    if events_parsed == 0:
        return None

    dedup_names: list[str] = []
    seen_names: set[str] = set()
    for item in skipped_details:
        if item["name"] not in seen_names:
            seen_names.add(item["name"])
            dedup_names.append(item["name"])

    return {
        "skip_detection": "structured",
        "skipped_count": len(skipped_details),
        "skipped_tests": dedup_names[:25],
        "skipped_test_details": skipped_details[:25],
        "executed_tests": len(executed),
        "display_stdout": "\n".join(display_lines).strip(),
    }


def parse_junit_xml(xml_path: Path) -> dict[str, Any] | None:
    """Parse JUnit XML test report (e.g. pytest --junitxml) for exact skipped test counts and reasons."""
    if not xml_path.is_file():
        return None
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError):
        return None
    root = tree.getroot()
    executed = 0
    skipped_details: list[dict[str, str]] = []
    seen: set[str] = set()

    for tc in root.iter("testcase"):
        executed += 1
        cls = str(tc.attrib.get("classname") or "").strip()
        name = str(tc.attrib.get("name") or "").strip()
        full_name = f"{cls}::{name}" if cls and name else (name or cls)
        skip_el = tc.find("skipped")
        if skip_el is not None:
            reason = str(skip_el.attrib.get("message") or (skip_el.text or "")).strip()[:240]
            if full_name and full_name not in seen:
                seen.add(full_name)
                skipped_details.append({"name": full_name, "package": cls, "reason": reason})

    return {
        "skip_detection": "structured",
        "skipped_count": len(skipped_details),
        "skipped_tests": [d["name"] for d in skipped_details][:25],
        "skipped_test_details": skipped_details[:25],
        "executed_tests": executed,
    }


def parse_js_test_json(json_path: Path) -> dict[str, Any] | None:
    """Parse Jest / Vitest JSON report file for exact skipped/pending test counts and names."""
    if not json_path.is_file():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("testResults"), list):
        return None

    executed = 0
    skipped_details: list[dict[str, str]] = []
    seen: set[str] = set()
    for suite in data["testResults"]:
        if not isinstance(suite, dict):
            continue
        suite_name = str(suite.get("name") or "")
        assertions = suite.get("assertionResults") or []
        if not isinstance(assertions, list):
            continue
        for item in assertions:
            if not isinstance(item, dict):
                continue
            executed += 1
            st = str(item.get("status") or "").lower()
            if st in ("pending", "skipped", "todo"):
                tname = str(item.get("fullName") or item.get("title") or "").strip()
                if tname and tname not in seen:
                    seen.add(tname)
                    skipped_details.append({"name": tname, "package": suite_name, "reason": st})

    return {
        "skip_detection": "structured",
        "skipped_count": len(skipped_details),
        "skipped_tests": [d["name"] for d in skipped_details][:25],
        "skipped_test_details": skipped_details[:25],
        "executed_tests": executed,
    }


def _has_observable_test_summary(stdout: str, stderr: str = "") -> bool:
    combined = "\n".join(part for part in (stdout or "", stderr or "") if part)
    if not combined.strip():
        return False
    return any(pat.search(combined) for pat in _OBSERVABLE_TEST_SUMMARY_RES)


def _extract_test_names_from_file(path: Path) -> list[str]:
    """Extract test function names declared in a changed/added test file."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    suffix = path.suffix.lower()
    if suffix == ".go":
        return list(dict.fromkeys(re.findall(r"^\s*func\s+(Test[A-Za-z0-9_]+)\s*\(", text, re.MULTILINE)))
    if suffix == ".py":
        return list(dict.fromkeys(re.findall(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(", text, re.MULTILINE)))
    if suffix == ".rs":
        return list(
            dict.fromkeys(
                re.findall(r"#\[(?:[\w:]+::)?test(?:\([^\]]*\))?\]\s*(?:async\s+)?fn\s+([A-Za-z0-9_]+)\s*\(", text)
            )
        )
    return []


def _normalize_skipped_test_base_name(raw_name: str) -> str:
    """Normalize Go subtests, pytest nodeids, unittest labels, and Rust module paths to base test name."""
    s = raw_name.strip()
    if " (" in s:
        s = s.split(" (", 1)[0].strip()
    if "::" in s:
        s = s.rsplit("::", 1)[-1].strip()
    if "/" in s:
        s = s.split("/", 1)[0].strip()
    if "[" in s:
        s = s.split("[", 1)[0].strip()
    return s


def find_skipped_changed_tests(
    worktree_path: Path,
    changed_files: list[str],
    skipped_tests: list[str],
) -> tuple[list[str], list[str]]:
    """Return (matched_skipped_test_names, changed_test_files) when changed/added tests were skipped."""
    if not changed_files or not skipped_tests:
        return ([], [])
    changed_test_files = [f for f in changed_files if is_test_file(f) and (worktree_path / f).is_file()]
    if not changed_test_files:
        return ([], [])

    declared_in_diff: set[str] = set()
    for rel in changed_test_files:
        declared_in_diff.update(_extract_test_names_from_file(worktree_path / rel))
    if not declared_in_diff:
        return ([], changed_test_files)

    matched: list[str] = []
    seen: set[str] = set()
    for skipped in skipped_tests:
        base = _normalize_skipped_test_base_name(skipped)
        if base in declared_in_diff and skipped not in seen:
            seen.add(skipped)
            matched.append(skipped)
    return (matched, changed_test_files)


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


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_assignments(items: list[str] | dict[str, str] | None) -> dict[str, str]:
    """Parse repeatable KEY=VALUE CLI flags or a dict into a validated env dict."""
    if not items:
        return {}
    out: dict[str, str] = {}
    if isinstance(items, dict):
        for k, v in items.items():
            key = str(k).strip()
            if not _ENV_KEY_RE.match(key):
                raise ValueError(f"Invalid environment variable name: {k!r}")
            out[key] = "" if v is None else str(v)
        return out
    for raw in items:
        if not isinstance(raw, str) or "=" not in raw:
            raise ValueError(f"Invalid --verify-env assignment {raw!r}; expected KEY=VALUE.")
        key_raw, val = raw.split("=", 1)
        key = key_raw.strip()
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"Invalid environment variable name in --verify-env: {key_raw!r}")
        out[key] = val
    return out


def build_stage_env(
    config: dict[str, Any],
    stage: str,
    explicit_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build host subprocess environment with configured and task-level verify/prepare overrides."""
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
    if isinstance(explicit_env, dict):
        for k, v in explicit_env.items():
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


_FAILURE_SIGNAL_RE = re.compile(
    r"(?:"
    r"^\s*---\s+FAIL:|"
    r"^FAIL(?:\s|$)|"
    r"\bAssertionError\b|"
    r"^Traceback \(most recent call last\):|"
    r"\b(?:ERROR|FATAL|PANIC)\s*:|"
    r"\bpanic:|"
    r"\bTimed out\b|"
    r"\btimed out\b|"
    r"\breturned\s+\d+,\s*expected\s+\d+|"
    r"\bexit\s+(?:code|status)\s+[1-9]\d*|"
    r"did not complete successfully|"
    r"\bno such host\b|"
    r"\bconnection refused\b|"
    r"\bpermission denied\b|"
    r"\bsyntax error\b|"
    r"\bundefined:\s+\S+|"
    r"\bcannot find\b"
    r")",
    re.IGNORECASE,
)

_COMPANION_SOURCE_EXTENSIONS = frozenset(
    {
        ".go",
        ".py",
        ".rs",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".java",
        ".kt",
        ".rb",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
    }
)


def extract_failure_excerpt(text: str, *, max_chars: int = 2400) -> str:
    """Return a concise failure excerpt preserving early root-cause lines even when teardown logs dominate the tail."""
    if not text:
        return ""
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped

    tail_start = len(text) - max_chars
    lines = text.splitlines(keepends=True)
    offset = 0
    first_match_idx: int | None = None
    for idx, line in enumerate(lines):
        if offset < tail_start and _FAILURE_SIGNAL_RE.search(line):
            first_match_idx = idx
            break
        offset += len(line)

    if first_match_idx is None:
        return text[-max_chars:].strip()

    sep = "\n... [truncated; see full log file] ...\n"
    head_budget = max(400, (max_chars - len(sep)) // 2)
    start_idx = max(0, first_match_idx - 5)
    end_idx = min(len(lines), first_match_idx + 25)
    window_text = "".join(lines[start_idx:end_idx]).strip()
    if len(window_text) > head_budget:
        window_text = window_text[:head_budget].rstrip()

    tail_budget = max(400, max_chars - len(window_text) - len(sep))
    tail_text = text[-tail_budget:].strip()
    if window_text and tail_text and window_text in tail_text:
        return tail_text
    return f"{window_text}{sep}{tail_text}".strip()


def _sanitize_log_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", (name or "check").strip()).strip("._")
    return cleaned or "check"


def _persist_check_logs(
    log_dir: Path,
    check_name: str,
    stdout: str,
    stderr: str,
) -> tuple[str | None, str | None]:
    """Persist full stdout/stderr of a failed verification/prepare check for post-mortem inspection."""
    stdout_path: str | None = None
    stderr_path: str | None = None
    if not stdout and not stderr:
        return (None, None)
    log_dir.mkdir(parents=True, exist_ok=True)
    slug = _sanitize_log_name(check_name)
    if stdout:
        p_out = log_dir / f"{slug}.stdout.log"
        atomic_write_text(p_out, stdout)
        stdout_path = str(p_out)
    if stderr:
        p_err = log_dir / f"{slug}.stderr.log"
        atomic_write_text(p_err, stderr)
        stderr_path = str(p_err)
    return (stdout_path, stderr_path)


def _is_same_package_companion(
    fpath: str,
    target_files: list[str],
    modified_files: set[str],
) -> bool:
    """Return True when a modified existing source file is a same-directory companion of a declared target file.

    Strictly requires:
      - existing file modified (not newly created/untracked or deleted),
      - non-test, non-config source file with a recognized code extension,
      - inside a non-root package directory (parent != '.'),
      - sharing the exact same parent directory and source extension with at least one explicit target file.
    """
    if fpath not in modified_files:
        return False
    if is_test_file(fpath) or is_config_file(fpath):
        return False
    p = Path(fpath)
    parent = p.parent.as_posix()
    if parent in ("", "."):
        return False
    ext = p.suffix.lower()
    if ext not in _COMPANION_SOURCE_EXTENSIONS:
        return False

    for scope in target_files:
        s = scope.rstrip("/")
        if not s or s.endswith("*"):
            continue
        sp = Path(s)
        if sp.parent.as_posix() == parent and sp.suffix.lower() == ext:
            return True
    return False


def run_dependency_prepare(
    *,
    task_dir: Path,
    worktree_path: Path,
    base_commit: str,
    config: dict[str, Any],
    explicit_commands: list[str] | None = None,
    explicit_env: dict[str, str] | None = None,
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
    stage_env = build_stage_env(config, "prepare", explicit_env=explicit_env)

    results: list[dict[str, Any]] = []
    passed_count = 0
    failed_count = 0
    generated_allowed_files: list[str] = []
    prep_log_dir = task_dir / "prepare_logs"
    if prep_log_dir.exists():
        shutil.rmtree(prep_log_dir, ignore_errors=True)

    for item in commands:
        cmd_str = item["command"]
        name = item["name"]
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
                timeout=eff_timeout,
                env=stage_env,
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
            full_stderr = f"Prepare command timed out after {eff_timeout}s"
            stderr_tail = full_stderr

        duration = round(time.monotonic() - t0, 2)
        ok = (exit_code == 0) and not timed_out
        stdout_log_path: str | None = None
        stderr_log_path: str | None = None
        if ok:
            passed_count += 1
            if name == "go:mod-tidy":
                generated_allowed_files.append("go.sum")
            elif name.startswith("go:mod-tidy:"):
                sub = name.split("go:mod-tidy:", 1)[1]
                generated_allowed_files.append(f"{sub}/go.sum")
        else:
            failed_count += 1
            stdout_log_path, stderr_log_path = _persist_check_logs(prep_log_dir, name, full_stdout, full_stderr)

        entry: dict[str, Any] = {
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
        if not ok:
            if stdout_log_path:
                entry["stdout_log_path"] = stdout_log_path
            if stderr_log_path:
                entry["stderr_log_path"] = stderr_log_path
            if full_stdout:
                entry["stdout_excerpt"] = extract_failure_excerpt(full_stdout)
            if full_stderr:
                entry["stderr_excerpt"] = extract_failure_excerpt(full_stderr)
        results.append(entry)

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


def apply_no_cache_verify_command(cmd: str) -> str:
    """Disable stale test-result caching on authoritative host verification commands across runners."""
    s = cmd.strip()
    if not s:
        return s
    # Go test caches passing package results unless -count=1 is set
    if re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*go\s+test\b", s):
        if not re.search(r"(?:^|\s)-count(?:=|\s)", s):
            return re.sub(r"(\bgo\s+test\b)", r"\1 -count=1", s, count=1)
        return s
    # Jest transform/test cache
    if re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:(?:npx|pnpm|yarn|bunx)\s+)?jest\b", s):
        if not re.search(r"(?:^|\s)--(?:no-)?cache\b", s):
            return f"{s} --no-cache"
        return s
    # Turborepo test task caching
    if re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:(?:npx|pnpm|yarn|bunx)\s+)?turbo\s+(?:run\s+)?test\b", s):
        if not re.search(r"(?:^|\s)(?:--force|--no-cache)\b", s):
            return f"{s} --force"
        return s
    return s


def _apply_no_cache_list(items: list[dict[str, str]], enabled: bool) -> list[dict[str, str]]:
    if not enabled:
        return items
    return [{**item, "command": apply_no_cache_verify_command(item["command"])} for item in items]


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
    cfg_verify = config.get("verify") or {}
    no_cache = bool(cfg_verify.get("no_cache", True)) if isinstance(cfg_verify, dict) else True

    if explicit_commands:
        return ("explicit", _apply_no_cache_list(_normalize_cmd_list(explicit_commands, default_kind="custom", prefix="check"), no_cache))

    if contract and contract.get("verify_commands"):
        norm = _normalize_cmd_list(contract["verify_commands"], default_kind="custom", prefix="check")
        if norm:
            return ("plan_contract", _apply_no_cache_list(norm, no_cache))

    if isinstance(cfg_verify, dict) and cfg_verify.get("commands"):
        norm = _normalize_cmd_list(cfg_verify["commands"], default_kind="custom", prefix="check")
        if norm:
            return ("config", _apply_no_cache_list(norm, no_cache))

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
        detected.append({"name": "go:test", "command": "go test -count=1 ./...", "kind": "test"})

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
    same_package_companions: list[str] = []
    modified_set = set(diff_info.get("modified") or [])
    if target_files:
        for fpath in diff_info.get("all_changed_files") or []:
            matched = False
            for scope in target_files:
                s = scope.rstrip("/")
                if fpath == s or fpath.startswith(s + "/") or (s.endswith("*") and fpath.startswith(s[:-1])):
                    matched = True
                    break
            if not matched:
                if _is_same_package_companion(fpath, target_files, modified_set):
                    same_package_companions.append(fpath)
                else:
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
        "same_package_companions": same_package_companions,
    }


def run_independent_verification(
    *,
    task_dir: Path,
    worktree_path: Path,
    base_commit: str,
    config: dict[str, Any],
    explicit_commands: list[str] | None = None,
    explicit_env: dict[str, str] | None = None,
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

    cfg_verify = config.get("verify") or {}
    structured_reporting = bool(cfg_verify.get("structured_reporting", True)) if isinstance(cfg_verify, dict) else True
    block_on_changed_test_skip = (
        bool(cfg_verify.get("block_on_changed_test_skip", True)) if isinstance(cfg_verify, dict) else True
    )

    checks: list[dict[str, Any]] = []
    passed_count = 0
    failed_count = 0
    observed_skipped_total = 0
    all_skipped_tests: list[str] = []
    all_skipped_details: list[dict[str, str]] = []
    seen_skipped_tests: set[str] = set()
    test_skip_modes: list[str] = []
    unobservable_commands: list[str] = []
    verify_env = build_stage_env(config, "verify", explicit_env=explicit_env)
    verify_log_dir = task_dir / "verify_logs"
    if verify_log_dir.exists():
        shutil.rmtree(verify_log_dir, ignore_errors=True)

    with tempfile.TemporaryDirectory(prefix="codex-dev-verify-") as tmp_report_dir_str:
        tmp_report_dir = Path(tmp_report_dir_str)
        for idx, item in enumerate(commands, 1):
            cmd_str = item["command"]
            name = item["name"]
            kind = item["kind"]
            inst = instrument_verify_command(
                cmd_str,
                tmp_report_dir,
                idx,
                kind=kind,
                structured_reporting=structured_reporting,
            )
            exec_cmd = inst["executed_command"]
            adapter = inst["adapter"]
            report_path = inst["report_path"]
            is_test_cmd = bool(inst["is_test_command"])

            t0 = time.monotonic()
            timed_out = False
            full_stdout = ""
            full_stderr = ""
            try:
                proc = subprocess.run(
                    exec_cmd,
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
                raw_err = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                timeout_msg = f"Verification command timed out after {timeout_sec}s"
                full_stderr = f"{raw_err.rstrip()}\n{timeout_msg}".strip() if raw_err.strip() else timeout_msg
                stdout_tail = full_stdout[-2000:]
                stderr_tail = full_stderr[-4000:]

            duration = round(time.monotonic() - t0, 2)
            ok = (exit_code == 0) and not timed_out
            if ok:
                passed_count += 1
            else:
                failed_count += 1

            parsed_struct: dict[str, Any] | None = None
            excerpt_stdout_src = full_stdout
            if adapter == "go_json":
                parsed_struct = parse_go_test_json(full_stdout)
                if parsed_struct and parsed_struct.get("display_stdout"):
                    excerpt_stdout_src = str(parsed_struct["display_stdout"])
                    stdout_tail = excerpt_stdout_src[-4000:]
            elif adapter == "junit_xml" and isinstance(report_path, Path):
                parsed_struct = parse_junit_xml(report_path)
            elif adapter == "js_json" and isinstance(report_path, Path):
                parsed_struct = parse_js_test_json(report_path)

            stdout_log_path: str | None = None
            stderr_log_path: str | None = None
            if not ok:
                stdout_log_path, stderr_log_path = _persist_check_logs(
                    verify_log_dir,
                    f"{idx:02d}_{name}",
                    full_stdout,
                    full_stderr,
                )

            check_skip_mode: str
            check_skipped_count: int | None
            check_skipped_names: list[str]
            check_skipped_details: list[dict[str, str]]

            if parsed_struct is not None:
                check_skip_mode = "structured"
                check_skipped_count = int(parsed_struct["skipped_count"])
                check_skipped_names = list(parsed_struct["skipped_tests"])
                check_skipped_details = list(parsed_struct.get("skipped_test_details") or [])
            else:
                heur = detect_skipped_tests(full_stdout, full_stderr)
                h_count = int(heur["skipped_count"])
                h_names = list(heur["skipped_tests"])
                if h_count > 0 or _has_observable_test_summary(full_stdout, full_stderr):
                    check_skip_mode = "heuristic"
                    check_skipped_count = h_count
                    check_skipped_names = h_names
                    check_skipped_details = [{"name": n, "package": "", "reason": ""} for n in h_names]
                elif is_test_cmd:
                    check_skip_mode = "unobservable"
                    check_skipped_count = None
                    check_skipped_names = []
                    check_skipped_details = []
                    unobservable_commands.append(cmd_str)
                else:
                    check_skip_mode = "not_applicable"
                    check_skipped_count = 0
                    check_skipped_names = []
                    check_skipped_details = []

            if check_skip_mode != "not_applicable":
                test_skip_modes.append(check_skip_mode)
            if isinstance(check_skipped_count, int):
                observed_skipped_total += check_skipped_count
            for detail_item in check_skipped_details:
                tname = detail_item["name"]
                if tname not in seen_skipped_tests:
                    seen_skipped_tests.add(tname)
                    all_skipped_tests.append(tname)
                    all_skipped_details.append(detail_item)

            check_entry: dict[str, Any] = {
                "name": name,
                "kind": kind,
                "command": cmd_str,
                "passed": ok,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_sec": duration,
                "skip_detection": check_skip_mode,
                "skipped_count": check_skipped_count,
                "skipped_tests": check_skipped_names,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            }
            if not ok:
                if stdout_log_path:
                    check_entry["stdout_log_path"] = stdout_log_path
                if stderr_log_path:
                    check_entry["stderr_log_path"] = stderr_log_path
                if excerpt_stdout_src:
                    check_entry["stdout_excerpt"] = extract_failure_excerpt(excerpt_stdout_src)
                if full_stderr:
                    check_entry["stderr_excerpt"] = extract_failure_excerpt(full_stderr)
            if exec_cmd != cmd_str:
                check_entry["executed_command"] = exec_cmd
            checks.append(check_entry)

    # Aggregate tri-state skip observability across all test checks
    if not test_skip_modes:
        report_skip_detection = "not_applicable"
        skipped_tests_total: int | None = 0
    elif all(m == "unobservable" for m in test_skip_modes):
        report_skip_detection = "unobservable"
        skipped_tests_total = None
    elif any(m == "unobservable" for m in test_skip_modes):
        report_skip_detection = "partial"
        skipped_tests_total = observed_skipped_total
    elif all(m == "structured" for m in test_skip_modes):
        report_skip_detection = "structured"
        skipped_tests_total = observed_skipped_total
    else:
        report_skip_detection = "heuristic"
        skipped_tests_total = observed_skipped_total

    if unobservable_commands:
        integrity_audit["warnings"].append(
            {
                "code": "unobservable_skips",
                "commands": unobservable_commands[:10],
                "message": (
                    f"Skip detection was unobservable for {len(unobservable_commands)} test command(s) "
                    f"({', '.join(unobservable_commands[:3])}); skipped test count may be incomplete."
                ),
            }
        )

    if isinstance(skipped_tests_total, int) and skipped_tests_total > 0:
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

    # Guard: block verification when tests newly added or modified in this diff were skipped
    if block_on_changed_test_skip and all_skipped_tests:
        skipped_changed, changed_test_files = find_skipped_changed_tests(
            worktree_path,
            list(diff_info.get("all_changed_files") or []),
            all_skipped_tests,
        )
        if skipped_changed:
            integrity_audit["blocking_issues"].append(
                {
                    "code": "changed_tests_skipped",
                    "file": ", ".join(changed_test_files[:5]),
                    "tests": skipped_changed[:25],
                    "message": (
                        f"Changed/added test(s) were skipped during verification ({', '.join(skipped_changed[:10])}); "
                        "configure required test prerequisites (e.g. TEST_DATABASE_URL) or ensure modified tests execute."
                    ),
                }
            )
            integrity_audit["passed"] = False

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
        "explicit_env_keys": sorted(explicit_env.keys()) if isinstance(explicit_env, dict) else [],
        "commands_total": len(checks),
        "commands_passed": passed_count,
        "commands_failed": failed_count,
        "skip_detection": report_skip_detection,
        "skipped_tests_total": skipped_tests_total,
        "skipped_tests": all_skipped_tests[:25],
        "skipped_test_details": all_skipped_details[:25],
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
