import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  registerAppResource,
  registerAppTool,
  RESOURCE_MIME_TYPE,
} from "@modelcontextprotocol/ext-apps/server";
import { z } from "zod";
import { formatPlainTextSummary, readMonitorSnapshot } from "./stateReader.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const MONITOR_URI = "ui://codex-dev/monitor.html";

function loadMonitorHtml(): string {
  const candidates = [
    join(__dirname, "monitor.html"),
    join(__dirname, "index.html"),
    join(__dirname, "../dist/monitor.html"),
    join(__dirname, "../dist/index.html"),
  ];
  for (const p of candidates) {
    if (existsSync(p)) {
      return readFileSync(p, "utf-8");
    }
  }
  throw new Error(`Bundled monitor HTML not found. Searched: ${candidates.join(", ")}`);
}

export function createMonitorServer(): McpServer {
  const server = new McpServer({
    name: "codex-dev-monitor",
    version: "1.0.0",
  });

  // 1. Primary read-only sidebar / global entrypoint tool
  registerAppTool(
    server,
    "open_monitor",
    {
      title: "Codex Dev Monitor",
      description:
        "Open the read-only Codex Dev Monitor side panel showing live Antigravity x Codex CLI task state, stages, events, verification results, git diff stats, repair rounds, token usage, and review verdict.",
      inputSchema: z.object({
        task_id: z
          .string()
          .optional()
          .describe("Optional task ID to inspect; defaults to the active or most recent task."),
      }),
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
      _meta: {
        ui: {
          resourceUri: MONITOR_URI,
        },
        "com.google.antigravity/ui": {
          entrypoints: [
            { type: "aux_pane", title: "Codex Dev Monitor" },
            { type: "global", title: "Codex Dev Monitor" },
          ],
        },
      },
    },
    async (args: { task_id?: string }) => {
      const snapshot = readMonitorSnapshot(args?.task_id);
      return {
        content: [
          {
            type: "text" as const,
            text: formatPlainTextSummary(snapshot),
          },
        ],
        structuredContent: snapshot as unknown as Record<string, unknown>,
      };
    },
  );

  // 2. App-only strictly read-only polling tool for live state refresh inside the iframe
  registerAppTool(
    server,
    "get_monitor_snapshot",
    {
      title: "Get Codex Dev Snapshot",
      description: "Read-only live snapshot of Codex Dev orchestrator task state for the monitor view.",
      inputSchema: z.object({
        task_id: z.string().optional(),
        events_limit: z.number().int().min(10).max(200).optional(),
      }),
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
      _meta: {
        ui: {
          resourceUri: MONITOR_URI,
          visibility: ["app"],
        },
      },
    },
    async (args: { task_id?: string; events_limit?: number }) => {
      const snapshot = readMonitorSnapshot(args?.task_id, args?.events_limit ?? 80);
      return {
        content: [
          {
            type: "text" as const,
            text: formatPlainTextSummary(snapshot),
          },
        ],
        structuredContent: snapshot as unknown as Record<string, unknown>,
      };
    },
  );

  // 3. Single-file HTML UI resource (`text/html;profile=mcp-app`)
  registerAppResource(
    server,
    "Codex Dev Monitor",
    MONITOR_URI,
    {},
    async () => ({
      contents: [
        {
          uri: MONITOR_URI,
          mimeType: RESOURCE_MIME_TYPE,
          text: loadMonitorHtml(),
          _meta: {
            ui: {
              prefersBorder: false,
            },
          },
        },
      ],
    }),
  );

  return server;
}

async function main(): Promise<void> {
  const server = createMonitorServer();
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  process.stderr.write(`[codex-dev-monitor] fatal error: ${err instanceof Error ? err.stack || err.message : String(err)}\n`);
  process.exit(1);
});
