"""Comprehensive Mock Codex test suite for Codex Dev orchestrator."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))

from codex_dev.models import InvalidModelError
from codex_dev.orchestrator import Orchestrator
from codex_dev.state import (
    STATE_BLOCKED,
    STATE_COMPLETED,
    STATE_EXECUTING,
    STATE_FAILED,
    STATE_PLANNED,
    STATE_REVIEWING,
    STATE_REVISING,
    TaskLockError,
    atomic_write_json,
)

MOCK_CODEX_CMD = f"{sys.executable} {ROOT_DIR / 'tests' / 'mock_codex.py'}"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _create_python_repo(base_dir: Path) -> Path:
    repo = base_dir / "sample_py_repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "import unittest\n"
        "from calc import add, multiply\n\n"
        "class TestCalc(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n"
        "    def test_multiply(self):\n"
        "        self.assertEqual(multiply(3, 4), 12)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    return repo


def _create_node_repo(base_dir: Path) -> Path:
    repo = base_dir / "sample_node_repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "sample-node",
                "version": "1.0.0",
                "type": "module",
                "scripts": {"test": "node test.mjs"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (repo / "math.mjs").write_text("export function sum(a, b) { return a + b; }\n", encoding="utf-8")
    (repo / "test.mjs").write_text(
        "import assert from 'node:assert/strict';\n"
        "import { sum, square } from './math.mjs';\n"
        "assert.equal(sum(2, 3), 5);\n"
        "assert.equal(square(5), 25);\n"
        "console.log('node tests ok');\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial node commit")
    return repo


PLAN_PY = """# Plan: Add multiply()

## Goal
Add `multiply(a, b)` to `calc.py`.

## Target Files
- `calc.py`

## Acceptance Criteria
- `multiply(3, 4)` returns `12`
- Existing `add()` remains intact

## Verification Commands
- `python3 -m unittest discover -v`
"""


class TestCodexDevOrchestrator(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="codex_dev_test_")
        self.tmp_path = Path(self._tmp.name)
        self.home_dir = self.tmp_path / "codex_dev_home"
        self.home_dir.mkdir(parents=True)
        # Seed isolated global config with verified mock models and mock codex binary
        cfg = {
            "version": 1,
            "codex_binary": MOCK_CODEX_CMD,
            "sandbox_mode": "workspace-write",
            "max_repair_rounds": 3,
            "codex_timeout_sec": 30,
            "verify_timeout_sec": 30,
            "model_policy": {
                "mode": "auto",
                "locked": False,
                "allow_escalation": True,
                "default_model": "gpt-6-luna",
                "authorized_models": ["gpt-6-luna", "gpt-6.2-sol", "gpt-6.1-terra"],
                "complexity_routing": {
                    "low": "gpt-6-luna",
                    "medium": "gpt-6-luna",
                    "high": "gpt-6.2-sol",
                },
                "escalation_order": ["gpt-6-luna", "gpt-6.2-sol"],
                "reasoning_effort": "low",
            },
            "reviewer": {
                "mode": "inherit_session",
                "routing_supported": False,
                "model": None,
            },
        }
        atomic_write_json(self.home_dir / "config.json", cfg)
        self.orch = Orchestrator(home_dir=self.home_dir)
        os.environ["MOCK_CODEX_SCENARIO"] = "success"
        os.environ.pop("MOCK_CODEX_RECORD_FILE", None)

    def tearDown(self) -> None:
        os.environ.pop("MOCK_CODEX_SCENARIO", None)
        os.environ.pop("MOCK_CODEX_RECORD_FILE", None)
        self._tmp.cleanup()

    def test_01_normal_execution_persistence_worktree_and_deliver(self) -> None:
        repo = _create_python_repo(self.tmp_path)
        # Add uncommitted dirty change in main repo to verify isolation!
        (repo / "dirty_user_file.txt").write_text("user uncommitted work\n", encoding="utf-8")
        (repo / "calc.py").write_text("# user local dirty edit\ndef add(a, b): return a + b\n", encoding="utf-8")

        init_res = self.orch.init_task(name="Implement multiply", repo_path=repo, complexity="low")
        tid = init_res["task"]["task_id"]
        self.assertEqual(init_res["state"]["status"], "CREATED")
        self.assertTrue(init_res["task"]["main_repo_dirty_at_init"])

        plan_res = self.orch.save_plan(tid, PLAN_PY)
        self.assertEqual(plan_res["state"]["status"], STATE_PLANNED)

        rr = self.orch.run_round(tid)
        self.assertTrue(rr["ok"])
        self.assertEqual(rr["next_action"], "review_required")
        self.assertEqual(rr["state"]["status"], STATE_REVIEWING)
        self.assertEqual(rr["state"]["token_usage"]["total_tokens"], 1550)

        # Verify untracked file and modified file both appear in diff inspection
        diff = self.orch.get_diff(tid)
        self.assertIn("calc.py", diff["modified"])
        self.assertIn("notes.txt", diff["untracked"])
        self.assertEqual(diff["files_changed_count"], 2)

        # Verify main repo uncommitted work was NEVER touched or overwritten
        self.assertTrue((repo / "dirty_user_file.txt").is_file())
        self.assertIn("# user local dirty edit", (repo / "calc.py").read_text(encoding="utf-8"))

        # Semantic Code Review approval -> COMPLETED
        rev = self.orch.record_review(tid, verdict="approve", summary="Clean implementation, all criteria met.")
        self.assertTrue(rev["ok"])
        self.assertEqual(rev["state"]["status"], STATE_COMPLETED)

        # Verify all persistent task artifacts exist
        tdir = self.orch.store.task_dir(tid)
        for fname in (
            "task.json",
            "PLAN.md",
            "contract.json",
            "EXECUTION_LOG.md",
            "RESULT.json",
            "state.json",
            "events.jsonl",
            "verification.json",
            "reviews.json",
        ):
            self.assertTrue((tdir / fname).is_file(), f"Missing persisted artifact: {fname}")

        # Default deliver does NOT auto-commit
        deliv_dry = self.orch.deliver_task(tid, confirm_commit=False)
        self.assertFalse(deliv_dry["committed"])
        self.assertTrue(deliv_dry["requires_confirmation"])

        # Explicit human-confirmed commit commits strictly in worktree branch
        deliv_ok = self.orch.deliver_task(tid, confirm_commit=True, commit_message="feat: add multiply")
        self.assertTrue(deliv_ok["committed"])
        wt_log = _git(Path(init_res["task"]["worktree_path"]), "log", "-1", "--pretty=%s")
        self.assertEqual(wt_log, "feat: add multiply")
        # Main repo HEAD still at initial commit
        self.assertEqual(_git(repo, "log", "-1", "--pretty=%s"), "initial commit")

    def test_02_auto_repair_and_model_escalation(self) -> None:
        repo = _create_python_repo(self.tmp_path)
        rec = self.tmp_path / "record.json"
        os.environ["MOCK_CODEX_SCENARIO"] = "fix_on_round_2"
        os.environ["MOCK_CODEX_RECORD_FILE"] = str(rec)

        tid = self.orch.init_task(name="Auto repair test", repo_path=repo, complexity="low")["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)

        # Round 0: Codex introduces bug -> independent verification catches test_failure -> REVISING (round=1)
        r0 = self.orch.run_round(tid)
        self.assertFalse(r0["ok"])
        self.assertEqual(r0["next_action"], "repair_available")
        self.assertEqual(r0["state"]["status"], STATE_REVISING)
        self.assertEqual(r0["state"]["failure_category"], "test_failure")
        self.assertEqual(r0["state"]["repair_round"], 1)
        self.assertTrue((self.orch.store.task_dir(tid) / "FEEDBACK.md").is_file())

        # Round 1: Auto mode escalates model within authorized list (gpt-6-luna -> gpt-6.2-sol) and fixes bug
        r1 = self.orch.run_round(tid)
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["state"]["status"], STATE_REVIEWING)

        calls = json.loads(rec.read_text(encoding="utf-8"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["model"], "gpt-6-luna")
        self.assertEqual(calls[1]["model"], "gpt-6.2-sol")
        self.assertTrue(calls[1]["has_feedback"])

        # Now test review request_changes -> REVISING -> approve
        rev1 = self.orch.record_review(
            tid,
            verdict="request_changes",
            summary="Needs docstring",
            issues=["Add clear docstring"],
        )
        self.assertFalse(rev1["ok"])
        self.assertEqual(rev1["state"]["status"], STATE_REVISING)
        self.assertEqual(rev1["state"]["failure_category"], "review_failure")
        self.assertEqual(rev1["state"]["repair_round"], 2)

        r2 = self.orch.run_round(tid)
        self.assertTrue(r2["ok"])
        rev2 = self.orch.record_review(tid, verdict="approve", summary="Approved after fix.")
        self.assertEqual(rev2["state"]["status"], STATE_COMPLETED)

    def test_03_retry_limit_exceeded_blocks_task(self) -> None:
        repo = _create_python_repo(self.tmp_path)
        os.environ["MOCK_CODEX_SCENARIO"] = "always_fail_test"

        tid = self.orch.init_task(name="Retry cap test", repo_path=repo, max_repair_rounds=3)["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)

        # Round 0 -> repair 1, Round 1 -> repair 2, Round 2 -> repair 3, Round 3 -> BLOCKED
        for expected_round in (1, 2, 3):
            res = self.orch.run_round(tid)
            self.assertFalse(res["ok"])
            self.assertEqual(res["state"]["status"], STATE_REVISING)
            self.assertEqual(res["state"]["repair_round"], expected_round)

        final_res = self.orch.run_round(tid)
        self.assertFalse(final_res["ok"])
        self.assertEqual(final_res["next_action"], "blocked_retry_limit")
        self.assertEqual(final_res["state"]["status"], STATE_BLOCKED)
        self.assertEqual(final_res["state"]["failure_category"], "retry_limit_exceeded")

    def test_04_timeout_and_abnormal_crash_and_recovery(self) -> None:
        repo = _create_python_repo(self.tmp_path)

        # 4A: Timeout
        os.environ["MOCK_CODEX_SCENARIO"] = "timeout"
        tid_to = self.orch.init_task(name="Timeout test", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid_to, PLAN_PY)
        res_to = self.orch.execute_round(tid_to, timeout_sec=1)
        self.assertFalse(res_to["ok"])
        self.assertEqual(res_to["state"]["status"], STATE_FAILED)
        self.assertEqual(res_to["state"]["failure_category"], "timeout")

        # 4B: Abnormal crash
        os.environ["MOCK_CODEX_SCENARIO"] = "crash"
        tid_cr = self.orch.init_task(name="Crash test", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid_cr, PLAN_PY)
        res_cr = self.orch.execute_round(tid_cr)
        self.assertFalse(res_cr["ok"])
        self.assertEqual(res_cr["state"]["status"], STATE_FAILED)
        self.assertEqual(res_cr["state"]["failure_category"], "execution_failure")

        # 4C: Interrupted task recovery (simulated dead PID in EXECUTING state)
        tid_rec = self.orch.init_task(name="Recover test", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid_rec, PLAN_PY)
        st = self.orch.store.load_state(tid_rec)
        st["status"] = STATE_EXECUTING
        self.orch.store.save_state(tid_rec, st)
        self.orch.store.write_run_pid(tid_rec, 999999, 999999)
        rec_out = self.orch.recover_task(tid_rec)
        self.assertTrue(rec_out["recovered"])
        self.assertEqual(rec_out["state"]["status"], STATE_FAILED)

    def test_05_model_locking_and_invalid_model_rejection(self) -> None:
        repo = _create_python_repo(self.tmp_path)
        rec = self.tmp_path / "lock_record.json"
        os.environ["MOCK_CODEX_SCENARIO"] = "fix_on_round_2"
        os.environ["MOCK_CODEX_RECORD_FILE"] = str(rec)

        # Invalid/unverified model must be rejected immediately
        with self.assertRaises(InvalidModelError):
            self.orch.init_task(name="Bad model", repo_path=repo, model_mode="manual", model="nonexistent-model-999")

        # Manual locked model must NOT escalate on repair rounds
        tid = self.orch.init_task(
            name="Locked model test",
            repo_path=repo,
            model_mode="manual",
            model="gpt-6-luna",
            lock_model=True,
        )["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)
        self.orch.run_round(tid)  # fails round 0
        self.orch.run_round(tid)  # repairs round 1 — MUST stay on gpt-6-luna!

        calls = json.loads(rec.read_text(encoding="utf-8"))
        self.assertEqual([c["model"] for c in calls], ["gpt-6-luna", "gpt-6-luna"])

        # Attempting to override a locked model must fail cleanly
        self.orch.record_review(tid, verdict="request_changes", summary="Try model swap")
        with self.assertRaises(InvalidModelError):
            self.orch.execute_round(tid, override_model="gpt-6.2-sol")

    def test_06_immutable_contract_tamper_and_test_weakening_audit(self) -> None:
        repo = _create_python_repo(self.tmp_path)

        # 6A: Test weakening detection (@unittest.skip)
        os.environ["MOCK_CODEX_SCENARIO"] = "tamper_test"
        tid = self.orch.init_task(name="Weakening audit", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)
        res = self.orch.run_round(tid)
        self.assertFalse(res["ok"])
        self.assertEqual(res["state"]["status"], STATE_REVISING)
        self.assertGreater(res["state"]["verification_summary"]["blocking_audit_issues"], 0)

        # 6B: Immutable PLAN.md contract tampering rejected before Builder runs
        plan_path = self.orch.store.task_dir(tid) / "PLAN.md"
        plan_path.write_text("# Tampered plan\n", encoding="utf-8")
        exec_res = self.orch.execute_round(tid)
        self.assertFalse(exec_res["ok"])
        self.assertEqual(exec_res["state"]["status"], STATE_FAILED)
        self.assertEqual(exec_res["state"]["failure_category"], "contract_violation")

    def test_07_concurrent_writer_lock_and_live_cancel(self) -> None:
        repo = _create_python_repo(self.tmp_path)
        tid = self.orch.init_task(name="Lock & cancel", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)

        # Exclusive lock prevents concurrent writer on the same task
        with self.orch.store.acquire_write_lock(tid):
            with self.assertRaises(TaskLockError):
                with self.orch.store.acquire_write_lock(tid):
                    pass

        # Live cancel stops a running long Codex process cleanly
        os.environ["MOCK_CODEX_SCENARIO"] = "timeout"
        result_box: dict = {}

        def _worker() -> None:
            result_box["res"] = self.orch.execute_round(tid, timeout_sec=30)

        t = threading.Thread(target=_worker)
        t.start()
        time.sleep(0.5)
        cancel_out = self.orch.cancel_task(tid, reason="Stopped from chat")
        t.join(timeout=10)
        self.assertTrue(cancel_out["cancelled"])
        st = self.orch.store.load_state(tid)
        self.assertEqual(st["status"], STATE_BLOCKED)
        self.assertEqual(st["failure_category"], "cancelled")

    def test_08_stack_agnostic_node_verification(self) -> None:
        repo = _create_node_repo(self.tmp_path)
        os.environ["MOCK_CODEX_SCENARIO"] = "node_success"
        tid = self.orch.init_task(name="Node square feature", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(
            tid,
            "# Plan\n\n## Acceptance Criteria\n- `square(5)` returns `25`\n",
        )
        rr = self.orch.run_round(tid)
        self.assertTrue(rr["ok"])
        self.assertEqual(rr["verification"]["report"]["command_source"], "auto_detected")
        self.assertEqual(rr["verification"]["report"]["checks"][0]["name"], "npm:test")
        self.assertEqual(rr["state"]["status"], STATE_REVIEWING)


if __name__ == "__main__":
    unittest.main(verbosity=2)
