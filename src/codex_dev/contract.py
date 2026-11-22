"""Immutable PLAN.md contract management and integrity verification."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .state import atomic_write_json, atomic_write_text, read_json, utc_now


class ContractViolationError(RuntimeError):
    """Raised when the immutable PLAN.md contract has been altered or corrupted."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_target_path(raw: str) -> str:
    """Strip only relative './' prefixes and leading '/', preserving dotfiles like .gitignore."""
    tok = raw.strip()
    while tok.startswith("./"):
        tok = tok[2:]
    tok = tok.lstrip("/")
    return tok


def parse_plan_markdown(plan_text: str) -> dict[str, Any]:
    """Extract structured sections (acceptance criteria, target files, verify commands) from PLAN.md."""
    sections: dict[str, list[str]] = {}
    current_heading = "overview"
    sections[current_heading] = []

    for line in plan_text.splitlines():
        m = re.match(r"^#{1,3}\s+(.+?)\s*$", line)
        if m:
            current_heading = m.group(1).strip().lower()
            sections.setdefault(current_heading, [])
        else:
            sections.setdefault(current_heading, []).append(line)

    def collect_bullets(keys: tuple[str, ...]) -> list[str]:
        items: list[str] = []
        for heading, lines in sections.items():
            if any(k in heading for k in keys):
                for raw in lines:
                    s = raw.strip()
                    bm = re.match(r"^(?:[-*+]|\d+\.)\s+(?:\[[ xX]\]\s+)?(.+)$", s)
                    if bm:
                        val = bm.group(1).strip()
                        if val:
                            items.append(val)
        return items

    acceptance_criteria = collect_bullets(
        ("acceptance", "criteria", "验收", "完成标准", "definition of done")
    )
    target_files_raw = collect_bullets(
        ("target file", "file scope", "files", "修改范围", "文件契约", "涉及文件")
    )
    # Extract backticked file paths or leading path tokens
    target_files: list[str] = []
    for item in target_files_raw:
        bt = re.findall(r"`([^`]+)`", item)
        if bt:
            for token in bt:
                tok = normalize_target_path(token)
                if tok and " " not in tok:
                    target_files.append(tok)
        else:
            tok = normalize_target_path(item.split()[0].strip("`'\""))
            if "/" in tok or "." in tok:
                target_files.append(tok)

    verify_cmds_raw = collect_bullets(
        ("verification", "verify", "test command", "验证命令", "测试命令")
    )
    verify_commands: list[str] = []
    for item in verify_cmds_raw:
        bt = re.findall(r"`([^`]+)`", item)
        if bt:
            verify_commands.append(bt[0].strip())
        elif item:
            verify_commands.append(item)

    prepare_cmds_raw = collect_bullets(
        ("prepare", "dependency prep", "预处理", "依赖准备")
    )
    prepare_commands: list[str] = []
    for item in prepare_cmds_raw:
        bt = re.findall(r"`([^`]+)`", item)
        if bt:
            prepare_commands.append(bt[0].strip())
        elif item:
            prepare_commands.append(item)

    return {
        "acceptance_criteria": acceptance_criteria,
        "target_files": list(dict.fromkeys(target_files)),
        "verify_commands": list(dict.fromkeys(verify_commands)),
        "prepare_commands": list(dict.fromkeys(prepare_commands)),
    }


def save_immutable_contract(
    task_dir: Path,
    plan_text: str,
    *,
    explicit_verify_commands: list[str] | None = None,
    explicit_prepare_commands: list[str] | None = None,
    explicit_target_files: list[str] | None = None,
    allow_replan: bool = False,
) -> dict[str, Any]:
    """Persist PLAN.md and its cryptographic contract.json digest."""
    plan_path = task_dir / "PLAN.md"
    contract_path = task_dir / "contract.json"
    if contract_path.is_file() and not allow_replan:
        existing = read_json(contract_path) or {}
        if existing.get("locked", True):
            raise ContractViolationError(
                "PLAN.md contract is immutable once saved. Pass allow_replan only via explicit Architect replan."
            )

    normalized = plan_text.rstrip() + "\n"
    parsed = parse_plan_markdown(normalized)
    verify_cmds = (
        list(explicit_verify_commands)
        if explicit_verify_commands is not None
        else parsed["verify_commands"]
    )
    prepare_cmds = (
        list(explicit_prepare_commands)
        if explicit_prepare_commands is not None
        else parsed["prepare_commands"]
    )
    target_files = (
        list(dict.fromkeys(normalize_target_path(p) for p in explicit_target_files if normalize_target_path(p)))
        if explicit_target_files is not None
        else parsed["target_files"]
    )

    digest = sha256_text(normalized)
    contract = {
        "version": 1,
        "locked": True,
        "plan_sha256": digest,
        "acceptance_criteria": parsed["acceptance_criteria"],
        "target_files": target_files,
        "verify_commands": verify_cmds,
        "prepare_commands": prepare_cmds,
        "created_at": utc_now(),
    }

    atomic_write_text(plan_path, normalized)
    atomic_write_json(contract_path, contract)
    return contract


def verify_contract_integrity(task_dir: Path) -> dict[str, Any]:
    """Verify that PLAN.md has not been tampered with since contract creation."""
    plan_path = task_dir / "PLAN.md"
    contract_path = task_dir / "contract.json"
    if not plan_path.is_file() or not contract_path.is_file():
        return {
            "valid": False,
            "reason": "Missing PLAN.md or contract.json",
            "expected_sha256": None,
            "actual_sha256": None,
        }

    contract = read_json(contract_path) or {}
    expected = contract.get("plan_sha256")
    actual = sha256_text(plan_path.read_text(encoding="utf-8"))
    if not expected or actual != expected:
        return {
            "valid": False,
            "reason": "PLAN.md SHA-256 mismatch: immutable contract was modified!",
            "expected_sha256": expected,
            "actual_sha256": actual,
        }

    return {
        "valid": True,
        "reason": "Immutable PLAN.md contract hash verified.",
        "expected_sha256": expected,
        "actual_sha256": actual,
        "acceptance_criteria_count": len(contract.get("acceptance_criteria") or []),
    }
