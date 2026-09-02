# Pernix

**A self-hosted AI agent server with persistent memory, tool execution, and a built-in web UI — runs locally with Ollama or in the cloud via OpenRouter.**

> ⚠️ **Use at your own risk. This is NOT production software.** Pernix executes shell commands and writes files on the host machine. Run it in an environment you are comfortable having an AI agent modify — a dedicated VM or container is strongly recommended. See [docs/security.md](docs/security.md) for the full security model.

---

## What is Pernix?

Pernix is a headless AI agent server you run on your own hardware. You own the data, the models, and the infrastructure. There are no accounts, no cloud subscriptions, and no usage telemetry — just a local server you control.

It ships with a full-featured web UI, a REST API with real-time streaming, persistent memory that survives across sessions, and a skill system that lets you teach the agent new capabilities without touching any code.

Pernix is designed to be a personal AI workstation: always available, aware of your history and preferences, able to search the web, write and run code, manage files, and coordinate complex multi-step work across parallel workers.

### Why this exists

This is the harness I (the developer) use personally to automate tasks I do every day — drafting LinkedIn posts, summarizing YouTube videos, pulling weather forecasts, doing research, organizing files, scheduling recurring jobs. It was also a way for me to learn how to build an agent harness from scratch and try out different ideas about scout-then-act planning, append-only context compaction, worker orchestration, and skill-based progressive disclosure.

It is **not** a polished commercial product. It is a working personal tool with rough edges that I find genuinely useful, shared as-is in case you find it useful too — or in case you want to fork it, take pieces of it, or learn from it. **Do not deploy this to production. Do not expose it to the public internet.** Run it on a dedicated machine or in a VM/container, treat it as a power tool, and have fun.

---

## Features

### LLM Support
- **Local models via Ollama** — no API key, no internet required, runs entirely on your hardware
- **Cloud models via OpenRouter** — access GPT-4o, Claude, Gemini, and hundreds of others with a single API key
- **Native OpenAI & OpenAI-compatible servers** — point `openai_base_url` at api.openai.com, vLLM, LM Studio, or a llama.cpp server; key via `OPENAI_API_KEY`
- **Automatic fallback** — if a call fails or a cloud provider hits a rate limit, Pernix retries on your backup model; it can cross providers (OpenRouter → local Ollama) or just swap models within the same provider
- **Prompt caching** — cache breakpoints for Anthropic models via OpenRouter (on by default); cache hit rates show in the session cost tooltip
- **Three model roles** — **Primary** (`llm_model`) handles your conversation and every quality-critical call (compaction, reflect, eval); **Background** (`background_model`) runs the cheap/offline tier (scout, auto-titling, memory distillation, idle work); **Backup** (`fallback_model`) catches failures from either. Plus an optional embedding model for semantic recall, and per-request overrides for workers

### Agent Capabilities
- **Persistent memory** — the agent remembers facts, decisions, and lessons across sessions using a full-text-searchable collection of markdown files
- **Semantic recall** — set an Ollama embedding model and memory search becomes hybrid BM25 + vector (with `[[wiki-links]]` between entries expanded at recall); leave it unset for pure lexical search
- **Web search** — Tavily (with AI summaries, requires API key at tavily.com)
- **Headless browser** — Playwright renders JavaScript-heavy pages, SPAs, and dynamic content
- **Workspace** — sandboxed file area the agent can read, write, and organize
- **Worker orchestration** — spawn parallel sub-agents running on different models for complex multi-part work
- **Skills system** — installable capability packs that teach the agent domain-specific procedures
- **MCP client** ([docs](docs/mcp.md)) — plug in any Model Context Protocol server, local (stdio) or remote (Streamable HTTP); its tools register as first-class Pernix tools with scout curation, the safety gate, and health metrics, managed from the Explorer → Capabilities → Servers (MCP) tab with paste-compatible Claude Code / Cursor configs
- **Cron scheduling** — run agents on a schedule for recurring tasks
- **Reflect & retry** — a quality gate verifies each response and automatically retries if the agent missed the intent
- **Session kernel** — an optional persistent per-session Python REPL (`repl` tool) whose variables survive turns, compaction, and restarts; huge tool results auto-bind as variables instead of flooding context
- **Background jobs** — detached long-running compute via `job_start` / `job_status` / `job_tail` / `job_kill`: output captured to a log, completion durable across server restarts, wall-clock caps, whole-group kill
- **Vision on demand** — `view_image` lets the agent look at images it has rendered or downloaded (vision models), instead of reasoning blind about its own plots and screenshots
- **Long-running autonomy** — deterministic gates (shell checks Reflect can't overrule), persistent goals with budgets and auto-continuations, and heartbeats steered into running work — composing into unattended multi-hour tasks ([docs](docs/internals/autonomy.md))

### Experimental Add-ons (all off by default)
- **RLM — recursive processing** ([docs](docs/internals/rlm.md)) — analyze inputs far larger than any model's context window: a root model writes code in a sandboxed REPL that holds the input as a variable, delegating chunks to budgeted sub-model calls. No new model roles — the root runs on Primary and sub-calls on Background, sharing one concurrency limiter across every recursion depth. Adds a "Recent RLM runs" panel
- **Candor — operational memory** ([docs](docs/internals/candor.md)) — the agent learns from recorded outcomes how reliable its own tools actually are; scout gets an exception-report intel brief where silence means healthy. Requires the separate `candor` package
- **Dream — introspection** ([docs](docs/internals/dream.md)) — during idle time the agent raises typed hypotheses about its own memory and behavior, then tries to falsify them against recorded outcomes; keeps a read-only daily journal in the sidebar and writes a periodic dream report
- **Canary suite** ([docs](docs/internals/canary-and-adaptive.md)) — golden tasks with deterministic gates run headlessly in isolated, tool-allowlisted workspaces, **when something they cover changes**: adaptive batches probe their covering canaries, skill edits re-test their embedded `verify:` blocks, model swaps and deploys re-baseline everything, and a small nightly heartbeat keeps history warm; full lifecycle control (create, edit, park, retire, one-off probes) from the Explorer → Self-tuning → Self-checks (Canary) tab
- **Adaptive layer** ([docs](docs/internals/canary-and-adaptive.md)) — a governed, machine-editable policy store: low-risk routing hints and prompt notes auto-apply at idle with full history and one-click rollback, high-risk edits wait for your approval, and a per-task canary tripwire flags — and can automatically roll back — any batch that makes the agent measurably worse
- **Telos — teleological layer** ([docs](docs/internals/telos.md)) — a non-convergent drive with correction machinery: turn anomalies mint questions, an idle-time cross-domain generator proposes falsifiable hypotheses (a testability gate keeps the untestable in a speculation pool), and slow loops re-rank strayed goals, detect Goodhart binding, measure whether completed goals actually discharged anything, and reconcile the agent's self-story against its append-only execution trace

### Access & UI
- **Built-in web UI** — PWA with real-time streaming, Monaco code editor, file explorer, and mobile support
- **REST API + SSE streaming** — build integrations, scripts, or custom clients using the same API the UI uses
- **Interactive API explorer** — because Pernix is built on FastAPI, a live Swagger UI is available at [http://localhost:8090/docs](http://localhost:8090/docs) (and ReDoc at `/redoc`). Browse every endpoint, see schemas, and try requests directly from the browser
- **Local mode** — binds to localhost with no auth (default)
- **Network mode** — HTTPS + Bearer token for LAN access from other devices
- **Push notifications** — browser push via service worker when the agent needs your attention
- **Voice input** — a mic button with four selectable engines (local whisper, remote whisper, model-direct audio, browser dictation), each labeled with exactly where your audio goes
- **Clipboard paste** — paste a screenshot or file anywhere in the app and it becomes a chat attachment

---

## Quick Start (Local, ~5 minutes)

### Prerequisites
- Python 3.11+
- [Ollama](https://ollama.ai) installed with at least one model pulled. Pick something current and capable — for example `ollama pull qwen3:8b` (good general-purpose), `ollama pull qwen3:32b` (if you have the VRAM), or `ollama pull qwen2.5-coder:32b` for code-heavy work. Avoid older models like `llama3.2` for serious use; agentic workloads benefit a lot from newer models with stronger tool-call and reasoning behavior.

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

# Install the Playwright browser binary (needed for browse_web)
playwright install chromium

# Set up your environment (optional API keys)
cp .env.example .env
# Edit .env to add OPENROUTER_API_KEY and/or TAVILY_API_KEY if you have them

# Start the server
python run.py
```

Open **http://localhost:8090** in your browser.

### First Configuration

1. Click the gear icon → **Settings**
2. Set **`llm_model`** — the name of your Ollama model (e.g. `qwen3:32b`) or an OpenRouter model ID (e.g. `anthropic/claude-sonnet-4.6`, `x-ai/grok-4.1-fast`)
3. Optionally set **`background_model`** — a fast, cheap model for scout planning, auto-titling, and idle work. Something like `qwen3:8b` locally, or `anthropic/claude-haiku-4.5` on OpenRouter, works well. Leave it empty and everything runs on `llm_model`
4. Click **Save**

Start a new session and say hello. Then, while it's working, open [`/docs`](http://localhost:8090/docs) in another tab to see the live API.

---

## Deployment Recommendation

> **Run Pernix on a machine that is NOT your production system, or inside an isolated VM or container.** The agent can execute shell commands, read and write files, and make outbound network requests. This is intentional — it is what makes Pernix useful — but it means you should treat it like any other local-execution tool and give it a contained environment.

Good options:
- A dedicated Linux VM (VirtualBox, Proxmox)
- Any Python 3.11+ container
- A separate physical machine or SBC

See [docs/installation.md](docs/installation.md#isolated-deployment-recommended) for details.

---

## LLM Providers

### Ollama (Local, Default)

[Ollama](https://ollama.ai) runs models entirely on your machine. No API key required, no internet needed for inference. Pull models with `ollama pull <model-name>`.

Configure in Settings:
- `llm_base_url`: `http://localhost:11434/v1` (default — change if Ollama runs elsewhere)
- `llm_model`: the model name as it appears in `ollama list`

### OpenRouter (Cloud, Optional)

[OpenRouter](https://openrouter.ai) provides access to dozens of frontier models (Claude, GPT, Gemini, Grok, etc.) through a single API. For agent workloads, prefer current frontier models — they are markedly better at tool use, reasoning, and following instructions than older or smaller models.

1. Create an account at [openrouter.ai](https://openrouter.ai) and get an API key
2. Add `OPENROUTER_API_KEY=sk-or-v1-...` to your `.env`
3. Set `OPENROUTER_MODELS` in `.env` to the model IDs you want available (comma-separated), or leave it empty to show all models on your account. Reasonable starting set: `anthropic/claude-sonnet-4.6,anthropic/claude-haiku-4.5,x-ai/grok-4.1-fast`
4. In Settings, set `llm_model` to an OpenRouter model ID (e.g. `anthropic/claude-sonnet-4.6`)

### OpenAI & OpenAI-Compatible Servers (Cloud or Self-Hosted, Optional)

A native provider for the OpenAI API — and, because the base URL is overridable, for **any OpenAI-compatible server**: vLLM, LM Studio, or a llama.cpp server.

1. Set `OPENAI_API_KEY=sk-...` in `.env` (the key is env-only by design — it never lands in `data/settings.json`). Self-hosted servers that don't check keys still work; just set the URL
2. In Settings, point `openai_base_url` at the server — `https://api.openai.com/v1` (default), or e.g. `http://your-box:8000/v1` for vLLM
3. List the models this server provides in `openai_models` (or the `OPENAI_MODELS` env var). This matters: bare names like `gpt-4o` otherwise route to Ollama — the whitelist wins over that heuristic and keeps the model dropdown curated
4. Set `llm_model` (or `background_model` / `fallback_model`) to one of those names

Full routing rules and walkthroughs: [docs/deployment/llm-providers.md](docs/deployment/llm-providers.md).

### Using Both Simultaneously

Pernix can use Ollama, OpenRouter, and the OpenAI provider at the same time. You can use a cloud model as your Primary (`llm_model`) and a local Ollama model as your Backup (`fallback_model`) — if OpenRouter is rate-limited, Pernix switches to Ollama automatically. Backup doesn't have to cross providers, though: a second Ollama model is a perfectly good backup for an Ollama primary, and failover still works.

When both providers have a model with the same name, Ollama wins (local, free, lower latency). To use the OpenRouter version of a model, add it explicitly to `OPENROUTER_MODELS` in your `.env`.

---

## The Web UI

Pernix ships with a full progressive web app (PWA) at the root URL. Key panels:

| Panel | What it does |
|---|---|
| **Session sidebar** | Create, switch between, and manage sessions — full-text search, and a legend to filter by type (chat, cron, worker, Dream) |
| **Chat** | Real-time conversation with streamed responses and tool call visibility |
| **Settings** | Configure all server settings without restarting |
| **File explorer** | Browse, upload, and open files in the workspace |
| **Jobs** | Cron jobs, live snooze activity, and recent RLM runs |
| **Timeline** | Step-by-step view of the state machine and every tool call in a turn — including in-flight tool calls as they run |
| **Notifications bell** | Alert when the agent is waiting for your input |

The UI works on mobile when accessed via network mode. It can also be installed as a PWA for a native-app-like experience.

---

## Skills, SOUL.md, and RULES.md

Pernix's behavior beyond raw LLM responses is shaped by three things:

**Skills** (`data/skills/`) are capability packs you install. Each skill teaches the agent a specific procedure — how to call a particular API, process a specific file type, or follow a domain procedure. Skills are plain markdown with YAML frontmatter; the agent discovers them automatically and loads their instructions only when relevant.

**SOUL.md** (`data/agent/SOUL.md`) defines who Pernix is — its personality, communication style, and core traits. Edit it freely to match how you want the agent to talk to you.

**RULES.md** (`data/agent/RULES.md`) defines how Pernix should act — which tools to prefer, how to handle failures, when to delegate to workers. Edit it to add project-specific constraints or procedures.

See [docs/authoring/writing-skills.md](docs/authoring/writing-skills.md) for the full guide including how to write your own skills.

---

## Network & Security

By default, Pernix binds only to `127.0.0.1` with no authentication. It is not reachable from other devices.

To access from your phone or another machine on your LAN:

1. Enable `network_enabled` in Settings
2. Restart the server
3. Run `python run.py --qr` — it prints a QR code link you can scan on your phone
4. For a smooth mobile experience (no certificate warnings), set up trusted TLS with [mkcert](docs/deployment/mkcert.md)

Full details on authentication, TLS certificates, SSRF protections, and safe configuration: **[docs/security.md](docs/security.md)**

---

## Make It Your Own

Pernix is built to be tinkered with. Some things to try once you have it running:

- **Edit `data/agent/SOUL.md`** to change how the agent talks to you. Want it more terse? More verbose? More opinionated about coding style? Just write that in.
- **Edit `data/agent/RULES.md`** to add operational guardrails specific to your workflow — for example, "always run tests before committing," or "never edit files in `/etc`."
- **Write a skill** for any repetitive task — calling an internal API, formatting output a particular way, walking through a multi-step procedure. See [docs/authoring/writing-skills.md](docs/authoring/writing-skills.md) for the format. Skills are just markdown files; the agent discovers them automatically on the next turn.
- **Browse the API at [`/docs`](http://localhost:8090/docs)** — every endpoint is documented and try-it-able right from the browser. This is the fastest way to learn the system.
- **Understand how it works.** Start with [docs/architecture.md](docs/architecture.md) — a guided walkthrough of Sessions, Scout, the agent loop, Reflect, Snooze, and the 10-state session state machine, written from concepts down to implementation.
- **Read the code.** It's a single Python codebase that fits in your head. The session state machine, agent loop, scout phase, compaction, and worker orchestration all live in `core/` and `sessions/`. Read [docs/internals/state-machine.md](docs/internals/state-machine.md) for the formal spec with file:line citations once you've got the concepts.
- **Schedule recurring agents** for jobs you do daily — morning news brief, weekly summary of activity logs, watchdog scripts. Pernix has a built-in cron scheduler.
- **Connect from your phone** — enable network mode, set up TLS with mkcert, scan the QR code, and you have a personal AI assistant in your pocket.

### A note on safety

You will probably break things while tinkering. That's fine and even encouraged on a dedicated machine — `python run.py --rebuild` wipes runtime state and gets you back to a clean slate without touching your settings or skills. But please:

- **Don't run Pernix on your daily-driver workstation** unless you understand the risk surface
- **Don't expose the network mode endpoint to the public internet** — it's designed for trusted LANs
- **Don't use `--dangerous`** (the startup flag that bypasses tool approval) until you've watched the agent run for a while and trust its judgment. In normal mode the agent must ask your permission for each dangerous action it takes — that's the intended behavior.

This is a power tool. Treat it like one.

---

## Documentation

The complete documentation lives in **[docs/](docs/)**. Start at **[docs/README.md](docs/README.md)** — it's organized by what you're trying to do.

Most-visited entry points:

| Doc | When to read |
|---|---|
| [docs/quickstart.md](docs/quickstart.md) | Five-minute path from zero to first chat |
| [docs/installation.md](docs/installation.md) | Full setup, environment variables, optional dependencies, isolated deployment |
| [docs/configuration.md](docs/configuration.md) | Every setting explained with defaults |
| [docs/security.md](docs/security.md) | Security model, network mode, TLS, auth tokens |
| [docs/architecture.md](docs/architecture.md) | How Pernix works: sessions, scout, agent loop, reflect, snooze |
| [docs/api.md](docs/api.md) | REST API + SSE event reference (live Swagger at `/docs` when running) |
| [docs/authoring/writing-skills.md](docs/authoring/writing-skills.md) | Skills system, SKILL.md schema, writing your own |
| [docs/guides/recipes.md](docs/guides/recipes.md) | Runnable end-to-end examples |
| [docs/faq.md](docs/faq.md) | Common gotchas |
| [LICENSE](LICENSE) | MIT license |

---

## Credits

Pernix is built by Calvin ([@calvincs](https://github.com/calvincs)) with **Claude** (Anthropic) as pair programmer — and with **Pernix itself** in the loop: the reference deployment runs the field campaigns, surfaces its own failures, and validates the fixes that shape each release. The v3.0.0 release is the work of all three.

---

## License

MIT — see [LICENSE](LICENSE).
