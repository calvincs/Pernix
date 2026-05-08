# Security & Safe Usage

## Use At Your Own Risk

Pernix is provided under the MIT license with **no warranty of any kind**. You are solely responsible for how you deploy and use it, what data you expose to it, and what actions you permit it to take on your systems. Read this document before enabling network access or running Pernix on any machine that holds data you care about.

---

## What Pernix Can Do (Risk Surface)

Understanding what an AI agent can actually do is the first step to deploying it safely:

- **Execute shell commands** on the host machine via the `bash` tool
- **Read and write files** anywhere within the configured workspace directory
- **Make outbound HTTP requests** — web searches, page fetches, and calls to LLM APIs
- **Store persistent data** in SQLite databases and markdown memory files on disk
- **Spawn sub-agents** (workers) that can do all of the above in parallel

None of this is hidden or unusual — it is the entire point of an agentic system. The implication is that Pernix should run in an environment **you are comfortable having an AI modify**.

---

## Recommended Deployment Posture

- **Run on a dedicated, non-production machine** — a spare box, a VM, or a container
- **Do not expose to the public internet** without a hardened reverse proxy in front of it
- **Start with `auto_approve_dangerous = false`** (the default) — the agent will ask before running destructive commands
- **Review `shell_allowlist`** — restrict which shell commands are permitted if you want tighter control
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
| **Shell security settings locked** | `shell_security_mode`, `shell_allowlist`, `shell_env_*` cannot be changed remotely |
| **`auto_approve_dangerous` locked** | Cannot be toggled remotely |

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

In network mode, every request (except `GET /`, `/static/*`, `/api/health`, and the service worker endpoint) must include a valid bearer token. The token is a randomly-generated 32-byte base64url string stored in `data/settings.json`.

The token can be passed three ways:

| Method | When to use |
|---|---|
| `Authorization: Bearer <token>` header | API clients, scripts |
| `pernix_auth` cookie | Browser sessions (set automatically on first login) |
| `?token=<token>` query parameter | QR-code login links, one-time URL sharing |

### Localhost Always Bypasses Auth

Requests originating from `127.0.0.1` or `::1` are never challenged, even in network mode. This prevents you from locking yourself out.

### Token-from-URL Login Flow (Mobile / LAN Access)

1. From localhost, call `GET /api/settings/access-qr` — returns a QR code image
2. The QR encodes `https://<LAN-IP>:<port>/?token=<token>`
3. Scan it on your phone — the token is extracted from the URL and stored in `localStorage`
4. Subsequent requests from that browser use the `Authorization` header automatically
5. Use `--qr` on startup as a shortcut: `python run.py --qr` prints the URL and QR code to the terminal

### Rotating the Token

```bash
# From localhost only:
curl -X POST http://localhost:8090/api/settings/auth-token/regenerate
```

After regeneration, all existing sessions (other browsers, API clients) must re-authenticate with the new token.

---

## Locked Settings in Network Mode

The following settings cannot be changed via the Settings UI or API while `network_enabled = true`. They can only be modified locally (by editing `data/settings.json` directly, or from localhost):

| Setting | Why it's locked |
|---|---|
| `llm_base_url`, `openrouter_base_url` | Prevents an attacker from redirecting LLM traffic to an internal service (SSRF) |
| `shell_security_mode`, `shell_allowlist`, `shell_env_*` | Prevents privilege escalation via shell settings |
| `auto_approve_dangerous` | Prevents remote disabling of the dangerous-tool confirmation gate |
| `auth_token` | Must use the dedicated `/regenerate` endpoint; cannot be set directly |

---

## SSRF Protections

Server-Side Request Forgery (SSRF) is a class of attack where a server is tricked into making requests to internal resources. Pernix includes:

- **`http_get` IP filtering**: In network mode, requests to RFC-1918 private IP ranges (10.x.x.x, 172.16-31.x.x, 192.168.x.x) and loopback addresses are blocked
- **Locked LLM base URLs**: Cannot be redirected to internal addresses remotely
- **Playwright SSRF intercept**: `browse_web` also filters internal address requests at the page-load level

---

## Admin Endpoints (Localhost-Only)

These endpoints are only accessible from `127.0.0.1` or `::1`, regardless of auth token:

| Endpoint | Purpose |
|---|---|
| `GET /api/health/detailed` | Full system diagnostics (providers, DB, tools) |
| `GET /api/settings/auth-token` | View the current token value |
| `POST /api/settings/auth-token/regenerate` | Rotate the token |
| `POST /api/admin/restart` | Graceful server restart |

---

## Dangerous Tool Gate

Tools classified as `dangerous` (primarily `bash`, file writes, unrestricted network access) require explicit per-invocation user confirmation before executing, unless the server was started with the `--dangerous` flag.

### Normal mode (default — `auto_approve_dangerous = false`)

The agent must go through a two-step handshake for every distinct dangerous action:

1. **`ask_user()`** — the agent describes the exact action it intends to perform. You see the specific command, URL, or file path — not just the tool name. The session suspends until you respond.
2. **`approve_dangerous_tool(tool_name, scope)`** — after you confirm, the agent registers approval for that specific action. Approvals are consumed after one use; a different call to the same tool requires a new confirmation.

Approved scopes are persisted to `data/tool_approvals.json`. If you've confirmed an action before (e.g. "run ps aux to list processes"), the `ask_user` step is skipped automatically on future occurrences. View and clear remembered approvals in **Settings → Security**.

Workers and cron jobs face the same gate — they cannot escalate privilege by spawning sub-agents.

### Run Dangerously mode (`--dangerous` startup flag)

Start the server with `python run.py --dangerous` to bypass the approval gate entirely. All dangerous tools execute immediately without confirmation in every session, including workers and cron jobs.

**This flag is the only activation path.** It cannot be set via `settings.json`, the API, or any environment variable while the server is running — this prevents a rogue process or prompt injection from silently elevating privileges mid-session. The current mode is shown read-only in **Settings → Security** and as a persistent red banner in the **Explorer → Tools** panel.

**Keep `auto_approve_dangerous = false` (do not use `--dangerous`)** unless you fully trust the current session context and plan to disable it immediately after.

---

## Shell Environment Controls

Three settings control what environment variables the shell tool sees:

| `shell_env_mode` | Behavior |
|---|---|
| `passthrough` (default) | Shell inherits the server process's full environment |
| `denylist` | All env vars passed except those in `shell_env_denylist` |
| `allowlist` | Only vars in `shell_env_allowlist` are passed to the shell |

Use `allowlist` mode for the most controlled environment.

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
- Review `shell_allowlist` and tighten it for your use case
- Keep `auto_approve_dangerous = false`
- Back up `data/` regularly — it contains all your sessions, memory, and workspace
