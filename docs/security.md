# Security & Safe Usage

## Use At Your Own Risk

Pernix is provided under the MIT license with **no warranty of any kind**. You are solely responsible for how you deploy and use it, what data you expose to it, and what actions you permit it to take on your systems. Read this document before enabling network access or running Pernix on any machine that holds data you care about.

---

## What Pernix Can Do (Risk Surface)

Understanding what an AI agent can actually do is the first step to deploying it safely:

- **Execute shell commands** on the host machine via the `bash` tool
- **Read and write files** — writes anywhere within the configured workspace directory plus `/tmp`; reads additionally cover the skills directory, the tool-output spill tree (`data/.tool_output`), and kernel snapshot payloads
- **Make outbound HTTP requests** — web searches, page fetches, and calls to LLM APIs
- **Store persistent data** in SQLite databases and markdown memory files on disk
- **Spawn sub-agents** (workers) that can do all of the above in parallel
- **Execute model-written Python** in a child process, when the RLM add-on is enabled (`rlm_enabled`, off by default) — the child gets a scrubbed environment (no API keys), resource limits, and brokered/budgeted LLM access, but it is a same-UID subprocess without namespaces: defense-in-depth, not a security boundary, the same stance as the `bash` tool. `rlm_process` registers at the `caution` safety tier (no per-call prompt, like `bash`). Details: [internals/rlm.md](internals/rlm.md)

None of this is hidden or unusual — it is the entire point of an agentic system. The implication is that Pernix should run in an environment **you are comfortable having an AI modify**.

---

## Recommended Deployment Posture

- **Run on a dedicated, non-production machine** — a spare box, a VM, or a container
- **Do not expose to the public internet** without a hardened reverse proxy in front of it
- **Start with `auto_approve_dangerous = false`** (the default) — the agent will ask before running destructive commands
- **Review `shell_allowlist`** — restrict which shell commands are permitted if you want tighter control. The allowlist is enforced only when `shell_security_mode = "strict"`; under the default `"permissive"` mode only the denylist scan applies (note: the RLM child REPL is a Python interpreter, not the bash tool, so the shell allowlist does not apply to it — the container/VM is its containment layer)
- **Back up `data/` periodically** — it contains your sessions, memory, and workspace

---

## Local Mode vs Network Mode

### Local Mode (default)

When `network_enabled = false` (the default), Pernix binds only to `127.0.0.1`. It is not reachable from any other machine on your network. There is no authentication — anyone with access to localhost can use it, meaning only you (and other processes on the same machine) can reach it. Traffic is plain HTTP.

This is the recommended mode for single-user personal use.

### Network Mode

When `network_enabled = true`, Pernix binds to `0.0.0.0` (all network interfaces), making it reachable from other devices on your LAN. Several security measures automatically engage:

| What changes | Detail |
|---|---|
| **HTTPS enforced** | TLS certificate required; HTTP is not available |
| **Bearer token required** | All API requests must include a valid token |
| **LLM base URLs locked** | Cannot be changed remotely (prevents SSRF via provider redirect) |

The shell security settings (`shell_security_mode`, `shell_allowlist`, `shell_env_*`) and `auto_approve_dangerous` are locked against the settings API **unconditionally** — in local mode too, not just when the network engages. See [Locked Settings](#locked-settings) below.

**How to enable network mode:**

1. Open Settings in the UI (or `POST /api/settings` with `{"network_enabled": true}`)
2. Restart the server — `POST /api/admin/restart` (localhost-only) or `Ctrl+C` and `python run.py`
3. Pernix will now bind on all interfaces with HTTPS

> Changing `network_enabled` always requires a full restart. The CORS middleware and bind address are set at startup.

---

## TLS / SSL Certificates

### Self-Signed (default)

On first start in network mode, Pernix calls `openssl` to generate a self-signed RSA-2048 certificate valid for 365 days. It is stored in `data/certs/` with restrictive permissions (directory `0700`, key file `0600`).

Browsers will display an "untrusted certificate" warning. You can click through it on a desktop browser. On mobile, or if you need Web Push notifications to work, self-signed certificates are not sufficient — use mkcert instead.

### Custom Certificates (mkcert recommended for LAN)

For a seamless experience on mobile and for push notification support, use [mkcert](https://github.com/FiloSottile/mkcert) to generate a certificate signed by a locally-trusted CA.

See **[deployment/mkcert.md](deployment/mkcert.md)** for step-by-step instructions including Android and iOS trust installation.

To use custom certificates:

1. Set `ssl_mode = "custom"` in Settings
2. Set `ssl_cert_path` and `ssl_key_path` to the full paths of your PEM files
3. Restart the server

---

## Bearer Token Authentication

### How It Works

In network mode, every request (except `GET /`, `/static/*`, `/favicon.ico`, `/api/health`, and the service worker endpoint) must include a valid bearer token. The token is a randomly-generated 32-byte base64url string stored in `data/settings.json`.

The server accepts the token exactly three ways; a fourth, client-side form exists for onboarding links:

| Method | When to use |
|---|---|
| `Authorization: Bearer <token>` header | API clients, scripts |
| `pernix_auth` cookie | Browser sessions (set automatically on first login) |
| `?token=<token>` query parameter | Legacy links; ad-hoc `curl` |
| `#token=<token>` URL fragment | QR-code login links, one-time URL sharing — **not a server auth path**: the fragment never leaves the browser; the page reads it from `location.hash` and authenticates with the header/cookie |

The token comparison is constant-time, so a wrong token reveals nothing about the right one through response timing.

> **Onboarding links use the URL fragment, not the query string.** A browser never transmits the part after `#`, so `/#token=<token>` cannot reach an access log, a reverse-proxy log, or a `Referer` header. The query form used to, and wrote a working credential into `docker compose logs` on every scan.
>
> `?token=` is still accepted, because links handed out before this change are still in circulation and it is convenient for `curl`. Pernix redacts it from its own access log (`_TokenRedactFilter` in `run.py`, applied to `uvicorn.access` before anything else sees the record), but that is a backstop, not a guarantee: a reverse proxy in front of Pernix keeps its own logs, and browser history still records the full URL. Prefer the fragment, and treat a token you have shared as a query string as disclosed.

### Localhost Bypasses Auth (by default)

Requests originating from loopback are not challenged, even in network mode — the check accepts `127.0.0.1`, `::1`, the literal `localhost`, and IPv6-mapped `::ffff:127.*` forms. This prevents you from locking yourself out and keeps localhost-only admin endpoints reachable.

**If you put Pernix behind a reverse proxy, turn this off.** A proxy terminating TLS on the same host connects to Pernix over loopback, so *every* proxied request — including ones from the internet — arrives as `127.0.0.1` and skips the token entirely. Set:

```json
{ "trust_local_requests": false }
```

in `data/settings.json` (or from Settings in the UI). It takes effect immediately; no restart needed. With it off, the proxy must forward a valid `Authorization: Bearer` header, and you should reach admin endpoints through the proxy with the token rather than relying on the bypass.

### Token-from-URL Login Flow (Mobile / LAN Access)

1. From localhost, call `GET /api/settings/access-qr` — returns a QR code image
2. The QR encodes `https://<LAN-IP>:<port>/#token=<token>` — the token is in the fragment, so the server never receives it
3. Scan it on your phone — the token is read from `location.hash`, stored in `localStorage`, and stripped from the address bar
4. Subsequent requests from that browser use the `Authorization` header automatically
5. Use `--qr` on startup as a shortcut: `python run.py --qr` prints the URL and QR code to the terminal

### Rotating the Token

```bash
# Any authenticated client may rotate — valid token, or trusted loopback:
curl -X POST http://localhost:8090/api/settings/auth-token/regenerate
```

After regeneration, all existing sessions (other browsers, API clients) must re-authenticate with the new token.

---

## Locked Settings

Most locked settings are locked **unconditionally**: `POST /api/settings` rejects them in local mode too, regardless of where the request comes from — the check is not client-address-aware. Only the LLM base URLs are network-mode-conditional. To change an unconditionally locked setting, edit `data/settings.json` directly with the server stopped (except `auto_approve_dangerous`, which cannot be set that way either — see below):

| Setting | When locked | Why it's locked |
|---|---|---|
| `llm_base_url`, `openrouter_base_url` | Network mode only | Prevents an attacker from redirecting LLM traffic to an internal service (SSRF) |
| `shell_security_mode`, `shell_allowlist`, `shell_env_*` | Always | Prevents privilege escalation via shell settings |
| `auto_approve_dangerous` | Always | Runtime-only: stripped on save and skipped on load, so neither the API nor `data/settings.json` can set it — the `--dangerous` startup flag is the only activation path |
| `auth_token` | Always | Must use the dedicated `/regenerate` endpoint; cannot be set directly |

---

## SSRF Protections

Server-Side Request Forgery (SSRF) is a class of attack where a server is tricked into making requests to internal resources. Pernix includes:

- **`http_get` IP filtering**: Requests to RFC-1918 private IP ranges (10.x.x.x, 172.16-31.x.x, 192.168.x.x) are always blocked. Loopback is additionally blocked in network mode, with one carve-out: the harness's own listen port is always reachable over loopback — the agent owns this server and needs to test workspace files it just wrote. Other loopback ports may be co-tenant services and stay blocked. (In localhost mode the agent may fetch loopback freely, e.g. its own workspace file server)
- **Locked LLM base URLs**: Cannot be redirected to internal addresses remotely
- **Playwright SSRF intercept**: `browse_web` also filters internal address requests at the page-load level

---

## Admin Endpoints (Localhost-Only)

These endpoints are only accessible from loopback (`127.0.0.1`, `::1`, the literal `localhost`, or `::ffff:127.*` mapped forms), regardless of auth token:

| Endpoint | Purpose |
|---|---|
| `GET /api/health/detailed` | Full system diagnostics (providers, DB, tools) |
| `POST /api/admin/snooze-cycle` | Run one snooze cycle now (skips only the cadence gate) |
| `POST /api/admin/restart` | Graceful server restart |

The token endpoints (`GET /api/settings/auth-token`, `POST /api/settings/auth-token/regenerate`) are deliberately **not** localhost-gated — they are ordinary token-gated endpoints. The localhost check made them unreachable in the deployment that needs them most (under `docker compose`, bridge-network sources are treated as remote), and it never stood between an attacker and anything: a caller must already hold a valid token to reach them, and that same token authorizes the agent's shell tool, which can read `settings.json` outright.

---

## Dangerous Tool Gate

Tools classified as `dangerous` require explicit per-invocation user confirmation before executing, unless the server was started with the `--dangerous` flag. The default set is:

| Tool | Why |
|---|---|
| `search_web`, `browse_web` | Outbound traffic and untrusted page content entering the context |
| `create_tool`, `update_tool` | Writes model-authored Python into the server's own source tree and imports it **into the server process** — see [Toolmaker](#toolmaker-model-authored-code-in-the-server-process) below |
| `create_skill`, `add_skill_script` | Authors instructions the agent will later load and follow, and scripts `load_skill` then tells it to run under `bash` |
| `add_gate` | Registers shell that re-runs unattended at every turn end for the life of the session |

You can promote or demote any tool via `POST /api/tools/set-safety` or the Explorer → Tools panel.

### What the gate is, and what it is not

**The gate surfaces intent. It is not a containment boundary.**

`bash` and `repl` stay at the `caution` level, which does not prompt. That is a deliberate choice, not an oversight: they are the product's core utility, and prompting on every call would make the agent unusable for ordinary work. The consequence has to be stated plainly — **every dangerous-gated action has an ungated equivalent through `bash`.** `create_skill` prompts; `bash` writing the same SKILL.md does not. `create_tool` prompts; `bash` writing the same file and waiting for a restart does not.

So the gate's real job is to make a consequential action *visible and deliberate* at the moment the agent takes it — it stops a careless tool call, not a determined one. **The VM or container Pernix runs in is the actual boundary.** This is the same posture [internals/rlm.md](internals/rlm.md) states for the RLM child sandbox, and the same one the shell denylist below is labeled with.

If you need a real boundary, run Pernix in a VM or container you are willing to lose, and give the process only the credentials it actually needs.

### Server-side approval state

Approval is stored on the session by `approve_dangerous_tool()` and matched against the call's arguments. **No tool argument can authorize its own call.** If you write a custom tool that takes an `approved`-style flag, that flag is a UI affordance and nothing more — the model supplies it, so the model can set it. Route real authorization through the `dangerous` safety level instead.

### Normal mode (default — `auto_approve_dangerous = false`)

The agent must go through a two-step handshake for every distinct dangerous action:

1. **`ask_user()`** — the agent describes the exact action it intends to perform. You see the specific command, URL, or file path — not just the tool name. The session suspends until you respond.
2. **`approve_dangerous_tool(tool_name, scope)`** — after you confirm, the agent registers approval for that specific action. Approvals are consumed after one use; a different call to the same tool requires a new confirmation.

Approved scopes are persisted to `data/tool_approvals.json`. If you've confirmed an action before (e.g. "run ps aux to list processes"), the `ask_user` step is skipped automatically on future occurrences. View and clear remembered approvals in **Settings → Security**.

Workers spawned from interactive sessions face the same gate — the agent cannot escalate privilege by spawning sub-agents. The exception is unattended runs: cron-scheduled and canary sessions (and workers spawned from them) skip the gate, because no user is present to answer `ask_user` prompts.

### Run Dangerously mode (`--dangerous` startup flag)

Start the server with `python run.py --dangerous` to bypass the approval gate entirely. All dangerous tools execute immediately without confirmation in every session, including workers and cron jobs.

**This flag is the only activation path.** It cannot be set via `settings.json`, the API, or any environment variable while the server is running — this prevents a rogue process or prompt injection from silently elevating privileges mid-session. The current mode is shown read-only in **Settings → Security** and as a persistent red banner in the **Explorer → Tools** panel.

**Keep `auto_approve_dangerous = false` (do not use `--dangerous`)** unless you fully trust the current session context and plan to disable it immediately after.

---

## Shell Environment Controls

Three settings control what environment variables the shell tool sees:

| `shell_env_mode` | Behavior |
|---|---|
| `allowlist` (default) | Only vars in `shell_env_allowlist` are passed to the shell |
| `denylist` | All env vars passed except those in `shell_env_denylist` |
| `passthrough` | Shell inherits the server process's full environment |

The default is `allowlist`. The server process holds every provider API key you have configured, and `passthrough` handed a copy of that environment to every `bash` child and to anything the child spawned — so a single `env` in a shell command, or any dependency that logs its environment, disclosed the full key set. `allowlist` builds a minimal environment (`PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, plus audio/display vars); `PATH`, `HOME`, and `VIRTUAL_ENV` are then set explicitly to the workspace venv, so the sandbox works exactly as before. This matches what the RLM child process already did.

> This is a hardening default, not a secret boundary. `.env` is still readable from disk by anything running as the same user, including `bash`. It removes the *accidental* disclosure path, not the deliberate one.

If a tool you rely on needs a variable that is not on the allowlist (a proxy setting, a cloud SDK credential), add it to `shell_env_allowlist` rather than reverting to `passthrough`.

**Existing installs are unaffected**: `shell_env_mode` is persisted in `data/settings.json`, so a value already written there keeps winning. Delete the key (or set it to `"allowlist"`) to pick up the new default.

---

## Toolmaker: Model-Authored Code in the Server Process

The `create_tool` / `update_tool` tools are the highest-authority surface in the system, and the documentation was previously silent about them. What actually happens:

1. The model supplies a Python source string.
2. It is written to `core/tools/builtin/custom_<name>.py` — **inside Pernix's own source tree**, via a raw `Path.write_text` that does not go through the workspace path-safety layer at all.
3. It is immediately `importlib.import_module`'d and `register(reg)` is called **in the server process** — with the full server environment (every API key), no resource limits, no separate process group.
4. `core/tools/builtin/__init__.py` re-imports every `custom_*.py` on **every boot**, so the code persists across restarts.

Module-level statements in that file execute at import time. There is no sandbox on this path.

**`PROHIBITED_PATTERNS` is a typo-guard, not a control.** It is an 11-entry substring scan over the submitted source. It is trivially bypassed — `os.popen` for `os.system`, `subprocess.run` for `subprocess.Popen`, double quotes for the single-quoted `open('/etc` patterns — and it is moot regardless, because it looks for *call sites* while module-level code needs none of them. Treat it as a lint that catches obvious mistakes, and do not reason about it as a security property.

What has been tightened:

- Both `create_tool` and `update_tool` are now `dangerous`, so they require the `ask_user` + `approve_dangerous_tool` handshake. Gating only `create_tool` would have left a one-call detour: create a benign tool once, then replace its body.
- Tool names are validated as `[a-z][a-z0-9_]{0,39}` **before** being interpolated into any path, in `create_tool`, `update_tool`, and `restore_tool_packages`.

What remains true, and is the reason to run Pernix in a container: **an approved `create_tool` call is arbitrary code execution in the server process, and it persists.** Relocating custom tools out of the source tree and into a sandboxed loader is the real fix and has not been done. Review `list_custom_tools` output and the contents of `core/tools/builtin/custom_*.py` if you ever approve one.

---

## MCP Servers: Third-Party Tool Providers

MCP servers are third-party software the agent calls as tools ([mcp.md](mcp.md)). The threat model and its controls:

- **Supply chain (stdio).** A stdio server entry is a command Pernix executes — arbitrary local code. `mcp_add_server` is `dangerous` (always confirmed), the Explorer add path is an explicit human action, and `mcp_stdio_enabled=false` turns stdio off entirely (remote-only mode). Pin versions in stdio commands (`npx -y pkg@1.2.3`): an unpinned `npx -y` runs whatever was published most recently.
- **Prompt injection via tool metadata.** Server-supplied names, descriptions, and schemas end up in the model's prompt. Descriptions are length-capped (`mcp_max_description_chars`) and provenance-prefixed `[MCP:<server>]` so the model always sees where a tool came from. Treat a server you add as able to talk to your agent.
- **Overbroad access.** Default safety is `caution`, a server-sent `destructiveHint` escalates to `dangerous` (never the reverse), per-server `tool_allowlist` narrows what registers, individual tools can be disabled in the Tools tab, and canary sessions are denied MCP tools outright.
- **Credentials.** Secrets live only in `.env` and are referenced as `${VAR}` in `mcp_servers.json`; literal-looking tokens in config are rejected. Pernix never forwards its own auth token or provider keys to an MCP server.
- **Network scope.** MCP URLs are operator-configured and not subject to the fetch tools' SSRF blocklist — that is deliberate (private, docker-network-local servers are a supported pattern), and it means adding a server URL is trusted input. Only a confirmed `mcp_add_server` call or an authenticated REST/UI action can add one.

---

## Deterministic Gates

`add_gate` registers a shell command that runs automatically before Reflect at **every turn end**, for the life of the session. That persistence — one approval buying repeated unattended execution the user never sees again — is what separates it from a one-shot `bash` call, and it is why `add_gate` is `dangerous`.

Gate commands are held to the same policy as `bash`:

- The command is checked against the shell denylist at **registration** time (so the agent gets the rejection while it can still fix the command) and again at **execution** time (so rows that predate the check, or that reached the table by another route, are still checked).
- The denylist scan applies in every `shell_security_mode`, including `strict` — `strict`'s first-word allowlist is tuned for interactive commands and would reject ordinary gates like `pytest -q`.
- `cwd` must resolve inside the workspace. It previously did not, so `add_gate(cwd="/")` relocated the entire policy surface.
- Gate children get `setsid` and the same `RLIMIT_AS` / `RLIMIT_FSIZE` limits as `bash`, and a timeout kills the whole process group rather than leaving orphans behind every turn.

A gate that policy refuses to run is recorded as a **failure**, not skipped — an unrunnable gate has verified nothing. It is logged and refused individually, so one bad legacy row does not break the turn-end sweep for the session's other gates.

---

## Skill Authoring

`create_skill` and `add_skill_script` are `dangerous`. A skill is an instruction package the agent will later load and follow, and `add_skill_script` writes an executable file that `load_skill` then advertises to the agent as `bash <skill>/scripts/<file>` — write-then-run, previously ungated at every step.

These two tools no longer take an `approved` argument. It was a **model-supplied boolean**: the first call posted an `ask_user` question and returned, and the model was told to call again with `approved=true` — but nothing correlated that argument with an actual user response, so the model could simply set it on the first call. Authorization now goes through the executor's server-side gate, which keeps approval state on the session where no argument can reach it.

The remaining skillmaker tools (`update_skill`, `add_skill_reference`, `remove_skill_script`, `remove_skill_reference`) still take `approved`. They edit markdown inside an existing skill, so the prompt is a speed bump rather than a control — but it is an honor-system speed bump, and should be read that way.

---

## Data Privacy

All data lives locally on your machine:

- **Session history**: `data/sessions.db` (SQLite)
- **Memory files**: `data/memories/*.md` + `data/memories/_index.db`
- **Workspace files**: `data/workspace/`
- **Settings**: `data/settings.json`
- **API keys**: `.env` file (never stored in settings.json or logs)

Data leaves your machine **only** when the agent explicitly:
- Calls a web search or `http_get` / `browse_web` tool
- Sends a request to an LLM API (Ollama local, or OpenRouter cloud)
- Triggers a configured webhook (`notify_webhook_url`)

---

## Recommendations Summary

- Run Pernix in a VM or container, not on your daily workstation
- Keep `network_enabled = false` unless you need LAN access
- If enabling network mode: use mkcert, rotate tokens, set explicit `cors_origins`
- Never expose Pernix directly to the internet; put it behind a reverse proxy if needed
- Review `shell_allowlist` and tighten it for your use case (enforced only when `shell_security_mode = "strict"`)
- Keep `auto_approve_dangerous = false`
- Back up `data/` regularly — it contains all your sessions, memory, and workspace
