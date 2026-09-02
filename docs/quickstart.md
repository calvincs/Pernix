# Quickstart

The five-minute path from clean machine to first chat. For full setup options (network mode, container deployment, optional dependencies), see [installation.md](installation.md).

---

## Prerequisites

- **Python 3.11+** — `python3 --version` to check
- **git** — to clone the repo
- **A model.** Either of:
  - **Ollama** running locally with at least one model pulled (free, offline). Install from [ollama.ai](https://ollama.ai), then `ollama pull qwen3:8b` for a quick start, or `qwen3:32b` if you have the VRAM.
  - **OpenRouter API key** (cloud, paid). Get one at [openrouter.ai](https://openrouter.ai).

You can use both at the same time, but you only need one of them to start.

---

## 1. Clone and install

```bash
git clone <repository-url>
cd pernix
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```


---

## 2. Add API keys (optional)

```bash
cp .env.example .env
```

Open `.env` and add any of these you have. **All three are optional** — Pernix starts fine without any of them, just with reduced capability.

```
TAVILY_API_KEY=tvly-...                           # enables search_web
OPENROUTER_API_KEY=sk-or-v1-...                   # enables OpenRouter cloud models
OPENROUTER_MODELS=anthropic/claude-sonnet-4.6     # comma-separated whitelist
```

---

## 3. Start the server

```bash
python run.py
```

You should see:

```
Pernix → http://127.0.0.1:8090
```

Open **<http://localhost:8090>** in your browser.

---

## 4. Pick your models

In the UI, click **Settings** in the status bar at the bottom of the window — the cog next to **Explorer**. The two settings you must set:

| Setting | What to put |
|---|---|
| `llm_model` | The primary model. Either an Ollama model you've pulled (e.g. `qwen3:32b`) or an OpenRouter model ID (e.g. `anthropic/claude-sonnet-4.6`). |
| `background_model` | A fast, smaller model for the offline tier (scout planning, titles, memory work). Something like `qwen3:8b` locally, or `anthropic/claude-haiku-4.5` on OpenRouter. Empty uses `llm_model`. |

Click **Save**. You're done with setup.

---

## 5. Send your first message

Click **New session** in the sidebar. Type something:

> *"What time is it, and who am I according to your memory? If we haven't met, just say so."*

Watch the timeline panel — you'll see scout plan the turn, then the main agent run. The reply streams in real time.

---

## What now

A few good directions to explore:

- **Skills.** Teach the agent reusable procedures by dropping skill packages into `data/skills/` — or ask the agent to write one for you. Once installed, invoke one by asking in plain language — *"use the linkedin-post-formatter skill to write me a post about ..."* — or just describe the task and scout surfaces a matching skill automatically. See [guides/using-skills.md](guides/using-skills.md).
- **Workspace.** Anything the agent writes lands in `data/workspace/`. The file panel in the UI shows the tree. See [guides/workspace-and-files.md](guides/workspace-and-files.md).
- **Memory.** Tell the agent something it should remember about you. Across new sessions, ask if it remembers. See [guides/memory-and-recall.md](guides/memory-and-recall.md).
- **Schedule a recurring agent.** Ask it to "run every weekday at 8 AM and email me a news brief" — it'll set up a cron job. See [guides/scheduling-cron.md](guides/scheduling-cron.md).
- **Spawn a worker.** For multi-part research, ask the agent to split work across parallel sub-agents. See [guides/workers.md](guides/workers.md).
- **Recipes.** Read [guides/recipes.md](guides/recipes.md) for end-to-end examples that stitch several features together.

---

## Common first-run snags

- **`llm_model` empty after Save** — check that the model name matches exactly. For Ollama, run `ollama list` and copy the name verbatim. For OpenRouter, use the full slug (`anthropic/claude-sonnet-4.6`, not `claude-sonnet-4.6`).
- **Browser shows "Settings won't save"** — usually a stale tab. Hard refresh (Ctrl+Shift+R) and try again.
- **Wanting to start over** — `python run.py --rebuild` wipes sessions, memory, and workspace but preserves your settings, API keys, skills, and certs. Type `yes` when prompted.
- **`search_web` returning a setup hint instead of results** — set `TAVILY_API_KEY` in `.env` (it's gated on the key as of recent versions; there's no longer a free fallback).

For more, see [faq.md](faq.md).
