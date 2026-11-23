#!/usr/bin/env node
/**
 * End-to-end stdio JSON-RPC test for the Codex Dev Monitor MCP App server.
 * Verifies:
 *  - initialize handshake
 *  - tools/list (strictly read-only annotations, aux_pane + global entrypoints, zero mutation tools)
 *  - resources/read (`ui://codex-dev/monitor.html`, MIME `text/html;profile=mcp-app`)
 *  - tools/call (`open_monitor` and `get_monitor_snapshot`) against real orchestrator task state
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const serverPath = process.argv[2]
  ? resolve(process.argv[2])
  : resolve(__dirname, "../mcp-app/dist/server.mjs");

// Create temporary CODEX_DEV_HOME with a realistic task snapshot
const tempHome = mkdtempSync(join(tmpdir(), "codex-dev-mcp-test-"));
const taskId = "cd-20261122-mcp01";
const taskDir = join(tempHome, "tasks", taskId);
mkdirSync(taskDir, { recursive: true });

writeFileSync(
  join(tempHome, "active_task.json"),
  JSON.stringify({ task_id: taskId, repo_path: "/tmp/demo-repo", name: "MCP Monitor Verification" }),
);
writeFileSync(
  join(taskDir, "task.json"),
  JSON.stringify({
    task_id: taskId,
    name: "MCP Monitor Verification",
    repo_path: "/tmp/demo-repo",
    base_branch: "main",
    branch: `codex-dev/${taskId}`,
    worktree_path: join(tempHome, "worktrees", taskId),
    created_at: new Date().toISOString(),
  }),
);
writeFileSync(
  join(taskDir, "state.json"),
  JSON.stringify({
    task_id: taskId,
    name: "MCP Monitor Verification",
    status: "REVIEWING",
    current_phase: "review",
    current_agent: "Antigravity (Reviewer)",
    phases: { plan: "completed", execute: "completed", verify: "completed", review: "running" },
    model_policy: { mode: "manual", locked: true, current_model: "gpt-6-luna", reviewer_model: "inherit_session" },
    repair_round: 1,
    max_repair_rounds: 3,
    token_usage: { available: true, input_tokens: 2100, cached_input_tokens: 800, output_tokens: 450, total_tokens: 2550 },
    diff_stats: {
      files_changed_count: 2,
      total_insertions: 48,
      total_deletions: 7,
      files: [
        { path: "src/app.py", change_type: "modified", insertions: 30, deletions: 7 },
        { path: "tests/test_app.py", change_type: "added", insertions: 18, deletions: 0 },
      ],
    },
    verification_summary: {
      passed: true,
      commands_total: 1,
      commands_passed: 1,
      commands_failed: 0,
      skipped_tests_total: 2,
      skipped_tests: ["TestProjectLifecycle", "TestRepositoryLifecycle"],
      contract_valid: true,
      blocking_audit_issues: 0,
      warnings: 1,
      checks: [
        {
          name: "python:unittest",
          kind: "test",
          command: "python3 -m unittest",
          passed: true,
          exit_code: 0,
          duration_sec: 0.24,
          skipped_count: 2,
        },
      ],
    },
    latest_review: {
      round: 0,
      verdict: "request_changes",
      summary: "Add boundary check for empty input",
      issues: ["Handle empty list cleanly"],
      reviewer_model: "Antigravity (session model)",
      timestamp: new Date().toISOString(),
    },
    updated_at: new Date().toISOString(),
  }),
);
writeFileSync(
  join(taskDir, "events.jsonl"),
  `${JSON.stringify({ seq: 1, timestamp: new Date().toISOString(), phase: "execute", agent: "Codex CLI (Builder)", type: "codex_invoked", message: "Builder running" })}\n`,
);

const child = spawn(process.execPath, [serverPath], {
  env: { ...process.env, CODEX_DEV_HOME: tempHome },
  stdio: ["pipe", "pipe", "pipe"],
});

let buffer = "";
const pending = new Map();

child.stdout.on("data", (chunk) => {
  buffer += chunk.toString("utf-8");
  let idx;
  while ((idx = buffer.indexOf("\n")) !== -1) {
    const line = buffer.slice(0, idx).trim();
    buffer = buffer.slice(idx + 1);
    if (!line) continue;
    const msg = JSON.parse(line);
    if (msg.id !== undefined && pending.has(msg.id)) {
      const { resolve: res } = pending.get(msg.id);
      pending.delete(msg.id);
      res(msg);
    }
  }
});

let stderrText = "";
child.stderr.on("data", (c) => {
  stderrText += c.toString("utf-8");
});

function rpc(id, method, params = {}) {
  return new Promise((res, rej) => {
    const timer = setTimeout(() => rej(new Error(`RPC timeout for ${method}: ${stderrText}`)), 8000);
    pending.set(id, {
      resolve: (msg) => {
        clearTimeout(timer);
        res(msg);
      },
    });
    child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
  });
}

try {
  // 1. Initialize
  const initResp = await rpc(1, "initialize", {
    protocolVersion: "2025-03-26",
    capabilities: {
      extensions: {
        "io.modelcontextprotocol/ui": { mimeTypes: ["text/html;profile=mcp-app"] },
        "com.google.antigravity/ui": { entrypoints: ["aux_pane", "global", "file"] },
      },
    },
    clientInfo: { name: "codex-dev-test-host", version: "1.0.0" },
  });
  assert.ok(initResp.result?.serverInfo?.name === "codex-dev-monitor", "Unexpected serverInfo");

  child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" })}\n`);

  // 2. Verify tools/list is strictly read-only & declares aux_pane entrypoint
  const toolsResp = await rpc(2, "tools/list", {});
  const tools = toolsResp.result?.tools || [];
  assert.equal(tools.length, 2, `Expected exactly 2 read-only tools, got ${tools.length}`);
  for (const t of tools) {
    assert.equal(t.annotations?.readOnlyHint, true, `Tool ${t.name} must be readOnlyHint=true`);
    assert.equal(t.annotations?.destructiveHint, false, `Tool ${t.name} must be destructiveHint=false`);
  }
  const openTool = tools.find((t) => t.name === "open_monitor");
  assert.ok(openTool, "open_monitor tool missing");
  assert.equal(openTool._meta?.ui?.resourceUri, "ui://codex-dev/monitor.html");
  const entrypoints = openTool._meta?.["com.google.antigravity/ui"]?.entrypoints || [];
  assert.ok(
    entrypoints.some((e) => e.type === "aux_pane"),
    "open_monitor must declare aux_pane entrypoint",
  );
  const pollTool = tools.find((t) => t.name === "get_monitor_snapshot");
  assert.ok(pollTool, "get_monitor_snapshot tool missing");
  assert.deepEqual(pollTool._meta?.ui?.visibility, ["app"]);

  // 3. Verify resources/read serves bundled single-file HTML with mcp-app MIME type
  const resResp = await rpc(3, "resources/read", { uri: "ui://codex-dev/monitor.html" });
  const content = resResp.result?.contents?.[0];
  assert.equal(content?.mimeType, "text/html;profile=mcp-app");
  assert.ok(content?.text?.includes("CODEX DEV"), "Monitor HTML missing expected markup");
  assert.ok(!content?.text?.includes("<button"), "Read-only monitor UI must not contain control buttons");

  // 4. Verify tools/call reads live orchestrator state accurately
  const openCall = await rpc(4, "tools/call", { name: "open_monitor", arguments: {} });
  const sc = openCall.result?.structuredContent;
  assert.equal(sc?.selected_task_id, taskId);
  assert.equal(sc?.state?.status, "REVIEWING");
  assert.equal(sc?.state?.model_policy?.current_model, "gpt-6-luna");
  assert.equal(sc?.state?.diff_stats?.files_changed_count, 2);
  assert.equal(sc?.state?.token_usage?.total_tokens, 2550);
  assert.equal(sc?.state?.verification_summary?.skipped_tests_total, 2);
  const summaryText = openCall.result?.content?.[0]?.text || "";
  assert.ok(summaryText.includes("2 skipped"), `Expected plain-text summary to mention skipped tests, got: ${summaryText}`);

  const pollCall = await rpc(5, "tools/call", { name: "get_monitor_snapshot", arguments: { task_id: taskId } });
  assert.equal(pollCall.result?.structuredContent?.events?.length, 1);

  // 5. Verify tail-only reading on large (>300KB) events.jsonl & bounded task listing (>30 tasks)
  const bigLines = [];
  for (let i = 1; i <= 1800; i++) {
    bigLines.push(
      JSON.stringify({
        seq: i,
        timestamp: new Date().toISOString(),
        phase: "execute",
        agent: "Codex CLI (Builder)",
        type: "codex_command",
        message: `Event ${i} ${"x".repeat(160)}`,
      }),
    );
  }
  writeFileSync(join(taskDir, "events.jsonl"), `${bigLines.join("\n")}\n`);
  for (let j = 1; j <= 30; j++) {
    const oldId = `cd-20261101-old${String(j).padStart(2, "0")}`;
    const oldDir = join(tempHome, "tasks", oldId);
    mkdirSync(oldDir, { recursive: true });
    writeFileSync(join(oldDir, "task.json"), JSON.stringify({ task_id: oldId, name: `Old ${j}`, repo_path: "/tmp/old" }));
    writeFileSync(
      join(oldDir, "state.json"),
      JSON.stringify({
        task_id: oldId,
        name: `Old ${j}`,
        status: "COMPLETED",
        current_phase: "deliver",
        updated_at: `2026-11-01T00:${String(j).padStart(2, "0")}:00.000Z`,
      }),
    );
  }
  const t0 = Date.now();
  const bigCall = await rpc(6, "tools/call", { name: "get_monitor_snapshot", arguments: { task_id: taskId, events_limit: 25 } });
  const elapsedMs = Date.now() - t0;
  const bigSc = bigCall.result?.structuredContent;
  assert.equal(bigSc?.events?.length, 25);
  assert.equal(bigSc?.events?.[24]?.seq, 1800);
  assert.equal(bigSc?.tasks?.length, 25);
  assert.ok(elapsedMs < 2000, `Expected fast bounded snapshot read, took ${elapsedMs}ms`);

  console.log(`  OK: MCP App server (${serverPath}) passed all protocol, read-only, aux_pane, UI resource, tail-read, and live state checks (${elapsedMs}ms).`);
} finally {
  child.kill();
  rmSync(tempHome, { recursive: true, force: true });
}
