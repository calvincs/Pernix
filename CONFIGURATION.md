# Configuration Reference

All Pernix settings are persisted to `data/settings.json` and can be changed at any time through the **Settings UI** or via `POST /api/settings`. API keys are stored separately in your `.env` file and are never written to `settings.json`.

Some settings (marked **requires restart**) only take effect when the server is restarted. Everything else applies immediately on save.

---

## How Settings Work

**Via the UI:** Open the gear icon in the sidebar → Settings. Changes save immediately.

**Via the API:**
```bash
curl -X POST http://localhost:8090/api/settings \
  -H "Content-Type: application/json" \
  -d '{"llm_model": "llama3.2", "scout_model": "llama3.2"}'
```

**Via direct file edit:** Edit `data/settings.json` while the server is stopped. Unknown keys are silently ignored on load.

---

## LLM Models & Providers

These are the most important settings to configure before first use.

| Setting | Default | Description |
|---|---|---|
| `llm_base_url` | `http://localhost:11434/v1` | Base URL for the primary LLM provider. Points to Ollama by default. Change to any OpenAI-compatible endpoint. |
| `llm_model` | *(empty)* | **Required.** The primary model used for agent turns. Set this before your first session. |
| `scout_model` | *(empty)* | Fast, small model used in the planning phase. Can be the same as `llm_model` to start. A lightweight model (3–8B) works well here. |
| `fallback_model` | *(empty)* | Ollama model to use when OpenRouter hits a rate limit or quota. Should be a locally-available Ollama model. |
| `background_model` | *(empty)* | Model used for background tasks: auto-titling sessions and memory distillation. Usually a smaller/faster model. |
| `llm_max_concurrent` | `1` | Maximum simultaneous requests to Ollama. Increase only if your hardware supports parallel inference. |
| `llm_session_timeout` | `1800` | Maximum wall-clock seconds a session may hold an LLM slot. Prevents hung sessions from blocking others. Set to `0` for unlimited. |

### OpenRouter

| Setting | Default | Description |
|---|---|---|
| `openrouter_base_url` | `https://openrouter.ai/api/v1` | OpenRouter API endpoint. Locked in network mode. |
| `openrouter_max_concurrent` | `2` | Simultaneous requests to OpenRouter. |
| `openrouter_models` | *(empty list)* | Comma-separated list of OpenRouter model IDs to make available (e.g. `anthropic/claude-sonnet-4-5,openai/gpt-4o`). If empty, all models from your OpenRouter account are shown. |
| `vision_model_overrides` | *(empty list)* | Force `supports_vision = true` for specific models where auto-detection fails. |

### How Model Resolution Works

Pernix can use Ollama and OpenRouter at the same time. When both have a model with the same name, **Ollama wins by default** (local, free, lower latency). If you want to use the OpenRouter version of a model, add it to `openrouter_models` — that list is the explicit whitelist that overrides the default.

---

## Context & Compaction

Pernix tracks how many tokens are in the active conversation and automatically compacts old messages to stay within the limit.

| Setting | Default | Description |
|---|---|---|
| `context_budget` | `192000` | Soft token limit for the conversation window. Set this to match your model's actual context length. |
| `max_tokens` | `32000` | Maximum tokens the model can generate per request (one response turn). |
| `compaction_threshold` | `0.75` | Compact when the conversation reaches this fraction of `context_budget`. At 75%, older messages are summarized and replaced with a compact representation. |
| `compaction_keep_tokens` | `51000` | How many tokens to preserve verbatim after compaction. Recent messages and tool results are kept. |
| `context_critical_threshold` | `0.85` | Show a visual warning in the UI when context fills to this fraction. |

> **Tip:** If you are using a model with a small context window (e.g. 8K or 16K tokens), reduce `context_budget` and `compaction_keep_tokens` accordingly.

---

## Agent Loop

| Setting | Default | Description |
|---|---|---|
| `max_tool_rounds` | `10` | Maximum number of tool-call cycles in a single turn. Prevents infinite tool loops. |
| `max_continuations` | `5` | Maximum in-turn continuations when the model's output is cut off mid-response (e.g. `finish_reason: length`). |

---

## Scout (Planning Phase)

The scout is a fast sub-agent that runs at the start of each turn to plan the approach: it searches memory, picks tools, and selects skills before handing off to the main agent.

| Setting | Default | Description |
|---|---|---|
| `scout_enabled` | `true` | Enable/disable the scout phase. Disable only for debugging; the scout significantly improves response quality. |
| `scout_timeout` | `90` | Seconds before the scout is abandoned and the main agent runs without its guidance. |
| `scout_retry_on_empty_approach` | `true` | Retry scout once if it returns no guidance (empty plan). |
| `scout_preload_memory_char_limit` | `150` | Characters per memory result injected into the scout prompt. Keeps the scout context lean. |

---

## Reflect (Quality Gate)

After each agent turn, a lightweight reflect pass verifies that the agent actually fulfilled the user's intent. If it did not, reflect can trigger a bounded retry.

| Setting | Default | Description |
|---|---|---|
| `reflect_enabled` | `true` | Enable/disable the reflect quality gate. |
| `reflect_max_retries` | `2` | Maximum number of automatic retries reflect can trigger per turn. |
| `reflect_min_messages` | `3` | Minimum messages in a conversation before reflect runs. Short exchanges (e.g., a simple one-liner) skip it. |

---

## Snooze (Idle Optimization)

During idle periods (no active sessions), Pernix runs background maintenance: deduplicating memory entries, consolidating similar notes, and profiling user preferences.

| Setting | Default | Description |
|---|---|---|
| `snooze_enabled` | `true` | Enable/disable idle-time background maintenance. |
| `snooze_interval_ticks` | `10` | How often snooze checks whether to run (each tick is approximately 60 seconds, so default = every 10 minutes). |

---

## Shell & Tool Safety

> See also: [docs/SECURITY.md](docs/SECURITY.md)

| Setting | Default | Description |
|---|---|---|
| `auto_approve_dangerous` | `false` | When `false`, the agent must ask the user before executing any tool classified as `dangerous` (primarily `bash`). Set to `true` to allow automatic execution without prompting. |
| `shell_security_mode` | `"permissive"` | `"permissive"`: only `shell_allowlist` applies. `"restrictive"`: additional syscall-level restrictions. |
| `shell_allowlist` | *(large default list)* | Commands the agent is permitted to run. The default includes common development tools (`python3`, `git`, `grep`, `curl`, `npm`, etc.). Edit to restrict or expand. |
| `shell_timeout` | `30` | Seconds before a shell command is killed. |
| `tool_timeout` | `300` | Seconds before any tool call is killed (covers file ops, HTTP, etc.). |
| `shell_address_space_limit_bytes` | `8589934592` | Virtual address space cap (8GB) applied per shell process via `RLIMIT_AS`. Set to `0` to disable. |
| `shell_env_mode` | `"passthrough"` | How environment variables are passed to the shell: `passthrough` (inherit all), `denylist` (all except listed), `allowlist` (only listed). |
| `shell_env_denylist` | *(empty)* | Variables to exclude when `shell_env_mode = "denylist"`. |
| `shell_env_allowlist` | *(empty)* | Variables to include when `shell_env_mode = "allowlist"`. |

---

## Memory

| Setting | Default | Description |
|---|---|---|
| `memory_recall` | `true` | Search memory at the start of each turn and inject relevant entries into the system prompt. |
| `memory_recall_min_score` | `2.0` | Minimum BM25 relevance score a memory entry must have to be included. Higher values = stricter filtering. |

---

## Network & Authentication

> See also: [docs/SECURITY.md](docs/SECURITY.md) for the full security model.

| Setting | Default | Requires restart | Description |
|---|---|---|---|
| `network_enabled` | `false` | **Yes** | `true` → bind to `0.0.0.0`, enforce HTTPS, require Bearer token auth. |
| `ssl_mode` | `"self_signed"` | **Yes** | `"self_signed"`: auto-generate a self-signed cert. `"custom"`: use `ssl_cert_path` + `ssl_key_path`. |
| `ssl_cert_path` | *(empty)* | **Yes** | Path to PEM certificate file (custom SSL mode only). |
| `ssl_key_path` | *(empty)* | **Yes** | Path to PEM private key file (custom SSL mode only). |
| `auth_token` | *(auto-generated)* | No | The Bearer token for network mode. Auto-generated on first network-mode start. Rotate via `POST /api/settings/auth-token/regenerate`. |
| `cors_origins` | *(empty list)* | **Yes** | Allowed CORS origins in network mode. If empty, the wildcard `*` is used (no credentials). Recommended: set explicitly to your client origins. |

---

## Browser

| Setting | Default | Description |
|---|---|---|
| `browser_enabled` | `false` | Enable the `browse_web` tool (requires Playwright installed). |
| `browser_headless` | `true` | Run Chromium without a visible window. Set to `false` to debug browser sessions visually (local mode only). |

---

## Web Search

| Setting | Default | Description |
|---|---|---|
| `web_search_enabled` | `true` | Enable the `search_web` tool. |

The search provider is selected automatically:
- If `TAVILY_API_KEY` is set in `.env` → Tavily is used as the primary provider
- Otherwise → DuckDuckGo is used (no key required, rate-limited at high volume)

---

## Notifications

| Setting | Default | Description |
|---|---|---|
| `notify_webhook_url` | *(empty)* | If set, Pernix sends a POST request to this URL whenever the agent uses `ask_user` to pause and wait for input. Useful for alerting via Slack, Home Assistant, etc. |
| `vapid_private_key` | *(auto-generated)* | VAPID private key for Web Push. Auto-generated on first run. |
| `vapid_public_key` | *(auto-generated)* | VAPID public key shared with service worker subscriptions. |
| `vapid_subject` | `mailto:admin@localhost` | VAPID subject — typically a `mailto:` address or URL identifying the push sender. |

---

## Sessions

| Setting | Default | Description |
|---|---|---|
| `max_pending_messages` | `10` | Maximum messages that can queue for a busy session. If a session is processing and more than this many messages arrive, further messages are rejected with a `session.queue_full` event. |
| `max_concurrent_workers` | `5` | Maximum simultaneously-running worker sub-agents per parent session. |
| `stall_threshold` | `120` | Seconds of inactivity before a session is considered stalled. |

---

## Other Tunables

These settings are advanced and rarely need adjusting. Listed here for completeness; defaults are sensible for most deployments.

| Setting | Default | Description |
|---|---|---|
| `max_fetch_size` | `100000` | Maximum bytes the `http_get` tool will fetch (100KB). |
| `browser_timeout` | `30` | Page load timeout for `browse_web` (seconds). |
| `plan_review_timeout` | `120` | Seconds the planning extension waits for user review before timing out. |
| `eval_auto` | `false` | Run evaluation extension automatically after qualifying turns. |
| `eval_threshold` | `0.7` | Minimum eval score to consider a turn successful. |
| `eval_max_retries` | `2` | Max evaluation-driven retries per turn. |
| `eval_browser_verify` | `false` | Use browser-based verification when evaluating frontend changes. |
| `reflect_max_retries_worker` | inherits `reflect_max_retries` | Separate retry cap for worker sub-agents. |
| `reflect_full_transcript` | `false` | Include the full transcript in reflect evidence (debug-only; verbose). |
| `reflect_model` | *(empty)* | Override model for reflect; empty uses `background_model`. |
| `post_mortem_retention_days` | `90` | Days to keep synthesized post-mortem records before snooze cleans them. |
| `notify_webhook_timeout` | `10` | HTTP timeout for `notify_webhook_url` POST (seconds). |
| `snooze_max_cycle_seconds` | `60` | Max time per Snooze cycle before yielding. |
| `snooze_cooldown_minutes` | `5` | Minimum idle time before Snooze starts running. |
| `snooze_dedup_interval_days` | `7` | Days between memory-dedup sweeps per file. |
| `snooze_consolidation_interval_hours` | `24` | Hours between memory-consolidation scans. |

---

## Architecture Reference

For a deep dive into the session state machine, agent turn loop, compaction algorithm, worker orchestration, and reflect/snooze internals, see:

- **[docs/workflow.md](docs/workflow.md)** — detailed architectural walkthrough
- **[docs/API.md](docs/API.md)** — REST API and SSE event reference
