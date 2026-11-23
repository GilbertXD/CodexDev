import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, join, resolve } from "node:path";

export interface TaskSummaryItem {
  task_id: string;
  name: string;
  repo_name: string;
  repo_path: string;
  branch: string;
  status: string;
  current_phase: string;
  current_model: string | null;
  repair_round: number;
  max_repair_rounds: number;
  updated_at: string;
}

export interface MonitorSnapshot {
  source_home: string;
  fetched_at: string;
  active_task_id: string | null;
  selected_task_id: string | null;
  tasks: TaskSummaryItem[];
  task: Record<string, unknown> | null;
  state: Record<string, unknown> | null;
  events: Array<Record<string, unknown>>;
  verification: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  reviews: Array<Record<string, unknown>>;
  revision: string;
}

export function getCodexDevHome(): string {
  const envHome = process.env.CODEX_DEV_HOME;
  if (envHome && envHome.trim()) {
    return resolve(envHome.trim());
  }
  return join(homedir(), ".codex-dev");
}

function safeReadJson<T>(filePath: string, fallback: T): T {
  try {
    if (!existsSync(filePath)) return fallback;
    const raw = readFileSync(filePath, "utf-8");
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function safeReadEvents(filePath: string, limit = 80): Array<Record<string, unknown>> {
  try {
    if (!existsSync(filePath)) return [];
    const lines = readFileSync(filePath, "utf-8").split(/\r?\n/);
    const out: Array<Record<string, unknown>> = [];
    for (const line of lines.slice(-limit)) {
      const s = line.trim();
      if (!s) continue;
      try {
        const parsed = JSON.parse(s);
        if (parsed && typeof parsed === "object") {
          out.push(parsed as Record<string, unknown>);
        }
      } catch {
        // Ignore partial line while writer appends
      }
    }
    return out;
  } catch {
    return [];
  }
}

function safeMtime(filePath: string): number {
  try {
    return statSync(filePath).mtimeMs;
  } catch {
    return 0;
  }
}

function isSafeTaskId(id: string): boolean {
  return Boolean(id) && !id.includes("/") && !id.includes("\\") && !id.includes("..");
}

export function readMonitorSnapshot(requestedTaskId?: string, eventsLimit = 80): MonitorSnapshot {
  const home = getCodexDevHome();
  const tasksDir = join(home, "tasks");
  const activeFile = join(home, "active_task.json");
  const activeDoc = safeReadJson<Record<string, unknown> | null>(activeFile, null);
  const activeTaskId =
    activeDoc && typeof activeDoc.task_id === "string" && isSafeTaskId(activeDoc.task_id)
      ? activeDoc.task_id
      : null;

  const tasks: TaskSummaryItem[] = [];
  if (existsSync(tasksDir)) {
    let entries: string[] = [];
    try {
      entries = readdirSync(tasksDir);
    } catch {
      entries = [];
    }
    for (const dirName of entries) {
      if (!isSafeTaskId(dirName)) continue;
      const tdir = join(tasksDir, dirName);
      const statePath = join(tdir, "state.json");
      if (!existsSync(statePath)) continue;
      const state = safeReadJson<Record<string, unknown> | null>(statePath, null);
      const meta = safeReadJson<Record<string, unknown> | null>(join(tdir, "task.json"), null);
      if (!state) continue;

      const repoPath = typeof meta?.repo_path === "string" ? meta.repo_path : "";
      const modelPolicy =
        state.model_policy && typeof state.model_policy === "object"
          ? (state.model_policy as Record<string, unknown>)
          : {};
      tasks.push({
        task_id: dirName,
        name: String(meta?.name || state.name || dirName),
        repo_name: repoPath ? basename(repoPath) : "repository",
        repo_path: repoPath,
        branch: String(meta?.branch || ""),
        status: String(state.status || "CREATED"),
        current_phase: String(state.current_phase || "plan"),
        current_model: typeof modelPolicy.current_model === "string" ? modelPolicy.current_model : null,
        repair_round: Number(state.repair_round ?? 0),
        max_repair_rounds: Number(state.max_repair_rounds ?? 3),
        updated_at: String(state.updated_at || meta?.created_at || ""),
      });
    }
  }

  tasks.sort((a, b) => b.updated_at.localeCompare(a.updated_at));

  let selectedTaskId: string | null = null;
  if (
    requestedTaskId &&
    isSafeTaskId(requestedTaskId) &&
    existsSync(join(tasksDir, requestedTaskId, "state.json"))
  ) {
    selectedTaskId = requestedTaskId;
  } else if (activeTaskId && existsSync(join(tasksDir, activeTaskId, "state.json"))) {
    selectedTaskId = activeTaskId;
  } else if (tasks.length > 0) {
    selectedTaskId = tasks[0].task_id;
  }

  let task: Record<string, unknown> | null = null;
  let state: Record<string, unknown> | null = null;
  let events: Array<Record<string, unknown>> = [];
  let verification: Record<string, unknown> | null = null;
  let result: Record<string, unknown> | null = null;
  let reviews: Array<Record<string, unknown>> = [];
  let revision = `empty:${tasks.length}`;

  if (selectedTaskId) {
    const tdir = join(tasksDir, selectedTaskId);
    task = safeReadJson<Record<string, unknown> | null>(join(tdir, "task.json"), null);
    state = safeReadJson<Record<string, unknown> | null>(join(tdir, "state.json"), null);
    events = safeReadEvents(join(tdir, "events.jsonl"), Math.min(Math.max(eventsLimit, 10), 200));
    verification = safeReadJson<Record<string, unknown> | null>(join(tdir, "verification.json"), null);
    result = safeReadJson<Record<string, unknown> | null>(join(tdir, "RESULT.json"), null);
    reviews = safeReadJson<Array<Record<string, unknown>>>(join(tdir, "reviews.json"), []);
    revision = [
      selectedTaskId,
      String(state?.updated_at || ""),
      String(events.length),
      String(events.length > 0 ? events[events.length - 1]?.seq ?? "" : ""),
      String(Math.floor(safeMtime(join(tdir, "events.jsonl")))),
      String(tasks.length),
    ].join("|");
  }

  return {
    source_home: home,
    fetched_at: new Date().toISOString(),
    active_task_id: activeTaskId,
    selected_task_id: selectedTaskId,
    tasks: tasks.slice(0, 25),
    task,
    state,
    events,
    verification,
    result,
    reviews,
    revision,
  };
}

export function formatPlainTextSummary(snapshot: MonitorSnapshot): string {
  if (!snapshot.state || !snapshot.task) {
    return (
      `Codex Dev Monitor (Read-Only)\n` +
      `State home: ${snapshot.source_home}\n` +
      `No active Codex Dev tasks recorded yet. Start a task via /codex-dev in chat.`
    );
  }
  const s = snapshot.state;
  const t = snapshot.task;
  const mp = (s.model_policy || {}) as Record<string, unknown>;
  const diff = (s.diff_stats || {}) as Record<string, unknown>;
  const ver = (s.verification_summary || {}) as Record<string, unknown>;
  const rev = (s.latest_review || null) as Record<string, unknown> | null;
  const tok = (s.token_usage || {}) as Record<string, unknown>;

  const lines = [
    `Codex Dev Monitor — ${String(t.name || snapshot.selected_task_id)}`,
    `Task ID: ${snapshot.selected_task_id}`,
    `Status: ${String(s.status)} | Phase: ${String(s.current_phase)} | Agent: ${String(s.current_agent)}`,
    `Branch: ${String(t.branch || "-")} | Worktree: ${String(t.worktree_path || "-")}`,
    `Builder Model: ${String(mp.current_model || "-")} (${String(mp.mode || "auto")}${mp.locked ? ", locked" : ""}${mp.current_reasoning_effort || mp.reasoning_effort ? `, effort=${String(mp.current_reasoning_effort || mp.reasoning_effort)}${mp.effort_locked ? " locked" : ""}` : ""})`,
    `Repair Round: ${String(s.repair_round ?? 0)} / ${String(s.max_repair_rounds ?? 3)}`,
    `Diff: ${String(diff.files_changed_count ?? 0)} files (+${String(diff.total_insertions ?? 0)} / -${String(diff.total_deletions ?? 0)})`,
  ];

  if (ver && typeof ver.commands_total === "number") {
    const skipped = Number(ver.skipped_tests_total ?? 0);
    const skipSuffix = skipped > 0 ? `, ${skipped} skipped` : "";
    lines.push(
      `Verification: ${ver.passed ? "PASSED" : "FAILED"} (${ver.commands_passed}/${ver.commands_total} checks${skipSuffix})`,
    );
  }
  if (tok && tok.available) {
    lines.push(
      `Tokens: total=${String(tok.total_tokens)} (in=${String(tok.input_tokens)}, cached=${String(tok.cached_input_tokens)}, out=${String(tok.output_tokens)})`,
    );
  }
  if (rev) {
    lines.push(`Latest Review: ${String(rev.verdict).toUpperCase()} — ${String(rev.summary || "")}`);
  }
  return lines.join("\n");
}
