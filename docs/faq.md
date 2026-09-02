# FAQ

Quick answers to the questions that come up most often. For deeper material, follow the per-topic links.

---

## Setup and start

### What's the difference between `--rebuild` and a fresh install?

`python run.py --rebuild` wipes runtime state — sessions, memory, workspace, logs, registry caches, cron jobs — but keeps everything you've configured: `data/settings.json`, `.env`, `data/skills/`, `data/certs/`, `data/agent/`. It's the "I want to clean slate without losing my setup" button. A fresh install means cloning the repo into a new directory; you'd lose all of those too.

You can think of `--rebuild` as a soft factory reset.

### Do I have to use Ollama? Or OpenRouter? Or both?

Either. Or both at once.

- **Just Ollama:** local-only, no API key needed, runs on your hardware.
- **Just OpenRouter:** cloud frontier models (Claude, GPT, Gemini, Grok), single API key.
- **Both:** use a cloud model as `llm_model` and an Ollama model as `fallback_model` — if OpenRouter is rate-limited or down, Pernix transparently switches.

When both providers offer a model with the same name, **Ollama wins** (local, free, lower latency). To force OpenRouter for a name collision, list the model explicitly in `OPENROUTER_MODELS`.

### Why isn't `search_web` working?

`search_web` requires a [Tavily](https://tavily.com) API key. Without it, the tool returns a setup hint instead of search results. Add `TAVILY_API_KEY=tvly-...` to your `.env` (free tier is fine for personal use).

There used to be a DuckDuckGo fallback; it was removed because it produced unreliable results. The Tavily key is now the gate.

### Why does the agent ask me to confirm things like web searches or deleting a skill?

That's the **dangerous-tool gate**. A handful of tools (`search_web`, `browse_web`, `create_skill`) need explicit per-call confirmation. The agent first calls `ask_user` describing exactly what it intends to do; you confirm; it then calls `approve_dangerous_tool(tool_name, scope)` and proceeds.

Approvals are remembered in `data/tool_approvals.json` keyed on the scope description, so identical actions in future sessions don't re-prompt. View and clear remembered approvals in **Settings → Tools & safety → Remembered Approvals**.

### How do I bypass the dangerous-tool gate for unattended cron jobs?

Two ways:

1. **Cron sessions skip the gate automatically.** Sessions started by the scheduler (and workers spawned from them) are recognized as unattended — there's no user present to answer `ask_user`, so the gate is bypassed for them. Manual sessions still require approval.

2. **`python run.py --dangerous`** disables the gate entirely for the running process. Every tool call runs immediately. Only use this when you trust the entire session context. The flag is the **only** way to enable this mode — it can't be set via Settings, the API, or env vars while the server is running.

---

## Behavior and turns

### Why does Pernix run "scout" before the agent? Doesn't that just slow things down?

Scout is the planning phase, run on a smaller, faster model in a fresh context window. It picks which tools and skills the main agent should be aware of and pre-fetches relevant memory. The cost is small (one fast LLM call); the savings are large (the main model doesn't have to sift through every tool definition and every memory entry every turn).

If you really want to skip it, set `scout_enabled = false` in Settings — but expect higher token usage and worse tool selection.

### My session is stuck in PROCESSING. What do I do?

A few possibilities:

- **The agent is genuinely working.** Long tool calls (browser, big file edits) can take a while. Tool calls stream into the transcript as they run; the state badge in the status bar opens the **State timeline** for the full step-by-step view.
- **A boot-time crash didn't clean up.** On startup, the manager sweeps `PROCESSING` and `AWAITING_WORKERS` sessions stuck from a prior crash and resets them to `IDLE_READY`. If you started cleanly, this should already have happened.
- **The reaper will free it.** A 5-minute reaper tick force-resets `PROCESSING` sessions that have no background tasks holding them. Just wait.
- **Last resort:** press **Stop** in the UI — the send button becomes it while a turn is running — or restart the server (`POST /api/admin/restart` from localhost, or Ctrl+C and `python run.py`).

For the full reaper rules and timeouts, see [internals/state-machine.md §0.7](internals/state-machine.md#07-reaper-rules-10-state).

### What happens if the agent goes off the rails?

Two safety nets:

- **`max_tool_rounds`** (default 50) caps the number of tool-call cycles per turn. The loop ends with a `round_ceiling` outcome instead of looping forever. It is a backstop against a runaway loop, not a spending limit — goal token/time budgets and the stuck detector are what actually bound cost. (It defaulted to 10 before the 2026-08 refactor, which was low enough that ordinary long tasks tripped it and had to be papered over by goal continuations.)
- **Reflect** runs after every turn. It compares the original request against what the agent produced, and can trigger up to 2 retries with corrective lessons (`reflect_max_retries = 2`). After that it escalates by surfacing the issue to you.

If the agent is doing something destructive, press **Stop** — the send button becomes it while a turn is running — which triggers a `CANCELLING → IDLE_READY` transition and tears down the loop within ~30 seconds.

### Why doesn't my Settings change take effect?

A handful of settings need a server restart — anything that changes the bind address, the TLS surface, or the CORS middleware:

- `network_enabled`
- `ssl_mode`, `ssl_cert_path`, `ssl_key_path`
- `cors_origins`

Changing these in the UI marks them as restart-pending. Use `POST /api/admin/restart` (localhost-only) or stop and start the process.

Everything else applies immediately on Save.

---

## Memory and history

### Where does my data live?

Everything is local:

| What | Where |
|---|---|
| Sessions, messages, tool calls | `data/sessions.db` (SQLite) |
| Long-term memories | `data/memories/*.md` (plain markdown) |
| Memory search index | `data/memories/_index.db` (FTS5) |
| Workspace files | `data/workspace/` |
| RLM run traces (when enabled) | `data/workspace/rlm/<run_id>/` — auto-purged after 30 days |
| Dream reports (when enabled) | `data/workspace/dreams/` |
| Candor evidence ledger (when enabled) | `data/candor/` |
| Settings | `data/settings.json` |
| API keys | `.env` |
| Skills | `data/skills/` |
| Agent identity | `data/agent/SOUL.md`, `RULES.md`, `SESSIONS.md` |
| TLS certs | `data/certs/` |

You can read, edit, or delete the markdown memory files directly with a text editor.

### How do I reset memory?

Delete the relevant `data/memories/*.md` files, or wipe everything with `python run.py --rebuild` (which preserves settings and API keys). The memory index regenerates on next start.

### What's the "Dream" session in my sidebar?

If you've enabled Dream (Settings → Autonomy & idle work → Dream (Introspection)), Pernix spends idle time examining its own memory and operational history — raising hypotheses about itself and testing them against recorded outcomes. Each day of dreaming keeps a journal as a read-only session in the sidebar (purple dot, titled like "Dream Jul 31"). You can't chat in it — Pernix writes it while dreaming — and it's excluded from search and memory distillation. Hide the whole category with the sidebar legend if you'd rather not see it, or turn `dream_enabled` off to stop dreaming entirely. Details: [internals/dream.md](internals/dream.md).

### Where do the agent's file outputs land?

`data/workspace/`. Subdirectories are organized by project. The Explorer's Files → Workspace tab shows the tree; the REST API at `GET /api/workspace` lists it; `GET /workspace/{path}` downloads any file.

The agent can only write inside `data/workspace/` — protected paths like `.env`, `SOUL.md`, and `data/sessions.db` are blocked.

---

## Network and security

### Can I access Pernix from my phone?

Yes — turn on network mode. In Settings, set `network_enabled = true` and restart. Then `python run.py --qr` prints a QR code containing the LAN URL plus your auth token; scan it with your phone and you're logged in.

For mobile without browser certificate warnings (and for Web Push notifications to work), set up trusted TLS via [deployment/mkcert.md](deployment/mkcert.md) instead of the default self-signed cert.

### Does the UI work properly on a phone or a tablet?

Yes, and it is not the desktop layout shrunk. Below 900px the sidebar becomes a drawer (swipe from the left edge or tap the hamburger), the Explorer and the modals become full-screen sheets, and each session row carries one `⋯` menu instead of hover-revealed icons. A tablet in landscape is treated as a big screen with a finger on it: the sidebar stays docked and the Explorer sits beside the conversation, at touch sizes. Dragging the sidebar's edge to resize it is a desktop affordance only — a phone and a tablet keep their own sidebar sizes.

On touch, Enter adds a new line and the send button sends — the opposite of the desktop default, because Enter is the on-screen keyboard's newline key. **Ctrl+Enter / Cmd+Enter always sends**, which is the answer for a tablet with a keyboard attached, and you can flip the default under Settings → Providers & models → *This browser* → "Enter sends the message". That preference is stored in the browser you set it in and is not synced.

How it works underneath — the two stylesheets, their gates, and why an iPad needs JavaScript to be recognised at all — is [internals/web-client.md](internals/web-client.md).

### Should I expose Pernix to the public internet?

**No.** Pernix is built for trusted LANs. It executes shell commands, writes files, and makes outbound network requests — even with the dangerous-tool gate, it's not designed for adversarial environments. If you need remote access from outside your LAN, use a VPN (Tailscale, WireGuard) rather than port-forwarding.

For the full security model, see [security.md](security.md).

### Why does network mode require a restart?

The bind address (`127.0.0.1` vs `0.0.0.0`), TLS context, and CORS middleware are all configured at process start. Changing them mid-run would mean tearing down active connections — Pernix opts to require an explicit restart instead. `POST /api/admin/restart` is the supported path; it uses `os.execv` to re-execute with the same arguments.

### Can I run multiple Pernix instances on one machine?

Yes, with two adjustments:

1. **Different ports.** `python run.py --port 8091` for the second instance.
2. **Different `data/` directories.** Either run from separate clones, or override `db_path`, `workspace_dir`, `memory_dir`, `skills_dir` per instance. The first option is simpler.

There's no built-in coordination between instances — they're fully independent.

---

## Authoring and customization

### How do I make Pernix talk less / more / differently?

Edit `data/agent/SOUL.md` — that's the agent's identity file, injected into every turn. Want it terse? Verbose? Opinionated about a domain? Just write it in.

For operational rules ("always run tests before committing", "never edit `/etc`"), edit `data/agent/RULES.md`.

Both files survive `--rebuild`. You can edit them while the server is running; the next turn picks up the changes.

### How do I teach Pernix a new capability?

Write a **skill**. Skills are just markdown files with YAML frontmatter, optionally bundling scripts. The agent discovers them automatically on the next turn. See [authoring/writing-skills.md](authoring/writing-skills.md).

### How do I add a custom tool?

Use the **toolmaker** extension's `create_tool` to author a Python tool from inside a chat — no code changes to Pernix itself. See [authoring/custom-tools.md](authoring/custom-tools.md).

---

## Costs and usage

### Does anything leave my machine?

Only when the agent makes a request that explicitly goes outbound:

- LLM calls to OpenRouter (cloud) — every request you route through OpenRouter
- Web search via Tavily
- Page fetches via `browse_web` or `http_get`
- Configured webhook (`notify_webhook_url`) when the agent uses `ask_user`

Ollama inference, memory storage, and session DB all stay local. Settings and API keys are never sent to any LLM.

### How do I cap my OpenRouter spending?

Use the OpenRouter dashboard — Pernix doesn't have a built-in per-month limit. Soft tools available locally:

- `OPENROUTER_MODELS` whitelist — restrict which models the UI even shows.
- `llm_session_timeout` — caps how long any single session can hold an LLM slot.
- If RLM is enabled, it is the biggest single-call spend vector (one `rlm_process` run can fire up to `rlm_max_subcalls` sub-calls, default 50). Sub-calls run on the **Background** model (`background_model`; the root runs on Primary), so point that at a local/Ollama model to keep runs free, and tune `rlm_max_subcalls`, `rlm_max_concurrent_subcalls`, and `rlm_timeout_seconds` in Settings → Tools & safety → Large-input runs (RLM).

For hard limits, configure them on OpenRouter's side.
