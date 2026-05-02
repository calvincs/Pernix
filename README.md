# Pernix

**A self-hosted AI agent server with persistent memory, tool execution, and a built-in web UI — runs locally with Ollama or in the cloud via OpenRouter.**

> ⚠️ **Use at your own risk.** Pernix executes shell commands and writes files on the host machine. Run it in an environment you are comfortable having an AI agent modify — a dedicated VM or container is strongly recommended. See [docs/SECURITY.md](docs/SECURITY.md) for the full security model.

---

## What is Pernix?

Pernix is a headless AI agent server you run on your own hardware. You own the data, the models, and the infrastructure. There are no accounts, no cloud subscriptions, and no usage telemetry — just a local server you control.

It ships with a full-featured web UI, a REST API with real-time streaming, persistent memory that survives across sessions, and a skill system that lets you teach the agent new capabilities without touching any code.

Pernix is designed to be a personal AI workstation: always available, aware of your history and preferences, able to search the web, write and run code, manage files, and coordinate complex multi-step work across parallel workers.

---

## Features

### LLM Support
- **Local models via Ollama** — no API key, no internet required, runs entirely on your hardware
- **Cloud models via OpenRouter** — access GPT-4o, Claude, Gemini, and hundreds of others with a single API key
- **Automatic fallback** — if OpenRouter hits a rate limit, Pernix seamlessly falls back to your local Ollama model
- **Multiple model roles** — primary model for conversations, scout model for fast planning, background model for auto-titling and memory tasks, fallback model for resilience

### Agent Capabilities
- **Persistent memory** — the agent remembers facts, decisions, and lessons across sessions using a full-text-searchable collection of markdown files
- **Web search** — Tavily (with AI summaries) or DuckDuckGo as a no-key fallback
- **Headless browser** — Playwright renders JavaScript-heavy pages, SPAs, and dynamic content
- **Workspace** — sandboxed file area the agent can read, write, and organize
- **Worker orchestration** — spawn parallel sub-agents running on different models for complex multi-part work
- **Skills system** — installable capability packs that teach the agent domain-specific workflows
- **Cron scheduling** — run agents on a schedule for recurring tasks
- **Reflect & retry** — a quality gate verifies each response and automatically retries if the agent missed the intent

### Access & UI
- **Built-in web UI** — PWA with real-time streaming, Monaco code editor, file explorer, and mobile support
- **REST API + SSE streaming** — build integrations, scripts, or custom clients using the same API the UI uses
- **Local mode** — binds to localhost with no auth (default)
- **Network mode** — HTTPS + Bearer token for LAN access from other devices
- **Push notifications** — browser push via service worker when the agent needs your attention

---

## Quick Start (Local, ~5 minutes)

### Prerequisites
- Python 3.11+
- [Ollama](https://ollama.ai) installed with at least one model pulled (e.g. `ollama pull llama3.2`)

### Setup

```bash
# Clone and enter the project
git clone <repository-url>
cd pernix

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Set up your environment (optional API keys)
cp .env.example .env
# Edit .env to add OPENROUTER_API_KEY and/or TAVILY_API_KEY if you have them

# Start the server
python run.py
```

Open **http://localhost:8090** in your browser.

### First Configuration

1. Click the gear icon → **Settings**
2. Set **`llm_model`** — the name of your Ollama model (e.g. `llama3.2`) or an OpenRouter model ID
3. Set **`scout_model`** — a fast model for planning (can be the same as `llm_model` to start)
4. Click **Save**

Start a new session and say hello.

---

## Deployment Recommendation

> **Run Pernix on a machine that is NOT your production system, or inside an isolated VM or container.** The agent can execute shell commands, read and write files, and make outbound network requests. This is intentional — it is what makes Pernix useful — but it means you should treat it like any other local-execution tool and give it a contained environment.

Good options:
- A dedicated Linux VM (VirtualBox, Proxmox)
- Any Python 3.11+ container
- A separate physical machine or SBC

See [INSTALLATION.md](INSTALLATION.md#isolated-deployment-recommended) for details.

---

## LLM Providers

### Ollama (Local, Default)

[Ollama](https://ollama.ai) runs models entirely on your machine. No API key required, no internet needed for inference. Pull models with `ollama pull <model-name>`.

Configure in Settings:
- `llm_base_url`: `http://localhost:11434/v1` (default — change if Ollama runs elsewhere)
- `llm_model`: the model name as it appears in `ollama list`

### OpenRouter (Cloud, Optional)

[OpenRouter](https://openrouter.ai) provides access to dozens of frontier models (Claude, GPT-4, Gemini, etc.) through a single API.

1. Create an account at [openrouter.ai](https://openrouter.ai) and get an API key
2. Add `OPENROUTER_API_KEY=sk-or-v1-...` to your `.env`
3. Set `OPENROUTER_MODELS` in `.env` to the model IDs you want available (comma-separated), or leave it empty to show all models on your account
4. In Settings, set `llm_model` to an OpenRouter model ID (e.g. `anthropic/claude-sonnet-4-5`)

### Using Both Simultaneously

Pernix can use Ollama and OpenRouter at the same time. You can use a cloud model as your primary (`llm_model`) and a local Ollama model as your `fallback_model` — if OpenRouter is rate-limited, Pernix switches to Ollama automatically.

When both providers have a model with the same name, Ollama wins (local, free, lower latency). To use the OpenRouter version of a model, add it explicitly to `OPENROUTER_MODELS` in your `.env`.

---

## The Web UI

Pernix ships with a full progressive web app (PWA) at the root URL. Key panels:

| Panel | What it does |
|---|---|
| **Session sidebar** | Create, switch between, and manage sessions |
| **Chat** | Real-time conversation with streamed responses and tool call visibility |
| **Settings** | Configure all server settings without restarting |
| **File explorer** | Browse, upload, and open files in the workspace |
| **Timeline** | Step-by-step view of the state machine and every tool call in a turn |
| **Notifications bell** | Alert when the agent is waiting for your input |

The UI works on mobile when accessed via network mode. It can also be installed as a PWA for a native-app-like experience.

---

## Skills, SOUL.md, and RULES.md

Pernix's behavior beyond raw LLM responses is shaped by three things:

**Skills** (`data/skills/`) are capability packs you install. Each skill teaches the agent a specific workflow — how to call a particular API, process a specific file type, or follow a domain procedure. Skills are plain markdown with YAML frontmatter; the agent discovers them automatically and loads their instructions only when relevant.

**SOUL.md** (`data/agent/SOUL.md`) defines who Pernix is — its personality, communication style, and core traits. Edit it freely to match how you want the agent to talk to you.

**RULES.md** (`data/agent/RULES.md`) defines how Pernix should act — which tools to prefer, how to handle failures, when to delegate to workers. Edit it to add project-specific constraints or workflows.

See [docs/SKILLS.md](docs/SKILLS.md) for the full guide including how to write your own skills.

---

## Network & Security

By default, Pernix binds only to `127.0.0.1` with no authentication. It is not reachable from other devices.

To access from your phone or another machine on your LAN:

1. Enable `network_enabled` in Settings
2. Restart the server
3. Run `python run.py --qr` — it prints a QR code link you can scan on your phone
4. For a smooth mobile experience (no certificate warnings), set up trusted TLS with [mkcert](docs/MKCERT_SETUP.md)

Full details on authentication, TLS certificates, SSRF protections, and safe configuration: **[docs/SECURITY.md](docs/SECURITY.md)**

---

## Documentation

| File | Contents |
|---|---|
| [INSTALLATION.md](INSTALLATION.md) | Full setup, environment variables, optional dependencies, startup flags, isolated deployment |
| [CONFIGURATION.md](CONFIGURATION.md) | Every setting explained with defaults and examples |
| [docs/SECURITY.md](docs/SECURITY.md) | Security model, network mode, TLS, auth tokens, recommendations |
| [docs/SKILLS.md](docs/SKILLS.md) | Skills system, SOUL.md, RULES.md, web capabilities, writing a skill |
| [docs/API.md](docs/API.md) | Complete REST API reference and SSE event catalog |
| [docs/MKCERT_SETUP.md](docs/MKCERT_SETUP.md) | Trusted TLS certificates for LAN access |
| [docs/workflow.md](docs/workflow.md) | Deep architectural reference: state machine, agent loop, compaction, workers |
| [LICENSE](LICENSE) | MIT license |

---

## License

MIT — see [LICENSE](LICENSE).
