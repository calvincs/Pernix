# Configuration Reference

Nearly all Pernix settings are persisted to `data/settings.json` and can be changed at any time through the **Settings UI** or via `POST /api/settings`. API keys are stored separately in your `.env` file and are never written to `settings.json`. Three exceptions: machine-local fields (`db_path`, `host`, `port`, and the `*_dir` paths) are never persisted; `auto_approve_dangerous` is runtime-only — set by the `--dangerous` CLI flag, never saved to or loaded from disk; and a set of security-critical fields (`shell_security_mode`, `shell_allowlist`, `shell_env_*`, `auth_token` — plus the LLM base URLs in network mode) is rejected by `POST /api/settings` regardless of where the request comes from (see [security.md](security.md#locked-settings)).

Some settings (marked **requires restart**) only take effect when the server is restarted. Everything else applies immediately on save.

---

## How Settings Work

**Via the UI:** Open the gear icon in the sidebar → Settings. Changes save immediately.

**Via the API:**
```bash
curl -X POST http://localhost:8090/api/settings \
  -H "Content-Type: application/json" \
  -d '{"llm_model": "qwen3:32b", "background_model": "qwen3:8b"}'
```

**Via direct file edit:** Edit `data/settings.json` while the server is stopped. Unknown keys are silently ignored on load.

**Via Swagger UI (try-it interactively):** Pernix is FastAPI-based, so a live API explorer is available at [`http://localhost:8090/docs`](http://localhost:8090/docs). The `POST /api/settings` endpoint is right there — click "Try it out", paste a JSON body, and execute. ReDoc lives at `/redoc` for a more reference-style view of the schema.

---

## LLM Models & Providers

These are the most important settings to configure before first use.

| Setting | Default | Description |
|---|---|---|
| `llm_base_url` | `http://localhost:11434/v1` | Base URL for the primary LLM provider. Points to Ollama by default. Change to any OpenAI-compatible endpoint. |
| `llm_model` | *(empty)* | **Required. Primary** role — agent turns, plus every quality-critical call: compaction summaries, reflect verdicts, eval, and the RLM root. Set this before your first session. |
| `fallback_model` | *(empty)* | **Backup** role — used whenever a Primary *or* Background call fails: provider failover, agent-loop stream failover, scout's last resort, and the one-shot retry wrapped around every non-streaming call. A different model on the **same** provider counts, so an all-Ollama setup still gets failover. Empty disables failover entirely. |
| `background_model` | *(empty)* | **Background** role — the fast/offline tier: scout planning, session auto-titling, memory distillation and ingest, refine input prep, LLM-backed Snooze activities, Dream, Telos, and RLM sub-calls. Quality-critical calls (compaction, reflect, eval) run on Primary instead. Empty falls back to `llm_model`. |
| `llm_max_concurrent` | `1` | Maximum simultaneous requests to Ollama. Increase only if your hardware supports parallel inference. |
| `llm_session_timeout` | `1800` | Maximum wall-clock seconds a session may hold an LLM slot. Prevents hung sessions from blocking others. Set to `0` for unlimited. |
| `provider_quota_cooldown_s` | `600` | When a model 403s on an exhausted quota, failover *to* that model is refused for this many seconds — so a dead key can't mask the real error. |

### OpenRouter

| Setting | Default | Description |
|---|---|---|
| `openrouter_base_url` | `https://openrouter.ai/api/v1` | OpenRouter API endpoint. Locked in network mode. |
| `openrouter_max_concurrent` | `4` | Simultaneous requests to OpenRouter. |
| `openrouter_models` | *(empty list)* | Comma-separated list of OpenRouter model IDs to make available (e.g. `anthropic/claude-sonnet-4.6,anthropic/claude-haiku-4.5,x-ai/grok-4.1-fast`). If empty, all models on your OpenRouter account are shown. Use current frontier models — agent workloads benefit a lot from strong tool-call and reasoning behavior. |
| `openrouter_cache_control` | `true` | Attach prompt-cache breakpoints (`cache_control` markers) to the system prompt for `anthropic/*` models routed via OpenRouter — the static prefix and the per-turn section become separately cacheable. Other models and providers are untouched. Cache reads/writes show in the session cost tooltip. |
| `vision_model_overrides` | *(empty list)* | Force `supports_vision = true` for specific models where auto-detection fails. |
| `ollama_think` | `false` | Reasoning mode (`think=true` on Ollama's native `/api/chat`) for the **Primary** role — agent turns and the quality-critical one-shots. Applies only to Ollama models that have a thinking mode (the qwen3 family, nemotron3, …); others ignore it. Off buys latency and output tokens, on buys quality on hard turns. The reasoning chain is not shown or stored — only the answer. |
| `ollama_think_background` | `false` | Same, for the **Background** role (scout, titles, distill, dream, snooze). Rarely worth turning on: scout's cost is already dominated by its ~12K-token prompt. **If `llm_model` and `background_model` name the same model the two roles cannot be told apart at the provider, and `ollama_think` applies to both** — point Background at a different (ideally smaller) tag to run the tiers differently. Backup follows Primary. |

### OpenAI (and OpenAI-compatible servers)

A native provider for the OpenAI API — and, because `openai_base_url` is overridable, for any OpenAI-compatible server (vLLM, LM Studio, llama.cpp server). The API key is **env-only**: set `OPENAI_API_KEY` in `.env` (or via Settings → LLM Providers → OpenAI API Key); it is deliberately never a `settings.json` field because that file is plaintext on disk.

| Setting | Default | Description |
|---|---|---|
| `openai_base_url` | `https://api.openai.com/v1` | OpenAI API endpoint. Point it at any OpenAI-compatible server to use vLLM, LM Studio, or llama.cpp instead. |
| `openai_max_concurrent` | `4` | Simultaneous requests to the OpenAI provider. |
| `openai_models` | *(empty list)* | Whitelist of model names served by this provider (also settable via the `OPENAI_MODELS` env var). Listing models is recommended: it routes bare names like `gpt-4o` to this provider (otherwise slash-less names go to Ollama) and keeps the UI dropdown curated. |

See [deployment/llm-providers.md](deployment/llm-providers.md) for setup walkthroughs.

### How Model Resolution Works

Pernix can use Ollama, OpenRouter, and the OpenAI provider at the same time. Slash-less model names route to Ollama by default; names listed in `openai_models` route to the OpenAI provider first (the whitelist wins before the heuristic); slugged `org/model` names go to OpenRouter. When two providers offer the same name, **Ollama wins by default** (local, free, lower latency) — the `openrouter_models` / `openai_models` whitelists are the explicit overrides. Rate-limited or overloaded cloud calls fall back to the local `fallback_model` regardless of which cloud provider they started on.

---

## Context & Compaction

Pernix tracks how many tokens are in the active conversation and automatically compacts old messages to stay within the limit.

Context is **auto-managed by default** (`context_auto`): the harness reads each model's real limits from the provider — Ollama `/api/show` for the trained context window, OpenRouter `/models` for `context_length` and the per-model completion cap — budgets against them, and pins `num_ctx` on every native Ollama request so the server-side window matches the harness budget (without it, Ollama applies its own default and silently truncates the prompt). Manual values below act as overrides/fallbacks.

| Setting | Default | Description |
|---|---|---|
| `context_auto` | `true` | Derive per-model context and output limits from live provider metadata. Off = `context_budget`/`max_tokens` rule unconditionally, and `num_ctx` is never sent to Ollama. |
| `ollama_num_ctx_cap` | `65536` | VRAM guard: effective Ollama window = `min(model max, cap)`. KV-cache size scales with `num_ctx`, so running a 256K-window model at full width can exhaust GPU memory. `0` = uncapped. |
| `context_budget` | `192000` | **Fallback only.** The budget is normally derived per session at turn start from the active model's registry `context_length` (`context_length × 0.9`). Used when the registry reports no context length for the model, or when `context_auto` is off. |
| `max_tokens` | `32000` | Ceiling on tokens the model may generate per request. With `context_auto` on, the effective request is `min(max_tokens, provider-reported completion cap)` — e.g. OpenRouter's `top_provider.max_completion_tokens`. |
| `compaction_threshold` | `0.75` | Compact when the conversation reaches this fraction of `context_budget`. At 75%, older messages are summarized and replaced with a compact representation. |
| `compaction_keep_tokens` | `51000` | How many tokens to preserve verbatim after compaction. Recent messages and tool results are kept. |
| `context_critical_threshold` | `0.85` | Show a visual warning in the UI when context fills to this fraction. |
| `max_inline_attach_bytes` | `33554432` (32 MB) | Ceiling on the total base64 attachment bytes inlined into a single compile. Past it, the oldest attachments fall back to text markers. 32 MB fits audio (a 19 MB WAV expands to ~25 MB base64). |

### View pruning

Before compaction is needed, the context compiler can stub oversized tool results out of the **compiled view**. Stored messages are never touched — re-reading the session or exporting it still shows the full result.

This used to be an unconditional hardcode: every tool result over 300 characters beyond the last 10 messages was stubbed on every compile, regardless of whether the context was under any pressure at all, and with no event to say it had happened. It is now budget-gated and emits `context.view_pruned` when it fires.

| Setting | Default | Description |
|---|---|---|
| `view_prune_pressure` | `0.5` | Only prune when history size exceeds this fraction of the (character-equivalent) context budget. Below it, nothing is stubbed. |
| `view_prune_keep_recent` | `30` | Number of most recent messages left completely intact. |
| `view_prune_min_chars` | `2000` | Only tool results larger than this are candidates for stubbing. |

> **Tip:** If you are using a model with a small context window (e.g. 8K or 16K tokens), reduce `compaction_keep_tokens` accordingly. `context_budget` normally follows the model automatically.

---

## Agent Loop

| Setting | Default | Description |
|---|---|---|
| `max_tool_rounds` | `50` | Maximum number of tool-call cycles in a single turn. A backstop against infinite tool loops — not a spend cap. Goal token/time budgets and the stuck detector are the real guards. (Raised from `10` in the 2026-08 refactor; ten rounds manufactured its own failures on ordinary long tasks.) |
| `round_cap_auto_continue` | `1` | Fresh round budgets granted when a turn exhausts `max_tool_rounds` while healthy (tools ran, no errors, no stuck spiral). Each grant leaves a transcript notice. `0` restores the hard stop. |
| `forced_followup_enabled` | `true` | When a reply ends by announcing more work ("Next, I'll…") with no tool calls, inject one in-turn nudge naming the unfinished item instead of ending the turn. The trigger is narrow: future-intent tail only, trailing questions and courtesy closers never fire it. |
| `forced_followup_max_per_turn` | `1` | Cap on forced follow-up nudges per turn (bounds 0–5). Keeps a genuinely finished task from being looped. |

---

## Scout (Planning Phase)

The scout is a fast sub-agent that runs at the start of each turn to plan the approach: it searches memory, picks tools, and selects skills before handing off to the main agent.

| Setting | Default | Description |
|---|---|---|
| `scout_enabled` | `true` | Enable/disable the scout phase. Disable only for debugging; the scout significantly improves response quality. |
| `scout_timeout` | `90` | Seconds before the scout is abandoned and the main agent runs without its guidance. |
| `scout_retry_on_empty_approach` | `true` | Retry scout once if it returns no guidance (empty plan). |
| `scout_preload_memory_char_limit` | `600` | Characters per memory result in the scout's auto-injected baseline. Only affects the preload phase — active recall tool calls return full entry content. |

---

## Reflect (Quality Gate)

After each agent turn, a lightweight reflect pass verifies that the agent actually fulfilled the user's intent. If it did not, reflect can trigger a bounded retry.

| Setting | Default | Description |
|---|---|---|
| `reflect_enabled` | `true` | Enable/disable the reflect quality gate. |
| `reflect_max_retries` | `2` | Maximum number of automatic retries reflect can trigger per turn. |
| `reflect_min_messages` | `3` | Minimum messages in a conversation before reflect runs. Short exchanges (e.g., a simple one-liner) skip it. |
| `reflect_deferred_normal` | `true` | Interactive sessions finalize immediately and get their grade later, observe-only — lessons, post-mortems, and experience records are written exactly as before, but no verdict can retry the turn. Off restores synchronous, retry-capable reflect on interactive turns. Cron/worker/canary sessions always keep the synchronous, retry-capable path, and deterministic gates still run (and clamp) in-line. |
| `reflect_defer_idle_s` | `300` | Quiet seconds before a deferred grade runs. Only the latest completed turn is graded — a turn superseded inside this window never is. |
| `reflect_experience` | `true` | Parse reflect's per-turn experience read (sentiment, friction, user observations) and feed it to Candor, post-mortems, and user-profile memory. |

---

## Snooze (Idle Optimization)

During idle periods (no active sessions), Pernix runs background maintenance: deduplicating memory entries, consolidating similar notes, profiling user preferences, purging expired RLM run directories past retention, and — when enabled — the [Dream](internals/dream.md) introspection step. Cycles run until the full activity ladder completes; any user activity cancels them instantly and the interrupted work resumes next cycle.

| Setting | Default | Description |
|---|---|---|
| `snooze_enabled` | `true` | Enable/disable idle-time background maintenance. |
| `snooze_interval_ticks` | `10` | How often snooze checks whether to run (each tick is approximately 60 seconds, so default = every 10 minutes). |

---

## Candor (Operational Memory Add-on)

Integration with the Candor memory substrate: calibrated reliability tracking for tools, turns, and reflect verdicts, with an auditable evidence ledger. The `candor` package installs with `pip install -r requirements.txt` (vendored wheel in `vendor/`; rebuild with `pip wheel --no-deps -w vendor/ /path/to/Candor` after upstream changes). Toggles live in Settings → Candor (Operational Memory). How it works: [internals/candor.md](internals/candor.md); design history: [dev/candor-integration-plan.md](dev/candor-integration-plan.md).

| Setting | Default | Description |
|---|---|---|
| `candor_enabled` | `false` | Master switch. Turn-end emission, snooze maintenance, and the scout brief toggle hot; the agent tools (`predict_reliability`, `why_reliability`, `reliability_questions`) register at startup only, so enabling them needs a restart. |
| `candor_scout_brief` | `true` | Inject the `[OPERATIONAL INTEL]` exception report (degraded tools, discovered conditions, open questions) into scout's pre-load context. |
| `candor_max_obs_per_turn` | `200` | Safety valve on how many observations one turn may emit. |
| `fetch_routing_enabled` | `true` | Candor-driven fetch rerouting (needs `candor_enabled`): `http_get` consults the calibrated per-domain `fetch_ok` rate and refuses domains that historically fail, pointing the agent at `browse_web` instead of burning a timeout on a bot wall. `force=true` on the call overrides. |
| `fetch_routing_min_obs` | `8` | Minimum observations on a domain before rerouting — below this the rate is noise and never reroutes. |
| `fetch_routing_threshold` | `0.40` | Reroute when the calibrated probability of `fetch_ok` falls below this. |

The store lives at `data/candor/` (machine-local, not in `settings.json`).

---

## RLM (Recursive Processing Add-on)

Recursive Language Models (arXiv 2512.24601): the agent processes inputs far beyond the context window — huge files, corpora, transcripts, log dumps — by writing code in a sandboxed child REPL that holds the input as a variable and delegates chunk work to budgeted sub-LLM calls. Adapted from the MIT-licensed reference implementation (no new dependency). Toggles live in Settings → General → RLM (Recursive Processing). RLM adds **no model roles of its own** — it reuses Primary and Background (see below). Architecture + security posture: [internals/rlm.md](internals/rlm.md).

| Setting | Default | Description |
|---|---|---|
| `rlm_enabled` | `false` | Master switch. Caps apply hot; the `rlm_process` tool registers at startup only, so enabling/disabling needs a restart. |
| *(RLM root)* | — | Runs on Primary (`llm_model`). |
| *(RLM sub-calls)* | — | Run on Background (`background_model`), falling back to Primary. The `model=` argument on `rlm_process` overrides this for one run. |
| `rlm_max_iterations` | `20` | Root REPL turns per run before best-effort synthesis. |
| `rlm_max_depth` | `1` | `1` = sub-calls only; `2`–`3` lets `rlm_query()` spawn nested RLM runs. |
| `rlm_max_subcalls` | `50` | Total sub-LLM calls per run (one ledger shared across recursion depths). |
| `rlm_max_concurrent_subcalls` | `3` | Parallel sub-calls (the global LLM scheduler still applies underneath). |
| `rlm_timeout_seconds` | `900` | Wall clock per run; the child process group is killed at the deadline. |
| `rlm_run_retention_days` | `30` | Age after which snooze purges `data/workspace/rlm/<run_id>/` dirs and their DB rows. |
| `worker_session_retention_days` | `30` | Worker sessions (spawn_worker) not updated within this window are deleted, except any a parent is still waiting on. The worker's result already lives in the parent transcript. Previously never pruned. |
| `dream_hypothesis_retention_days` | `90` | Dream hypotheses in a terminal status (refuted, expired, archived, promoted) older than this are deleted; pending and validated rows are never touched. Previously never pruned (~57 rows/day on the live box). |

---

## Dream (Introspection Add-on)

Idle-time introspection: during snooze the agent examines its own memory, Candor evidence, and post-mortems; generates typed hypotheses about itself (contradictions, stale memory, ineffective lessons, tool patterns); validates them against recorded outcomes; and writes a periodic report to `workspace/dreams/`. Hypotheses influence nothing until validated. Each day of dreaming narrates itself into a read-only Dream journal session in the sidebar. Toggles live in Settings → Dream (Introspection); all apply hot. How it works: [internals/dream.md](internals/dream.md).

| Setting | Default | Description |
|---|---|---|
| `dream_enabled` | `false` | Master switch. Off removes the dream activity from the snooze cycle entirely. |
| `dream_hypotheses_per_cycle` | `6` | Cap on new hypotheses per dream step. |
| `dream_max_pending` | `200` | Validation backlog cap — above it, generation pauses until validation drains. |
| `dream_validation_replays_per_day` | `8` | Budget for counterfactual scout replays (the most expensive validation). `0` disables replay validation. |
| `dream_report_interval_days` | `7` | Cadence for `workspace/dreams/DREAM-<date>.md` reports. |
| `dream_journal_retention_days` | `14` | Days of Dream journal sessions kept (one per day). |
| `dream_rlm_probe` | `false` | Deep cross-file probes over the whole memory corpus via [RLM](internals/rlm.md) — also requires `rlm_enabled`. |
| `dream_rlm_probe_interval_days` | `7` | Minimum days between deep probes. |

---

## Telos (Teleological Layer Add-on)

A non-convergent drive with correction machinery over the whole loop: turn anomalies mint Questions, an idle-time SOUP generates cross-domain hypotheses (only falsifiable ones execute; the rest wait in a speculation pool), and slow loops audit the goal hierarchy daily — re-ranking strayed goals (Ordo), detecting Goodhart binding, measuring goal discharge (Hevel), reconciling the agent's self-story against its append-only trace ledger, and keeping exploration entropy above floor. All state is markdown+YAML under `data/telos/`. Toggles live in Settings → Telos (Teleological Layer); everything applies hot except tool registration (restart). How it works: [internals/telos.md](internals/telos.md); derivation: [dev/telos-spec.md](dev/telos-spec.md).

| Setting | Default | Description |
|---|---|---|
| `telos_enabled` | `false` | Master switch. Off: no directories created, snooze Activity 16 skipped, cron never installs, post-task hook inert. Registers the `telos_status` / `telos_ask` tools (restart). |
| `telos_root_text` | `"What is actually going on here, and what is it for?"` | The root objective — a question with no satisfaction predicate. Re-expressing it is an operator-only edit. |
| `telos_schedule` | `0 4 * * *` | Daily slow-loop cron (UTC): retirement sweeps, with the weekly entropy-control block watermarked inside it. |
| `telos_serendipity_budget` | `0.15` | Share of scheduler throughput reserved for high-surprise questions with no goal relevance. |
| `telos_eig_floor` | `0.15` | Testability-gate admission floor on expected information gain. |
| `telos_hypotheses_per_question` | `3` | SOUP output cap per generation pass. |
| `telos_max_gated_backlog` | `12` | Above this many gated hypotheses, every idle step evaluates instead of generating. |
| `telos_max_eval_tokens` | `20000` | Gate ceiling on a hypothesis's estimated evaluation cost. |
| `telos_question_max_attempts` | `3` | Dry generation passes before a question is abandoned. |
| `telos_anomaly_remint_cooldown_days` | `7` | One anomaly line of inquiry per source (`tool:X`, `reflect:retry`, …) per window — stops the same flaky tool minting a near-identical question every day. `0` disables. |
| `telos_soup_context_entries` | `10` | Memory entries in the band-sampled SOUP context. |
| `telos_soup_retention_days` | `30` | Age after which an unexamined pooled hypothesis is archived `expired` into `soup/archive/` — moved out of the loop's scans, never deleted. 0 = keep it in the pool forever. |
| `telos_soup_archive_retention_days` | `180` | Hard-delete horizon for `soup/archive/` — the only place a hypothesis file is unlinked. Long by design: the archive is the calibration review's forensic record. 0 = keep forever. |

---

## Autonomy (Gates, Goals, Heartbeats, Session Kernel)

The long-running-autonomy substrate: deterministic gates Reflect cannot overrule, persistent cross-turn goals with budgets, heartbeats steered into running work, and a persistent per-session Python REPL. All off by default; a goal + gates + a heartbeat compose into an autonomous task. Toggles live in Settings → Autonomy (Gates, Goals, Heartbeats, Kernel). How it works: [internals/autonomy.md](internals/autonomy.md).

| Setting | Default | Description |
|---|---|---|
| `gates_enabled` | `false` | Deterministic gates: user-authored shell checks that run before Reflect; a failing gate mechanically clamps a `pass` verdict to `retry`. Registers the `add_gate` / `list_gates` / `remove_gate` tools (restart). |
| `goals_enabled` | `false` | Persistent cross-turn goals with token/time/continuation budgets; only `goal_complete` finishes one. Registers the `goal_create` / `goal_status` / `goal_update` / `goal_complete` tools (restart). |
| `heartbeats_enabled` | `false` | Recurring instructions steered into running work at round boundaries. Registers the agent's `set_heartbeat` / `clear_heartbeat` / `list_heartbeats` tools (restart) and enables the user heartbeat API. |
| `session_kernel_enabled` | `false` | Persistent per-session Python REPL (the `repl` tool, registered at startup): variables survive tool rounds, turns, compaction, and — via snapshots — restarts. |
| `kernel_idle_seconds` | `1500` | Idle seconds before a kernel is snapshotted and reaped. Deliberately below the 1800 s session reap so a kernel never outlives its session as an orphan process. |
| `kernel_snapshot_max_bytes` | `268435456` | Cap (256 MB) on a kernel's dill snapshot; oversized namespaces skip the offending variables and report them. |
| `kernel_max_concurrent` | `3` | Live kernels across all sessions; beyond the cap the least-recently-used idle kernel is snapshotted and reaped. |
| `large_result_bind_threshold` | `20000` | Tool results larger than this (chars) are loaded into the kernel as `tool_result_<n>` variables, with only a head/tail stub in context. Applies to every tool except `repl`, `rlm_process`, and the conversational ones. |
| `kernel_rss_warn_bytes` | `4294967296` | Kernel RSS above this (4 GB) appends a memory-watermark warning to cell results, so the agent sees the pressure before the hard 8 GB rlimit kills the process. |

---

## Canary Suite

Golden-task canaries: canned tasks with deterministic gates, run headlessly through the full pipeline (scout → agent → gates → reflect) in isolated, tool-allowlisted temp workspaces. **Change-driven**: canaries run when something they cover changes — an adaptive batch (a targeted post-batch probe), a skill edit (via `covers:`/verify blocks), a model swap or a deploy (full sweeps) — plus a small nightly heartbeat that keeps every active canary's history warm. The Adaptive Layer's tripwire reads the post-batch results per task. Zero rows, zero behavior change while off. Toggles live in Settings → Canary Suite; runs and full CRUD (create, edit, park, retire, one-off probes) surface in the Explorer's Canary tab. How it works: [internals/canary-and-adaptive.md](internals/canary-and-adaptive.md).

| Setting | Default | Description |
|---|---|---|
| `canary_enabled` | `false` | Master switch for the suite: sweeps, the `canary_run` / `canary_status` tools, and the API. |
| `canaries_dir` | `data/canaries` | Directory scanned for `<name>/CANARY.md` task definitions. |
| `canary_schedule` | `0 3 * * *` | Cron expression for the nightly heartbeat (default: 03:00). |
| `canary_heartbeat_per_night` | `2` | How many least-recently-run active (non-parked) canaries each heartbeat runs. |
| `canary_post_batch_max` | `4` | Cap on canaries per post-batch probe: the ones covering the batch's edit kinds first, `sentinel`-tagged ones riding along. |
| `canary_retention_days` | `30` | Age after which Snooze prunes `canary_runs` rows and their sessions. |
| `canary_baseline_runs` | `5` | The green precondition: a canary may testify against a batch only when this many trailing runs before the apply all passed. |
| `canary_regression_delta` | `0.15` | Drift threshold for the **passive** post-mortem signal only (the canary signal is per-task, not a rate delta). |
| `canary_auto_admit` | `true` | Auto-admit machine-proposed canaries whose gate commands pass an allowlist proof plus the vetting runs; specs the machine can't prove safe still queue for human review. |
| `canary_auto_maintain` | `true` | Maintenance sweep: promotes vetted canaries, tags flapping ones flaky, parks long-green ones, syncs skill verify blocks, retires exhausted probes. A canary whose latest run failed is never auto-mutated — except that a red run un-parks. |
| `canary_vetting_runs` | `3` | Consistent runs required to promote a canary out of vetting. |
| `canary_park_after_passes` | `25` | Consecutive passes before a canary is parked (off the heartbeat, still in the suite; any red run un-parks it). Replaces `canary_retire_after_passes`. |
| `canary_purge_after_days` | `30` | Retired canaries (DELETE API, exhausted probes) sit in `.retired/` this long before deletion — the undo window. |
| `canary_max_suite` | `24` | Auto-admission stops at this suite size (the human path stays open). |

---

## Adaptive Layer

A governed, machine-editable policy store — routing hints and prompt notes the agent may auto-apply at idle (with full history and exact rollback), and policies that route through the proposal queue: a **veto window**, not an approval gate. Content is gated at the mouth (v3.1): every machine edit passes an actionability lint (instructions in, narrative out), per-entry usage is measured (scout and reflect citations), unused entries retire on their own, and both you and the agent have direct authorship paths. A pending proposal you don't reject applies itself after `adaptive_auto_approve_after_hours`; validation happens after application, on observed behavior (tripwire, post-batch canary sweeps), with rollback as your standing veto. While off: zero rows, compiler output byte-identical, no producer emits edits. Toggles live in Settings → Adaptive Layer; entries, events, and proposals surface in the Explorer's Adaptive tab. How it works: [internals/canary-and-adaptive.md](internals/canary-and-adaptive.md).

| Setting | Default | Description |
|---|---|---|
| `adaptive_enabled` | `false` | Master switch for the store, the producers, and the compiler/scout consumption. |
| `adaptive_auto_apply` | `true` | Auto-apply low-risk kinds (`routing_hint`, `prompt_note`) during idle windows; high-risk kinds always route through the proposal queue. Run the canary suite for at least a week before relying on this. |
| `adaptive_auto_rollback` | `false` | Promote a canary-regression tripwire hit to automatic rollback. Off until the metric earns trust — a hit otherwise only flags the batch `suspect`. |
| `adaptive_max_entries_per_kind` | `24` | Cap on active entries per kind. |
| `adaptive_max_auto_applies_per_day` | `24` | Cap on auto-applied batches per day. |
| `adaptive_edit_cooldown_hours` | `24` | Minimum hours between machine edits to the same entry. |
| `adaptive_tripwire_window_turns` | `20` | Organic turns after a batch over which post-mortem retry drift is watched (the passive tripwire; canary-stamped post-mortems excluded). |
| `adaptive_max_pending_proposals` | `200` | Review-queue cap; at the cap new proposals are refused (the producer re-raises once the queue drains). `0` = unbounded. |
| `adaptive_max_pending_per_producer` | `60` | One producer's share of the queue, so a chatty producer cannot silence the quieter ones. `0` = unbounded. |
| `adaptive_proposal_ttl_days` | `30` | Pending proposals lapse (`expired`) after this — a proposal is a snapshot of evidence, and the producer re-raises it from current evidence if it still holds. `0` = never. |
| `adaptive_auto_approve_after_hours` | `24` | The veto window. A proposal still pending after this many hours is approved by the system itself — same apply path as a human approval, journaled, swept, rollback-able, resolved as `auto_approved` for the audit trail. Canary-suite proposals are excluded (they keep their human gate; `canary_auto_admit` is their autonomy path). `0` = human approval only. |
| `adaptive_max_auto_approvals_per_day` | `40` | Cap on veto-window auto-approvals per rolling 24h. |
| `adaptive_usage_retire_days` | `45` | Entries with zero recorded uses over this many *instrumented* days (counted from the usage epoch, stamped on the sweep's first run) are retired — journaled soft-deletes, one aggregate notification, one-click rollback. Candor-owned and human-authored entries exempt. `0` disables. |
| `adaptive_prompt_note_ttl_days` | `90` | Backstop TTL for `prompt_note` (the kind with no producer-side retirement loop). `0` = keep forever. |
| `adaptive_suspect_ttl_days` | `7` | A suspect flag raised by the passive post-mortem signal alone can never self-clear (its windows are frozen at the apply); it auto-clears with an annotation after this many days. Canary-confirmed flags are exempt. `0` = flags wait for your dismiss. |
| `adaptive_agent_notes_enabled` | `false` | The `adaptive_note` tool: the live agent may mint `prompt_note`/`routing_hint` edits the moment it learns something — content lint applies, 2/day, normal pipeline + tripwire, never `policy`. Registration needs a restart. |

---

## Shell & Tool Safety

> See also: [security.md](security.md)

| Setting | Default | Description |
|---|---|---|
| `auto_approve_dangerous` | `false` | **Read-only via API.** When `false`, dangerous tools require explicit per-invocation user approval (see below). Can only be set to `true` at startup via the `--dangerous` flag — it cannot be changed while the server is running. |
| `shell_security_mode` | `"permissive"` | `"permissive"` (default): commands are screened by the denylist scan — the command denylist (system-altering commands like `dd`, `mkfs`, `systemctl`), `sudo`, and an `rm -rf` pattern check; `shell_allowlist` is not consulted. `"strict"`: the command's first word must be in `shell_allowlist`. |
| `shell_allowlist` | *(large default list)* | First-word allowlist of commands the agent may run — consulted **only when `shell_security_mode = "strict"`**; inert under the default permissive mode. The default includes common development tools (`python3`, `git`, `grep`, `curl`, `npm`, etc.). Edit to restrict or expand. |
| `shell_timeout` | `30` | Seconds before a shell command is killed. |
| `tool_timeout` | `300` | Seconds before any tool call is killed (covers file ops, HTTP, etc.). |
| `shell_address_space_limit_bytes` | `8589934592` | Virtual address space cap (8GB) applied per shell process via `RLIMIT_AS`. Set to `0` to disable. |
| `shell_env_mode` | `"allowlist"` | How environment variables are passed to the shell: `allowlist` (only listed — the default, so provider API keys and other server secrets never reach shell children), `denylist` (all except listed), `passthrough` (inherit all — opt-in only; hands every server secret to every command). |
| `shell_env_denylist` | *(empty)* | Variables to exclude when `shell_env_mode = "denylist"`. |
| `shell_env_allowlist` | `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, plus audio/display vars | Variables to include when `shell_env_mode = "allowlist"`. |

### Dangerous Tool Approval Flow

When `auto_approve_dangerous` is `false` (the default), every tool marked `dangerous` goes through a two-step human-in-the-loop confirmation before it runs:

1. **`ask_user()`** — the agent describes the exact action it intends to take (command, URL, file path). The session suspends until you respond.
2. **`approve_dangerous_tool(tool_name, scope)`** — after you confirm, the agent registers the approval. `scope` is a short description of what was approved (e.g. `"run ps aux to list processes"`).

Approvals are **per-invocation by default** — approving `bash` for `ps aux` does not cover a later `mv /etc/passwd`. Pass `persistent=True` only for genuinely repetitive low-risk actions (e.g. browsing several pages during research) where re-asking each call would be noise.

**Previously approved scopes are remembered** in `data/tool_approvals.json`. The next time the agent calls `approve_dangerous_tool()` with the same scope, the `ask_user` step is skipped automatically. You can view and clear this file in **Settings → Security → Remembered Approvals**.

---

## Background Jobs

Detached long-compute processes via the `job_start` / `job_status` / `job_tail` / `job_kill` tools: output captured to a log file, completion durable across server restarts (exit-code sidecar), wall-clock capped via coreutils `timeout`, whole process group killed on `job_kill`. Jobs run under the same rlimits as `bash`.

| Setting | Default | Description |
|---|---|---|
| `jobs_enabled` | `true` | Register the four job tools at startup. Restart required for a change to take effect. |
| `jobs_max_concurrent` | `3` | Running jobs per session. Further `job_start` calls are refused until one finishes. |
| `jobs_default_timeout_s` | `7200` | Wall-clock cap when the caller doesn't pass one (2 h). |
| `jobs_max_timeout_s` | `21600` | Ceiling for caller-supplied caps (6 h). |

---

## Memory

| Setting | Default | Description |
|---|---|---|
| `memory_recall` | `true` | Search memory at the start of each turn and inject relevant entries into the system prompt. |
| `distill_audit_enabled` | `true` | Distillation coverage audit (a Snooze activity): sampled re-derivation of a distilled session's durable facts, checked against the store — misses are recorded and repaired instead of staying invisible to every downstream consumer. |
| `distill_audit_per_day` | `2` | Sessions sampled per UTC day. `0` disables. |
| `embedding_model` | *(empty)* | Ollama embedding model (e.g. `nomic-embed-text`) for semantic memory retrieval — setting it **is** the switch; empty keeps every search purely lexical (BM25). Vectors live in a rebuildable sidecar next to the FTS index. See [guides/memory-and-recall.md](guides/memory-and-recall.md#semantic-retrieval). |
| `embedding_batch_size` | `16` | Texts per `/api/embed` call during the background embedding sweeps that run in Snooze. |
| `embedding_fallback_model` | `BAAI/bge-small-en-v1.5` | Local CPU model (fastembed/ONNX, pulled once into `data/models/fastembed`) used while the remote embedding server is down. Its vectors live under the name `local:<model>`, so the two spaces never mix; search and the snooze sweep read whichever model is active. Empty disables the fallback; it is also inert when `fastembed` is not installed. |
| `embedding_fallback_after_minutes` | `30` | Continuous remote failure before switching to the local model (the corpus then re-embeds locally, a few hundred entries per idle cycle). |
| `embedding_fallback_recover_minutes` | `60` | The remote must answer the snooze sweep's probe for this long before Pernix switches back (and re-embeds under the remote model again). Hysteresis against a flapping server. |

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

## Voice Input

| Setting | Default | Description |
|---|---|---|
| `voice_mode` | `off` | Speech-to-text engine behind the chat mic button: `off`, `local_whisper`, `remote_whisper`, `model_direct`, or `web_speech`. |
| `voice_whisper_model` | `base` | faster-whisper model size for `local_whisper`: `tiny`, `base`, `small`, `medium`, or `large-v3`. Downloads on first use (~150MB for `base`) — pre-fetch when baking images for offline boxes. |
| `voice_remote_url` | *(empty)* | OpenAI-compatible base URL for `remote_whisper` (the server POSTs to `{url}/audio/transcriptions`). API key via the `VOICE_STT_API_KEY` env var (Settings → Voice Input → Remote STT API Key). |
| `voice_remote_model` | `whisper-1` | Model name sent to the remote transcription endpoint. |
| `voice_language` | *(empty)* | ISO-639-1 language hint for the whisper engines and browser dictation. Empty = autodetect. |
| `voice_auto_send` | `false` | Send the message automatically once dictation produces a non-empty transcript. Transcription engines only — an empty transcript (no speech detected) never sends, and `model_direct` voice notes stay manual. |
| `voice_web_speech_fallback` | `false` | Fall back to browser dictation when the chosen engine is unavailable (whisper not installed, chat model can't hear audio). Off by default: enabling it is your acknowledgment that fallback audio is processed by your browser vendor's speech service, not your machines. |

Each engine has a different privacy profile, shown as a disclaimer in
Settings → Voice Input: `local_whisper` keeps audio on the Pernix server;
`remote_whisper` uploads recordings to the endpoint you configure;
`model_direct` attaches the recording to your message so an audio-capable chat
model hears it directly (local with Ollama, remote with cloud providers);
`web_speech` sends audio to the browser vendor (e.g. Google for Chrome) and
requires internet. The mic needs a secure context — HTTPS network mode or
localhost. The mic button and Ctrl/Cmd+Shift+M (the combo Discord and
Teams use) share one gesture model: tap to toggle listening on and off,
or press-and-hold for push-to-talk — release stops and transcribes.
Esc cancels a recording without transcribing.

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
| `cron_dispatch_timeout` | `3600` | Wall-clock ceiling (seconds) on one scheduled dispatch — a cron fire or a heartbeat idle tick. A wedged unattended job fails and notifies within the hour instead of holding its slot for the old implicit `tool_timeout` × `max_tool_rounds` product. |

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
| `post_mortem_retention_days` | `90` | Days to keep synthesized post-mortem records before snooze cleans them. |
| `notification_retention_days` | `30` | Notifications older than this are pruned (snooze Activity 11 + the maintenance 24h tier) — the bell is a recent-events surface, not an archive. `0` = keep forever. |
| `notify_webhook_timeout` | `10` | HTTP timeout for `notify_webhook_url` POST (seconds). |
| `snooze_max_cycle_seconds` | `900` | Hang backstop per Snooze cycle — runaway protection, not a budget. Cycles run until the activity ladder completes; user activity cancels them instantly. Local (Ollama) background models get 4x headroom. |
| `snooze_cooldown_minutes` | `5` | Minimum idle time before Snooze starts running. |
| `snooze_dedup_interval_days` | `7` | Days between memory-dedup sweeps per file. |
| `snooze_consolidation_interval_hours` | `24` | Hours between memory-consolidation scans. |
| `snooze_consolidation_cluster_threshold` | `0.55` | Minimum pair-similarity score for two memory entries to be clustered during consolidation. |
| `reflect_retry_budget_cap_s` | `600` | Ceiling on the computed minimum-budget-for-retry threshold (seconds); prevents high `scout_timeout` values from blocking retries. |
| `shell_fsize_limit_bytes` | `2147483648` | Per-shell-process file-size write cap (2 GB) via `RLIMIT_FSIZE`. `0` disables. |
| `max_file_write_size` | `104857600` | Max bytes per `file_write` / `file_edit` / `multiedit` call (100 MB). `0` disables. |
| `max_edit_read_size` | `5242880` | Max file size for `file_edit`'s whole-file fuzzy-match path (5 MB). `0` disables. |
| `audio_model_overrides` | *(empty list)* | Force `supports_audio = true` for models where auto-detection misses audio capability. |
| `backup_keep_count` | `7` | Timestamped snapshots kept in `data/backups` by the 24h backup tier. Rotation is per-artifact (DB snapshots and memory corpora rotate independently), so a restore always has a matching pair. Clamped to 0–90 at use time; `0` disables scheduled backups. |
| `tool_executor_workers` | `32` | Threads in the tool-call pool. Tools run on their own pool so they can never occupy asyncio's default executor, which every API route needs for its DB reads. Occupants are blocked on IO, so raising it costs memory and PIDs rather than throughput. |
| `background_executor_workers` | `8` | Threads for long-running idle-time background work (dream deep probes, canary maintenance, synthesis, backups, memory dedup). Small on purpose: occupants are heavyweight and idle-time-only. |

---

## Architecture Reference

For a deep dive into the session state machine, agent turn loop, compaction algorithm, worker orchestration, and reflect/snooze internals, see:

- **[internals/state-machine.md](internals/state-machine.md)** — detailed architectural walkthrough
- **[api.md](api.md)** — REST API and SSE event reference
