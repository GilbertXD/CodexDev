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

from codex_dev.codex_runner import build_codex_prompt
from codex_dev.config import DEFAULT_CONFIG, load_config, resolve_codex_timeouts
from codex_dev.contract import normalize_target_path, parse_plan_markdown
from codex_dev.models import InvalidModelError
from codex_dev.orchestrator import Orchestrator
from codex_dev.verifier import detect_prepare_commands, detect_skipped_tests
from codex_dev.state import (
    STATE_BLOCKED,
    STATE_COMPLETED,
    STATE_EXECUTING,
    STATE_FAILED,
    STATE_PLANNED,
    STATE_REVIEWING,
    STATE_REVISING,
    StateTransitionError,
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

    def test_09_dotfile_target_paths_preserved_without_false_unexpected_files(self) -> None:
        self.assertEqual(normalize_target_path(".gitignore"), ".gitignore")
        self.assertEqual(normalize_target_path("./.gitignore"), ".gitignore")
        self.assertEqual(normalize_target_path("././.env.example"), ".env.example")
        self.assertEqual(normalize_target_path(".dockerignore"), ".dockerignore")
        self.assertEqual(normalize_target_path("./.github/workflows/ci.yml"), ".github/workflows/ci.yml")
        self.assertEqual(normalize_target_path("/src/app.py"), "src/app.py")

        plan_with_dotfiles = """# Plan: Dotfiles

## Target Files
- `.gitignore`
- `./.env.example`
- `.dockerignore`
- `.github/workflows/ci.yml`
- ./calc.py

## Acceptance Criteria
- Dotfiles and calc.py updated cleanly

## Verification Commands
- `python3 -m unittest discover -v`
"""
        parsed = parse_plan_markdown(plan_with_dotfiles)
        self.assertEqual(
            parsed["target_files"],
            [".gitignore", ".env.example", ".dockerignore", ".github/workflows/ci.yml", "calc.py"],
        )

        repo = _create_python_repo(self.tmp_path)
        os.environ["MOCK_CODEX_SCENARIO"] = "dotfiles_success"
        tid = self.orch.init_task(name="Dotfile scope audit", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid, plan_with_dotfiles)
        rr = self.orch.run_round(tid)
        self.assertTrue(rr["ok"])
        audit = rr["verification"]["report"]["diff_audit"]
        self.assertEqual(audit["unexpected_files"], [])
        self.assertEqual(audit["warnings"], [])

    def test_10_dynamic_timeout_idle_timeout_and_attempt_log_preservation(self) -> None:
        # Dynamic timeout scales up for high reasoning_effort / complexity
        t_low, i_low = resolve_codex_timeouts(DEFAULT_CONFIG, complexity="low", reasoning_effort="low")
        t_high_eff, i_high_eff = resolve_codex_timeouts(DEFAULT_CONFIG, complexity="medium", reasoning_effort="high")
        t_high_comp, _ = resolve_codex_timeouts(DEFAULT_CONFIG, complexity="high", reasoning_effort="low")
        t_explicit, i_explicit = resolve_codex_timeouts(
            DEFAULT_CONFIG, explicit_timeout_sec=45, complexity="high", reasoning_effort="high"
        )
        self.assertEqual(t_low, 600)
        self.assertEqual(i_low, 300)
        self.assertEqual(t_high_eff, 1200)
        self.assertEqual(i_high_eff, 300)
        self.assertEqual(t_high_comp, 1200)
        self.assertEqual(t_explicit, 45)
        self.assertEqual(i_explicit, 45)

        repo = _create_python_repo(self.tmp_path)
        tid = self.orch.init_task(name="Timeout & attempt logs", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)
        tdir = self.orch.store.task_dir(tid)

        # Attempt 1: stalled process hits idle timeout (1s) well before wall-clock limit (10s)
        os.environ["MOCK_CODEX_SCENARIO"] = "timeout"
        res1 = self.orch.execute_round(tid, timeout_sec=10, idle_timeout_sec=1)
        self.assertFalse(res1["ok"])
        self.assertEqual(res1["state"]["status"], STATE_FAILED)
        self.assertEqual(res1["state"]["failure_category"], "timeout")
        self.assertEqual(res1["outcome"]["timeout_reason"], "idle")
        self.assertEqual(res1["outcome"]["attempt"], 1)
        attempt1_log = tdir / "codex_round_0_attempt_1.jsonl"
        self.assertTrue(attempt1_log.is_file())
        attempt1_content = attempt1_log.read_text(encoding="utf-8")
        self.assertIn("sleep 60", attempt1_content)

        # Attempt 2: active streaming keeps alive past 1s idle timeout and succeeds; attempt 1 log is NOT overwritten!
        os.environ["MOCK_CODEX_SCENARIO"] = "active_then_success"
        res2 = self.orch.execute_round(tid, timeout_sec=10, idle_timeout_sec=1)
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["outcome"]["attempt"], 2)
        attempt2_log = tdir / "codex_round_0_attempt_2.jsonl"
        self.assertTrue(attempt2_log.is_file())
        self.assertEqual(attempt1_log.read_text(encoding="utf-8"), attempt1_content)
        self.assertIn("Progress step 4", attempt2_log.read_text(encoding="utf-8"))

    def test_11_sandbox_prompt_guidance(self) -> None:
        prompt = build_codex_prompt(
            task_name="Demo",
            plan_text=PLAN_PY,
            feedback_text=None,
            repair_round=0,
            worktree_path=Path("/tmp/wt"),
        )
        self.assertIn("workspace-write", prompt)
        self.assertIn("GOCACHE", prompt)
        self.assertIn("127.0.0.1", prompt)
        self.assertIn("Docker daemon", prompt)
        self.assertIn("non-sandbox dependency preparation", prompt)
        self.assertIn("independent verification", prompt)

    def test_12_verify_from_failed_guarded_transition(self) -> None:
        repo = _create_python_repo(self.tmp_path)

        # 12A: Crash failure (non-timeout) must be rejected even with from_failed=True
        os.environ["MOCK_CODEX_SCENARIO"] = "crash"
        tid_crash = self.orch.init_task(name="Verify from crash rejected", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid_crash, PLAN_PY)
        self.orch.execute_round(tid_crash)
        with self.assertRaises(StateTransitionError):
            self.orch.verify_task(tid_crash, from_failed=True)

        # 12B: Timeout with NO worktree changes must be rejected even with from_failed=True
        os.environ["MOCK_CODEX_SCENARIO"] = "timeout"
        tid_empty = self.orch.init_task(name="Verify from empty timeout rejected", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid_empty, PLAN_PY)
        self.orch.execute_round(tid_empty, timeout_sec=1)
        with self.assertRaises(StateTransitionError):
            self.orch.verify_task(tid_empty, from_failed=True)

        # 12C: Timeout AFTER writing valid files: default verify_task fails, --from-failed succeeds!
        os.environ["MOCK_CODEX_SCENARIO"] = "timeout_after_write"
        tid_ok = self.orch.init_task(name="Verify from timeout artifacts", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid_ok, PLAN_PY)
        exec_res = self.orch.execute_round(tid_ok, timeout_sec=1)
        self.assertFalse(exec_res["ok"])
        self.assertEqual(exec_res["state"]["status"], STATE_FAILED)
        self.assertEqual(exec_res["state"]["failure_category"], "timeout")

        # Default (from_failed=False) must still forbid FAILED -> VERIFYING
        with self.assertRaises(StateTransitionError):
            self.orch.verify_task(tid_ok, from_failed=False)

        # Explicit Architect override (from_failed=True) verifies worktree artifacts cleanly -> REVIEWING
        ver_res = self.orch.verify_task(tid_ok, from_failed=True)
        self.assertTrue(ver_res["ok"])
        self.assertEqual(ver_res["next_action"], "review_required")
        self.assertEqual(ver_res["state"]["status"], STATE_REVIEWING)

    def test_13_builder_gocache_add_dir_and_model_refresh_noise_suppression(self) -> None:
        repo = _create_python_repo(self.tmp_path)
        rec = self.tmp_path / "builder_flags.json"
        os.environ["MOCK_CODEX_SCENARIO"] = "stderr_noise_success"
        os.environ["MOCK_CODEX_RECORD_FILE"] = str(rec)

        tid = self.orch.init_task(name="GoCache & stderr noise", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)
        res = self.orch.execute_round(tid)
        self.assertTrue(res["ok"])

        tdir = self.orch.store.task_dir(tid)
        expected_gocache = str((tdir / "gocache").resolve())
        expected_gotmp = str((tdir / "gotmp").resolve())
        calls = json.loads(rec.read_text(encoding="utf-8"))
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["env_gocache"], expected_gocache)
        self.assertEqual(c["env_gotmpdir"], expected_gotmp)
        self.assertIn(expected_gocache, c["add_dirs"])
        self.assertIn(expected_gotmp, c["add_dirs"])
        self.assertIn("api_key_model_discovery", c["disabled"])
        self.assertTrue(any("check_for_update_on_startup=false" in x for x in c["configs"]))
        self.assertTrue(any("shell_environment_policy.set.GOCACHE=" in x for x in c["configs"]))
        self.assertTrue(any("shell_environment_policy.set.GOTMPDIR=" in x for x in c["configs"]))

        # Repetitive benign model-refresh timeout lines are collapsed while real stderr lines remain
        stderr_tail = res["outcome"]["stderr_tail"]
        self.assertIn("warning: real non-benign notice", stderr_tail)
        self.assertIn("suppressed 5 benign codex model-refresh timeout", stderr_tail)
        self.assertNotIn("codex_models_manager::manager", stderr_tail)

    def test_14_non_sandbox_dependency_prepare_env_and_failure_isolation(self) -> None:
        repo = _create_python_repo(self.tmp_path)
        # Configure verify.env / prepare.env passthrough (e.g. GOPROXY)
        cfg_path = self.home_dir / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["verify"] = {"commands": [], "env": {"GOPROXY": "https://goproxy.cn,direct", "VERIFY_FLAG": "from_verify"}}
        cfg["prepare"] = {
            "enabled": True,
            "auto_go_mod_tidy": True,
            "commands": [],
            "timeout_sec": 60,
            "env": {"PREP_FLAG": "from_prepare"},
        }
        atomic_write_json(cfg_path, cfg)

        # 14A: Auto-detection of go mod tidy when go.mod changed
        os.environ["MOCK_CODEX_SCENARIO"] = "go_mod_change"
        tid_go = self.orch.init_task(name="Go mod auto tidy detection", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid_go, "# Plan\n\n## Target Files\n- `go.mod`\n- `main.go`\n")
        self.orch.execute_round(tid_go)
        meta_go = self.orch.store.load_task_meta(tid_go)
        wt_go = Path(meta_go["worktree_path"])
        diff_go = self.orch.get_diff(tid_go, include_diff=False)
        src, prep_cmds = detect_prepare_commands(wt_go, diff_go, cfg)
        self.assertEqual(src, "auto_detected")
        self.assertEqual(prep_cmds[0]["command"], "go mod tidy")

        # 14B: Explicit prepare command receives GOPROXY + PREP_FLAG, generates go.sum without unexpected_files warning,
        # and verify receives GOPROXY + VERIFY_FLAG
        os.environ["MOCK_CODEX_SCENARIO"] = "success"
        tid_ok = self.orch.init_task(
            name="Prepare env passthrough",
            repo_path=repo,
            prepare_commands=[
                'python3 -c "import os; assert os.environ.get(\'GOPROXY\')==\'https://goproxy.cn,direct\'; '
                'assert os.environ.get(\'PREP_FLAG\')==\'from_prepare\'; open(\'prepared.ok\', \'w\').write(\'ok\')"'
            ],
            verify_commands=[
                'python3 -c "import os, pathlib; assert os.environ.get(\'GOPROXY\')==\'https://goproxy.cn,direct\'; '
                'assert os.environ.get(\'VERIFY_FLAG\')==\'from_verify\'; assert pathlib.Path(\'prepared.ok\').is_file()"',
                "python3 -m unittest discover -v",
            ],
        )["task"]["task_id"]
        self.orch.save_plan(tid_ok, PLAN_PY)
        rr_ok = self.orch.run_round(tid_ok)
        self.assertTrue(rr_ok["ok"])
        self.assertEqual(rr_ok["state"]["status"], STATE_REVIEWING)
        self.assertTrue((self.orch.store.task_dir(tid_ok) / "prepare.json").is_file())

        # 14C: Failing prepare blocks task as dependency_prepare_failure WITHOUT burning a repair_round
        tid_fail = self.orch.init_task(
            name="Prepare failure does not burn repair round",
            repo_path=repo,
            prepare_commands=['python3 -c "import sys; sys.stderr.write(\'proxy unreachable\\n\'); sys.exit(1)"'],
        )["task"]["task_id"]
        self.orch.save_plan(tid_fail, PLAN_PY)
        rr_fail = self.orch.run_round(tid_fail)
        self.assertFalse(rr_fail["ok"])
        self.assertEqual(rr_fail["next_action"], "blocked_dependency_prepare")
        self.assertEqual(rr_fail["state"]["status"], STATE_BLOCKED)
        self.assertEqual(rr_fail["state"]["failure_category"], "dependency_prepare_failure")
        self.assertEqual(rr_fail["state"]["repair_round"], 0)

    def test_15_diff_summary_only_omits_diff_body(self) -> None:
        repo = _create_python_repo(self.tmp_path)
        os.environ["MOCK_CODEX_SCENARIO"] = "large_diff"
        tid = self.orch.init_task(name="Large diff summary test", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)
        self.orch.execute_round(tid)

        # Full diff includes diff_excerpt and full_diff (> 10 KB)
        full_d = self.orch.get_diff(tid, include_diff=True)
        self.assertIn("full_diff", full_d)
        self.assertIn("diff_excerpt", full_d)

        # Summary-only omits both full_diff and diff_excerpt completely
        summary_d = self.orch.get_diff(tid, include_diff=False)
        self.assertNotIn("full_diff", summary_d)
        self.assertNotIn("diff_excerpt", summary_d)
        self.assertNotIn("diff_truncated", summary_d)
        self.assertEqual(summary_d["files_changed_count"], 2)
        self.assertLess(len(json.dumps(summary_d)), 2000)

        # Verify via CLI `diff --summary-only` directly too
        cli_proc = subprocess.run(
            [sys.executable, "-m", "codex_dev", "--home", str(self.home_dir), "diff", "--task-id", tid, "--summary-only"],
            cwd=str(ROOT_DIR),
            env={**os.environ, "PYTHONPATH": str(ROOT_DIR / "src")},
            capture_output=True,
            text=True,
            check=True,
        )
        cli_data = json.loads(cli_proc.stdout)
        self.assertNotIn("full_diff", cli_data)
        self.assertNotIn("diff_excerpt", cli_data)
        self.assertLess(len(cli_proc.stdout), 2000)

    def test_16_verifying_clears_stale_error_and_verification_summary(self) -> None:
        repo = _create_python_repo(self.tmp_path)
        os.environ["MOCK_CODEX_SCENARIO"] = "timeout_after_write"
        tid = self.orch.init_task(name="Verify rerun clears stale error", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)
        tdir = self.orch.store.task_dir(tid)

        # Simulate previous failed state with stale last_error + stale verification.json on disk
        res_to = self.orch.execute_round(tid, timeout_sec=1)
        self.assertEqual(res_to["state"]["status"], STATE_FAILED)
        self.assertIsNotNone(res_to["state"]["last_error"])
        atomic_write_json(
            tdir / "verification.json",
            {"passed": False, "checks": [{"name": "old", "command": "old", "passed": False, "exit_code": 1}]},
        )

        # Run a verify command that inspects state.json and verification.json WHILE verify is in flight
        mid_check_file = self.tmp_path / "mid_verify_snapshot.json"
        probe_cmd = (
            f"python3 -c \"import json, pathlib; "
            f"tdir = pathlib.Path({str(tdir)!r}); "
            f"st = json.loads((tdir / 'state.json').read_text()); "
            f"out = {{'status': st['status'], 'last_error': st['last_error'], "
            f"'failure_category': st['failure_category'], 'verification_summary': st['verification_summary'], "
            f"'ver_file_exists': (tdir / 'verification.json').exists()}}; "
            f"pathlib.Path({str(mid_check_file)!r}).write_text(json.dumps(out))\""
        )
        ver_res = self.orch.verify_task(
            tid,
            verify_commands=[probe_cmd, "python3 -m unittest discover -v"],
            from_failed=True,
        )
        self.assertTrue(ver_res["ok"])
        self.assertEqual(ver_res["state"]["status"], STATE_REVIEWING)

        mid = json.loads(mid_check_file.read_text(encoding="utf-8"))
        self.assertEqual(mid["status"], "VERIFYING")
        self.assertIsNone(mid["last_error"])
        self.assertIsNone(mid["failure_category"])
        self.assertIsNone(mid["verification_summary"])
        self.assertFalse(mid["ver_file_exists"])

    def test_17_repo_overrides_and_local_config_precedence(self) -> None:
        repo_a = _create_python_repo(self.tmp_path / "a")
        repo_b = _create_python_repo(self.tmp_path / "b")

        # Configure global verify.env + repo_overrides scoped strictly to repo_a
        global_cfg = json.loads((self.home_dir / "config.json").read_text(encoding="utf-8"))
        global_cfg["verify"] = {"commands": [], "env": {"GLOBAL_VAR": "from-global", "SHARED_VAR": "global"}}
        global_cfg["repo_overrides"] = {
            str(repo_a): {
                "verify": {
                    "env": {
                        "SHARED_VAR": "repo-override",
                        "TEST_DATABASE_URL": "postgres://localhost:5432/repo_a",
                    }
                }
            }
        }
        atomic_write_json(self.home_dir / "config.json", global_cfg)

        # Shared repo config (lower precedence than repo_overrides)
        atomic_write_json(
            repo_a / ".codex-dev.json",
            {"verify": {"env": {"SHARED_VAR": "repo-shared", "REPO_SHARED_ONLY": "yes"}}},
        )

        cfg_a = load_config(repo_path=repo_a, home_dir=self.home_dir)
        self.assertEqual(cfg_a["verify"]["env"]["GLOBAL_VAR"], "from-global")
        self.assertEqual(cfg_a["verify"]["env"]["REPO_SHARED_ONLY"], "yes")
        self.assertEqual(cfg_a["verify"]["env"]["SHARED_VAR"], "repo-override")
        self.assertEqual(cfg_a["verify"]["env"]["TEST_DATABASE_URL"], "postgres://localhost:5432/repo_a")

        # repo_b must not inherit repo_a's repo_overrides
        cfg_b = load_config(repo_path=repo_b, home_dir=self.home_dir)
        self.assertEqual(cfg_b["verify"]["env"]["SHARED_VAR"], "global")
        self.assertNotIn("TEST_DATABASE_URL", cfg_b["verify"]["env"])

        # Machine-local .codex-dev.local.json overrides repo_overrides
        atomic_write_json(
            repo_a / ".codex-dev.local.json",
            {"verify": {"env": {"SHARED_VAR": "repo-local", "LOCAL_ONLY": "1"}}},
        )
        cfg_a_local = load_config(repo_path=repo_a, home_dir=self.home_dir)
        self.assertEqual(cfg_a_local["verify"]["env"]["SHARED_VAR"], "repo-local")
        self.assertEqual(cfg_a_local["verify"]["env"]["LOCAL_ONLY"], "1")
        self.assertEqual(cfg_a_local["verify"]["env"]["TEST_DATABASE_URL"], "postgres://localhost:5432/repo_a")

        # End-to-end verification subprocess receives the merged repo-level env vars
        (repo_a / ".codex-dev.json").unlink()
        (repo_a / ".codex-dev.local.json").unlink()
        tid = self.orch.init_task(name="Verify repo_overrides env", repo_path=repo_a)["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)
        self.orch.execute_round(tid)
        ver_res = self.orch.verify_task(
            tid,
            verify_commands=[
                "python3 -c \"import os, sys; sys.exit(0 if os.environ.get('TEST_DATABASE_URL') == 'postgres://localhost:5432/repo_a' else 1)\"",
                "python3 -m unittest discover -v",
            ],
        )
        self.assertTrue(ver_res["ok"])
        self.assertEqual(ver_res["state"]["status"], STATE_REVIEWING)

    def test_18_skipped_tests_detection_and_warning(self) -> None:
        # Multi-stack parser checks
        go_out = (
            "=== RUN   TestProjectLifecycle\n"
            "    project_integration_test.go:124: TEST_DATABASE_URL is unset\n"
            "--- SKIP: TestProjectLifecycle (0.00s)\n"
            "=== RUN   TestRepositoryLifecycle\n"
            "--- SKIP: TestRepositoryLifecycle (0.00s)\n"
            "PASS\n"
        )
        go_skips = detect_skipped_tests(go_out)
        self.assertEqual(go_skips["skipped_count"], 2)
        self.assertEqual(go_skips["skipped_tests"], ["TestProjectLifecycle", "TestRepositoryLifecycle"])

        py_out = "test_db (test_app.TestDB) ... skipped 'no db'\n\nRan 3 tests in 0.01s\n\nOK (skipped=2)\n"
        py_skips = detect_skipped_tests("", py_out)
        self.assertEqual(py_skips["skipped_count"], 2)
        self.assertIn("test_db (test_app.TestDB)", py_skips["skipped_tests"])

        pytest_out = "tests/test_db.py::test_insert SKIPPED [ 50%]\n====== 1 passed, 1 skipped in 0.04s ======\n"
        pytest_skips = detect_skipped_tests(pytest_out)
        self.assertEqual(pytest_skips["skipped_count"], 1)
        self.assertEqual(pytest_skips["skipped_tests"], ["tests/test_db.py::test_insert"])

        rust_out = "test db::test_pg ... ignored\ntest result: ok. 4 passed; 0 failed; 1 ignored; 0 measured;\n"
        rust_skips = detect_skipped_tests(rust_out)
        self.assertEqual(rust_skips["skipped_count"], 1)
        self.assertEqual(rust_skips["skipped_tests"], ["db::test_pg"])

        node_out = "# tests 5\n# pass 3\n# skipped 2\n"
        self.assertEqual(detect_skipped_tests(node_out)["skipped_count"], 2)

        jest_out = "Tests:       3 skipped, 10 passed, 13 total\n"
        self.assertEqual(detect_skipped_tests(jest_out)["skipped_count"], 3)

        # Clean run has zero skips
        self.assertEqual(detect_skipped_tests("PASS\nok example 0.01s\n")["skipped_count"], 0)

        # End-to-end verification records skipped_tests_total + non-blocking audit warning
        repo = _create_python_repo(self.tmp_path / "skip_repo")
        tid = self.orch.init_task(name="Detect skipped tests", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)
        self.orch.execute_round(tid)
        emit_skip_cmd = (
            "python3 -c \"print('--- SKIP: TestProjectLifecycle (0.00s)\\n--- SKIP: TestRepositoryLifecycle (0.00s)\\nPASS')\""
        )
        ver_res = self.orch.verify_task(
            tid,
            verify_commands=[emit_skip_cmd, "python3 -m unittest discover -v"],
        )
        self.assertTrue(ver_res["ok"])
        self.assertEqual(ver_res["state"]["status"], STATE_REVIEWING)
        report = ver_res["report"]
        self.assertEqual(report["skipped_tests_total"], 2)
        self.assertEqual(report["skipped_tests"], ["TestProjectLifecycle", "TestRepositoryLifecycle"])
        self.assertEqual(report["checks"][0]["skipped_count"], 2)
        self.assertEqual(report["checks"][1]["skipped_count"], 0)
        warning_codes = [w["code"] for w in report["diff_audit"]["warnings"]]
        self.assertIn("skipped_tests", warning_codes)

        vs = ver_res["state"]["verification_summary"]
        self.assertEqual(vs["skipped_tests_total"], 2)
        self.assertEqual(vs["skipped_tests"], ["TestProjectLifecycle", "TestRepositoryLifecycle"])
        self.assertEqual(vs["checks"][0]["skipped_count"], 2)
        self.assertGreaterEqual(vs["warnings"], 1)

    def test_19_benign_stderr_does_not_reset_idle_timeout_and_sandbox_prompt(self) -> None:
        from codex_dev.codex_runner import build_codex_prompt

        prompt = build_codex_prompt(
            task_name="Sandbox guidance check",
            plan_text=PLAN_PY,
            feedback_text=None,
            repair_round=0,
            worktree_path=self.tmp_path,
        )
        self.assertIn("Prefer running offline/unit package tests", prompt)
        self.assertIn("docker info", prompt)

        repo = _create_python_repo(self.tmp_path / "benign_hang")
        os.environ["MOCK_CODEX_SCENARIO"] = "benign_stderr_hang"
        tid = self.orch.init_task(name="Benign stderr idle timeout", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)

        exec_res = self.orch.execute_round(tid, timeout_sec=10, idle_timeout_sec=1)
        self.assertFalse(exec_res["ok"])
        outcome = exec_res["outcome"]
        self.assertTrue(outcome["timed_out"])
        self.assertEqual(outcome["timeout_reason"], "idle")
        self.assertLess(outcome["duration_sec"], 6.0)
        self.assertIn("benign codex model-refresh timeout", outcome["stderr_tail"])

    def test_20_builder_strict_isolation_codex_home_and_flags(self) -> None:
        fake_user_codex = self.tmp_path / "user_codex_home"
        fake_user_codex.mkdir(parents=True, exist_ok=True)
        (fake_user_codex / "auth.json").write_text('{"token":"fake-auth"}\n', encoding="utf-8")
        (fake_user_codex / "models_cache.json").write_text('{"models":[]}\n', encoding="utf-8")
        (fake_user_codex / "AGENTS.md").write_text("# Personal instructions\n", encoding="utf-8")
        (fake_user_codex / "skills").mkdir(exist_ok=True)
        (fake_user_codex / "skills" / "custom.md").write_text("personal skill\n", encoding="utf-8")
        orig_codex_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(fake_user_codex)
        try:
            repo = _create_python_repo(self.tmp_path / "iso_repo")
            rec_file = self.tmp_path / "iso_record.json"
            os.environ["MOCK_CODEX_RECORD_FILE"] = str(rec_file)

            # 1) Default strict isolation: isolated CODEX_HOME + disabled plugins/apps/hooks/skill_search
            tid = self.orch.init_task(name="Strict isolation test", repo_path=repo)["task"]["task_id"]
            self.orch.save_plan(tid, PLAN_PY)
            res = self.orch.execute_round(tid)
            self.assertTrue(res["ok"])

            records = json.loads(rec_file.read_text(encoding="utf-8"))
            self.assertEqual(len(records), 1)
            rec = records[0]
            expected_iso = str((self.orch.home_dir / "codex_home").resolve())
            self.assertEqual(rec["env_codex_home"], expected_iso)
            self.assertNotEqual(rec["env_codex_home"], str(fake_user_codex.resolve()))
            for feat in ("api_key_model_discovery", "plugins", "apps", "hooks", "skill_search"):
                self.assertIn(feat, rec["disabled"])
            self.assertIn("notify=[]", rec["configs"])

            iso_dir = Path(expected_iso)
            self.assertTrue((iso_dir / "auth.json").is_symlink())
            self.assertTrue((iso_dir / "models_cache.json").is_symlink())
            self.assertEqual((iso_dir / "auth.json").resolve(), (fake_user_codex / "auth.json").resolve())
            self.assertFalse((iso_dir / "AGENTS.md").exists())
            self.assertFalse((iso_dir / "skills").exists())
            cfg_toml = (iso_dir / "config.toml").read_text(encoding="utf-8")
            self.assertIn("plugins = false", cfg_toml)
            self.assertIn("notify = []", cfg_toml)
            # User's original ~/.codex remains completely untouched
            self.assertTrue((fake_user_codex / "AGENTS.md").is_file())
            self.assertTrue((fake_user_codex / "skills" / "custom.md").is_file())

            # 2) Inherit mode via repo override preserves user CODEX_HOME and does not disable plugins
            (repo / ".codex-dev.local.json").write_text(
                json.dumps({"builder_isolation": "inherit"}), encoding="utf-8"
            )
            tid2 = self.orch.init_task(name="Inherit isolation test", repo_path=repo)["task"]["task_id"]
            self.orch.save_plan(tid2, PLAN_PY)
            res2 = self.orch.execute_round(tid2)
            self.assertTrue(res2["ok"])
            records = json.loads(rec_file.read_text(encoding="utf-8"))
            rec2 = records[1]
            self.assertEqual(rec2["env_codex_home"], str(fake_user_codex))
            self.assertNotIn("plugins", rec2["disabled"])
            self.assertIn("api_key_model_discovery", rec2["disabled"])
        finally:
            if orig_codex_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = orig_codex_home

    def test_21_sandbox_builder_checks_and_network_access_override(self) -> None:
        repo = _create_python_repo(self.tmp_path / "sandbox_split_repo")
        rec_file = self.tmp_path / "sandbox_split_record.json"
        os.environ["MOCK_CODEX_RECORD_FILE"] = str(rec_file)
        os.environ["MOCK_CODEX_SCENARIO"] = "ready_for_verify"

        plan_with_builder_checks = (
            PLAN_PY
            + "\n## Builder Checks\n"
            + "- `python3 -m py_compile calc.py`\n"
        )
        tid = self.orch.init_task(name="Sandbox split default", repo_path=repo)["task"]["task_id"]
        plan_res = self.orch.save_plan(tid, plan_with_builder_checks)
        self.assertEqual(plan_res["contract"]["builder_checks"], ["python3 -m py_compile calc.py"])

        round_res = self.orch.run_round(tid)
        self.assertTrue(round_res["ok"])
        self.assertEqual(round_res["next_action"], "review_required")
        self.assertEqual(round_res["execution"]["outcome"]["structured_result"]["status"], "ready_for_verify")

        records = json.loads(rec_file.read_text(encoding="utf-8"))
        self.assertTrue(records[0]["has_builder_checks"])
        self.assertNotIn("sandbox_workspace_write.network_access=true", records[0]["configs"])

        # Enable per-repo builder.network_access via repo_overrides
        cfg_path = self.orch.home_dir / "config.json"
        cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg_data.setdefault("repo_overrides", {})[str(repo.resolve())] = {
            "builder": {"network_access": True}
        }
        cfg_path.write_text(json.dumps(cfg_data, indent=2), encoding="utf-8")

        tid2 = self.orch.init_task(name="Sandbox network override", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid2, plan_with_builder_checks)
        exec_res2 = self.orch.execute_round(tid2)
        self.assertTrue(exec_res2["ok"])
        records = json.loads(rec_file.read_text(encoding="utf-8"))
        self.assertIn("sandbox_workspace_write.network_access=true", records[1]["configs"])

    def test_22_post_turn_completed_graceful_termination(self) -> None:
        cfg_path = self.orch.home_dir / "config.json"
        cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg_data["codex_post_turn_grace_sec"] = 1
        cfg_path.write_text(json.dumps(cfg_data, indent=2), encoding="utf-8")

        repo = _create_python_repo(self.tmp_path / "post_turn_hang_repo")
        os.environ["MOCK_CODEX_SCENARIO"] = "hang_after_turn_completed"
        tid = self.orch.init_task(name="Post turn grace termination", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)

        round_res = self.orch.run_round(tid, codex_timeout_sec=20)
        self.assertTrue(round_res["ok"])
        self.assertEqual(round_res["next_action"], "review_required")
        outcome = round_res["execution"]["outcome"]
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["exit_code"], 0)
        self.assertLess(outcome["duration_sec"], 6.0)
        self.assertEqual(outcome["structured_result"]["status"], "completed")

        result_doc = json.loads((self.orch.store.task_dir(tid) / "RESULT.json").read_text(encoding="utf-8"))
        self.assertTrue(result_doc["graceful_post_turn_exit"])
        events = self.orch.store.read_events(tid)
        self.assertIn("codex_graceful_exit", [e.get("type") for e in events])

    def test_23_task_level_verify_env_injection_and_precedence(self) -> None:
        from codex_dev.verifier import parse_env_assignments

        with self.assertRaises(ValueError):
            parse_env_assignments(["NO_EQUALS_SIGN"])
        with self.assertRaises(ValueError):
            parse_env_assignments(["123BAD=value"])

        repo = _create_python_repo(self.tmp_path / "verify_env_repo")
        (repo / ".codex-dev.local.json").write_text(
            json.dumps({"verify": {"env": {"FROM_CONFIG": "cfg-val", "OVERRIDE_ME": "from-config"}}}),
            encoding="utf-8",
        )
        os.environ["MOCK_CODEX_SCENARIO"] = "success"
        tid = self.orch.init_task(
            name="Task verify env",
            repo_path=repo,
            verify_env=["LIVE_TARGET=/tmp/sample-repo", "OVERRIDE_ME=from-init"],
        )["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY, verify_env=["PLAN_SECRET=s3cr3t-value"])

        check_cmd = (
            "python3 -c \"import hashlib, os; "
            "assert os.environ.get('FROM_CONFIG') == 'cfg-val'; "
            "assert os.environ.get('LIVE_TARGET') == '/tmp/sample-repo'; "
            "assert hashlib.sha256(os.environ.get('PLAN_SECRET', '').encode()).hexdigest() == "
            "'1f3fa74b1208842aad0b685f0cd06053a9e84f0eb7f2c1c94c96ea25cb13cd77'; "
            "assert os.environ.get('OVERRIDE_ME') == 'from-call'; "
            "print('ALL_ENV_OK')\""
        )
        res = self.orch.run_round(
            tid,
            verify_commands=[check_cmd, "python3 -m unittest discover -v"],
            verify_env=["OVERRIDE_ME=from-call"],
        )
        self.assertTrue(res["ok"])
        report = res["verification"]["report"]
        self.assertEqual(report["skipped_tests_total"], 0)
        self.assertEqual(
            report["explicit_env_keys"],
            ["LIVE_TARGET", "OVERRIDE_ME", "PLAN_SECRET"],
        )
        # verification.json records only env key names, never raw secret values
        ver_raw = (self.orch.store.task_dir(tid) / "verification.json").read_text(encoding="utf-8")
        self.assertNotIn("s3cr3t-value", ver_raw)

    def test_24_reasoning_effort_routing_escalation_clamping_and_disallowed_ban(self) -> None:
        from codex_dev.models import resolve_task_model_policy, select_model_for_round

        # 1) max and ultra are permanently banned on explicit requests
        repo = _create_python_repo(self.tmp_path / "effort_repo")
        for forbidden in ("max", "ultra", "MAX", "Ultra"):
            with self.assertRaises(InvalidModelError):
                self.orch.init_task(name="Banned effort", repo_path=repo, reasoning_effort=forbidden)

        # 2) Complexity -> reasoning_effort auto-routing (low/medium/high)
        t_low = self.orch.init_task(name="Low tier", repo_path=repo, complexity="low")["state"]["model_policy"]
        t_med = self.orch.init_task(name="Med tier", repo_path=repo, complexity="medium")["state"]["model_policy"]
        t_high = self.orch.init_task(name="High tier", repo_path=repo, complexity="high")["state"]["model_policy"]
        self.assertEqual(t_low["current_reasoning_effort"], "low")
        self.assertEqual(t_med["current_reasoning_effort"], "medium")
        self.assertEqual(t_high["current_reasoning_effort"], "high")
        self.assertEqual(t_high["max_auto_effort"], "xhigh")

        # 3) Legacy config with max/ultra in auto mode clamps cleanly to <= xhigh and never leaks max/ultra
        cfg = {
            "model_policy": {
                "mode": "auto",
                "authorized_models": ["gpt-6-luna", "gpt-6.2-sol"],
                "complexity_routing": {"low": "gpt-6-luna", "medium": "gpt-6-luna", "high": "gpt-6.2-sol"},
                "escalation_order": ["gpt-6-luna", "gpt-6.2-sol"],
                "reasoning_effort": "ultra",
                "effort_by_complexity": {"low": "low", "medium": "medium", "high": "max"},
                "max_auto_effort": "ultra",
            }
        }
        cached = [
            {"slug": "gpt-6-luna", "supported_efforts": ["low", "medium", "high"]},
            {"slug": "gpt-6.2-sol", "supported_efforts": ["low", "medium", "high", "xhigh", "max", "ultra"]},
        ]
        pol_high = resolve_task_model_policy(cfg, complexity="high", cached_models=cached)
        self.assertEqual(pol_high["current_reasoning_effort"], "xhigh")
        self.assertEqual(pol_high["max_auto_effort"], "xhigh")

        # Capability clamping when model only supports up to high
        pol_clamped = resolve_task_model_policy(
            {
                "model_policy": {
                    "mode": "auto",
                    "authorized_models": ["gpt-6-luna"],
                    "complexity_routing": {"high": "gpt-6-luna"},
                    "effort_by_complexity": {"high": "xhigh"},
                    "max_auto_effort": "xhigh",
                }
            },
            complexity="high",
            cached_models=cached,
        )
        self.assertEqual(pol_clamped["current_reasoning_effort"], "high")

        # 4) Dynamic escalation: test_failure escalates effort high -> xhigh (and stops at xhigh, never max);
        #    targeted review_failure retains current effort!
        pol = resolve_task_model_policy(
            {
                "model_policy": {
                    "mode": "auto",
                    "authorized_models": ["gpt-6.2-sol"],
                    "complexity_routing": {"high": "gpt-6.2-sol"},
                    "escalation_order": ["gpt-6.2-sol"],
                    "effort_by_complexity": {"high": "high"},
                    "max_auto_effort": "xhigh",
                }
            },
            complexity="high",
            cached_models=cached,
        )
        sel_review = select_model_for_round(
            pol,
            cfg,
            repair_round=1,
            escalate=True,
            failure_category="review_failure",
            cached_models=cached,
        )
        self.assertEqual(sel_review.reasoning_effort, "high")
        self.assertIsNone(sel_review.effort_escalated_from)

        sel_test = select_model_for_round(
            pol,
            cfg,
            repair_round=1,
            escalate=True,
            failure_category="test_failure",
            cached_models=cached,
        )
        self.assertEqual(sel_test.reasoning_effort, "xhigh")
        self.assertEqual(sel_test.effort_escalated_from, "high")

        # Already at xhigh -> never escalates to max/ultra
        pol["current_reasoning_effort"] = "xhigh"
        sel_cap = select_model_for_round(
            pol,
            cfg,
            repair_round=2,
            escalate=True,
            failure_category="test_failure",
            cached_models=cached,
        )
        self.assertEqual(sel_cap.reasoning_effort, "xhigh")
        self.assertIsNone(sel_cap.effort_escalated_from)

        # 5) Locked effort prevents escalation and per-round override, and per-round max/ultra is rejected
        tid_locked = self.orch.init_task(
            name="Locked effort",
            repo_path=repo,
            complexity="high",
            reasoning_effort="medium",
            lock_effort=True,
        )["task"]["task_id"]
        self.orch.save_plan(tid_locked, PLAN_PY)
        with self.assertRaises(InvalidModelError):
            self.orch.execute_round(tid_locked, override_effort="high")
        with self.assertRaises(InvalidModelError):
            self.orch.execute_round(tid_locked, override_effort="max")

    def test_25_active_wall_clock_grace_and_token_accounting(self) -> None:
        cfg_path = self.orch.home_dir / "config.json"
        cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg_data["codex_active_grace_sec"] = 2
        cfg_data["codex_active_grace_window_sec"] = 2
        cfg_data["codex_max_active_extensions"] = 2
        cfg_path.write_text(json.dumps(cfg_data, indent=2), encoding="utf-8")

        repo = _create_python_repo(self.tmp_path / "active_grace_repo")

        # 1) Active Builder streaming past soft deadline gets extended and completes cleanly with exact tokens
        os.environ["MOCK_CODEX_SCENARIO"] = "active_past_soft_deadline"
        tid = self.orch.init_task(name="Active soft grace", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid, PLAN_PY)
        res = self.orch.execute_round(tid, timeout_sec=1, idle_timeout_sec=5)
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(res["outcome"]["active_extensions_used"], 1)
        self.assertGreater(res["outcome"]["effective_timeout_sec"], 1)
        self.assertEqual(res["state"]["token_usage"]["total_tokens"], 1550)
        self.assertEqual(res["state"]["token_usage"]["rounds_with_usage"], 1)
        self.assertEqual(res["state"]["token_usage"]["rounds_missing_usage"], 0)
        self.assertFalse(res["state"]["token_usage"]["partial"])
        ev_types = [e.get("type") for e in self.orch.store.read_events(tid)]
        self.assertIn("codex_timeout_extended", ev_types)

        # 2) Runaway active stream hits hard ceiling after max_active_extensions and records partial/estimated missing usage
        cfg_data["codex_active_grace_sec"] = 1
        cfg_data["codex_max_active_extensions"] = 1
        cfg_path.write_text(json.dumps(cfg_data, indent=2), encoding="utf-8")
        os.environ["MOCK_CODEX_SCENARIO"] = "active_runaway_forever"
        tid2 = self.orch.init_task(name="Runaway hard ceiling", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid2, PLAN_PY)
        res2 = self.orch.execute_round(tid2, timeout_sec=1, idle_timeout_sec=5)
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["outcome"]["timeout_reason"], "wall_clock")
        self.assertEqual(res2["outcome"]["active_extensions_used"], 1)
        tok2 = res2["state"]["token_usage"]
        self.assertTrue(tok2["partial"])
        self.assertEqual(tok2["rounds_missing_usage"], 1)
        self.assertEqual(tok2["total_tokens"], 0)
        self.assertGreater(tok2["estimated_missing_tokens"], 0)
        self.assertIsNotNone(res2["state"]["model_history"][0].get("estimated_token_usage"))

        # 3) Cumulative token_count events do not double-count exact totals
        os.environ["MOCK_CODEX_SCENARIO"] = "multi_turn_usage"
        tid3 = self.orch.init_task(name="Cumulative token count", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(tid3, PLAN_PY)
        res3 = self.orch.execute_round(tid3)
        self.assertTrue(res3["ok"])
        self.assertEqual(res3["outcome"]["token_usage"]["total_tokens"], 1550)
        self.assertEqual(res3["state"]["token_usage"]["total_tokens"], 1550)
        self.assertEqual(res3["state"]["token_usage"]["input_tokens"], 1240)
        self.assertEqual(res3["state"]["token_usage"]["output_tokens"], 310)

    def test_26_sandbox_builder_checks_stack_agnostic_loopback_filter(self) -> None:
        from codex_dev.sandbox_filter import (
            detect_go_loopback_tests,
            detect_js_loopback_test_files,
            detect_python_loopback_tests,
            detect_rust_loopback_tests,
            filter_builder_checks_for_sandbox,
        )

        # 1) Go: direct httptest.NewServer + helper calling net.Listen; pure unit test stays included
        go_wt = self.tmp_path / "go_filter_wt"
        (go_wt / "pkg").mkdir(parents=True)
        (go_wt / "pkg" / "server_test.go").write_text(
            "package pkg\n"
            "import (\"net\"; \"net/http/httptest\"; \"testing\")\n"
            "func startFixture(t *testing.T) { _, _ = net.Listen(\"tcp\", \"127.0.0.1:0\") }\n"
            "func TestPureUnit(t *testing.T) {}\n"
            "func TestHTTPFixture(t *testing.T) { _ = httptest.NewServer(nil) }\n"
            "func TestViaHelper(t *testing.T) { startFixture(t) }\n",
            encoding="utf-8",
        )
        self.assertEqual(detect_go_loopback_tests(go_wt), ["TestHTTPFixture", "TestViaHelper"])
        go_filtered = filter_builder_checks_for_sandbox(
            ["go vet ./...", "go test -race -short ./..."],
            go_wt,
            sandbox_mode="workspace-write",
            builder_network_access=False,
        )
        self.assertTrue(go_filtered["rewritten"])
        self.assertEqual(go_filtered["deferred_tests"], ["TestHTTPFixture", "TestViaHelper"])
        self.assertEqual(go_filtered["checks"][0], "go vet ./...")
        self.assertIn("-skip '^(TestHTTPFixture|TestViaHelper)$'", go_filtered["checks"][1])

        # Bypassed when builder.network_access=True
        go_net = filter_builder_checks_for_sandbox(
            ["go test -race -short ./..."],
            go_wt,
            sandbox_mode="workspace-write",
            builder_network_access=True,
        )
        self.assertFalse(go_net["rewritten"])
        self.assertEqual(go_net["checks"], ["go test -race -short ./..."])

        # 2) Python / pytest
        py_wt = self.tmp_path / "py_filter_wt"
        (py_wt / "tests").mkdir(parents=True)
        (py_wt / "tests" / "test_api.py").write_text(
            "from http.server import HTTPServer\n"
            "def test_pure():\n    assert 1 + 1 == 2\n\n"
            "def test_loopback_http():\n    srv = HTTPServer(('127.0.0.1', 0), None)\n",
            encoding="utf-8",
        )
        self.assertEqual(detect_python_loopback_tests(py_wt), ["test_loopback_http"])
        py_filtered = filter_builder_checks_for_sandbox(
            ["python3 -m pytest -q"],
            py_wt,
            sandbox_mode="workspace-write",
            builder_network_access=False,
        )
        self.assertTrue(py_filtered["rewritten"])
        self.assertIn("-k 'not (test_loopback_http)'", py_filtered["checks"][0])

        # 3) JS / Jest & Vitest
        js_wt = self.tmp_path / "js_filter_wt"
        (js_wt / "tests").mkdir(parents=True)
        (js_wt / "tests" / "pure.test.ts").write_text("test('math', () => expect(1).toBe(1));\n", encoding="utf-8")
        (js_wt / "tests" / "http.spec.ts").write_text(
            "import http from 'node:http';\nhttp.createServer().listen(0);\n", encoding="utf-8"
        )
        self.assertEqual(detect_js_loopback_test_files(js_wt), ["tests/http.spec.ts"])
        js_filtered = filter_builder_checks_for_sandbox(
            ["npx jest", "npx vitest run"],
            js_wt,
            sandbox_mode="workspace-write",
            builder_network_access=False,
        )
        self.assertIn("--testPathIgnorePatterns", js_filtered["checks"][0])
        self.assertIn("--exclude tests/http.spec.ts", js_filtered["checks"][1])

        # 4) Rust / cargo test
        rs_wt = self.tmp_path / "rs_filter_wt"
        (rs_wt / "tests").mkdir(parents=True)
        (rs_wt / "tests" / "net.rs").write_text(
            "#[test]\nfn pure_test() { assert_eq!(1, 1); }\n\n"
            "#[tokio::test]\nasync fn binds_port() { let _ = TcpListener::bind(\"127.0.0.1:0\"); }\n",
            encoding="utf-8",
        )
        self.assertEqual(detect_rust_loopback_tests(rs_wt), ["binds_port"])
        rs_filtered = filter_builder_checks_for_sandbox(
            ["cargo test"],
            rs_wt,
            sandbox_mode="workspace-write",
            builder_network_access=False,
        )
        self.assertEqual(rs_filtered["checks"][0], "cargo test -- --skip binds_port")

        # 5) End-to-end orchestrator dispatch injects filtered Builder Checks & logs event without altering target repo
        repo = _create_python_repo(self.tmp_path / "e2e_filter_repo")
        (repo / "test_socket.py").write_text(
            "import socket\ndef test_bind_port():\n    s = socket.socket(); s.bind(('127.0.0.1', 0))\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "test_socket.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add socket test"], cwd=repo, check=True, capture_output=True)
        rec_file = self.tmp_path / "e2e_filter_rec.json"
        os.environ["MOCK_CODEX_RECORD_FILE"] = str(rec_file)
        os.environ["MOCK_CODEX_SCENARIO"] = "success"
        tid = self.orch.init_task(name="Sandbox auto filter", repo_path=repo)["task"]["task_id"]
        self.orch.save_plan(
            tid,
            PLAN_PY + "\n## Builder Checks\n- `pytest -q`\n",
        )
        exec_res = self.orch.execute_round(tid)
        self.assertTrue(exec_res["ok"])
        rec = json.loads(rec_file.read_text(encoding="utf-8"))[0]
        self.assertIn("pytest -q -k 'not (test_bind_port)'", rec["prompt"])
        self.assertIn("automatically deferred to host verification", rec["prompt"])
        ev_types = [e.get("type") for e in self.orch.store.read_events(tid)]
        self.assertIn("builder_checks_filtered", ev_types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
