#!/usr/bin/env python3
"""Deterministic Mock Codex CLI implementing `codex exec` for automated testing."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "exec":
        sys.stderr.write("mock_codex only supports 'exec' subcommand\n")
        return 2

    parser = argparse.ArgumentParser(prog="codex exec")
    parser.add_argument("-m", "--model", required=True)
    parser.add_argument("-s", "--sandbox", default="workspace-write")
    parser.add_argument("-C", "--cd", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output-schema", default=None)
    parser.add_argument("-o", "--output-last-message", required=True)
    parser.add_argument("--ephemeral", action="store_true")
    parser.add_argument("--disable", action="append", default=[])
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument("-c", "--config", action="append", default=[])
    parser.add_argument("prompt_arg", nargs="?", default="-")
    args = parser.parse_args(sys.argv[2:])

    prompt = sys.stdin.read() if args.prompt_arg == "-" else args.prompt_arg
    wt = Path(args.cd)
    out_file = Path(args.output_last_message)
    scenario = os.environ.get("MOCK_CODEX_SCENARIO", "success")
    record_file = os.environ.get("MOCK_CODEX_RECORD_FILE")

    if record_file:
        rec_path = Path(record_file)
        history = []
        if rec_path.is_file():
            history = json.loads(rec_path.read_text(encoding="utf-8"))
        history.append(
            {
                "model": args.model,
                "sandbox": args.sandbox,
                "cd": str(wt),
                "has_feedback": "REPAIR ROUND" in prompt,
                "prompt_len": len(prompt),
                "add_dirs": args.add_dir,
                "disabled": args.disable,
                "configs": args.config,
                "env_gocache": os.environ.get("GOCACHE"),
                "env_gotmpdir": os.environ.get("GOTMPDIR"),
            }
        )
        rec_path.write_text(json.dumps(history, indent=2), encoding="utf-8")

    emit({"type": "thread.started", "thread_id": "mock-thread-001"})
    emit({"type": "turn.started"})

    if scenario == "timeout":
        emit(
            {
                "type": "item.started",
                "item": {"type": "command_execution", "command": "sleep 60"},
            }
        )
        time.sleep(30)
        return 0

    if scenario == "timeout_after_write":
        # Builder writes valid working code, emits event, then times out before exiting
        (wt / "calc.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
            "def multiply(a: int, b: int) -> int:\n    return a * b\n",
            encoding="utf-8",
        )
        emit(
            {
                "type": "item.completed",
                "item": {"type": "file_change", "changes": [{"path": "calc.py"}]},
            }
        )
        time.sleep(30)
        return 0

    if scenario == "crash":
        emit({"type": "error", "message": "Simulated Codex fatal runtime crash"})
        sys.stderr.write("Fatal: simulated unexpected termination\n")
        return 137

    is_repair = "REPAIR ROUND" in prompt

    if scenario == "active_then_success":
        # Emits heartbeat events every 0.35s for ~1.4s (exceeds a 1s idle timeout ONLY if idle is broken!)
        for i in range(4):
            emit(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": f"Progress step {i + 1}"},
                }
            )
            time.sleep(0.35)
        (wt / "calc.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
            "def multiply(a: int, b: int) -> int:\n    return a * b\n",
            encoding="utf-8",
        )
        summary = "Finished cleanly while streaming active events."
    elif scenario == "dotfiles_success":
        (wt / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        (wt / ".env.example").write_text("PORT=8080\n", encoding="utf-8")
        (wt / ".dockerignore").write_text(".git\n", encoding="utf-8")
        (wt / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (wt / ".github" / "workflows" / "ci.yml").write_text("name: CI\n", encoding="utf-8")
        (wt / "calc.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
            "def multiply(a: int, b: int) -> int:\n    return a * b\n",
            encoding="utf-8",
        )
        summary = "Updated dotfiles and calc.py."
    elif scenario == "stderr_noise_success":
        for _ in range(5):
            sys.stderr.write(
                "2026-11-22T03:11:30Z ERROR codex_models_manager::manager: failed to refresh available models: request timed out\n"
            )
        sys.stderr.write("warning: real non-benign notice\n")
        sys.stderr.flush()
        (wt / "calc.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
            "def multiply(a: int, b: int) -> int:\n    return a * b\n",
            encoding="utf-8",
        )
        summary = "Succeeded despite benign model refresh stderr noise."
    elif scenario == "large_diff":
        lines = [f"LINE_{i:04d} = '{'x' * 80}'" for i in range(400)]
        (wt / "big_data.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (wt / "calc.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
            "def multiply(a: int, b: int) -> int:\n    return a * b\n",
            encoding="utf-8",
        )
        summary = "Wrote large file for diff summary test."
    elif scenario == "go_mod_change":
        (wt / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
        (wt / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
        summary = "Updated go.mod and main.go."
    elif scenario == "success":
        (wt / "calc.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
            "def multiply(a: int, b: int) -> int:\n    return a * b\n",
            encoding="utf-8",
        )
        (wt / "notes.txt").write_text("untracked helper artifact\n", encoding="utf-8")
        emit(
            {
                "type": "item.completed",
                "item": {"type": "file_change", "changes": [{"path": "calc.py"}, {"path": "notes.txt"}]},
            }
        )
        emit(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "python3 -m unittest discover -v",
                    "status": "completed",
                    "exit_code": 0,
                },
            }
        )
        summary = "Implemented multiply() cleanly and verified tests."

    elif scenario == "fix_on_round_2":
        if not is_repair:
            # Round 0 intentionally introduces a bug so independent verification fails
            (wt / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\n"
                "def multiply(a: int, b: int) -> int:\n    return a + b  # intentional bug\n",
                encoding="utf-8",
            )
            summary = "Initial implementation (contains deliberate bug for repair test)."
        else:
            # Repair round fixes the bug
            (wt / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\n"
                "def multiply(a: int, b: int) -> int:\n    return a * b\n",
                encoding="utf-8",
            )
            summary = "Fixed multiply() bug based on verification feedback."
        emit(
            {
                "type": "item.completed",
                "item": {"type": "file_change", "changes": [{"path": "calc.py"}]},
            }
        )

    elif scenario == "always_fail_test":
        (wt / "calc.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
            "def multiply(a: int, b: int) -> int:\n    return 0\n",
            encoding="utf-8",
        )
        summary = "Broken implementation that never passes tests."

    elif scenario == "tamper_test":
        # Codex tries to cheat by adding @unittest.skip to test_calc.py
        (wt / "test_calc.py").write_text(
            "import unittest\nfrom calc import add\n\n"
            "class TestCalc(unittest.TestCase):\n"
            "    @unittest.skip('cheating')\n"
            "    def test_multiply(self):\n"
            "        self.assertEqual(add(1, 1), 2)\n",
            encoding="utf-8",
        )
        summary = "Skipped test to appear green."

    elif scenario == "node_success":
        (wt / "math.mjs").write_text(
            "export function sum(a, b) { return a + b; }\n"
            "export function square(n) { return n * n; }\n",
            encoding="utf-8",
        )
        summary = "Implemented square() in Node.js project."

    else:
        summary = f"Unknown scenario {scenario}"

    emit(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1240,
                "cached_input_tokens": 420,
                "output_tokens": 310,
                "total_tokens": 1550,
            },
        }
    )

    result_obj = {
        "status": "completed",
        "summary": summary,
        "files_changed": ["calc.py"],
        "commands_run": [{"command": "python3 -m unittest discover -v", "exit_code": 0, "summary": "mock run"}],
        "acceptance_criteria_status": [
            {"criterion": "Implement requested behavior", "met": True, "evidence": "Updated source file"}
        ],
        "risks_or_notes": [],
    }
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(result_obj, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
