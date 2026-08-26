# Changelog

A reverse-chronological summary of user-visible changes. For the full details of any entry, follow the git commit.

This is **not** a complete commit log — only changes you'd actually care about as a user. For the implementation-side history, see `git log`. For migration-induced upgrade steps, see [upgrade.md](upgrade.md).

---

## v3.0.0 — 2026-08-26

The first tagged release since v2.9.0, and the biggest. The quick tour of what's new, then what's gone. Everything new that changes agent behavior ships **off by default** unless noted.

**Long-running autonomy.** Deterministic **gates** (shell checks the Reflect quality gate cannot overrule), persistent **goals** with token/time/continuation budgets, and **heartbeats** — recurring instructions steered into running work. Composes into unattended multi-hour tasks. See [internals/autonomy.md](internals/autonomy.md).

**The session kernel.** An optional persistent per-session Python REPL (`repl` tool): variables survive rounds, turns, compaction, and — via snapshots — restarts. Huge tool results auto-bind as variables instead of flooding context. See [internals/autonomy.md](internals/autonomy.md).

**Background jobs** (on by default, `jobs_enabled`). `job_start` / `job_status` / `job_tail` / `job_kill` make detached long compute first-class: output captured to a log, completion durable across server restarts, wall-clock caps, whole-group kill.

**`view_image`.** The agent can look at images it renders or downloads: on a vision model, `view_image(path)` routes a workspace image into the next round's context as real pixels.

**Semantic memory retrieval.** Set `embedding_model` and memory search becomes hybrid BM25 + vector with `[[wiki-link]]` expansion at recall. A local CPU embedding fallback keeps recall alive when the remote embedding model goes down.

**The self-improvement stack.** The golden-task **canary suite** measures whether the agent is actually getting better or worse; the governed **adaptive layer** applies low-risk policy edits with a veto window, full history, and one-click rollback; **Telos** adds a non-convergent drive with correction machinery. See [internals/canary-and-adaptive.md](internals/canary-and-adaptive.md) and [internals/telos.md](internals/telos.md).

**RLM grows up.** Every `rlm_process` run now gets a live, read-only trace session nested in the sidebar; a run whose result was orphaned by a turn teardown is surfaced on the next turn instead of vanishing; cancels report as cancels, not failures.

**Voice input.** A mic button with four engines — local whisper, remote whisper, model-direct audio, browser dictation — each labeled with exactly where your audio goes. Plus: paste a screenshot straight into chat.

**Three model roles instead of six.** **Primary** (`llm_model`), **Background** (`background_model`), **Backup** (`fallback_model`). If you had a distinct `scout_model`, copy it to `background_model`; stale keys are ignored, not fatal. See [deployment/llm-providers.md](deployment/llm-providers.md).

**Native OpenAI-compatible provider.** Point `openai_base_url` at api.openai.com, vLLM, LM Studio, or llama.cpp; key via `OPENAI_API_KEY`.

**Defaults modernized.** `max_tool_rounds` 10 → 50, and a healthy turn that exhausts it now auto-continues once (`round_cap_auto_continue`); the context budget derives from the model's real window; prompt-cache breakpoints for Anthropic models via OpenRouter; Ollama reasoning mode is a per-role setting.

**Removed: the workflow engine.** Never used — literally zero invocations — and structurally at odds with how an agent works. Skills + workers + gates + goals + `schedule_job` cover everything it did; conversion table in [upgrade.md](upgrade.md#2026-08-12--the-workflow-engine-was-removed). Also removed: the `schedule_workflow` tool and the six-role model scheme.

The DB schema lands at **v29** (from v19); all ten migrations run automatically on first start. Action items, if any apply to you, are in [upgrade.md](upgrade.md#2026-08-26--v300).

*Credits: built by Calvin ([@calvincs](https://github.com/calvincs)) with Claude (Anthropic) pair-programming — and Pernix itself, whose reference deployment ran the field campaigns that surfaced and validated most of these changes.*

## 2026-08

**The workflow engine was removed.** `run_workflow` and its six sibling tools (seven in total), the `/api/workflows` routes, the Explorer's Workflows tab and `WORKFLOW.md` parsing are gone. It was never used — zero invocations across the reference deployment's entire history — and a step graph you have to declare before the work begins is the one assumption an agent lets you drop. Everything it did is still available from its parts: write the procedure as a **skill**, run steps with `spawn_worker` / `await_workers`, pass data through workspace files, enforce hard pass/fail with **gates**, schedule with `schedule_job`, bound long runs with **goals**. Your `data/workflows/` directory is left alone and no migration runs. If a cron job or skill of yours calls `run_workflow` by name, rewrite its prompt. Full rationale and a conversion table in [upgrade.md](upgrade.md#2026-08-12--the-workflow-engine-was-removed).

**Skill-improvement proposals moved to `/api/skills/proposals*`.** They were served from the workflows router by accident of history; they target `SKILL.md` files and are unrelated to workflows. The Explorer already points at the new paths — only external callers need updating.

## 2026-07

**Pernix can now dream: idle-time introspection that fact-checks its own memory.** A new Dream add-on — off by default, Settings → Dream (Introspection) — runs as the final snooze activity: it examines memory, Candor evidence, and post-mortems; raises typed hypotheses about itself (contradictions, stale memory, ineffective lessons, tool patterns); and tries to *falsify* them against recorded outcomes, including counterfactual scout replays of past failed turns. Nothing influences live behavior until validated — the observable output is a weekly report in `workspace/dreams/` and a read-only **Dream journal session** per day in the sidebar (purple dot, own legend filter). See [internals/dream.md](internals/dream.md). (migration v19)

**The agent now learns how reliable its own tools actually are.** A new Candor add-on — off by default, Settings → Candor (Operational Memory) — records tool outcomes and reflect verdicts into an auditable evidence ledger and gives scout an `[OPERATIONAL INTEL]` exception report before each turn: degraded tools are flagged, healthy ones are omitted, so silence means "no known problem." The agent can also answer reliability questions on demand (`predict_reliability`, `why_reliability`). See [internals/candor.md](internals/candor.md).

**Snooze cycles now run to completion instead of being cut off mid-ladder.** The old 60-second wall clock starved the tail of the maintenance ladder behind one slow model call. Cycles now run until every activity finishes; your activity (a prompt, a cron fire, shutdown) cancels them instantly — even mid-LLM-call — and interrupted work resumes next cycle. `snooze_max_cycle_seconds` (now 900) is demoted to a hang backstop. A localhost-only `POST /api/admin/snooze-cycle` triggers a cycle on demand for debugging.

**Memory consolidation no longer silently loses entries.** Fused entries keep their type, tags, and weight; entries the merge verdict omitted are rescued instead of stranded in archived files — previously unbounded silent data loss. The advertised `@tags:` recall filter now actually filters (it used to degrade to an ordinary search token), and recall output now shows each entry's age and origin (`@origin: external` marks web-derived content).

**Scout no longer hands the agent a blank report.** A scout that ran out of revision rounds could previously submit nothing — the turn proceeded planless. The final round now always retains `submit_report`, and a scout that still never submits degrades to a deterministic fallback report instead of an empty one.

**`SOUL.md` and `RULES.md` now reach the model whole.** The identity/rules/session files are injected verbatim by the context compiler instead of being excerpted by scout — so a rule you write is a rule the model sees, every turn. Long files are capped at 32K chars with a loud log warning instead of silent truncation.

**The agent can now fully analyze inputs far larger than any model's context window.** A new RLM (Recursive Language Models) add-on — off by default, enabled in Settings → General → RLM (Recursive Processing) — gives the agent an `rlm_process` tool: instead of paginating a huge file and losing the whole-picture view, the input is held as a variable in a sandboxed Python REPL and a root model writes code to slice it, delegating chunks to budgeted sub-model calls until it has one answer. Works on documents, corpora, transcripts, logs, and codebase dumps. You pick the root and sub-call models under Settings → Models → Model Roles *(superseded 2026-08: the dedicated RLM model roles are gone — the root now runs on the Primary model, sub-calls on Background)*; iteration/sub-call/time caps prevent runaway runs; model-written code runs in a separate locked-down process that never sees your API keys. Run traces land in `workspace/rlm/<run_id>/` and are auto-purged after 30 days.

## 2026-05

**Scout context now includes UTC and local time.** The scout's input and the main agent's `[TEMPORAL CONTEXT]` block both now show two timestamps: `Current time (UTC)` and `Current time (local)` (the machine's system timezone, e.g. CDT). Previously only UTC was shown, causing the agent to report the wrong calendar date for users in non-UTC timezones. The temporal context block also gained a `FINDING SESSION HISTORY` guidance note explaining when to use `list_recent_sessions` (chronological, timestamp-ordered) vs `search_sessions` (FTS5 keyword search over message content — not date-based). (commit `69bab50`)

**`search_web` and `browse_web` are now gated at registration time.** Previously both tools registered unconditionally at startup and only checked their settings flags (`web_search_enabled`, `browser_enabled`) when actually called — so the scout and tool registry always saw them as available even when disabled. They now skip registration entirely when the flag is off, so `discover_tools`, the scout's baseline search, and schema exports never surface a tool that will fail. Restart required for flag changes to take effect (the registry is built once per process). (commit `69bab50`)

**Memory entries can now be corrected or deleted in place.** Two new agent tools, `update_memory(file, epoch, content)` and `forget(file, epoch)`, let the agent fix a wrong stored fact rather than appending a contradictory entry next to it. Both are `safety_level=caution`. The store is still append-by-default for `remember`, `ingest`, distillation, and Snooze — only these explicit tools mutate. Epochs are preserved across updates so cross-references stay valid. `recall` / `search_memory` outputs now include `epoch=N` per result so the agent knows which entry to target. (commit `d9867dc`)

**Reflect retry budget gate no longer over-blocks at high `scout_timeout`.** The threshold formula `scout_timeout × 3 + 30` is now capped by the new `reflect_retry_budget_cap_s` setting (default 600 s). Previously, `scout_timeout=266 s` produced a 828 s threshold that blocked retries with 826 s of LLM budget remaining — 13+ minutes of usable time treated as "exhausted." Default `scout_timeout=90 s` behavior unchanged (300 s threshold). (commit `d9867dc`)

**Reflect now sees real tool result bodies, not just call counts.** The compact-evidence path (which only sent reflect a tool-summary with counts/failures and the agent's final response) is replaced with per-attempt-transcript visibility plus a structured **turn digest** that reflect emits alongside its verdict. The digest carries forward to the next scout on retry, so scout-N+1 plans against verbatim tool-result excerpts instead of a free-form summary. Verdict-first emission in the prompt prevents narrative drift. The legacy `reflect_full_transcript` setting is now a no-op (deprecated). New settings: `reflect_emit_digest_on_pass`, `reflect_digest_max_chars_per_excerpt`. Regression target: false-negative retries on factually-supported answers (e.g. agent crawls a URL, returns it, reflect dismisses it as hallucinated because it never saw the page body).

**search_web is now strictly Tavily-gated.** The DuckDuckGo fallback was removed. Without `TAVILY_API_KEY` in `.env`, `search_web` returns a setup hint rather than degrading silently. This avoids surprising "results aren't very good" behavior. (commit `2bd843d`)

**ask_user dialog now persists across page refresh and unblocks the agent on dismissal.** Closing or refreshing a tab while the agent was waiting for input no longer leaves the session stuck. (commit `13587fb`)

**Custom tools register into the active schema immediately after `create_tool`.** Previously needed a session restart to be visible. Now the agent can author a tool and call it in the same turn. (commit `82f9e50`)

**Custom tools are excluded from formatters and git** — the `core/tools/builtin/custom_*.py` files are gitignored and treated as user data, not source. Authoring a tool no longer pollutes the project's lint or git status. (commit `c198cff`)

**Memory search overhaul** — `prepare_fts_query()` no longer strips hyphens or short tokens (so `2026-04-27` queries work). Hybrid search adds a ripgrep fallback when FTS5 returns nothing. New `deep_recall()` tool runs LLM-backed multi-query search and returns a synthesized answer instead of raw search noise. Snooze now health-checks the index on startup so manual file edits are visible without a 6-hour delay. (commit `db95006`)

**Agent identity refactor.** The file in `data/agent/` previously called `AGENTS.md` is now **`SESSIONS.md`** and holds deployment-specific session context (timezone, recurring intents, naming conventions). Falls back to `INSTRUCTIONS.md` if absent. SOUL.md adds `Anticipatory` and `Calibrated` core traits. RULES.md gains a Proactive Behavior section. (commits `590f33b`, `49469e7`)

**Session-state state machine v2.** The 5-state legacy enum (`IDLE | SCOUTING | PROCESSING | ERROR | DELETED`) was replaced with a true 10-state machine: `IDLE_READY`, `SCOUTING`, `PROCESSING`, `COMPACTING`, `PAUSE_REQUESTED`, `PAUSED`, `CANCELLING`, `FINALIZING`, `AWAITING_USER`, `AWAITING_WORKERS`. Plus terminal markers (`COMPLETE`, `ROUND_CEILING`, `COMPACTION_FAILED`, `CANCELLED`, `ERROR`, `SCOUT_ERROR`, `BUDGET_EXHAUSTED`). All transitions go through `sessions.state_v2.transition()`, persist to `session_state_log`, and emit `session.state_changed` SSE events.

**Restart recovery for stuck sessions.** Sessions stuck in `PROCESSING` or `AWAITING_WORKERS` from a prior crash are reconciled to `IDLE_READY` immediately at startup, instead of waiting for the 5-minute reaper tick. (migration v16, commit `cf849fa`)

**`delete_skill` and `delete_workflow` tools.** Both are dangerous (require `ask_user` + `approve_dangerous_tool`); cron sessions auto-bypass the gate. (commit `9085876`) *(`delete_workflow` removed 2026-08.)*

**Workspace upload limit raised from 10 MB to 250 MB.** Per file. Useful for media-cast skills and bulk file analysis. (commit `9b883b3`)

**Session reliability fixes.** Several rare deadlock and stuck-state issues fixed across the agent loop, scout bypass logic, ask_user reentry, and reflect-as-compiler edge cases. (commits `0e83411`, `559db98`, `ae5969d`, `d81ac97`)

**Three-second rapid-fire merge.** If you send a follow-up message within 3 seconds of the previous one, Pernix folds it into the running turn instead of starting a new one. Prevents the "I sent two messages, the agent got confused" bug.

---

## 2026-04 (state machine + reliability wave)

**Reliability & observability wave** — bounded retries, scout retry-on-empty-approach, reflect failure classification, post-mortem records, snooze health checks.

**Concurrency and safety hardening** — `_spawn_lock` in worker spawner to guard TOCTOU, semaphore enforcement for both Ollama and OpenRouter, llm_session_timeout cap.

**Memory expansion + DB integrity** — FTS5 index isolation, BM25 score thresholds, dedup interval, consolidation threshold tuning.

**Auth and security hardening** — Bearer token rotation, localhost auth bypass, SSRF blocks for RFC-1918 ranges in `http_get` and `browse_web`.

**Network and mobile mode** — `network_enabled` flag, auto-generated self-signed cert, QR-code login flow, Web Push via VAPID.

**Earlier April** — many smaller changes around tool safety levels, the dangerous-tool approval gate, snooze tuning, and prompt-acceptance policy.

---

## Database migrations (chronological)

The DB schema is at v29. Each migration runs automatically on next start. Unless you've made manual changes to `data/sessions.db`, you don't need to do anything — Pernix migrates forward in place.

| Version | Description |
|---|---|
| 1 | Initial schema (sessions, messages, artifacts, token_usage, questions, notifications) |
| 2 | Snooze support (snooze_state table, snooze_reviewed_at column) |
| 3 | Messages FTS5 for cross-session search |
| 4 | Merge orchestrator session-type into normal |
| 5 | Add metadata column to messages, session_role index |
| 6 | Add subtitle column, reset broken thinking-process titles |
| 7 | Add project + workspace_path to artifacts (unified workspace) |
| 8 | Add latency_ms to messages (tool execution tracking) |
| 9 | Add push_subscriptions for Web Push VAPID |
| 10 | Add post_mortems for reflect-as-compiler artifacts |
| 11 | Add scout_signals for snooze-curated observations |
| 12 | Add post_mortems.synthesized_at watermark |
| 13 | Add session_state_log for state machine forensics |
| 14 | Add workflow_runs and skill_improvement_proposals tables |
| 15 | Session-origin proposals + trial-use tracking |
| 16 | Persist v2 state and watched_worker_ids on sessions for restart recovery |
| 17 | Add pinned flag on sessions for sidebar pinning |
| 18 | Add rlm_runs audit index for RLM (recursive processing) runs |
| 19 | Add dream_hypotheses and dream_reports for Dream (introspection) |
| 20 | RLM run view sessions (`rlm_runs.ui_session_id`) |
| 21 | `cron_runs.fire_time` — claim-before-deliver cron scheduling |
| 22 | `gates` table for deterministic gates |
| 23 | `session_goals` + `token_usage.goal_id` for persistent goals |
| 24 | `canary_runs` for the golden-task canary suite |
| 25 | `adaptive_*` tables for the adaptive layer |
| 26 | One-active-goal-per-session unique index (older duplicates retired) |
| 27 | `jobs` table for background jobs (`job_start`/`job_status`/`job_tail`/`job_kill`) |
| 28 | Questions become an audit trail (`answer`, `answered_at`; rows kept) |
| 29 | `rlm_runs.surfaced_at` — orphaned-run surfacing (history backfilled) |

---

## Earlier history

For history before 2026-04, see `git log`.

---

## How this file is maintained

This changelog is a curated companion to:

- **`db/database.py:MIGRATIONS`** — every schema migration with a one-line description.
- **git log** — full commit history.

If you've just made a change with user-visible impact, please add an entry here in the same PR. Keep it short.
