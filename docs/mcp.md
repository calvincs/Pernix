# MCP Servers

Pernix speaks the Model Context Protocol as a client: point it at any MCP
server — local (a subprocess over stdio) or remote (Streamable HTTP) — and
that server's tools become normal Pernix tools named `mcp_<server>_<tool>`.
They show up in the Tools tab, go through the dangerous-tool gate, get
scout-curated into turns like everything else, and accrue the same health
metrics and reputation signals.

MCP support is on by default but completely inert until you configure a
server. Configuring a server is the opt-in.

## Adding a server

Three equivalent ways:

1. **Explorer → MCP tab** — click `+`, paste a standard `mcpServers` config
   (the same JSON Claude Code, Claude Desktop, Cursor, and VS Code use),
   hit **Test**, then **Save & Connect**.
2. **Ask the agent** — "add the GitHub MCP server". The agent calls
   `mcp_add_server`, which is a dangerous-gated tool: you confirm before
   anything is installed or spawned.
3. **Edit `data/mcp_servers.json`** directly, then reload from the MCP tab
   (or ask the agent to run `mcp_reload_server`).

### Config format

```json
{
  "mcpServers": {
    "github": {
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": { "Authorization": "Bearer ${GITHUB_MCP_TOKEN}" }
    },
    "fs": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem@2025.8.21", "/app/data/workspace"]
    }
  }
}
```

- `url` ⇒ remote (Streamable HTTP); `command` ⇒ local stdio subprocess. An
  explicit `"type"` (`stdio` | `http` | `sse`) overrides the inference —
  `sse` is the deprecated legacy transport, for old servers only.
- **Secrets never go in this file.** Put them in `.env`
  (`GITHUB_MCP_TOKEN=...`) and reference them as `"${GITHUB_MCP_TOKEN}"`.
  A value that looks like a pasted literal token is rejected.
- Pin stdio server versions (`pkg@1.2.3`, not `pkg`) — an unpinned `npx -y`
  runs whatever was published most recently, which is a supply-chain risk.
- Optional per-server keys (Pernix extras, ignored by other clients):
  - `enabled` (default true)
  - `safety` — `safe` | `caution` | `dangerous` for this server's tools
    (default: the `mcp_default_safety` setting, `caution`)
  - `timeout` — per-call seconds (default: `mcp_call_timeout`)
  - `tool_allowlist` — only expose these of the server's tools
  - `env`, `cwd`, `headers` as usual

## How the tools behave

- **Naming**: `mcp_<server>_<tool>`, flat snake_case, capped at 64 chars
  (overflow gets a stable hash suffix). Descriptions are prefixed
  `[MCP:<server>]` so provenance is always visible in the prompt.
- **Safety**: default `caution`. A server-sent `destructiveHint` escalates
  that tool to `dangerous`; server annotations can never *lower* a level.
  Per-tool overrides in the Tools tab work and survive reconnects.
- **Surface**: MCP tools are scout-curated — they enter a turn's schema when
  scout recommends them (or once the session has used them), so a server
  with 40 tools doesn't bloat every prompt.
- **Sessions**: canary sessions never call MCP tools (external side effects
  in synthetic runs).
- **Results**: text comes back inline; images/audio are saved under
  `workspace/mcp/<server>/` (view images with `view_image`); huge outputs go
  through the normal truncation + kernel-binding machinery.

## Lifecycle and failure behavior

- Servers connect in the background at startup — a dead server never blocks
  boot. Its tools stay registered; calls return a clear error while the
  manager retries with exponential backoff (max 5 min), and you get one
  notification per incident.
- Idle **stdio** servers are suspended after `mcp_idle_seconds` (default
  15 min): the child process is reaped, the tools stay, and the next call
  respawns it transparently.
- Tool lists refresh on the server's `listChanged` notification, on a
  periodic sweep (`mcp_refresh_interval_s`), and on manual reload.
- stdio server stderr is captured to `data/logs/mcp_<name>.stderr.log`.
- Toggling `mcp_enabled` off is hot: local server processes die, tools stay
  visible but refuse with a "disabled" error. Toggling back on reconnects.

## Settings

Settings → MCP Servers: `mcp_enabled`, `mcp_stdio_enabled` (off = remote
servers only — the supply-chain valve), `mcp_default_safety`,
`mcp_call_timeout`, `mcp_connect_timeout`, `mcp_idle_seconds`,
`mcp_max_servers`, `mcp_max_tools_per_server`, `mcp_refresh_interval_s`.

## REST

- `GET /api/mcp/servers` — configs + live status
- `POST /api/mcp/servers` — add one (`{"name", "config"}`) or import a
  pasted `{"mcpServers": {...}}` blob
- `DELETE /api/mcp/servers/{name}`
- `POST /api/mcp/servers/{name}/toggle` — `{"enabled": bool}`
- `POST /api/mcp/servers/{name}/reload`
- `POST /api/mcp/test` — dry-run connect, nothing saved

## Not (yet) supported

Interactive input requests from servers (MRTR/elicitation) return a clear
error asking for the data as tool arguments; OAuth login flows for remote
servers (use a pre-issued token in a header for now); MCP resources/prompts
as first-class objects; sampling/roots/logging (deprecated in the 2026-07-28
spec — deliberately skipped).

Design history: [dev/mcp-integration-plan.md](dev/mcp-integration-plan.md).
