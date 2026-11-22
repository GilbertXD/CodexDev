#!/usr/bin/env node
/**
 * Local MCP Apps Host Preview Server for real visual inspection of Codex Dev Monitor.
 * Mounts the exact bundled `dist/monitor.html` inside a sidebar-width iframe and speaks
 * the genuine `@modelcontextprotocol/ext-apps` postMessage JSON-RPC protocol backed by
 * live orchestrator state from CODEX_DEV_HOME (via the compiled MCP server over stdio!).
 */
import http from "node:http";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.PREVIEW_PORT || 43917);
const serverPath = process.env.MCP_SERVER_PATH || resolve(__dirname, "../mcp-app/dist/server.mjs");
const monitorHtmlPath = process.env.MONITOR_HTML_PATH || resolve(__dirname, "../mcp-app/dist/monitor.html");
const codexDevHome = process.env.CODEX_DEV_HOME || "";

// Spawn the real MCP server over stdio so the preview exercises the exact MCP backend!
const mcpChild = spawn(process.execPath, [serverPath], {
  env: { ...process.env, ...(codexDevHome ? { CODEX_DEV_HOME: codexDevHome } : {}) },
  stdio: ["pipe", "pipe", "pipe"],
});

let buf = "";
let nextId = 1;
const pending = new Map();

mcpChild.stdout.on("data", (chunk) => {
  buf += chunk.toString("utf-8");
  let idx;
  while ((idx = buf.indexOf("\n")) !== -1) {
    const line = buf.slice(0, idx).trim();
    buf = buf.slice(idx + 1);
    if (!line) continue;
    const msg = JSON.parse(line);
    if (msg.id !== undefined && pending.has(msg.id)) {
      const fn = pending.get(msg.id);
      pending.delete(msg.id);
      fn(msg);
    }
  }
});

function mcpRpc(method, params = {}) {
  const id = nextId++;
  return new Promise((res, rej) => {
    const t = setTimeout(() => rej(new Error(`MCP RPC timeout: ${method}`)), 8000);
    pending.set(id, (msg) => {
      clearTimeout(t);
      res(msg);
    });
    mcpChild.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
  });
}

await mcpRpc("initialize", {
  protocolVersion: "2025-03-26",
  capabilities: {
    extensions: {
      "io.modelcontextprotocol/ui": { mimeTypes: ["text/html;profile=mcp-app"] },
      "com.google.antigravity/ui": { entrypoints: ["aux_pane", "global"] },
    },
  },
  clientInfo: { name: "antigravity-preview-host", version: "2.34.0" },
});
mcpChild.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" })}\n`);

const hostPage = `<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Codex Dev Monitor — Visual Verification Harness</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #141414;
      color: #e5e5e5;
      font-family: -apple-system, BlinkMacSystemFont, "Inter", sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 20px;
      gap: 16px;
    }
    .bar {
      display: flex;
      gap: 12px;
      align-items: center;
      font-size: 12px;
      font-family: ui-monospace, monospace;
      color: #a3a3a3;
    }
    .frames {
      display: flex;
      gap: 24px;
      align-items: flex-start;
      flex-wrap: wrap;
      justify-content: center;
    }
    .frame-col {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .frame-label {
      font-family: ui-monospace, monospace;
      font-size: 11px;
      letter-spacing: 0.08em;
      color: #a3a3a3;
      text-transform: uppercase;
    }
    iframe {
      width: 420px;
      height: 820px;
      border: 1px solid #333;
      border-radius: 4px;
      background: #fff;
    }
    iframe.dark {
      background: #0a0a0a;
    }
  </style>
</head>
<body>
  <div class="bar">
    <span>ANTIGRAVITY AUX_PANE PREVIEW · LIVE MCP STDIO BACKEND</span>
  </div>
  <div class="frames">
    <div class="frame-col">
      <div class="frame-label">Light Monochrome · 420px Aux Pane</div>
      <iframe id="frame-light" src="/monitor.html"></iframe>
    </div>
    <div class="frame-col">
      <div class="frame-label">Dark Monochrome · 420px Aux Pane</div>
      <iframe id="frame-dark" class="dark" src="/monitor.html"></iframe>
    </div>
  </div>
  <script>
    const frameLight = document.getElementById("frame-light");
    const frameDark = document.getElementById("frame-dark");

    function themeForSource(win) {
      return win === frameDark.contentWindow ? "dark" : "light";
    }

    window.addEventListener("message", async (ev) => {
      const msg = ev.data;
      if (!msg || msg.jsonrpc !== "2.0") return;
      const src = ev.source;
      if (!src) return;
      const theme = themeForSource(src);

      if (msg.method === "ui/initialize") {
        src.postMessage({
          jsonrpc: "2.0",
          id: msg.id,
          result: {
            protocolVersion: "2026-01-26",
            hostInfo: { name: "Antigravity Preview Host", version: "2.34.0" },
            hostCapabilities: { serverTools: {} },
            hostContext: { theme, displayMode: "inline" },
          },
        }, "*");
        return;
      }

      if (msg.method === "tools/call") {
        try {
          const resp = await fetch("/api/tool", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(msg.params || {}),
          });
          const data = await resp.json();
          src.postMessage({ jsonrpc: "2.0", id: msg.id, result: data }, "*");
        } catch (err) {
          src.postMessage({
            jsonrpc: "2.0",
            id: msg.id,
            error: { code: -32000, message: String(err) },
          }, "*");
        }
        return;
      }
    });
  </script>
</body>
</html>`;

const server = http.createServer(async (req, res) => {
  if (req.url === "/" || req.url === "/index.html") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(hostPage);
    return;
  }
  if (req.url === "/monitor.html") {
    const html = readFileSync(monitorHtmlPath, "utf-8");
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(html);
    return;
  }
  if (req.url === "/api/tool" && req.method === "POST") {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", async () => {
      try {
        const params = JSON.parse(body || "{}");
        const rpcResp = await mcpRpc("tools/call", params);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify(rpcResp.result || {}));
      } catch (err) {
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: String(err) }));
      }
    });
    return;
  }
  res.writeHead(404);
  res.end("Not found");
});

server.listen(port, "127.0.0.1", () => {
  console.log(`Codex Dev Monitor preview listening on http://127.0.0.1:${port}`);
});

process.on("SIGTERM", () => {
  mcpChild.kill();
  server.close(() => process.exit(0));
});
process.on("SIGINT", () => {
  mcpChild.kill();
  server.close(() => process.exit(0));
});
