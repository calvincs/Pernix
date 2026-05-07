# Installation Guide

This guide covers everything you need to get Pernix running, from system requirements through your first successful session.

---

## Requirements

| Requirement | Notes |
|---|---|
| **Python 3.11 or newer** | Required. Python 3.12 recommended. |
| **git** | For cloning the repository. |
| **openssl** | Only needed in network mode for TLS certificate generation. Usually pre-installed on Linux/macOS. |
| **Ollama** | Strongly recommended for local model inference. Free, runs entirely offline. Install from [ollama.ai](https://ollama.ai). |

**Optional API keys** (the system works without them, with reduced capability):

| Key | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | Access to cloud models (GPT-4o, Claude, Gemini, etc.) via [openrouter.ai](https://openrouter.ai) |
| `TAVILY_API_KEY` | Higher-quality web search via [tavily.com](https://tavily.com). Falls back to DuckDuckGo if absent. |

---

## Step-by-Step Setup

### 1. Clone the Repository

```bash
git clone <repository-url>
cd pernix
```

### 2. Create a Virtual Environment

Pernix requires its own isolated Python environment. **Do not install packages into your system Python.**

```bash
python3 -m venv .venv
```

### 3. Activate the Virtual Environment

```bash
# Linux / macOS
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

You should see `(.venv)` in your shell prompt. All `pip install` commands below assume the venv is active.

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

### 5. Configure Environment Variables

Copy the example file and edit it:

```bash
cp .env.example .env
```

Open `.env` in a text editor. The available variables are:

```env
# Tavily web search API (primary search provider)
TAVILY_API_KEY=tvly-...

# OpenRouter cloud LLM provider
OPENROUTER_API_KEY=sk-or-v1-...

# OpenRouter models to make available (comma-separated)
# Only these models will appear in the model list
OPENROUTER_MODELS=anthropic/claude-sonnet-4.6,anthropic/claude-haiku-4.5,x-ai/grok-4.1-fast
```

All three variables are optional — Pernix starts without any of them. Without an OpenRouter key you can still use Ollama models. Without a Tavily key, web search falls back to DuckDuckGo automatically.

### 6. Start Pernix

```bash
python run.py
```

You should see output like:

```
Pernix starting on http://127.0.0.1:8090
```

### 7. Open the UI

Navigate to **http://localhost:8090** in your browser. The web UI will load.

### 8. Configure Your First Model

Before you can have a conversation, you need to tell Pernix which model to use:

1. Click the gear icon in the sidebar → **Settings**
2. Set **`llm_model`** to a model you have available:
   - For Ollama: the name of a model you've already pulled (e.g. `qwen3:32b`, `qwen2.5-coder:32b`, or `qwen3:8b` if you have less VRAM). Older models like Llama 3.2 work but tend to be weaker at tool use and multi-step reasoning — agentic workloads do much better on current Qwen 3.x or comparable frontier-tier local models
   - For OpenRouter: the full model ID (e.g. `anthropic/claude-sonnet-4.6`, `x-ai/grok-4.1-fast`, or any current frontier model from your account)
3. Set **`scout_model`** — this is used for the planning phase. A small, fast model works well (e.g. `qwen3:8b` locally, or `anthropic/claude-haiku-4.5` on OpenRouter). You can use the same model as `llm_model` while getting started.
4. Click **Save**

> **Verify:** Navigate to `http://localhost:8090/api/health` — it should return `{"status": "ok", ...}`.

> **Tip:** Open `http://localhost:8090/docs` in your browser. Pernix is built on FastAPI, so a live Swagger UI is auto-generated for every endpoint. ReDoc is also available at `/redoc`. These are the easiest way to explore what the API can do without reading [docs/API.md](docs/API.md) end to end.

---

## Optional Dependencies

None of these are required. Pernix detects which optional packages are available and adjusts its behavior accordingly.

### Playwright — JavaScript Browser Rendering

Enables the `browse_web` tool, which renders JavaScript-heavy pages using a headless Chromium browser.

`playwright` and `trafilatura` are already included in `requirements.txt` and installed by the standard `pip install -r requirements.txt` step. You only need to download the browser binary once:

```bash
playwright install chromium
```

Then enable in Settings: `browser_enabled = true`.

Without the browser binary, `browse_web` is unavailable. The `search_web` and `http_get` tools still work.

### tiktoken — Accurate Token Counting

Without tiktoken, Pernix estimates token counts using a character-based heuristic. The estimate is conservative and works fine in practice, but tiktoken gives exact counts for OpenAI-compatible tokenizers.

```bash
pip install tiktoken
```

### Tavily — Enhanced Web Search

Better search quality than DuckDuckGo, with AI-generated summaries alongside results. Requires an account at [tavily.com](https://tavily.com) (free tier available).

```bash
pip install tavily-python
# Then add TAVILY_API_KEY to .env
```

### DuckDuckGo Search — No-Key Fallback

Pernix uses DuckDuckGo automatically when Tavily is not configured. If you want to explicitly install the search library:

```bash
pip install ddgs
```

### QR Code — LAN Access URL

Prints a QR code to the terminal when you run `python run.py --qr`, useful for scanning with a phone to get the LAN access URL.

```bash
pip install qrcode
```

---

## Startup Flags

```
python run.py                       Normal start (http://localhost:8090)
python run.py --rebuild             Wipe all state, start clean (see below)
python run.py --dangerous           Bypass dangerous-tool approval gate (see below)
python run.py --qr                  Print LAN access URL + QR code (network mode)
python run.py --port 9000           Override port (default: 8090)
python run.py --host 0.0.0.0        Override bind address
```

### `--rebuild` — Start Clean

Wipes all sessions, memory, and workspace data. The server will prompt you to type `yes` to confirm before doing anything.

**What gets deleted:**
- `data/sessions.db` — all session history and messages
- `data/memories/` — all memory files and the search index
- `data/workspace/` — all workspace files
- `data/logs/` — log files
- Internal registry and model preference caches

**What is preserved:**
- `data/settings.json` — your configuration
- `.env` — your API keys
- `data/skills/` — your installed skills
- `data/certs/` — your TLS certificates
- `data/agent/` — agent identity files (SOUL.md, RULES.md, AGENTS.md). The birthdate header in SOUL.md is reset; user-authored content below it is preserved. To restore defaults, run `git checkout data/agent/`.

Use `--rebuild` when you want a completely fresh start or are troubleshooting state corruption.

### `--dangerous` — Bypass Tool Approval Gate

Starts the server with the dangerous-tool approval gate disabled. Every tool call executes immediately — no `ask_user` confirmation, no `approve_dangerous_tool()` step required.

**This flag is the only way to enable this mode.** It cannot be toggled via Settings, the API, or any environment variable while the server is running. This prevents a rogue process or prompt injection from silently elevating its own privileges mid-session. The mode is visible in **Settings → Security** (read-only status badge) and in a red banner at the top of the **Explorer → Tools** panel.

**The flag survives server restarts** — if `POST /api/admin/restart` is used, the new process inherits `--dangerous` from the original `sys.argv`.

Use only when you fully trust the current session context. Disable by restarting without the flag.

---

## First-Run Checklist

Before your first conversation, verify:

- [ ] `llm_model` is set in Settings
- [ ] `scout_model` is set (can be the same model to start)
- [ ] `http://localhost:8090/api/health` returns `{"status": "ok"}`
- [ ] If using Ollama: `ollama list` shows the model you configured
- [ ] If using OpenRouter: `OPENROUTER_API_KEY` is in `.env`

---

## Isolated Deployment (Recommended)

> **Why isolate?** Pernix can run shell commands and write files on the host machine. This is intentional — it's what makes it useful as an agent — but it means you should run it in an environment you are comfortable having an AI modify.

### Dedicated VM

The cleanest option for most users. A fresh Linux VM (Ubuntu, Debian, Fedora) lets you:
- Give Pernix a defined set of resources
- Snapshot and restore easily
- Expose only the ports you choose

VirtualBox, Proxmox, and VMware all work well. Allocate at least 2GB RAM and whatever disk space your models and workspace need (Ollama models are typically 4–30GB).

### Container

Any Python 3.11+ container image works. Pernix does not include an official Dockerfile, but the standard setup runs cleanly in something like `python:3.12-slim`:

```
# Rough outline — adapt to your environment
FROM python:3.12-slim
RUN apt-get update && apt-get install -y git openssl
WORKDIR /app
COPY . .
RUN python -m venv .venv && .venv/bin/pip install -r requirements.txt
RUN .venv/bin/playwright install chromium --with-deps
EXPOSE 8090
CMD [".venv/bin/python", "run.py"]
```

Mount `data/` as a volume so your sessions and memory persist across container restarts.

### Separate Physical Machine

A small SBC (Raspberry Pi 5 in arm64 mode, mini-PC, or old laptop) dedicated to running Pernix + Ollama works well and keeps your main workstation clean.

---

## Upgrading

```bash
# Pull the latest changes
git pull

# Update dependencies (picks up any new packages)
source .venv/bin/activate
pip install -r requirements.txt

# Restart — database migrations run automatically
python run.py
```

No manual database migration steps are needed. Schema migrations run at startup automatically.

---

## LAN / Mobile Access (Network Mode)

If you want to use Pernix from a phone, tablet, or another machine on your network:

1. **Enable network mode** in Settings: `network_enabled = true`
2. **Restart** the server
3. **Scan the QR code**: run `python run.py --qr` and scan with your phone, or visit the URL printed in the terminal

For the best experience on mobile (no browser certificate warnings, push notifications support), set up trusted TLS certificates with mkcert. See **[docs/MKCERT_SETUP.md](docs/MKCERT_SETUP.md)** for instructions.

For the full security model (auth tokens, locked settings, SSRF protections), see **[docs/SECURITY.md](docs/SECURITY.md)**.
