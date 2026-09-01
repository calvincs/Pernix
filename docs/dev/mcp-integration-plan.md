# MCP Client Integration Plan — the "MCP bridge"

Status: **Phase 1 implemented** on `next-3.1-testing` (2026-09-01, same day as
the draft; Calvin approved and delegated §10 to industry-standard defaults —
resolutions recorded inline there). User docs: `docs/mcp.md`.
Date: 2026-09-01
Branch context: `next-3.1-testing` @ `42f9deb`

---

## 1. Goal

Let Pernix connect to external MCP (Model Context Protocol) servers and use their
tools — with the same gating, observability, and scout curation every native tool
gets — and make adding/removing servers something a user can do in one paste or
one sentence to the agent.

Non-goals (v1): being an MCP *server*, MCP sampling/roots/logging (deprecated in
the spec — see §3), MCP Apps, the tasks extension.

## 2. Prior decision context

`docs/dev/adaptation-plan.md:65,159` deferred MCP: *"later; their in-kernel-SDK
pattern is right for us and Phase 2 is its prerequisite."* Phase 2 (the session
kernel, `core/kernel/`) is now implemented. This plan takes the registry-first
route for v1 (MCP tools become ordinary registry tools) and treats in-kernel
exposure (calling MCP tools from kernel code) as a Phase 3 complement, not a
replacement — registry-first is what makes MCP tools visible to scout, the
dangerous-tool gate, per-tool health metrics, and `scout_signals` for free.

## 3. Protocol landscape (as of 2026-09)

- **Current spec: 2026-07-28.** The protocol went stateless: no
  `initialize` handshake, no `Mcp-Session-Id`, every request self-describing via
  `_meta`, new `server/discover` RPC, `subscriptions/listen` replaces the GET
  stream and `resources/subscribe`. Streamable HTTP requires `Mcp-Method` /
  `Mcp-Name` headers. List results carry `ttlMs`/`cacheScope` cache hints, and
  servers SHOULD return tools in deterministic order (good for our prompt-cache
  discipline).
- **Deprecated:** Roots, Sampling, Logging; the old HTTP+SSE transport; OAuth
  Dynamic Client Registration (in favor of Client ID Metadata Documents).
  Twelve-month windows. **We build none of the deprecated features.**
- **Server-initiated asks** (elicitation, etc.) are now MRTR (Multi Round-Trip
  Requests): a tool call can return `resultType: "input_required"`; the client
  retries the call with `inputResponses`. v1 surfaces this as a clear error;
  Phase 2 wires it to the questions system.
- **Ecosystem reality:** most public servers still speak 2025-06-18 /
  2025-11-25 (stateful). The **official `mcp` Python SDK** (Tier 1) negotiates
  versions both ways, ships stdio + Streamable HTTP transports with safe
  subprocess teardown (kill-tree, cancellation-shielded), and provides OAuth
  (PKCE + refresh), static-bearer, and client-credentials auth. We build on it
  and never hand-roll the protocol.
- **Official registry** at `registry.modelcontextprotocol.io` (REST API frozen
  at v0.1) — the basis for a Phase 3 "search and install a server" UX.
- **Config convention:** every major client (Claude Code/Desktop, Cursor,
  VS Code) uses a JSON `mcpServers` map — `command`/`args`/`env` for local
  subprocess servers, `url` for remote ones. We adopt the same shape so users
  can paste configs straight in.

## 4. Design overview

```
data/mcp_servers.json ──▶ core/extensions/mcp/
                              │  register() gated by settings.mcp_enabled
                              ▼
                          MCPManager (singleton, main event loop)
                              │  one MCPServerConnection per configured server
                              │  states: disabled → connecting → ready → degraded
                              │  SDK ClientSession + transport per connection
                              ▼
                    tools/list ──▶ ToolRegistry.register(source="mcp",
                              │        name="mcp_<server>_<tool>", …)
                              │        + rebuild_index()
                              ▼
        sync wrapper on tool thread ──run_coroutine_threadsafe──▶ session.call_tool()
                              ▼
        CallToolResult → (str, metadata) → normal executor pipeline
        (truncation, large-result kernel binding, SSE events, metrics)
```

One new bundled extension package, `core/extensions/mcp/`, appended to
`BUNDLED_EXTENSIONS`. It follows the house convention verbatim
(`core/extensions/rlm/__init__.py:11-13`): `register()` is a hard off-switch at
startup; every call path re-checks `settings.mcp_enabled` so a hot toggle-off
degrades to a clear error.

## 5. Components

### 5.1 `MCPManager` and connection lifecycle

- Singleton owned by the FastAPI lifespan (created after `load_extensions`,
  shut down alongside the browser/candor teardown in `api/app.py`). Connections
  live on the main asyncio loop — the SDK is asyncio-native, so this is the
  natural home; tool threads marshal in via the established
  `_context["_loop"]` + `run_coroutine_threadsafe` pattern
  (`core/extensions/web/__init__.py:43-50`).
- Per-server `MCPServerConnection`: a supervisor task that enters the SDK's
  async context stack (transport → `ClientSession`), performs
  initialize/discover, runs `tools/list`, registers tools, then holds the
  session open serving calls. `ClientSession` multiplexes concurrent requests,
  so no per-call queueing is needed.
- **Boot does not block on servers.** Lifespan kicks off connects as background
  tasks with `settings.mcp_connect_timeout` (default 30s); a slow server's
  tools appear when it becomes ready. Mirrors the browser-singleton lazy-init
  pattern.
- **Reconnect policy:** on transport failure the connection flips to
  `degraded`, tools stay registered (calls return a clear "server X
  unreachable — try mcp_reload_server" error), and the supervisor retries with
  exponential backoff (cap ~5 min). After N consecutive call failures the
  server is marked degraded pre-emptively (Candor `_guarded` precedent, but
  per-server and recoverable, never process-inert).
- **Idle policy:** stdio subprocess servers can be reaped after
  `mcp_idle_seconds` by the maintenance heartbeat and respawned on demand
  (KernelRegistry precedent). Remote HTTP connections are cheap; under the
  2026 stateless transport there is nothing to keep alive at all.
- **Child-process notes:** the SDK spawns from the long-lived event-loop
  thread, so the thread-scoped-PDEATHSIG trap
  (`core/extensions/rlm/child_runner.py:457-473`) does not bite, and the SDK
  already does kill-tree teardown; `init: true` in docker-compose reaps
  stragglers. We still record child PIDs on the manager for the shutdown
  escalation path (3s graceful → kill, browser precedent).

### 5.2 Tool registration

- **Naming:** `mcp_<server>_<tool>`, lowercased snake_case, sanitized. Server
  aliases are validated `[a-z0-9_]{1,16}` at config time. Names are capped at
  64 chars (OpenAI function-name limit); overflow truncates the tool part and
  appends a 4-char hash. Collisions get the same treatment. Flat names keep
  `_TOOL_ALIASES`/difflib hinting and the Tools tab working unchanged.
- **Provenance:** `source="mcp"` (new value alongside builtin/extension/custom),
  `category="mcp:<server>"`, `tags=[server, "mcp", …]`, and the description is
  prefixed `[MCP:<server>] `. Descriptions are length-capped
  (`mcp_max_description_chars`, default 1024) — server-supplied text is
  untrusted input headed for the system prompt.
- **Safety level:** default `settings.mcp_default_safety` (**"caution"**),
  overridable per server in config. MCP tool annotations may only tighten,
  never loosen: `destructiveHint: true` ⇒ `dangerous`; `readOnlyHint` is
  ignored for loosening (annotations are server-controlled, i.e. untrusted).
  Existing per-tool overrides in `data/tools.json` still apply on top and
  survive refreshes (names are stable).
- **Session-type denial:** `denied_session_types={"canary"}` by default —
  canaries must not hit external services.
- **Timeout:** per-server `timeout` (default `mcp_call_timeout`, 60s), capped
  by `max_timeout`. Runs on the normal tool pool; the ≥30s
  `_credit_long_call` budget guarantee applies unchanged.
- **Surface discipline (important):** `_resolve_tool_surface` only
  auto-includes `source=="builtin"` tools, so MCP tools enter the active
  surface exclusively via scout recommendation, the monotonic per-session
  allowlist, and co-occurrence — i.e. **big MCP tool sets do not blow up the
  prompt**; scout curation is the natural fit here. Each server's tools are
  added as a co-occurrence sibling group. One gap to patch: the no-scout
  fallback (`core/agent.py:1911-1915`) includes *all* enabled tools; it should
  exclude `source=="mcp"` beyond a small cap.
- `registry.rebuild_index()` after every registration batch (hard requirement
  for scout/`discover_tools` visibility). Registration/refresh reuses the
  mid-turn widening hooks (`_expand_tools_from_discovery`) so a newly connected
  server is callable next round.

### 5.3 Registry changes (small, core)

1. `ToolRegistry.unregister(name)` — does not exist today; needed for server
   removal, refresh diffs, and disable. Removes from `_tools`, metrics kept.
2. Accept `source="mcp"` wherever source is switched on.
3. Nothing else — schemas already flow verbatim (raw JSON Schema dicts), and
   `_validate_arg_types` (`core/agent.py:646`) handles the subset it knows and
   ignores keywords it doesn't. The 2026 spec allows full JSON Schema 2020-12
   (`$ref`, composition) in `inputSchema`; we pass through untouched and let
   the server do final validation. One check: our drop-unknown-properties
   behavior vs servers that rely on `additionalProperties` defaulting to true —
   resolve during implementation with a test.

### 5.4 Call bridge and result mapping

Sync wrapper (runs on `pernix-tool` thread) → `run_coroutine_threadsafe` →
`session.call_tool(name, args, read_timeout_seconds=…)`. Mapping
`CallToolResult` → Pernix `(str, metadata)`:

- `TextContent` blocks: joined as the result string.
- `structuredContent` (and no text): pretty-printed JSON.
- `ImageContent`: written to `data/workspace/mcp/<server>/…`, result references
  the path — the `view_image` bridge (591cb46) can then display it.
- `EmbeddedResource` / resource links: rendered as URI + a hint to call
  `mcp_read_resource`.
- `isError: true`: returned as an error string → executor records a failure
  (not a refusal) → metrics/post-mortems/Candor grade it correctly.
- `resultType: "input_required"` (MRTR): v1 returns a structured error naming
  the missing inputs; Phase 2 converts to an `ask_user` round trip and retries
  with `inputResponses`.
- Metadata: `{"mcp_server": name, "was_error": …}`.
- Oversized results need no new code: `truncate_output` and
  `_bind_large_results` already handle them.

### 5.5 Configuration

**Per-item registry file `data/mcp_servers.json`** (precedent:
`cron_jobs.json`), ecosystem-standard shape plus optional Pernix keys:

```json
{
  "mcpServers": {
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": { "Authorization": "Bearer ${GITHUB_MCP_TOKEN}" },
      "enabled": true,
      "safety": "caution",
      "timeout": 60,
      "tool_allowlist": null
    },
    "fs": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/app/data/workspace"],
      "env": { "LOG_LEVEL": "warn" }
    }
  }
}
```

- `type` may be omitted (inferred: `url` ⇒ http, `command` ⇒ stdio), so a
  config pasted from Claude Code/Cursor works verbatim.
- `${VAR}` in `headers`/`env`/`args` expands from process env / `.env` at
  connect time. **Secrets never live in this file**; the add paths warn when a
  header value looks like a literal token and offer to move it to `.env`
  (`write_env_var` already exists).
- Atomic writes (tempfile + `os.replace`, same as settings.json).

**New `Settings` fields** (flat, house style): `mcp_enabled` (default
**False** until burn-in — canary/dream precedent), `mcp_default_safety`,
`mcp_call_timeout`, `mcp_connect_timeout`, `mcp_idle_seconds`,
`mcp_max_servers` (8), `mcp_max_tools_per_server` (40, excess skipped with a
warning), `mcp_max_description_chars`, `mcp_refresh_interval_s`,
`mcp_stdio_enabled` (escape hatch: remote-only mode — stdio servers are
arbitrary local code). Settings-modal section added to `SECTIONS`; changing
`mcp_enabled` is hot (manager start/stop), no restart fields.

### 5.6 User surfaces

- **Agent tools** (the "one sentence to the agent" path):
  `mcp_add_server` (**dangerous** — config mutation + supply chain),
  `mcp_remove_server` (dangerous), `mcp_list_servers` (safe: status, tool
  counts, health), `mcp_reload_server` (caution). Add performs a test-connect
  and reports the discovered tools before finishing.
- **REST** `api/routers/mcp.py`: `GET /api/mcp/servers` (config + live
  status + tools), `POST /api/mcp/servers` (add/update; accepts a full pasted
  `mcpServers` blob for import), `DELETE /api/mcp/servers/{name}`,
  `POST /api/mcp/servers/{name}/toggle|reload`, `POST /api/mcp/test`
  (dry-run connect without saving).
- **UI:** new **MCP tab** in the Explorer panel (Jobs/Skills tab precedent):
  server list with status dot (ready/degraded/disabled), tool count,
  enable/disable, reload, remove, per-server safety select, and a paste-JSON
  import box. Individual MCP tools also appear in the existing Tools tab
  automatically (toggle + safety UI for free).
- **Docs:** `docs/mcp.md` user guide; update `docs/internals/extensions.md`
  (whose "discovers on next start" claim is wrong today anyway — the
  `BUNDLED_EXTENSIONS` list is literal).

### 5.7 Resources and prompts (Phase 2)

Not one-tool-per-resource. Two generic tools per the ecosystem norm:
`mcp_resources(server?)` (list, honoring `ttlMs` caching) and
`mcp_read_resource(server, uri)` (routed through truncation/binding).
MCP *prompts* map naturally onto skill-shaped text: expose
`mcp_prompts`/`mcp_get_prompt` tools; deeper skill-registry integration only
if real usage demands it.

### 5.8 Auth

- **Phase 1:** static bearer/API-key headers via `${VAR}` expansion, plus
  client-credentials (machine-to-machine) via the SDK. Covers most real
  remote servers today and fits a headless box.
- **Phase 2:** interactive OAuth (PKCE). Headless-container flow: SDK
  `OAuthClientProvider` + a callback route (`/api/mcp/oauth/callback`) on the
  existing web server; the authorization URL is surfaced through the
  notifications/questions UI for Calvin to open on any device. `TokenStorage`
  backed by `data/mcp_tokens.json` (0600). Per the 2026 spec: validate `iss`,
  key credentials by issuer, prefer Client ID Metadata Documents over DCR.

### 5.9 Refresh and change detection

- Refresh triggers: `listChanged` notification (stateful servers — SDK
  callback), `ttlMs` expiry (2026 servers), periodic sweep
  (`mcp_refresh_interval_s`, via maintenance heartbeat), manual
  `mcp_reload_server`.
- Refresh = diff against registered set → register/unregister/update →
  `rebuild_index()`.
- **Tool pinning (Phase 2, security):** hash each tool's
  name+description+schema at first registration. On change, quarantine the
  tool (disabled) and raise a notification — this is the standard "rug pull"
  defense (a benign-looking server later swapping in a malicious description).

### 5.10 Observability and failure handling

Almost everything is inherited by design, because MCP tools are registry tools:
`tool.start`/`tool.call`/`tool.call.intercepted` SSE events, transcript
`role="tool"` messages, `ToolHealthMetrics`, durable `scout_signals`
reputation, post-mortems, Candor `tool_ok` observations, turn-ledger. Additions:

- `pernix.ext.mcp` logger namespace.
- `GET /api/mcp/servers` as the status surface; server states also summarized
  in `GET /api/health/detailed`.
- Operator alerting on server-down / quarantine via `add_notification`,
  deduped one-shot per server per incident (`_alert_tavily_once` precedent).
- A `[MCP]` line in the scout brief / degraded banner when a configured server
  is degraded — a dead integration must not render as all-clear (Candor
  DEGRADED-banner lesson).

## 6. Security model (summary)

| Threat | Control |
|---|---|
| Malicious stdio server (supply chain) | `mcp_add_server` is dangerous-gated; UI add is explicit; `mcp_stdio_enabled` off-switch; SDK scrubbed default env; version-pin guidance in docs (`npx -y pkg@1.2.3`) |
| Prompt injection via tool descriptions | length cap + `[MCP:server]` provenance prefix; tool pinning + quarantine on change (Phase 2) |
| Overbroad access | per-server `tool_allowlist` in config; per-tool disable via existing Tools UI; default `caution` safety; `denied_session_types={"canary"}` |
| Credential leakage / token passthrough | secrets only in `.env`, expanded at connect; per-server creds keyed by issuer; never forward Pernix's own tokens |
| Runaway/hung server | per-call timeout + `_kill_tool_subprocess` unaffected; per-server breaker + backoff; idle reap |
| Unattended sessions auto-approving | unchanged semantics of `_is_unattended_session()` apply — flagged as an open decision below |

## 7. Deliberately not building

Sampling, Roots, Logging (deprecated in-spec; sampling would also collide with
the **hard Calvin constraint: harness never auto-routes models**), HTTP+SSE
legacy transport (SDK fallback only if free), OAuth DCR (CIMD instead), MCP
Apps, tasks extension, acting as an MCP server.

## 8. Phasing

**Phase 1 — MVP (tools end-to-end):** dependency (`mcp` SDK, pinned),
extension package + manager + supervisor/reconnect, stdio + Streamable HTTP,
tool registration (naming/safety/limits) + `unregister()`, call bridge +
result mapping, `data/mcp_servers.json` + settings fields + `${VAR}`
expansion, agent tools, REST router, minimal MCP tab, static auth, tests,
docs. Ship dark (`mcp_enabled=False`), burn in on the box with 2–3 real
servers (one stdio, one remote), then flip default.

**Phase 2 — depth:** resources + prompts tools, refresh triad + tool
pinning/quarantine, interactive OAuth, MRTR→questions bridge, scout brief
integration polish, UI paste-import polish, per-server health history.

**Phase 3 — reach:** registry.modelcontextprotocol.io search/one-click add,
in-kernel MCP calls (code-mode: kernel helper to call MCP tools from Python —
the adaptation-plan's original instinct), `ttlMs`-aware caching, Dockerfile
adds `uv`/`uvx` (base image already ships Node/npx), optional MCP tasks
extension for long-running server jobs.

## 9. Testing

- In-repo stub server (`tests/fixtures/mcp_stub.py`, FastMCP, stdio): tools
  incl. slow/error/image/huge-output cases.
- Unit: config parse + `${VAR}` expansion + secret-literal warning; name
  sanitization/collision; safety mapping (annotations only tighten); refresh
  diff incl. unregister; result mapping matrix; surface discipline
  (`source=="mcp"` excluded from always-on and from no-scout fallback).
- Lifecycle: connect/degrade/reconnect/idle-reap/shutdown under the manager;
  no leaked children (SDK teardown + escalation).
- Integration (marker-gated): real `@modelcontextprotocol/server-filesystem`
  via npx.
- `check.sh` green incl. the 63% coverage floor; regression tests for the
  registry changes.

## 10. Open decisions — RESOLVED 2026-09-01 (Calvin delegated to
industry-standard defaults)

1. **Unattended auto-approval:** unchanged `_is_unattended_session()`
   semantics for now, with two mechanical guards shipped: canary sessions
   are denied MCP outright (`denied_session_types`), and cron charters'
   `tool_allowlist` already binds at schema + executor. Matches the
   host-client norm (headless runs use pre-allowlisted tools). Revisit only
   if a cron job starts touching dangerous MCP tools in practice.
2. **No-scout fallback:** MCP tools are excluded from the fallback surface
   entirely, EXCEPT ones this session already used successfully — proven
   need, deterministic, no arbitrary cap (`core/agent.py`,
   `tests/test_mcp_surface.py`).
3. **Default:** `mcp_enabled=True` from the start — the industry convention
   is that the feature is present and *configuring a server* is the opt-in;
   with zero servers it is fully inert. (Supersedes the draft's ship-dark
   plan; the real burn-in gate is adding the first server on the box.)
4. **Legacy SSE:** included — the SDK ships `sse_client`, so it cost one
   import behind `"type": "sse"`. Documented as deprecated.
5. **Limits:** `mcp_max_servers=10`, `mcp_max_tools_per_server=50` (excess
   skipped with a warning), both settings-tunable with API bounds.

## 11. Key references

- Spec + changelog: modelcontextprotocol.io/specification/2026-07-28
- 2026-07-28 release post: blog.modelcontextprotocol.io/posts/2026-07-28/
- Python SDK: github.com/modelcontextprotocol/python-sdk
- Registry: registry.modelcontextprotocol.io (API v0.1 freeze)
- Config convention: gofastmcp.com/integrations/mcp-json-configuration
- Security: obot.ai/resources/learning-center/mcp-security ·
  blog.mcpservers.org/posts/mcp-security-best-practices
