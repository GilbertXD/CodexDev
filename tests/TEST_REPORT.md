# Codex Dev Test Record

Recorded on: `2026-11-22` (Asia/Shanghai)
Environment:
- OS: macOS (Apple Silicon)
- Antigravity App: `2.34.0` (`ANTIGRAVITY_LS_VERSION=hub-2.34.0`)
- Python: `3.14.7`
- Node.js / npm: `v24.21.0` / `11.19.0`
- Git: `2.54.0`
- Codex CLI: `0.171.0` (ChatGPT authenticated; global config uses notarized `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`)

---

## 1. Mock Codex Orchestrator Unit & Integration Suite

Command:
```bash
python3 -m unittest tests/test_orchestrator.py -v
```

Result: **16/16 PASSED** (`0` failures, `0` warnings).

| Test Case | Verified Capabilities | Result |
| :--- | :--- | :--- |
| `test_01_normal_execution_persistence_worktree_and_deliver` | Full `CREATED -> PLANNED -> EXECUTING -> VERIFYING -> REVIEWING -> COMPLETED` flow, all 9 persisted task artifacts, worktree isolation preserving main-repo dirty/uncommitted files, untracked file detection, default non-committing `deliver`, human-confirmed `--confirm-commit` | PASS |
| `test_02_auto_repair_and_model_escalation` | Round 0 verification failure (`test_failure` -> `REVISING`), `FEEDBACK.md` generation, automatic authorized model escalation (`gpt-6-luna` -> `gpt-6.2-sol`), review `request_changes` (`review_failure` -> `REVISING`) -> Round 2 repair -> `COMPLETED` | PASS |
| `test_03_retry_limit_exceeded_blocks_task` | Exceeding `max_repair_rounds=3` transitions cleanly to `BLOCKED` with `failure_category="retry_limit_exceeded"` | PASS |
| `test_04_timeout_and_abnormal_crash_and_recovery` | Subprocess timeout (`failure_category="timeout"`), abnormal exit code 137 (`failure_category="execution_failure"`), interrupted-process crash recovery (`recover_task`) | PASS |
| `test_05_model_locking_and_invalid_model_rejection` | Unverified/unauthorized model ID immediately raises `InvalidModelError`; locked manual model stays pinned across repair rounds and rejects overrides | PASS |
| `test_06_immutable_contract_tamper_and_test_weakening_audit` | Anti-weakening audit blocks `@unittest.skip` insertion; SHA-256 tamper check rejects modified `PLAN.md` (`contract_violation`) before Builder runs | PASS |
| `test_07_concurrent_writer_lock_and_live_cancel` | Advisory `fcntl.flock` rejects concurrent writer (`TaskLockError`); live `cancel_task` terminates running process group and transitions to `BLOCKED` (`cancelled`) | PASS |
| `test_08_stack_agnostic_node_verification` | Auto-detects and executes `npm run test` on a Node.js repository without language hardcoding | PASS |
| `test_09_dotfile_target_paths_preserved_without_false_unexpected_files` | `normalize_target_path` strips only `./` and leading `/`, preserving `.gitignore`, `.env.example`, `.dockerignore`, `.github/workflows/ci.yml` with zero false `unexpected_files` | PASS |
| `test_10_dynamic_timeout_idle_timeout_and_attempt_log_preservation` | Dynamic timeout scaling for `high` effort/complexity (`1200s`), idle no-event timeout vs active event streaming, and per-attempt `codex_round_<r>_attempt_<a>.jsonl` log preservation | PASS |
| `test_11_sandbox_prompt_guidance` | `build_codex_prompt` keeps `workspace-write` sandbox while instructing Builder not to work around network/port/Docker restrictions and deferring to external verifier | PASS |
| `test_12_verify_from_failed_guarded_transition` | Guarded `verify --from-failed` permits `FAILED -> VERIFYING` only when `failure_category == "timeout"` and worktree has changes; default and non-timeout/empty cases remain rejected | PASS |
| `test_13_builder_gocache_add_dir_and_model_refresh_noise_suppression` | Auto-provisions task-scoped writable `GOCACHE`/`GOTMPDIR` via `--add-dir` and `shell_environment_policy.set`, disables `api_key_model_discovery` & startup update check, and collapses benign model-refresh timeout stderr noise | PASS |
| `test_14_non_sandbox_dependency_prepare_env_and_failure_isolation` | Auto-detects `go mod tidy` when `go.mod` changes, passes `verify.env`/`prepare.env` (e.g. `GOPROXY`) to host Prepare & Verify stages, and blocks on `dependency_prepare_failure` without consuming a `repair_round` | PASS |
| `test_15_diff_summary_only_omits_diff_body` | `codex-dev diff --summary-only` skips unified diff generation and omits both `full_diff` and `diff_excerpt` | PASS |
| `test_16_verifying_clears_stale_error_and_verification_summary` | Entering/re-entering `VERIFYING` immediately clears `last_error`, `failure_category`, `verification_summary`, and removes stale `verification.json` so Monitor renders clean running state | PASS |

---

## 2. Read-Only MCP App Server Protocol Verification

Command:
```bash
node tests/test_mcp_server.mjs ~/.gemini/config/plugins/codex-dev/mcp-app/dist/server.mjs
```

Result: **PASSED**

Verified assertions:
- `initialize` handshake with `io.modelcontextprotocol/ui` and `com.google.antigravity/ui` capabilities succeeded.
- `tools/list` exposed exactly 2 tools (`open_monitor`, `get_monitor_snapshot`), both with `readOnlyHint: true`, `destructiveHint: false`, and zero mutating tools.
- `open_monitor` declared `_meta.ui.resourceUri = "ui://codex-dev/monitor.html"` and `_meta["com.google.antigravity/ui"].entrypoints` containing `aux_pane` and `global`.
- `get_monitor_snapshot` declared `_meta.ui.visibility = ["app"]`.
- `resources/read` returned bundled single-file HTML (`text/html;profile=mcp-app`) with zero `<button>` control elements.
- `tools/call` accurately read live orchestrator task state (`status`, `model_policy`, `diff_stats`, `token_usage`, `events`).

---

## 3. Global Installation & Visual UI Inspection

Command:
```bash
./scripts/verify_install.sh
```

Result: **PASSED**
- Antigravity Language Server (`GetAllPlugins`) discovered global plugin `codex-dev` (`isGlobal: true`, `disabled: false`, `skills: ["codex-dev"]`, `mcp: ["codex-dev_monitor"]`).
- Built-in browser visual inspection of `tests/preview_server.mjs` at `420px` side-panel width verified both Light and Dark Minimal Monochrome themes with real MCP stdio backend:
  - 0 console errors, 0 `<button>` / `role="button"` control elements, 0 horizontal overflow.
  - Secondary monochrome contrast (`--fg-muted`, `--fg-faint`) and file count pluralization (`1 FILE` vs `N FILES`) refined after screenshot review and rebuilt globally.

---

## 4. Real End-to-End Test (Temporary Isolated Git Repository)

Temporary repository (cleaned up after verification): `/tmp/codex-dev-real-e2e-repo`
- Baseline commit: `8f6fa37` (`text_metrics.py`, `test_text_metrics.py`, `.gitignore`)
- Main-repo dirty guard: uncommitted file `scratch_uncommitted.txt` present in main repository throughout execution.
- User-selected & locked Builder model: `gpt-6.2-sol` (`mode=manual`, `locked=true`, `sandbox=workspace-write`).

### Attempt 1 (Diagnostic Finding) — `cd-20261122-031116-9ee71d`
- Branch: `codex-dev/cd-20261122-031116-9ee71d` (removed during cleanup)
- Result: `BLOCKED`
- Observation: Homebrew Cask `/opt/homebrew/Caskroom/codex/0.171.0/bin/codex-code-mode-host` carried macOS `com.apple.quarantine`, triggering Gatekeeper and causing Codex CLI to return structured `status: "blocked"` (`83,107` tokens).
- Remediation applied:
  1. Updated `codex_runner.py` & `orchestrator.py` so Codex `status: "blocked"` immediately transitions the task to `BLOCKED` (`failure_category="builder_blocked"`) instead of continuing to `VERIFYING`.
  2. Updated `config.py` to prefer the Apple-notarized `CODEX_CLI_PATH` (`/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`) when configured in `~/.codex/config.toml`.

### Attempt 2 (Full Real E2E Pass) — `cd-20261122-031623-2327b0`
- Task: `Add reading_time_seconds and top_words to text_metrics`
- Branch: `codex-dev/cd-20261122-031623-2327b0` (removed during cleanup)
- Immutable `PLAN.md` SHA-256: `f0c17c9cf59741ccbee4cf3c5b2b23473130ede0d38caf7acfc3ade0be5b5c1b`
- Execution:
  - Codex CLI (`gpt-6.2-sol`, `-s workspace-write`, `--json`, `--output-schema`) modified strictly the 2 target files:
    - `text_metrics.py` (`+20 / -0`)
    - `test_text_metrics.py` (`+36 / -1`)
  - Token usage (from Codex JSONL): `95,288` total (`input=93,972`, `cached=64,384`, `output=1,316`)
  - Repair rounds: `0 / 3`
- Independent Verification (`python3 -m unittest discover -v`):
  - **PASSED**: `8/8` unit tests (`exit 0`, `0.04s`), `contract_valid: true`, `blocking_audit_issues: 0`, `warnings: 0`.
  - Main repository `scratch_uncommitted.txt` and baseline files remained completely untouched.
- Semantic Code Review (Antigravity Reviewer):
  - Verdict: `APPROVE` -> state transitioned cleanly to `COMPLETED`.
  - Default `deliver` verified `committed: false` (`requires_confirmation: true`).
- Live Antigravity Side Panel Verification:
  - Invoked `codex-dev_monitor` -> `open_monitor` live inside Antigravity during `REVIEWING` and after `COMPLETED`; monitor returned and displayed live task status, model, diff stats, verification results, token usage, and review verdict.

---

## 5. Cleanup Summary

After recording this report:
- Removed isolated worktrees `cd-20261122-031116-9ee71d` and `cd-20261122-031623-2327b0`.
- Deleted temporary branches `codex-dev/cd-20261122-031116-9ee71d` and `codex-dev/cd-20261122-031623-2327b0`.
- Deleted temporary Git repository `/tmp/codex-dev-real-e2e-repo`.
- Cleared temporary E2E task records and `active_task.json` under `~/.codex-dev/` while keeping global configuration (`~/.codex-dev/config.json`), runtime (`~/.codex-dev/runtime/`), CLI (`~/.local/bin/codex-dev`), and Antigravity plugin (`~/.gemini/config/plugins/codex-dev/`) intact.
