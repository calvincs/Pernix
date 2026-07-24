# Network mode

By default, Pernix binds to `127.0.0.1` with no authentication — usable only from the same machine. **Network mode** changes this: Pernix binds to `0.0.0.0`, requires HTTPS, and demands a Bearer token on every request. Use it when you want to reach Pernix from your phone or another device on your LAN.

> **Network mode is for trusted LANs.** Do not expose Pernix directly to the public internet. If you need remote access, run a VPN (Tailscale, WireGuard) and access over that.

For the broader security model, see [../security.md](../security.md).

---

## Enabling network mode

1. Open the UI's Settings panel.
2. Set `network_enabled = true`. (Or `POST /api/settings` with `{"network_enabled": true}`.)
3. Restart the server. Either `POST /api/admin/restart` (localhost-only) or stop and start the process: `python run.py`.

On restart, Pernix:

- Binds to `0.0.0.0` (all interfaces).
- Generates a self-signed TLS certificate in `data/certs/` if one isn't already there. Or uses your custom cert if `ssl_mode = custom`.
- Auto-generates a Bearer token (32-byte URL-safe base64) and writes it to `data/settings.json` if `auth_token` is empty.
- Locks the SSRF-sensitive settings (`llm_base_url`, `openrouter_base_url`, shell sandboxing, dangerous-tool bypass) so they can't be changed by a remote API client.

---

## What changes when network mode is on

| Component | Localhost mode | Network mode |
|---|---|---|
| Bind address | `127.0.0.1` | `0.0.0.0` |
| Protocol | HTTP | HTTPS |
| Auth | None | Bearer token required |
| `llm_base_url`, `openrouter_base_url` | Editable from UI | Locked (must edit `data/settings.json` from localhost) |
| `shell_security_mode`, `shell_allowlist`, `shell_env_*` | Editable | Locked (same) |
| `auto_approve_dangerous` | Editable | Locked (same) |
| `cors_origins` | Editable, takes effect immediately | Editable, requires restart to take effect |
| `auth_token` | Optional | Required (auto-generated if empty) |
| Admin endpoints | Available | Restricted to localhost connections |

---

## Why a restart?

The bind address, TLS context, and CORS middleware are configured at process start. Pernix doesn't tear down active connections to reconfigure them; instead it requires an explicit restart. `POST /api/admin/restart` is the supported path — it uses `os.execv` to re-run the original command line.

If `network_enabled`, `ssl_mode`, `ssl_cert_path`, `ssl_key_path`, or `cors_origins` change, the UI marks them as restart-pending until you restart.

---

## TLS / SSL certificates

Two options for the cert:

### Self-signed (default)

On first start in network mode, Pernix calls `openssl` to generate an RSA-2048 self-signed certificate valid for 365 days. It's stored at `data/certs/cert.pem` and `data/certs/key.pem` with restrictive perms (directory `0700`, key file `0600`).

Browsers will show "untrusted certificate" warnings — click through to proceed. **Limitation:** mobile browsers and Web Push require a trusted certificate; self-signed won't work for those.

### Custom (mkcert recommended for LAN)

For a smooth mobile experience and to enable Web Push notifications, use [mkcert](https://github.com/FiloSottile/mkcert). It generates a certificate signed by a locally-trusted root CA you install on each device.

Steps:

1. Install mkcert and generate certs — see [mkcert.md](mkcert.md).
2. In Pernix Settings: set `ssl_mode = "custom"`, `ssl_cert_path = /path/to/cert.pem`, `ssl_key_path = /path/to/key.pem`.
3. Restart.

---

## Bearer token authentication

Network mode protects every endpoint except `/`, `/api/health`, `/static/*`, `/favicon.ico`, and the service worker. Everything else requires a valid token.

Three ways to send the token:

| Method | When |
|---|---|
| `Authorization: Bearer <token>` header | API clients, scripts, mobile apps |
| `pernix_auth` cookie | Browser sessions (set automatically after token-from-URL login) |
| `?token=<token>` query parameter | QR-code login links, one-time URL sharing |

Localhost connections (`127.0.0.1`, `::1`) bypass authentication by default, even in network mode. This prevents you from locking yourself out and lets `POST /api/admin/restart`, `POST /api/settings/auth-token/regenerate`, and similar admin endpoints stay accessible.

**Behind a reverse proxy, set `trust_local_requests: false`.** A proxy terminating TLS on the same host reaches Pernix over loopback, so every proxied request — wherever it actually came from — looks like `127.0.0.1` and skips the token. With the setting off, the proxy must forward `Authorization: Bearer <token>` like any other client. The change is read per-request, so it applies immediately without a restart.

Token comparison is constant-time. Note that `?token=` lands in access logs and browser history — rotate after using it for onboarding.

---

## QR-code login flow

Easiest way to log in from your phone:

1. **Generate the URL.** From localhost, hit `GET /api/settings/access-qr` (or run `python run.py --qr` on startup). You get a URL like `https://192.168.1.50:8090/?token=<32-byte-token>`.
2. **Scan it on your phone.** The QR encodes the URL; any camera app reads it.
3. **The browser opens, extracts `?token=<...>` from the URL, and stores it in `localStorage`.** Subsequent requests use the `Authorization` header automatically.

After login, the URL bar shows just the host — the token is no longer in the URL, so you can share screenshots safely.

---

## Rotating the token

```bash
curl -X POST http://localhost:8090/api/settings/auth-token/regenerate
```

This is **localhost-only**. After regeneration, every existing client (browser tabs, scripts, mobile apps) must re-authenticate with the new token. Useful if you suspect a token has leaked.

---

## SSRF protections

In network mode, several SSRF mitigations engage automatically:

- **`http_get` blocks RFC-1918 private IP ranges** (10.x, 172.16-31.x, 192.168.x) and loopback addresses. The agent can't fetch internal services on the LAN through Pernix.
- **`browse_web` blocks the same ranges** at the page-load level via Playwright's request interception.
- **`llm_base_url` and `openrouter_base_url` are locked** so a remote API client can't redirect LLM traffic to an attacker-controlled endpoint.

There is no per-URL exemption list — private-range blocking applies to all agent-originated fetches (in localhost mode loopback is allowed, and the server's own port is always reachable so the agent can preview workspace files). If the agent must talk to an internal LAN service, wrap that access in a custom tool or skill that you control, rather than weakening the fetch tools.

---

## CORS

`cors_origins` is a list of allowed origins for cross-origin requests. By default it's empty.

- **Empty in localhost mode:** only `http://localhost:<port>` and `http://127.0.0.1:<port>` are allowed (with credentials).
- **Empty in network mode:** Pernix permits the wildcard `*` but disables credential cookies (the browser spec forbids `*` with credentials) — so cross-origin browser requests with cookies won't work.
- **Set explicitly:** lists the origins your client uses (e.g., your iPhone's user-script or your custom integration host). Cookies work normally.

`cors_origins` requires restart to take effect.

---

## Public-internet exposure (don't)

If you really need Pernix accessible outside your LAN, the safe pattern is:

1. **Run on the LAN.** Don't change anything about Pernix's network mode.
2. **Set up a VPN** like [Tailscale](https://tailscale.com) or WireGuard. Your phone joins the VPN.
3. **From outside, connect to the VPN, then access Pernix at its LAN address.**

Don't:

- Port-forward Pernix to the public internet.
- Put Pernix behind a reverse proxy that doesn't add real authentication beyond the Bearer token.
- Disable HTTPS thinking "it's only for me."

These are all reachable-by-anyone deployments and the agent's surface area (shell, browser, file write) is too broad for that.

---

## Troubleshooting

- **"Certificate not trusted"** on phone — switch to mkcert. Self-signed won't work on mobile.
- **"401 Unauthorized" everywhere** — the token in your client doesn't match `auth_token` in `data/settings.json`. Either rotate the token or update the client.
- **"Network mode enabled but server still listens on 127.0.0.1 only"** — you didn't restart. The bind address is set at process start; nothing else takes effect.
- **Push notifications don't work** — Web Push requires HTTPS with a trusted cert. Self-signed isn't enough; use mkcert.
- **Port 8090 already in use** — another Pernix is running, or another service is squatting. Either kill it or `python run.py --port 8091`.
