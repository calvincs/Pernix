# LLM providers

Pernix routes every model call through one of three providers: **Ollama** (local), **OpenRouter** (cloud), or the **OpenAI provider** (api.openai.com or any OpenAI-compatible server such as vLLM, LM Studio, or llama.cpp). It supports them simultaneously, with automatic failover.

This page explains the provider model, the model roles, how routing decides which provider handles a call, and how rate-limit failover works.

---

## Providers, picked by model name

Pernix detects which provider to use based on the model name:

| Model name format | Provider |
|---|---|
| A bare name listed in `openai_models` (`gpt-4o`) | **OpenAI provider** (if `OPENAI_API_KEY` is set) — the whitelist wins before the heuristic below |
| `qwen3:32b`, `llama3.2`, anything else without a slash | **Ollama** |
| `anthropic/claude-sonnet-4.6`, `openai/gpt-4o`, anything with a slash | **OpenRouter** (if `OPENROUTER_API_KEY` is set) |

The slash rule matches OpenRouter's standard `org/model` slug format. Bare names default to Ollama — which is why listing your OpenAI-provider models in `openai_models` matters: without the whitelist, `gpt-4o` would misroute to Ollama.

If a name has a slash but you don't have an OpenRouter key, the call falls through to Ollama (which will fail with "model not found" — Ollama doesn't use slashed names).

---

## Ollama (local)

[Ollama](https://ollama.ai) runs models entirely on your machine. No API key, no internet needed for inference. Pernix's default points at the Ollama default endpoint:

| Setting | Default | Notes |
|---|---|---|
| `llm_base_url` | `http://localhost:11434/v1` | OpenAI-compatible Ollama endpoint |
| `llm_max_concurrent` | `1` | Max simultaneous Ollama requests |
| `llm_session_timeout` | `1800` | Max seconds any session may hold an Ollama slot (`0` = unlimited) |

To use a different Ollama install (different machine, custom port), change `llm_base_url`. In network mode it's locked — edit `data/settings.json` directly from localhost if you need to.

### Pulling a model

```bash
ollama pull qwen3:8b
ollama pull qwen3:32b
ollama list                          # see what's installed
```

Then in Pernix Settings, set `llm_model` (or `scout_model`) to the exact name from `ollama list`.

### Concurrency

`llm_max_concurrent` defaults to 1. Bump it if your hardware can run multiple models or multiple instances of one model in parallel. The semaphore is per-Pernix-process, not per-Ollama-process — Ollama itself has its own queue.

---

## OpenRouter (cloud)

[OpenRouter](https://openrouter.ai) is a single API to dozens of frontier models — Claude, GPT, Gemini, Grok, and many more.

Setup:

1. Create an account, get an API key.
2. Add `OPENROUTER_API_KEY=sk-or-v1-...` to `.env`.
3. Optionally set `OPENROUTER_MODELS=anthropic/claude-sonnet-4.6,...` in `.env` to whitelist which models appear in Pernix's model picker.
4. In Settings, set `llm_model` to a slugged OpenRouter model ID.

| Setting | Default | Notes |
|---|---|---|
| `openrouter_base_url` | `https://openrouter.ai/api/v1` | Locked in network mode |
| `openrouter_max_concurrent` | `2` | Max simultaneous OpenRouter requests |
| `openrouter_models` | empty list | Whitelist; if set, only these models appear in the UI picker |
| `vision_model_overrides` | empty list | Force `supports_vision=true` on listed models if auto-detection misses |

Without `OPENROUTER_API_KEY`, OpenRouter is invisible to Pernix — only Ollama is available.

### Prompt caching for Anthropic models

When an `anthropic/*` model runs via OpenRouter, Pernix attaches prompt-cache breakpoints (`cache_control` markers) at the system prompt's stable boundaries, so the static prefix is cached across turns instead of re-billed. This is `openrouter_cache_control`, **on by default**; other models and providers are untouched. Cache reads and writes appear in the session cost tooltip — writes with no subsequent reads suggest the breakpoints are landing on unstable bytes.

---

## OpenAI (and OpenAI-compatible servers)

A native provider for the OpenAI API. Because the base URL is overridable, the same provider speaks to anything OpenAI-compatible — vLLM, LM Studio, a llama.cpp server.

Setup:

1. Add `OPENAI_API_KEY=sk-...` to `.env` (or set it in Settings → LLM Providers → OpenAI API Key). The key is **env-only by design** — it is never stored in `settings.json`, which is plaintext on disk.
2. List the models you want in `openai_models` (Settings) or `OPENAI_MODELS` in `.env` (comma-separated, e.g. `gpt-4o,gpt-4o-mini`). This is what routes bare names to the provider.
3. In Settings, set `llm_model` (or any role) to one of those names.

For a self-hosted OpenAI-compatible server, additionally point `openai_base_url` at it (e.g. `http://localhost:8000/v1` for vLLM, `http://localhost:1234/v1` for LM Studio) and whitelist the model names it serves. The server still needs *some* `OPENAI_API_KEY` value set for the provider to activate — most local servers accept any string.

| Setting | Default | Notes |
|---|---|---|
| `openai_base_url` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint |
| `openai_max_concurrent` | `2` | Max simultaneous requests to this provider |
| `openai_models` | empty list | Whitelist; routes bare names here and curates the UI picker |

Resolution and fallback work like OpenRouter's: a whitelisted bare name routes here (on a name collision with a local model, Ollama wins unless the name is whitelisted), and rate-limit/overload/timeout failures fall back to your local `fallback_model` with the same message sanitization. Cached prompt tokens reported by the API (`prompt_tokens_details.cached_tokens`) surface as cache reads in the cost tooltip.

Without `OPENAI_API_KEY`, this provider is invisible to Pernix.

---

## When both providers offer the same name

You can have a model with the same name on both providers (e.g., a fine-tuned local model that mirrors a cloud one). Conflict policy:

- **Ollama wins by default.** Local, free, lower latency.
- **To force OpenRouter,** add the model explicitly to `OPENROUTER_MODELS`. The whitelist overrides the default.

This is a deliberate "prefer the cheap and offline option" stance.

---

## The four model roles

Pernix uses **four model roles**. You can assign a different model to each, or use the same one across all four to start.

| Role | Setting | What it does | Typical choice |
|---|---|---|---|
| Primary | `llm_model` | Main agent turns, streaming, tool calls | Your strongest model — `qwen3:32b` locally, `anthropic/claude-sonnet-4.6` on OpenRouter |
| Scout | `scout_model` | Planning phase: memory recall, tool selection, skill discovery. Runs in fresh context | A fast smaller model — `qwen3:8b`, `anthropic/claude-haiku-4.5` |
| Fallback | `fallback_model` | Used when OpenRouter rate-limits or errors out. Should be a locally-available Ollama model | Any decent Ollama model — `qwen3:8b` works |
| Background | `background_model` | Auto-titling sessions, message distillation, reflect re-analysis (fire-and-forget) | A small fast model — `qwen3:8b`, `anthropic/claude-haiku-4.5` |

You don't have to fill all four. If `background_model` is empty, Pernix falls back to the primary `llm_model` for background tasks. If `fallback_model` is empty, rate-limit failover is disabled.

There is also a fifth, optional role: `embedding_model` — a local Ollama embedding model (e.g. `nomic-embed-text`) that turns memory search hybrid (BM25 + vector). Setting it is the switch; empty keeps search purely lexical. See [memory-and-recall.md](../guides/memory-and-recall.md#semantic-retrieval).

### Why a separate scout model?

Scout runs in a fresh context window every turn. Its job is fast and structured: read the user message, search memory, pick tools/skills, draft a plan. A small model is fine — sometimes better, since it tends to follow the structured-output instructions more reliably.

Running scout on the same heavy model as the main agent is wasteful — you'd pay the slow model's latency for a job a fast model handles in 2 seconds.

---

## Per-session model override

The four roles above are global. To change just the primary model for one session:

```bash
curl -X PATCH http://localhost:8090/api/sessions/{id} \
  -H "Content-Type: application/json" \
  -d '{"model_override": "anthropic/claude-haiku-4.5"}'
```

Or via the session menu in the UI. The override only affects the primary; scout/fallback/background still follow the global setting.

Common pattern: most sessions use a heavy model, but a "quick lookup" session uses a fast cheap model. The override lets you do that without changing global settings.

---

## Failover semantics

When OpenRouter returns a rate-limit, quota-exceeded, or context-overflow error, the router attempts failover:

1. The original failure is logged (`FailoverError` with classified reason).
2. If `fallback_model` is set and the failure is one of the failover-eligible classes, the request is retransmitted to the fallback Ollama model.
3. The message stream is **sanitized for the fallback** — `core/llm/router.py:sanitize_for_fallback()` strips vision content blocks and converts tool messages to text, since most local models don't support those formats.
4. The user-facing response continues without interruption. The UI may show a small badge indicating which provider answered.

Failover is **not** triggered for transient network errors or for OpenRouter being down entirely (those bubble up as errors). It's specifically targeted at rate limits.

If `fallback_model` is empty, failover is skipped and the user sees the OpenRouter error.

---

## Concurrency semaphores

Each provider has its own concurrency limit, enforced via async semaphores:

- `llm_max_concurrent` (default 1) — Ollama
- `openrouter_max_concurrent` (default 2) — OpenRouter
- `openai_max_concurrent` (default 2) — OpenAI provider

Workers and the main session share the semaphore. If you spawn 5 parallel workers all using the same provider, they queue against this limit.

Tune these to match your hardware (Ollama) and your provider plan (OpenRouter).

---

## Vision-capable models

Pernix auto-detects which models support vision (image inputs) by querying the provider's model registry. If a model is misclassified, override:

```json
{ "vision_model_overrides": ["anthropic/claude-sonnet-4.6"] }
```

When `vision_model_overrides` includes a model, Pernix treats it as multimodal regardless of what the registry says.

---

## Diagnosing a routing problem

If a model you set isn't being used the way you expect:

1. **Check the active model.** `GET /api/models` shows which models Pernix knows about and which provider they're routed through.
2. **Check the logs.** `data/logs/pernix.log` records the provider and model name for every LLM call.
3. **Check the conflict.** If both providers have the same name and you wanted OpenRouter to win, you need it in `OPENROUTER_MODELS`.
4. **Check the slot.** If concurrency is maxed, requests queue silently — bump `llm_max_concurrent` or `openrouter_max_concurrent`.

For deeper provider diagnostics, `GET /api/health/detailed` (localhost-only) shows provider connectivity and last-seen status.
