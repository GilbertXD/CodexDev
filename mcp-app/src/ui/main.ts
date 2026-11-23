import { App, applyDocumentTheme } from "@modelcontextprotocol/ext-apps";

interface TaskSummaryItem {
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

interface VerificationCheck {
  name: string;
  kind: string;
  command: string;
  passed: boolean;
  exit_code: number;
  duration_sec: number;
  skipped_count?: number;
}

interface DiffFileItem {
  path: string;
  change_type: string;
  insertions: number;
  deletions: number;
  binary?: boolean;
}

interface EventItem {
  seq?: number;
  timestamp?: string;
  phase?: string;
  agent?: string;
  type?: string;
  message?: string;
}

interface ReviewItem {
  round?: number;
  verdict?: string;
  summary?: string;
  issues?: string[];
  reviewer_model?: string;
  timestamp?: string;
}

interface MonitorSnapshot {
  source_home?: string;
  fetched_at?: string;
  active_task_id?: string | null;
  selected_task_id?: string | null;
  tasks?: TaskSummaryItem[];
  task?: Record<string, unknown> | null;
  state?: Record<string, unknown> | null;
  events?: EventItem[];
  verification?: Record<string, unknown> | null;
  reviews?: ReviewItem[];
  revision?: string;
}

const ACTIVE_STATES = new Set(["CREATED", "PLANNED", "EXECUTING", "VERIFYING", "REVIEWING", "REVISING"]);

const contentEl = document.getElementById("content") as HTMLElement;
const selectorEl = document.getElementById("task-selector") as HTMLSelectElement;
const syncPillEl = document.getElementById("sync-indicator") as HTMLElement;
const syncLabelEl = document.getElementById("sync-label") as HTMLElement;

let currentSnapshot: MonitorSnapshot | null = null;
let userPinnedTaskId: string | null = null;
let lastRevision = "";
let lastSeenEventSeq = 0;
let lastRenderedAgent = "";
let lastPhaseStatuses: Record<string, string> = {};
let pollTimer: number | null = null;
let pollInFlight = false;
let connected = false;

const app = new App({ name: "Codex Dev Monitor", version: "1.0.0" });

function esc(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function shortPath(fullPath: string): string {
  if (!fullPath) return "—";
  const parts = fullPath.split("/").filter(Boolean);
  if (parts.length <= 3) return fullPath;
  return `…/${parts.slice(-2).join("/")}`;
}

function formatTime(iso?: string): string {
  if (!iso) return "--:--:--";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return d.toTimeString().slice(0, 8);
}

function formatNumber(n: unknown): string {
  const num = Number(n);
  if (!Number.isFinite(num)) return "0";
  return num.toLocaleString("en-US");
}

function formatCompact(n: unknown): string {
  const num = Number(n);
  if (!Number.isFinite(num)) return "0";
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(2)}M`;
  if (num >= 10_000) return `${(num / 1_000).toFixed(1)}k`;
  return num.toLocaleString("en-US");
}

function changeTag(changeType: string): string {
  switch (changeType) {
    case "added":
      return "A";
    case "deleted":
      return "D";
    case "untracked":
      return "U";
    default:
      return "M";
  }
}

function stageGlyph(status: string): string {
  if (status === "completed") {
    return `<svg width="9" height="9" viewBox="0 0 10 10" fill="none" aria-hidden="true"><path d="M2 5.3L4.1 7.4L8.2 2.8" stroke="currentColor" stroke-width="1.5" stroke-linecap="square"/></svg>`;
  }
  if (status === "failed" || status === "blocked") {
    return `<svg width="8" height="8" viewBox="0 0 10 10" fill="none" aria-hidden="true"><path d="M2.5 2.5L7.5 7.5M7.5 2.5L2.5 7.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="square"/></svg>`;
  }
  if (status === "changes_requested") {
    return `<span class="mono" style="font-size:9px;line-height:1">!</span>`;
  }
  return "";
}

function stageLabelText(status: string): string {
  switch (status) {
    case "completed":
      return "DONE";
    case "running":
      return "ACTIVE";
    case "failed":
      return "FAILED";
    case "blocked":
      return "BLOCKED";
    case "changes_requested":
      return "REVISE";
    default:
      return "WAIT";
  }
}

function updateSyncIndicator(stateStr: string | null): void {
  if (document.hidden) {
    syncPillEl.classList.remove("is-live");
    syncLabelEl.textContent = "PAUSED";
    return;
  }
  if (stateStr && ACTIVE_STATES.has(stateStr)) {
    syncPillEl.classList.add("is-live");
    syncLabelEl.textContent = "LIVE";
  } else if (connected) {
    syncPillEl.classList.remove("is-live");
    syncLabelEl.textContent = "IDLE";
  } else {
    syncPillEl.classList.remove("is-live");
    syncLabelEl.textContent = "SYNC";
  }
}

function updateTaskSelector(snapshot: MonitorSnapshot): void {
  const tasks = snapshot.tasks || [];
  if (tasks.length <= 1) {
    selectorEl.classList.add("hidden");
    return;
  }
  selectorEl.classList.remove("hidden");
  const selected = snapshot.selected_task_id || "";
  const existingKeys = Array.from(selectorEl.options).map((o) => `${o.value}:${o.textContent}`).join("|");
  const nextKeys = tasks.map((t) => `${t.task_id}:${t.name} (${t.status})`).join("|");
  if (existingKeys !== nextKeys) {
    selectorEl.innerHTML = tasks
      .map(
        (t) =>
          `<option value="${esc(t.task_id)}"${t.task_id === selected ? " selected" : ""}>${esc(t.name)} (${esc(t.status)})</option>`,
      )
      .join("");
  } else if (selectorEl.value !== selected && selected) {
    selectorEl.value = selected;
  }
}

function renderSnapshot(snapshot: MonitorSnapshot): void {
  currentSnapshot = snapshot;
  updateTaskSelector(snapshot);

  const state = snapshot.state;
  const task = snapshot.task;

  if (!state || !task) {
    updateSyncIndicator(null);
    contentEl.innerHTML = `
      <div class="empty-state">
        <div class="empty-mark" aria-hidden="true"></div>
        <p class="empty-title">No active Codex Dev task</p>
        <p class="empty-sub">Start a workflow using <span class="mono">/codex-dev</span> in Antigravity chat. This panel updates automatically in read-only mode.</p>
      </div>
    `;
    return;
  }

  const status = String(state.status || "CREATED");
  const isActive = ACTIVE_STATES.has(status);
  updateSyncIndicator(status);

  const taskName = String(task.name || state.name || snapshot.selected_task_id || "Untitled Task");
  const taskId = String(snapshot.selected_task_id || task.task_id || "");
  const branch = String(task.branch || "—");
  const baseBranch = String(task.base_branch || "");
  const worktreePath = String(task.worktree_path || "—");
  const repoPath = String(task.repo_path || "");
  const currentAgent = String(state.current_agent || "—");
  const currentPhase = String(state.current_phase || "plan").toUpperCase();

  const agentChanged = Boolean(lastRenderedAgent && lastRenderedAgent !== currentAgent);
  lastRenderedAgent = currentAgent;

  const mp = (state.model_policy || {}) as Record<string, unknown>;
  const builderModel = String(mp.current_model || "—");
  const modelMode = String(mp.mode || "auto").toUpperCase();
  const modelLocked = Boolean(mp.locked);
  const builderEffort = String(mp.current_reasoning_effort || mp.reasoning_effort || "").toUpperCase();
  const effortLocked = Boolean(mp.effort_locked);
  const reviewerModel = String(mp.reviewer_model || "inherit_session");
  const effortLabel = builderEffort ? ` · ${builderEffort}${effortLocked ? " LOCKED" : ""}` : "";
  const modelModeLabel = `${modelLocked ? `${modelMode} · LOCKED` : modelMode}${effortLabel}`;

  const modelHistory = Array.isArray(state.model_history) ? (state.model_history as Array<Record<string, unknown>>) : [];
  const latestBuilderEntry = [...modelHistory].reverse().find((m) => m.role === "builder");
  const modelRationale = String(latestBuilderEntry?.reason || mp.initial_reason || "");

  const phases = (state.phases || {}) as Record<string, string>;
  const stageOrder: Array<{ key: string; label: string }> = [
    { key: "plan", label: "Plan" },
    { key: "execute", label: "Execute" },
    { key: "verify", label: "Verify" },
    { key: "review", label: "Review" },
  ];

  const repairRound = Number(state.repair_round ?? 0);
  const maxRepairs = Math.max(1, Number(state.max_repair_rounds ?? 3));

  const verSummary = (state.verification_summary || null) as Record<string, unknown> | null;
  const verFull = (snapshot.verification || null) as Record<string, unknown> | null;
  const isVerifyingNow = status === "VERIFYING" && !verSummary;
  const checks: VerificationCheck[] = isVerifyingNow
    ? []
    : Array.isArray(verSummary?.checks)
      ? (verSummary?.checks as VerificationCheck[])
      : Array.isArray(verFull?.checks)
        ? (verFull?.checks as VerificationCheck[])
        : [];

  const verTotal = Number(verSummary?.commands_total ?? checks.length ?? 0);
  const verPassed = Number(verSummary?.commands_passed ?? checks.filter((c) => c.passed).length ?? 0);
  const verFailed = Number(verSummary?.commands_failed ?? checks.filter((c) => !c.passed).length ?? 0);
  const verSkipped = isVerifyingNow
    ? 0
    : Number(verSummary?.skipped_tests_total ?? verFull?.skipped_tests_total ?? 0);
  const verSkippedNames: string[] = isVerifyingNow
    ? []
    : Array.isArray(verSummary?.skipped_tests)
      ? (verSummary?.skipped_tests as string[])
      : Array.isArray(verFull?.skipped_tests)
        ? (verFull?.skipped_tests as string[])
        : [];
  const verOverall = isVerifyingNow ? "RUN" : verSummary ? (verSummary.passed ? "PASS" : "FAIL") : "WAIT";

  const diff = (state.diff_stats || {}) as Record<string, unknown>;
  const filesCount = Number(diff.files_changed_count ?? 0);
  const totalIns = Number(diff.total_insertions ?? 0);
  const totalDel = Number(diff.total_deletions ?? 0);
  const diffFiles: DiffFileItem[] = Array.isArray(diff.files) ? (diff.files as DiffFileItem[]) : [];

  const tok = (state.token_usage || {}) as Record<string, unknown>;
  const tokAvail = Boolean(tok.available);
  const tokTotal = formatCompact(tok.total_tokens ?? 0);
  const tokSub = tokAvail
    ? `in ${formatCompact(tok.input_tokens)} · out ${formatCompact(tok.output_tokens)}`
    : "Unavailable";

  const latestReview = (state.latest_review || null) as ReviewItem | null;
  const failureCategory = state.failure_category ? String(state.failure_category) : null;
  const lastError = state.last_error ? String(state.last_error) : null;

  const badgeClass = isActive
    ? "is-active"
    : status === "COMPLETED"
      ? "is-completed"
      : status === "FAILED"
        ? "is-failed"
        : "is-blocked";

  const events = Array.isArray(snapshot.events) ? snapshot.events : [];
  const prevMaxSeq = lastSeenEventSeq;
  let newMaxSeq = prevMaxSeq;
  for (const ev of events) {
    if (typeof ev.seq === "number" && ev.seq > newMaxSeq) {
      newMaxSeq = ev.seq;
    }
  }
  lastSeenEventSeq = newMaxSeq;

  const stageHtml = stageOrder
    .map((st) => {
      const stVal = String(phases[st.key] || "pending");
      const justCompleted = lastPhaseStatuses[st.key] !== "completed" && stVal === "completed";
      return `
        <div class="stage${justCompleted ? " just-completed" : ""}" data-status="${esc(stVal)}">
          <div class="stage-rail-row">
            <div class="stage-node" aria-label="${esc(st.label)}: ${esc(stVal)}">${stageGlyph(stVal)}</div>
            <div class="stage-line" aria-hidden="true"></div>
          </div>
          <div class="stage-label">${esc(st.label)}</div>
          <div class="stage-state-text">${esc(stageLabelText(stVal))}</div>
        </div>
      `;
    })
    .join("");

  lastPhaseStatuses = { ...phases };

  const pipsHtml = Array.from({ length: maxRepairs }, (_, idx) => {
    const used = idx < repairRound;
    return `<span class="pip${used ? " is-used" : ""}" aria-hidden="true"></span>`;
  }).join("");

  const checksHtml =
    checks.length > 0
      ? `<div class="check-list">
          ${checks
            .map(
              (c) => `
              <div class="check-row">
                <div class="check-left">
                  <span class="glyph-box ${c.passed ? "is-pass" : "is-fail"}">${c.passed ? "✓" : "×"}</span>
                  <div class="check-info">
                    <div class="check-name">${esc(c.name)}</div>
                    <div class="check-cmd" title="${esc(c.command)}">${esc(c.command)}</div>
                  </div>
                </div>
                <div class="check-right">exit ${esc(c.exit_code)} · ${esc(c.duration_sec)}s${
                  c.skipped_count && c.skipped_count > 0 ? ` · ${esc(c.skipped_count)} skip` : ""
                }</div>
              </div>
            `,
            )
            .join("")}
        </div>`
      : `<p class="muted-placeholder">${isVerifyingNow ? "Running independent verification…" : "No verification commands executed yet."}</p>`;

  const auditChips: Array<{ text: string; warn?: boolean; title?: string }> = [];
  if (verSummary) {
    auditChips.push({ text: `CONTRACT ${verSummary.contract_valid ? "VERIFIED" : "TAMPERED"}` });
    const blockingCount = Number(verSummary.blocking_audit_issues ?? 0);
    const warnCount = Number(verSummary.warnings ?? 0);
    auditChips.push({ text: `AUDIT ${blockingCount === 0 ? "CLEAN" : `${blockingCount} BLOCKING`}` });
    if (verSkipped > 0) {
      const tip = verSkippedNames.length > 0 ? `Skipped: ${verSkippedNames.slice(0, 8).join(", ")}` : `${verSkipped} test(s) skipped`;
      auditChips.push({ text: `${verSkipped} SKIPPED`, warn: true, title: tip });
    }
    if (warnCount > 0) auditChips.push({ text: `${warnCount} WARN`, warn: true });
  }

  const reviewHtml = latestReview
    ? `
      <div class="review-box">
        <div class="review-head">
          <span class="review-verdict${latestReview.verdict === "approve" ? " is-approve" : ""}">${esc(
            (latestReview.verdict || "pending").replace("_", " "),
          )}</span>
          <span class="section-meta">${esc(latestReview.reviewer_model || "Antigravity")} · ${esc(
            formatTime(latestReview.timestamp),
          )}</span>
        </div>
        <p class="review-summary">${esc(latestReview.summary || "")}</p>
        ${
          Array.isArray(latestReview.issues) && latestReview.issues.length > 0
            ? `<ul class="review-issues">${latestReview.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`
            : ""
        }
      </div>
    `
    : `<p class="muted-placeholder">Awaiting Antigravity semantic Code Review.</p>`;

  const filesHtml =
    diffFiles.length > 0
      ? `<div class="file-list">
          ${diffFiles
            .slice(0, 30)
            .map(
              (f) => `
              <div class="file-row">
                <div class="file-left">
                  <span class="glyph-box">${esc(changeTag(f.change_type))}</span>
                  <span class="file-path" title="${esc(f.path)}">${esc(f.path)}</span>
                </div>
                <span class="file-stats">${f.binary ? "BIN" : `+${formatNumber(f.insertions)} / -${formatNumber(f.deletions)}`}</span>
              </div>
            `,
            )
            .join("")}
          ${
            diffFiles.length > 30
              ? `<div class="file-row"><span class="muted-placeholder">+${diffFiles.length - 30} additional files</span></div>`
              : ""
          }
        </div>`
      : `<p class="muted-placeholder">Working tree has no changes relative to base commit.</p>`;

  const orderedEvents = [...events].reverse().slice(0, 40);
  const eventsHtml =
    orderedEvents.length > 0
      ? `<div class="event-stream">
          ${orderedEvents
            .map((ev) => {
              const isNew = prevMaxSeq > 0 && typeof ev.seq === "number" && ev.seq > prevMaxSeq;
              return `
                <div class="event-item${isNew ? " is-new" : ""}">
                  <span class="event-time">${esc(formatTime(ev.timestamp))}</span>
                  <span class="event-phase">${esc(ev.phase || "sys")}</span>
                  <div class="event-msg">
                    ${esc(ev.message || ev.type || "")}
                    ${ev.agent ? `<span class="event-agent">${esc(ev.agent)}</span>` : ""}
                  </div>
                </div>
              `;
            })
            .join("")}
        </div>`
      : `<p class="muted-placeholder">No execution events recorded yet.</p>`;

  contentEl.innerHTML = `
    <section class="section">
      <div class="task-head">
        <div class="task-title-wrap">
          <h1 class="task-name">${esc(taskName)}</h1>
          <div class="task-id-line">
            <span>${esc(taskId)}</span>
            ${repoPath ? `<span>·</span><span title="${esc(repoPath)}">${esc(shortPath(repoPath))}</span>` : ""}
          </div>
        </div>
        <div class="state-badge ${badgeClass}">
          <span class="state-badge-dot" aria-hidden="true"></span>
          <span>${esc(status)}</span>
        </div>
      </div>

      <div class="meta-grid">
        <div class="meta-cell">
          <div class="meta-label">Phase &amp; Agent</div>
          <div class="meta-value${agentChanged ? " handoff-enter" : ""}" title="${esc(currentAgent)}">${esc(currentAgent)}</div>
          <div class="meta-sub">STAGE · ${esc(currentPhase)}</div>
        </div>
        <div class="meta-cell">
          <div class="meta-label">Builder Model</div>
          <div class="meta-value mono" title="${esc(modelRationale)}">${esc(builderModel)}</div>
          <div class="meta-sub">${esc(modelModeLabel)} · REV ${esc(reviewerModel === "inherit_session" ? "SESSION" : reviewerModel)}</div>
        </div>
        <div class="meta-cell">
          <div class="meta-label">Git Branch</div>
          <div class="meta-value mono" title="${esc(branch)}">${esc(branch)}</div>
          <div class="meta-sub">${baseBranch ? `BASE · ${esc(baseBranch)}` : "ISOLATED BRANCH"}</div>
        </div>
        <div class="meta-cell">
          <div class="meta-label">Worktree</div>
          <div class="meta-value mono" title="${esc(worktreePath)}">${esc(shortPath(worktreePath))}</div>
          <div class="meta-sub">ISOLATED GIT WORKTREE</div>
        </div>
      </div>

      ${
        lastError
          ? `<div class="notice-banner"><span class="notice-tag">${esc(failureCategory || "NOTICE")}</span>${esc(lastError)}</div>`
          : ""
      }
    </section>

    <section class="section">
      <div class="section-header">
        <span class="section-title">Pipeline</span>
        <span class="section-meta">${esc(status)}</span>
      </div>
      <div class="pipeline">${stageHtml}</div>
      <div class="repair-row">
        <span class="repair-label">REPAIR ROUNDS · ${repairRound} / ${maxRepairs}</span>
        <div class="repair-pips" aria-label="Repair rounds ${repairRound} of ${maxRepairs}">${pipsHtml}</div>
      </div>
    </section>

    <div class="telemetry-grid">
      <div class="telemetry-cell">
        <div class="telemetry-label">Tests</div>
        <div class="telemetry-value">${esc(verOverall)}</div>
        <div class="telemetry-sub">${
          isVerifyingNow
            ? "Running checks…"
            : verTotal > 0
              ? `${verPassed} pass · ${verFailed} fail${verSkipped > 0 ? ` · ${verSkipped} skip` : ""}`
              : "Not run"
        }</div>
      </div>
      <div class="telemetry-cell">
        <div class="telemetry-label">Git Diff</div>
        <div class="telemetry-value">${filesCount} ${filesCount === 1 ? "file" : "files"}</div>
        <div class="telemetry-sub">+${formatNumber(totalIns)} / -${formatNumber(totalDel)}</div>
      </div>
      <div class="telemetry-cell">
        <div class="telemetry-label">Tokens</div>
        <div class="telemetry-value">${tokAvail ? esc(tokTotal) : "—"}</div>
        <div class="telemetry-sub" title="${tokAvail ? `cached ${formatNumber(tok.cached_input_tokens)}` : "Not emitted"}">${esc(tokSub)}</div>
      </div>
    </div>

    <section class="section">
      <div class="section-header">
        <span class="section-title">Independent Verification</span>
        <span class="section-meta">${isVerifyingNow ? "RUNNING" : verSummary ? `${verPassed}/${verTotal} PASSED` : "PENDING"}</span>
      </div>
      ${checksHtml}
      ${
        auditChips.length > 0
          ? `<div class="audit-strip">${auditChips
              .map(
                (c) =>
                  `<span class="audit-chip${c.warn ? " is-warn" : ""}"${c.title ? ` title="${esc(c.title)}"` : ""}>${esc(c.text)}</span>`,
              )
              .join("")}</div>`
          : ""
      }
    </section>

    <section class="section">
      <div class="section-header">
        <span class="section-title">Latest Code Review</span>
        <span class="section-meta">${latestReview ? `ROUND ${esc(latestReview.round ?? 0)}` : "PENDING"}</span>
      </div>
      ${reviewHtml}
    </section>

    <section class="section">
      <div class="section-header">
        <span class="section-title">Modified Files</span>
        <span class="section-meta">${filesCount} ${filesCount === 1 ? "FILE" : "FILES"} · +${formatNumber(totalIns)} / -${formatNumber(totalDel)}</span>
      </div>
      ${filesHtml}
    </section>

    <section class="section">
      <div class="section-header">
        <span class="section-title">Execution Events</span>
        <span class="section-meta">${events.length} EVENTS</span>
      </div>
      ${eventsHtml}
    </section>
  `;
}

function applySnapshotIfChanged(snapshot: MonitorSnapshot): void {
  if (!snapshot) return;
  const rev = String(snapshot.revision || "");
  if (rev && rev === lastRevision && currentSnapshot) {
    const status = snapshot.state ? String(snapshot.state.status || "") : null;
    updateSyncIndicator(status);
    return;
  }
  lastRevision = rev;
  renderSnapshot(snapshot);
}

async function pollOnce(): Promise<void> {
  if (!connected || document.hidden || pollInFlight) return;
  pollInFlight = true;
  try {
    const args: Record<string, unknown> = { events_limit: 80 };
    if (userPinnedTaskId) {
      args.task_id = userPinnedTaskId;
    }
    const res = await app.callServerTool({
      name: "get_monitor_snapshot",
      arguments: args,
    });
    const sc = (res as { structuredContent?: MonitorSnapshot })?.structuredContent;
    if (sc && typeof sc === "object") {
      applySnapshotIfChanged(sc);
    }
  } catch {
    // Keep last valid snapshot silently on transient poll hiccup
  } finally {
    pollInFlight = false;
    scheduleNextPoll();
  }
}

function scheduleNextPoll(): void {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
  if (!connected || document.hidden) return;
  const status = currentSnapshot?.state ? String(currentSnapshot.state.status || "") : "";
  const intervalMs = ACTIVE_STATES.has(status) ? 1500 : 4000;
  pollTimer = window.setTimeout(() => {
    void pollOnce();
  }, intervalMs);
}

selectorEl.addEventListener("change", () => {
  const chosen = selectorEl.value || null;
  userPinnedTaskId = chosen;
  lastRevision = "";
  lastSeenEventSeq = 0;
  lastPhaseStatuses = {};
  void pollOnce();
});

document.addEventListener("visibilitychange", () => {
  document.body.classList.toggle("is-hidden", document.hidden);
  const status = currentSnapshot?.state ? String(currentSnapshot.state.status || "") : null;
  updateSyncIndicator(status);
  if (document.hidden) {
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
  } else {
    void pollOnce();
  }
});

app.ontoolresult = (result) => {
  const sc = (result as { structuredContent?: MonitorSnapshot })?.structuredContent;
  if (sc && typeof sc === "object") {
    applySnapshotIfChanged(sc);
  }
};

app.onhostcontextchanged = (ctx) => {
  if (ctx?.theme) {
    applyDocumentTheme(ctx.theme);
    document.documentElement.setAttribute("data-theme", ctx.theme);
  }
};

async function bootstrap(): Promise<void> {
  try {
    await app.connect();
    connected = true;
    const hostCtx = app.getHostContext();
    if (hostCtx?.theme) {
      applyDocumentTheme(hostCtx.theme);
      document.documentElement.setAttribute("data-theme", hostCtx.theme);
    }
    await pollOnce();
  } catch (err) {
    contentEl.innerHTML = `
      <div class="empty-state">
        <div class="empty-mark" aria-hidden="true"></div>
        <p class="empty-title">Monitor host connection unavailable</p>
        <p class="empty-sub mono">${esc(err instanceof Error ? err.message : String(err))}</p>
      </div>
    `;
  }
}

void bootstrap();
