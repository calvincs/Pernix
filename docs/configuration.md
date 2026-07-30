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
  -d '{"llm_model": "qwen3:32b", "scout_model": "qwen3:8b"}'
```

**Via direct file edit:** Edit `data/settings.json` while the server is stopped. Unknown keys are silently ignored on load.

**Via Swagger UI (try-it interactively):** Pernix is FastAPI-based, so a live API explorer is available at [`http://localhost:8090/docs`](http://localhost:8090/docs). The `POST /api/settings` endpoint is right there — click "Try it out", paste a JSON body, and execute. ReDoc lives at `/redoc` for a more reference-style view of the schema.

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
| `openrouter_models` | *(empty list)* | Comma-separated list of OpenRouter model IDs to make available (e.g. `anthropic/claude-sonnet-4.6,anthropic/claude-haiku-4.5,x-ai/grok-4.1-fast`). If empty, all models on your OpenRouter account are shown. Use current frontier models — agent workloads benefit a lot from strong tool-call and reasoning behavior. |
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
| `scout_preload_memory_char_limit` | `300` | Characters per memory result in the scout's auto-injected baseline. Only affects the preload phase — active recall tool calls return full entry content. |

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

## Candor (Operational Memory Add-on)

Integration with the Candor memory substrate: calibrated reliability tracking for tools, turns, and reflect verdicts, with an auditable evidence ledger. The `candor` package installs with `pip install -r requirements.txt` (vendored wheel in `vendor/`; rebuild with `pip wheel --no-deps -w vendor/ /path/to/Candor` after upstream changes). Toggles live in Settings → Candor (Operational Memory). Design details: [dev/candor-integration-plan.md](dev/candor-integration-plan.md).

| Setting | Default | Description |
|---|---|---|
| `candor_enabled` | `false` | Master switch. Turn-end emission, snooze maintenance, and the scout brief toggle hot; the agent tools (`predict_reliability`, `why_reliability`, `reliability_questions`) register at startup only, so enabling them needs a restart. |
| `candor_scout_brief` | `true` | Inject the `[OPERATIONAL INTEL]` exception report (degraded tools, discovered conditions, open questions) into scout's pre-load context. |
| `candor_max_obs_per_turn` | `200` | Safety valve on how many observations one turn may emit. |

The store lives at `data/candor/` (machine-local, not in `settings.json`).

---

## RLM (Recursive Processing Add-on)

Recursive Language Models (arXiv 2512.24601): the agent processes inputs far beyond the context window — huge files, corpora, transcripts, log dumps — by writing code in a sandboxed child REPL that holds the input as a variable and delegates chunk work to budgeted sub-LLM calls. Adapted from the MIT-licensed reference implementation (no new dependency). Toggles live in Settings → General → RLM (Recursive Processing); model roles under Settings → Models. Architecture + security posture: [internals/rlm.md](internals/rlm.md).

| Setting | Default | Description |
|---|---|---|
| `rlm_enabled` | `false` | Master switch. Caps and model roles apply hot; the `rlm_process` tool registers at startup only, so enabling/disabling needs a restart. |
| `rlm_root_model` | *(empty)* | Root orchestrator model. Falls back to `llm_model`. |
| `rlm_sub_model` | *(empty)* | Sub-call model for chunk work (the bulk of spend). Falls back to `background_model`, then `llm_model`. |
| `rlm_max_iterations` | `20` | Root REPL turns per run before best-effort synthesis. |
| `rlm_max_depth` | `1` | `1` = sub-calls only; `2`–`3` lets `rlm_query()` spawn nested RLM runs. |
| `rlm_max_subcalls` | `50` | Total sub-LLM calls per run (one ledger shared across recursion depths). |
| `rlm_max_concurrent_subcalls` | `3` | Parallel sub-calls (the global LLM scheduler still applies underneath). |
| `rlm_timeout_seconds` | `900` | Wall clock per run; the child process group is killed at the deadline. |
| `rlm_run_retention_days` | `30` | Age after which snooze purges `data/workspace/rlm/<run_id>/` dirs and their DB rows. |

---

## Shell & Tool Safety

> See also: [security.md](security.md)

| Setting | Default | Description |
|---|---|---|
| `auto_approve_dangerous` | `false` | **Read-only via API.** When `false`, dangerous tools require explicit per-invocation user approval (see below). Can only be set to `true` at startup via the `--dangerous` flag — it cannot be changed while the server is running. |
| `shell_security_mode` | `"permissive"` | `"permissive"`: only `shell_allowlist` applies. `"restrictive"`: additional syscall-level restrictions. |
| `shell_allowlist` | *(large default list)* | Commands the agent is permitted to run. The default includes common development tools (`python3`, `git`, `grep`, `curl`, `npm`, etc.). Edit to restrict or expand. |
| `shell_timeout` | `30` | Seconds before a shell command is killed. |
| `tool_timeout` | `300` | Seconds before any tool call is killed (covers file ops, HTTP, etc.). |
| `shell_address_space_limit_bytes` | `8589934592` | Virtual address space cap (8GB) applied per shell process via `RLIMIT_AS`. Set to `0` to disable. |
| `shell_env_mode` | `"passthrough"` | How environment variables are passed to the shell: `passthrough` (inherit all), `denylist` (all except listed), `allowlist` (only listed). |
| `shell_env_denylist` | *(empty)* | Variables to exclude when `shell_env_mode = "denylist"`. |
| `shell_env_allowlist` | `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, plus audio/display vars | Variables to include when `shell_env_mode = "allowlist"`. |

### Dangerous Tool Approval Flow

When `auto_approve_dangerous` is `false` (the default), every tool marked `dangerous` goes through a two-step human-in-the-loop confirmation before it runs:

1. **`ask_user()`** — the agent describes the exact action it intends to take (command, URL, file path). The session suspends until you respond.
2. **`approve_dangerous_tool(tool_name, scope)`** — after you confirm, the agent registers the approval. `scope` is a short description of what was approved (e.g. `"run ps aux to list processes"`).

Approvals are **per-invocation by default** — approving `bash` for `ps aux` does not cover a later `mv /etc/passwd`. Pass `persistent=True` only for genuinely repetitive low-risk actions (e.g. browsing several pages during research) where re-asking each call would be noise.

**Previously approved scopes are remembered** in `data/tool_approvals.json`. The next time the agent calls `approve_dangerous_tool()` with the same scope, the `ask_user` step is skipped automatically. You can view and clear this file in **Settings → Security → Remembered Approvals**.

---

## Memory

| Setting | Default | Description |
|---|---|---|
| `memory_recall` | `true` | Search memory at the start of each turn and inject relevant entries into the system prompt. |
| `memory_recall_min_score` | `2.0` | Minimum BM25 relevance score a memory entry must have to be included. Higher values = stricter filtering. |

---

## Network & Authentication

> See also: [security.md](security.md) for the full security model.

| Setting | Default | Requires restart | Description |
|---|---|---|---|
| `network_enabled` | `false` | **Yes** | `true` → bind to `0.0.0.0`, enforce HTTPS, require Bearer token auth. |
| `ssl_mode` | `"self_signed"` | **Yes** | `"self_signed"`: auto-generate a self-signed cert. `"custom"`: use `ssl_cert_path` + `ssl_key_path`. |
| `ssl_cert_path` | *(empty)* | **Yes** | Path to PEM certificate file (custom SSL mode only). |
| `ssl_key_path` | *(empty)* | **Yes** | Path to PEM private key file (custom SSL mode only). |
| `auth_token` | *(auto-generated)* | No | The Bearer token for network mode. Auto-generated on first network-mode start. Rotate via `POST /api/settings/auth-token/regenerate`. |
| `trust_local_requests` | `true` | No | Skip auth for requests from `127.0.0.1`/`::1`. Set `false` behind a reverse proxy — proxied requests arrive from loopback and would otherwise bypass the token entirely. Read per-request; no restart needed. |
| `cors_origins` | *(empty list)* | **Yes** | Allowed CORS origins in network mode. If empty, the wildcard `*` is used (no credentials). Recommended: set explicitly to your client origins. |

---

## Browser

| Setting | Default | Description |
|---|---|---|
| `browser_enabled` | `true` | Enable the `browse_web` tool (requires Playwright installed). Independent of `web_search_enabled` — `browse_web` works without a Tavily key. |
| `browser_headless` | `true` | Run Chromium without a visible window. Set to `false` to debug browser sessions visually (local mode only). |

---

## Web Search

| Setting | Default | Description |
|---|---|---|
| `web_search_enabled` | `true` | Enable the `search_web` tool. |

`search_web` requires a Tavily API key. Add it in Settings → Web → Tavily API Key
(free tier at tavily.com). Without it the tool returns a setup hint rather than
silently degrading. `web_search_enabled` can be used to disable the tool entirely.

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
| `stall_threshold` | `120` | Seconds of inactivity before a worker is flagged as stalled (surfaced in the UI and by `await_workers`). |

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
| `reflect_max_retries_worker` | `2` | Separate retry cap for worker sub-agents (bounds fan-out cost). |
| `reflect_emit_digest_on_pass` | `false` | Have reflect emit a turn digest even on `pass` verdicts. Default off; the digest is always emitted on `retry`/`escalate` so the next scout can plan around real evidence. |
| `reflect_digest_max_chars_per_excerpt` | `2000` | Per-call cap on each tool result excerpt inside the turn digest. Enforced at parse time. |
| `reflect_full_transcript` | `false` | **Deprecated.** Reflect now always sees the per-attempt transcript; this flag is a no-op kept for back-compat. |
| `reflect_model` | *(empty)* | Override model for reflect; empty uses `background_model`. |
| `post_mortem_retention_days` | `90` | Days to keep synthesized post-mortem records before snooze cleans them. |
| `notify_webhook_timeout` | `10` | HTTP timeout for `notify_webhook_url` POST (seconds). |
| `snooze_max_cycle_seconds` | `60` | Max time per Snooze cycle before yielding. |
| `snooze_cooldown_minutes` | `5` | Minimum idle time before Snooze starts running. |
| `snooze_dedup_interval_days` | `7` | Days between memory-dedup sweeps per file. |
| `snooze_consolidation_interval_hours` | `24` | Hours between memory-consolidation scans. |
| `snooze_consolidation_cluster_threshold` | `0.55` | Minimum pair-similarity score for two memory entries to be clustered during consolidation. |
| `reflect_retry_budget_cap_s` | `600` | Ceiling on the computed minimum-budget-for-retry threshold (seconds); prevents high `scout_timeout` values from blocking retries. |
| `shell_fsize_limit_bytes` | `2147483648` | Per-shell-process file-size write cap (2 GB) via `RLIMIT_FSIZE`. `0` disables. |
| `max_file_write_size` | `104857600` | Max bytes per `file_write` / `file_edit` / `multiedit` call (100 MB). `0` disables. |
| `max_edit_read_size` | `5242880` | Max file size for `file_edit`'s whole-file fuzzy-match path (5 MB). `0` disables. |
| `audio_model_overrides` | *(empty list)* | Force `supports_audio = true` for models where auto-detection misses audio capability. |

---

## Architecture Reference

For a deep dive into the session state machine, agent turn loop, compaction algorithm, worker orchestration, and reflect/snooze internals, see:

- **[internals/state-machine.md](internals/state-machine.md)** — detailed architectural walkthrough
- **[api.md](api.md)** — REST API and SSE event reference
