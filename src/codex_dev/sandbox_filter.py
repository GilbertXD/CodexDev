"""Stack-agnostic pre-filter for Builder Checks running inside offline sandboxes.

Detects tests that bind local sockets / loopback ports (which macOS/Linux
`workspace-write` sandboxes deny with EPERM) across Go, Python, Node/JS/TS,
and Rust, and rewrites Builder Checks commands to defer only those tests to
authoritative host verification — without modifying any target repository code.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

SKIP_WALK_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        "target",
        "bin",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
    }
)

# --- Go loopback / socket bind detection ---
_GO_BIND_RE = re.compile(
    r"\b(?:"
    r"httptest\.New(?:TLS|Unstarted)?Server"
    r"|net\.Listen(?:TCP|UDP|Packet|Unix)?"
    r"|tls\.Listen"
    r"|http\.ListenAndServe(?:TLS)?"
    r"|miniredis\.(?:RunT?|NewMiniRedis)"
    r"|embeddedpostgres\.NewDatabase"
    r")\s*\("
)
_GO_FUNC_RE = re.compile(r"^func\s+(?:\([^)]*\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(")

_DEPENDENCY_MANIFEST_RE = re.compile(
    r"(?:^|/)(?:go\.mod|package\.json|Cargo\.toml|pyproject\.toml|requirements(?:[-_.][A-Za-z0-9]+)?\.txt|Gemfile|pom\.xml|build\.gradle(?:\.kts)?)$"
)


def detect_plan_dependency_manifests(target_files: list[str] | None) -> list[str]:
    """Return deduplicated dependency manifest paths declared in PLAN.md Target Files."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in target_files or []:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip().lstrip("./")
        if cleaned and _DEPENDENCY_MANIFEST_RE.search(cleaned) and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out

# --- Python loopback / socket bind detection ---
_PY_BIND_RE = re.compile(
    r"(?:\.bind\s*\(|\b(?:HTTPServer|ThreadingHTTPServer|TCPServer|ThreadingTCPServer|WSGIServer)\s*\("
    r"|\b(?:start_server|create_server|TCPSite|uvicorn\.run)\s*\(|\blive_server\b)"
)
_PY_DEF_RE = re.compile(r"^(\s*)(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# --- JS/TS loopback / socket bind detection ---
_JS_BIND_RE = re.compile(
    r"(?:\.listen\s*\(|\b(?:http|https|net|tls)\.createServer\s*\(|\bcreateServer\s*\()"
)
_JS_TEST_FILE_RE = re.compile(r"\.(?:test|spec)\.(?:[cm]?[jt]sx?)$", re.I)

# --- Rust loopback / socket bind detection ---
_RUST_BIND_RE = re.compile(r"\b(?:TcpListener|UdpSocket|UnixListener)::bind\s*\(")
_RUST_TEST_ATTR_RE = re.compile(r"^\s*#\[(?:[A-Za-z0-9_:]+::)?test(?:\([^)]*\))?\]\s*$")
_RUST_FN_RE = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _iter_source_files(worktree_path: Path, predicate: Any) -> list[Path]:
    matched: list[Path] = []
    if not worktree_path.is_dir():
        return matched
    for root, dirs, files in os.walk(worktree_path):
        dirs[:] = [d for d in dirs if d not in SKIP_WALK_DIRS and not d.startswith(".git")]
        for fname in files:
            if predicate(fname):
                matched.append(Path(root) / fname)
    matched.sort()
    return matched


def _scan_go_blocks(text: str) -> dict[str, str]:
    """Extract top-level Go function bodies keyed by function name."""
    blocks: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        m = _GO_FUNC_RE.match(lines[i])
        if not m:
            i += 1
            continue
        fn_name = m.group(1)
        body_lines = [lines[i]]
        depth = lines[i].count("{") - lines[i].count("}")
        opened = "{" in lines[i]
        i += 1
        while i < n and not (opened and depth <= 0):
            line = lines[i]
            body_lines.append(line)
            if "{" in line:
                opened = True
            depth += line.count("{") - line.count("}")
            i += 1
        blocks[fn_name] = "\n".join(body_lines)
    return blocks


def detect_go_loopback_tests(worktree_path: Path) -> list[str]:
    """Return sorted Go Test* names whose bodies (directly or via test helpers) bind sockets."""
    by_pkg: dict[Path, dict[str, str]] = {}
    for fpath in _iter_source_files(worktree_path, lambda name: name.endswith("_test.go")):
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        blocks = _scan_go_blocks(text)
        if not blocks:
            continue
        pkg_blocks = by_pkg.setdefault(fpath.parent, {})
        pkg_blocks.update(blocks)

    found: set[str] = set()
    for pkg_blocks in by_pkg.values():
        binding_funcs = {name for name, body in pkg_blocks.items() if _GO_BIND_RE.search(body)}
        if not binding_funcs:
            continue
        # Propagate through package-local test helpers (up to 5 hops)
        for _ in range(5):
            added = False
            helper_pat = re.compile(
                r"\b(?:" + "|".join(re.escape(h) for h in sorted(binding_funcs)) + r")\s*\("
            )
            for name, body in pkg_blocks.items():
                if name in binding_funcs:
                    continue
                # Strip signature before checking calls (works for single-line and multi-line funcs)
                body_only = body.split("{", 1)[1] if "{" in body else ""
                if helper_pat.search(body_only):
                    binding_funcs.add(name)
                    added = True
            if not added:
                break
        for name in binding_funcs:
            if name.startswith("Test") and len(name) > 4:
                found.add(name)
    return sorted(found)


def _scan_python_defs(text: str) -> dict[str, str]:
    """Extract Python function bodies keyed by function name (indentation-based)."""
    blocks: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        m = _PY_DEF_RE.match(lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        fn_name = m.group(2)
        body_lines = [lines[i]]
        i += 1
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                cur_indent = len(line) - len(line.lstrip())
                if cur_indent <= indent:
                    break
            body_lines.append(line)
            i += 1
        blocks[fn_name] = "\n".join(body_lines)
    return blocks


def detect_python_loopback_tests(worktree_path: Path) -> list[str]:
    """Return sorted Python test_* function names that bind local sockets/servers."""
    found: set[str] = set()
    files = _iter_source_files(
        worktree_path,
        lambda name: name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")),
    )
    for fpath in files:
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        blocks = _scan_python_defs(text)
        if not blocks:
            continue
        binding_funcs = {name for name, body in blocks.items() if _PY_BIND_RE.search(body)}
        if not binding_funcs:
            continue
        for _ in range(2):
            added = False
            helper_pat = re.compile(
                r"\b(?:" + "|".join(re.escape(h) for h in sorted(binding_funcs)) + r")\s*\("
            )
            for name, body in blocks.items():
                if name in binding_funcs:
                    continue
                body_only = body.split(":", 1)[1] if ":" in body else ""
                if helper_pat.search(body_only):
                    binding_funcs.add(name)
                    added = True
            if not added:
                break
        for name in binding_funcs:
            if name.startswith("test_"):
                found.add(name)
    return sorted(found)


def detect_js_loopback_test_files(worktree_path: Path) -> list[str]:
    """Return sorted relative JS/TS test file paths that bind local sockets/servers."""
    found: list[str] = []
    for fpath in _iter_source_files(worktree_path, lambda name: bool(_JS_TEST_FILE_RE.search(name))):
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _JS_BIND_RE.search(text):
            found.append(fpath.relative_to(worktree_path).as_posix())
    return sorted(found)


def detect_rust_loopback_tests(worktree_path: Path) -> list[str]:
    """Return sorted Rust #[test] function names that bind local sockets."""
    found: set[str] = set()
    for fpath in _iter_source_files(worktree_path, lambda name: name.endswith(".rs")):
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.splitlines()
        i = 0
        n = len(lines)
        while i < n:
            if not _RUST_TEST_ATTR_RE.match(lines[i]):
                i += 1
                continue
            j = i + 1
            while j < n and (lines[j].strip().startswith("#[") or not lines[j].strip()):
                j += 1
            if j >= n:
                break
            m = _RUST_FN_RE.match(lines[j])
            if not m:
                i = j + 1
                continue
            fn_name = m.group(1)
            body_lines = [lines[j]]
            depth = lines[j].count("{") - lines[j].count("}")
            opened = "{" in lines[j]
            k = j + 1
            while k < n and not (opened and depth <= 0):
                line = lines[k]
                body_lines.append(line)
                if "{" in line:
                    opened = True
                depth += line.count("{") - line.count("}")
                k += 1
            if _RUST_BIND_RE.search("\n".join(body_lines)):
                found.add(fn_name)
            i = k
    return sorted(found)


def _rewrite_go_test_cmd(cmd: str, skip_names: list[str]) -> str | None:
    if not skip_names:
        return None
    if not re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*go\s+test\b", cmd):
        return None
    if re.search(r"(?:^|\s)-skip(?:=|\s)", cmd):
        return None
    pattern = "^(" + "|".join(re.escape(n) for n in skip_names) + ")$"
    return re.sub(r"(\bgo\s+test\b)", rf"\1 -skip {shlex.quote(pattern)}", cmd, count=1)


def _rewrite_pytest_cmd(cmd: str, skip_names: list[str]) -> str | None:
    if not skip_names:
        return None
    if not re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:python3?\s+-m\s+pytest|pytest)\b", cmd):
        return None
    if re.search(r"(?:^|\s)-k(?:=|\s)", cmd):
        return None
    expr = "not (" + " or ".join(skip_names) + ")"
    return f"{cmd} -k {shlex.quote(expr)}"


def _rewrite_js_test_cmd(cmd: str, skip_files: list[str]) -> str | None:
    if not skip_files:
        return None
    if re.search(r"\bjest\b", cmd):
        if "--testPathIgnorePatterns" in cmd:
            return None
        pattern = "|".join(re.escape(f) for f in skip_files)
        return f"{cmd} --testPathIgnorePatterns {shlex.quote(pattern)}"
    if re.search(r"\bvitest\b", cmd):
        if "--exclude" in cmd:
            return None
        flags = " ".join(f"--exclude {shlex.quote(f)}" for f in skip_files)
        return f"{cmd} {flags}"
    return None


def _rewrite_cargo_test_cmd(cmd: str, skip_names: list[str]) -> str | None:
    if not skip_names:
        return None
    if not re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*cargo\s+test\b", cmd):
        return None
    if "--skip" in cmd:
        return None
    skip_flags = " ".join(f"--skip {shlex.quote(n)}" for n in skip_names)
    if re.search(r"(?:^|\s)--(?:\s|$)", cmd):
        return f"{cmd} {skip_flags}"
    return f"{cmd} -- {skip_flags}"


def filter_builder_checks_for_sandbox(
    builder_checks: list[str],
    worktree_path: Path,
    *,
    sandbox_mode: str,
    builder_network_access: bool,
    auto_filter: bool = True,
    extra_skip_tests: list[str] | None = None,
) -> dict[str, Any]:
    """Rewrite sandbox Builder Checks to skip loopback-binding tests across supported stacks.

    Returns:
        {
            "checks": list[str],
            "deferred_tests": list[str],
            "rewritten": bool,
        }
    """
    checks = list(builder_checks or [])
    extras = [str(s).strip() for s in (extra_skip_tests or []) if isinstance(s, str) and str(s).strip()]

    # Only auto-detect loopback tests when running inside an offline workspace-write sandbox
    offline_sandbox = sandbox_mode == "workspace-write" and not builder_network_access
    if not checks or (not extras and (not offline_sandbox or not auto_filter)):
        return {"checks": checks, "deferred_tests": [], "rewritten": False}

    # Lazily scan each stack only if a matching test command appears in builder_checks
    go_cache: list[str] | None = None
    py_cache: list[str] | None = None
    js_cache: list[str] | None = None
    rs_cache: list[str] | None = None

    filtered: list[str] = []
    deferred: set[str] = set()
    rewritten = False

    for cmd in checks:
        new_cmd: str | None = None
        if re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*go\s+test\b", cmd):
            if go_cache is None:
                go_cache = detect_go_loopback_tests(worktree_path) if (offline_sandbox and auto_filter) else []
            targets = sorted(set(go_cache) | set(extras))
            new_cmd = _rewrite_go_test_cmd(cmd, targets)
            if new_cmd:
                deferred.update(targets)
        elif re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:python3?\s+-m\s+pytest|pytest)\b", cmd):
            if py_cache is None:
                py_cache = detect_python_loopback_tests(worktree_path) if (offline_sandbox and auto_filter) else []
            targets = sorted(set(py_cache) | set(extras))
            new_cmd = _rewrite_pytest_cmd(cmd, targets)
            if new_cmd:
                deferred.update(targets)
        elif re.search(r"\b(?:jest|vitest)\b", cmd):
            if js_cache is None:
                js_cache = detect_js_loopback_test_files(worktree_path) if (offline_sandbox and auto_filter) else []
            targets = sorted(set(js_cache) | set(extras))
            new_cmd = _rewrite_js_test_cmd(cmd, targets)
            if new_cmd:
                deferred.update(targets)
        elif re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*cargo\s+test\b", cmd):
            if rs_cache is None:
                rs_cache = detect_rust_loopback_tests(worktree_path) if (offline_sandbox and auto_filter) else []
            targets = sorted(set(rs_cache) | set(extras))
            new_cmd = _rewrite_cargo_test_cmd(cmd, targets)
            if new_cmd:
                deferred.update(targets)

        if new_cmd and new_cmd != cmd:
            filtered.append(new_cmd)
            rewritten = True
        else:
            filtered.append(cmd)

    return {
        "checks": filtered,
        "deferred_tests": sorted(deferred),
        "rewritten": rewritten,
    }
