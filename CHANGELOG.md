# Changelog


## Unreleased

- fix(reflect): verdict-calibration pass from the 2026-08-27 audit — 63 non-pass verdicts over 5 live days read one by one; ~⅔ were justified, the rest clustered into four repeatable over-strictness patterns, each now addressed:
  - **Plan-literalism (prompt):** the `deliverables` field instruction told the grader to "grade each plan item", which dragged verdicts back toward scout's invented artifacts even though the plan-literalism rules forbid it (field case: a complete inline answer retried because scout's plan wanted a report file the user never asked for). The instruction now states outright that an unmet plan item the user's request doesn't require cannot justify a non-pass verdict.
  - **Punitive process retries (prompt):** completed work is never retried for efficiency/procedure violations (call-cap breaches, forbidden re-reads, stray formatting) — those are pass-with-lessons. Field case: a turn that delivered everything was retried over a 2-call budget breach, and the retry attempt — with nothing left to do — was then failed for "not verifying" work its charter forbade it from re-reading.
  - **Escalate-as-messaging (prompt):** escalate grades the TURN, not the situation — a cron run the verdict itself called "delivered cleanly" was escalated to surface a stale-thread question, poisoning verdict stats with a failure that never happened. Completed turns pass; open questions go in `experience.note`.
  - **Verifier blindness (prompt + mechanics):** a verdict whose only nameable failure is that the *verifier* couldn't see the evidence (elided tool bodies, unrenderable image bytes, sandboxed workspaces) grades pass with low confidence. Mechanically: `_build_evidence` now lists the session's EFFECTIVE workspace (`workspace_override` honored, labeled as a sandbox) instead of always the shared one — a job test-run had been escalated for "ignoring" real-workspace files it could never see.
  - **Materiality floor (mechanics):** new `reflect_nonpass_confidence_floor` (0.5; 0 disables; Settings → Reflect) — a retry/escalate the grader explicitly rates below the floor downgrades to pass-with-lessons, matching the prompt's own "<0.5 = ambiguous" definition. Coerced/malformed grades and grades that omit confidence stay conservative.

- feat(adaptive,scout,synthesis,llm): outcome-driven retention + a real task taxonomy + decoupled cost/latency channels — three lessons adapted from the JIT-Agent paper (arXiv:2608.25593), scoped hard to measure-and-advise: **nothing here routes, switches models, or changes execution**. The pieces:
  - **Failure-dominated retirement.** The adaptive value sweep now reads the OUTCOME half of the `adaptive_entry` signal, not just usage: an entry with ≥ `adaptive_harmful_retire_min_uses` (5) attributed outcomes whose success share is below `adaptive_harmful_retire_max_success` (0.3) retires even though it is used. Usage-only retention had the perverse edge — a harmful hint cited every turn was immortal *because* it was cited. No age/epoch gate (the outcomes are the observed window); candor/user sources stay exempt; deletions stay journaled with one-click rollback. The scout hints block also re-ranks by Laplace-smoothed success share `(s+1)/(n+2)` (unattributed entries neutral at 0.5) before usage, so when the 12-line cap bites, a reliable hint outranks a much-cited failing one.
  - **Task-type taxonomy.** Scout classifies each turn (`task_type`: research | coding | data_analysis | writing | ops | conversational — advisory statistics label, clamped against one shared enum on both parse paths) and reflect stamps it as the post-mortem's `task_category`, falling back to the legacy `execution_mode` stamp for reports that predate the field. `model_route` counters and the `[MODEL ROUTING INTEL]` brief therefore aggregate per real task type instead of per execution mode (whose two live values made every category read "inline"). The brief drops rows whose counters haven't moved in 45 days, so legacy-keyed subjects age out without a migration.
  - **Decoupled resource channels.** Reflect stamps `turn_metrics` (tokens + LLM calls + wall-clock, retries included, anchored on the turn's user message) into every post-mortem; synthesis accumulates per-(model, category) averages in the signal payload (read-merge-write — metric-less observations preserve the totals); the routing brief renders them as "avg ~Nk tok, ~Ns/turn" context. Reward stays the primary signal; the resource channels are observability only.
  - **Fallback-burn watch.** A new snooze activity encodes the 2026-08-19 silent-reroute incident (primary provider key died on rebuild → every call billed to the paid fallback for days) as a standing check: when `fallback_model` served ≥ `fallback_burn_alert_share` (25%) of the trailing 24h's tokens with at least `fallback_burn_min_tokens` (50k) of volume, one high-urgency notification/day fires naming the share, volume, and the compose-level-.env fix. Watch-only; `share=0` disables.
  - **cost_estimate finally written.** `token_usage.cost_estimate` was a dead column (never computed). The stream ladder now prices each usage frame from the new `model_prices` setting (`{model_id: {"in": $/Mtok prompt, "out": $/Mtok completion}}`, exact-id match only); unpriced/local models keep NULL, and the existing per-session cost sums light up on their own.

- feat(scheduling,harness,ui): the strengths-spec follow-up batch — closing the gaps the first batch shipped with, plus Feature 7. The pieces:
  - **Job validate + test-run** (spec Feature 7). Creating or editing a scheduled job validates the spec (cron parse, non-trivial prompt, `allowed_tools` exist — hard errors; unknown model — warning) and stamps the result on the job. `test_job` / `POST /api/jobs/{name}/test` dry-runs the prompt ONCE in a throwaway temp workspace under the job's own model + allow-list (canary-runner mechanics, 5-minute bound), records **no** cron_runs row, keeps the transcript as a `Job test: <name>` session, flags an `ask_user` call as the hang it would be unattended, and lands the outcome as a `job.test_done` event + bell notification + a per-job `last_test` stamp. The jobs panel gains valid/invalid/unvalidated + test badges and a per-job **test** button.
  - **Deploy detection survives same-version rebuilds.** The boot stamp's git-sha half never existed inside the image (no .git baked), so every rebuild between releases read as "no deploy" and the full canary sweep never fired. A content hash over the shipped code (py/js/css/html, deterministic) now stands in when git is absent — the stamp changes exactly when the code does, hand-patched containers included.
  - **The nudge is measured.** Each forced follow-up records whether the agent then acted or re-idled — one aggregate `scout_signals` row (`forced_followup`/`global`) plus a `turn.forced_followup_outcome` event — so a week of live traffic answers "is this feature earning its keep" with one query.
  - **Dead workers get a UI home.** The worker strip now keeps up to 4 recently-finished workers as dimmed chips (kind + termination reason) with a ↻ revive button wired to the resume endpoint — revival was tool/API-only before.
  - **Gate hardening**: `enum` membership is enforced (after coercion, so `"5"` still matches an integer enum) and array parameters validate their scalar item types with per-element coercion — both reject in-round with the constraint spelled out.
  - **Resume parity**: `resume_worker` gains `auto_resume_parent` (watch-set registration, same contract as spawn) and extends the parent's LLM wall-clock on revival so revive-then-await doesn't die on the parent's spent clock.

- feat(orchestration,harness): four capability adds from the strengths-spec review (DB schema → **v31**: `sessions.model_override` + `sessions.worker_kind`). The pieces:
  - **Typed worker kinds.** `spawn_worker(kind=...)` selects a named bundle — role preamble, an *exclusive* tool allowlist (enforced at the existing two charter points: schema intersection + executor refusal), a default model, and verification criteria reflect grades against. Built-ins: `research`, `code`, `explore`, `debug`, `transform`; operators override or add kinds via `data/worker_kinds/<name>.json` (re-read per spawn, `"model": "background"` resolves the Background role). `research`/`explore` also get a cheap deterministic gate in `get_worker_result` (`# KIND GATE` warning when a summary names zero sources / zero file citations). `retry_worker` replacements inherit the kind; `worker.started` events, `check_workers` lines, worker-strip chips and the `/workers` API all show it.
  - **Resumable workers.** `resume_worker(worker_id, note?)` unifies "bring the worker back": paused → released (unchanged); cancelled / errored / round-capped / reaped / restart-lost → **revived** from persisted state — history rehydrates (compacted if long), the kind allowlist and pinned model are restored on `get_or_create` (the v31 columns), a vanished model falls back to the default with a model-visible note, the stale summary stamp is cleared so an old `# CANCELLED` header can't shadow the new result, the worker re-attaches to its parent, and a continuation turn starts carrying the note. Emits `worker.resumed`; `POST /api/sessions/{id}/workers/{worker_id}/resume` revives too (optional `{"note"}` body) and now checks parentage against the DB row so it works after a restart.
  - **Forced follow-up nudge.** A turn that ends with a future-intent tail ("Next, I'll update the settings file.") and no tool calls gets one bounded in-turn nudge naming the unfinished item (active goal objective, else the model's own announced intent) instead of ending the turn and paying for a reflect retry. Deliberately narrow trigger: last-two-sentences scope, trailing questions and courtesy closers ("let me know if…") never fire it. `forced_followup_enabled` (on) / `forced_followup_max_per_turn` (1, bounds 0–5), surfaced in Settings → Agent; emits `turn.forced_followup`.
  - **Pre-execution argument validation.** The tool-call gate now type-checks arguments against the live JSON schema before dispatch: numeric strings / `"true"` / scalars-for-strings are coerced with a `[note: coerced…]` prefix on the tool result, unknown parameters are dropped with a note instead of reaching the tool, and uncoercible mismatches are rejected in-round with the expected type spelled out — no more burning a full round on a TypeError-shaped tool error. Gate actions (alias rewrites included) emit `tool.call.intercepted {action, reason}`; alias rewrites get a visible system line in the UI.
- feat(adaptive,dream,telos): the value redesign — content gates, a usefulness signal, self-curation, and the TELOS carve, from the 2026-08-27 live audit (21 of 24 policy slots held Dream narrative complaints auto-approved unread; ~2,300 tokens of adaptive content in every turn, uncapped; no per-entry benefit signal anywhere; TELOS 89% question abandonment with a goal tree of one root node). No migrations. The pieces:
  - **The actionability floor.** `core/adaptive/lint.py` under every machine producer: narrative shapes refused, negative tool claims require the fix clause, policy/hint content must carry a directive. Dream's two adaptive channels pass a promote-time rewrite gate (imperative rule or `reported:not-actionable`, terminal, finding stays in the dream report); refine's contract gains Do-NOT-capture rules + a worked example + a mechanically-enforced confidence floor and 2-edit cap; telos hints drop the "Supported hypothesis" framing and must pass the lint or stand as claims. One-shot cleanup script `scripts/adaptive_cleanup_narratives.py` retires the standing pre-lint noise (journaled, rollbackable).
  - **The usefulness signal.** Rendered entries carry ids; scout echoes `used_hints` (counted once at the fresh-report seam), reflect cites `cited_policies` against a new id-carrying evidence section; both land as `adaptive_entry` rows in `scout_signals` (no migration) and surface as usage badges in the Adaptive tab.
  - **Self-curation.** `retire_unused_entries`: zero-use entries retire after `adaptive_usage_retire_days` (45) of *instrumented* life (usage epoch stamped on first run — pre-instrumentation entries get a full observed window); `prompt_note` gains its first retirement loop (90d TTL backstop). Passive-only suspect flags auto-clear after `adaptive_suspect_ttl_days` (7) — they could never self-clear by construction. Cap-reached notifications dedup per producer per day; approved-proposals-that-applied-nothing annotate their rationale. Rendering is capped: scout hints ranked by usage, 12 lines/1.6k chars with a truncation marker (making `search_adaptive`'s trigger real); the agent block capped at 12 policies with deterministic source priority (user > refine > candor > telos > dream), prefix-cache-safe.
  - **Authorship.** `POST /api/adaptive/entries` + an Adaptive-tab form (human: immediately active, journaled, deliberately unlinted) and the `adaptive_note` agent tool behind `adaptive_agent_notes_enabled` (lint, 2/day, prompt_note+routing_hint only, normal pipeline + tripwire).
  - **Carves.** `worker_spec` removed end-to-end (fully-built consumption, zero live rows, no reachable producer; −2 DB reads/compile). TELOS loses its goal-DAG half — ordo/binding/hevel/reconcile/discharge (~950 LOC + 6 settings + the goals API/tab) — all provably no-ops with a root-only tree; deletion also removes the binding-ack daily-renotify bug and the never-written `closed` question state. **Two regression tests are deleted with the modules they pinned:** `test_2026-08-07_telos_reconcile_bounds_check` and `test_2026-08-07_introspective_measurement_honesty`. Old `data/telos/ledgers/first_person/` directories on disk are inert and may be removed by hand.
  - **TELOS yield + perf.** Tool-failure questions mint only when Candor has no record (kills the abandoned-question class), open questions cap 120→24, one OPEN line of inquiry per source; the turn-end hook does one corpus scan instead of six, `TelosStore.open()` ensures dirs once per process, the compiler's drive block is fully behind its 60s TTL.
  - **System-wide hygiene.** Notifications get retention (`notification_retention_days`, pruned from snooze + the maintenance tier), an opt-in per-day `dedup_key`, and a bounded read. The compiler mtime-caches directive files (SOUL.md was read twice per compile, up to 100 reads per turn).

- feat(canary): the canary suite becomes **change-driven** — a ground-up redesign after a 15-day live audit showed the suite saturated (99% pass rate), 80% of run volume re-testing tasks nothing had touched, and the tripwire's canary signal mathematically unable to fire (one failure among 8 canaries = a 12.5% drop against the 0.15 aggregate delta; the armed auto-rollback had never run once). The DB schema lands at **v30** (`canary_runs.outcome` + `error` — timeout/error/noop stop masquerading as gate failures; legacy failed rows keep outcome NULL and never feed the tripwire). The pieces:
  - **Per-task tripwire.** A non-flaky canary whose trailing `canary_baseline_runs` (now 5) runs before the apply were all green testifies on its own: a post-batch `gate_fail` is confirm-rerun once inside the same sweep; two gate_fails = confirmed regression (auto-rollback eligible), one = suspect-only. Timeouts/errors/noops neither trip nor certify. `canary_regression_delta` survives only for the passive post-mortem signal.
  - **Heartbeat instead of nightly full sweeps.** The scheduled job runs the `canary_heartbeat_per_night` (2) least-recently-run active canaries. Post-batch probes are targeted: canaries whose `covers:` matches the batch's edit kinds first, `sentinel`-tagged ones riding along, capped at `canary_post_batch_max` (4). Full sweeps (model swap via either switch path, deploy detection via a boot version stamp, "Run all") run everything including parked canaries and retry instead of being eaten by the sweep lock.
  - **Parking replaces cadence demotion.** After `canary_park_after_passes` (25, renamed from `canary_retire_after_passes` — a hand-saved old value silently reverts to the default) consecutive passes a canary is parked: off the heartbeat, still in the suite, still coverage/full/manual-run, auto-unparked by any red run. The 2026-08-07 "retirement starved the tripwire" regression pin is consciously revised to the successor invariant: parked, never removed. `canary_max_concurrent` (dead config) is deleted.
  - **Full lifecycle control.** CRUD API (`POST /api/canary`, `GET/PUT/PATCH/DELETE /api/canary/{name}`, `POST /{name}/reviewed`) and matching Canary-tab controls: create (allowlist-proof verdicts as warnings, never blockers), edit, park/unpark, mark reviewed, retire into `.retired/` with the purge grace window. One-off **probes** (`max_runs`/`expires`) run, report a pass/fail tally notification, and retire themselves — exempt from the Goodhart lock because retirement-with-tally IS the report.
  - **Skills finally get measured.** A sha256 watermark per SKILL.md (`snooze_state['skill_hash:*']`) detects every mutation path including hand edits; a changed skill fires one targeted sweep of its covering canaries at idle. A skill can embed its own behavioral test as a `verify:` block, materialized as the managed canary `skill--<name>` and resynced/retired with the block. Verify gates execute on the host and SKILL.md is machine-editable, so they must pass the same allowlist proof as canary auto-admission.
  - **Canary sessions are tool-allowlisted** (computation + reads only, enforced at the three existing charter points): machine-authored prompts and injected skill bodies cannot reach workers, jobs, notifications, or skill/tool/memory mutation. The executor's allowlist refusal wording is generalized accordingly.

## v3.0.0 — 2026-08-26

Pernix 3.0 — everything since v2.9.0 (2026-06-11) in one tagged release. The headline arc: **long-running autonomy** (deterministic gates, persistent goals with budgets, heartbeats), the **persistent session kernel** (a per-session Python REPL whose variables survive turns, compaction, and restarts), **RLM recursive processing** with live trace visibility, **semantic memory retrieval** (hybrid BM25 + vector with wiki-links), the introspection-and-self-improvement stack (**Candor**, **Dream**, the **canary suite**, the governed **adaptive layer**, and **Telos**), **background jobs** for detached long compute, **voice input**, **view_image** (the agent can look at images it renders), a native **OpenAI-compatible provider**, and model roles consolidated to three (Primary / Background / Backup). Removed: the never-used **workflow engine** (see docs/upgrade.md for the conversion table) and the six-role model scheme. The DB schema lands at **v29**; migrations run automatically on first start.

Credits: built by Calvin ([@calvincs](https://github.com/calvincs)) with Claude (Anthropic) pair-programming the whole arc — and Pernix itself, which ran the field campaigns on the reference box, surfaced its own failures, and validated the fixes.

- feat(tools): view_image — the agent can look at what it renders. The compiler inlines image bytes for exactly one location (an `[attached:]` reference in the latest user-role message, vision models only) and every agent-side path was text, so agents ground ASCII coordinate tables for hours while the answer was visible at a glance in their own PNG renders (ARC-2 field case 136b0064). `view_image(path)` validates the file (allowed roots, extension, inline byte budget) and inserts a clearly-labelled synthetic user-role note carrying the `[attached:]` reference — next round that note is the latest user message and the existing battle-tested expansion delivers real pixels, with no compiler or provider changes; the label states it is harness-injected on the agent's own request so reflect never quotes it as the human. Follow-up fix, same release: the note now stamps `metadata.injected` like `/api/chat/inject` — without it the turn-scoping filter silently dropped every note (and every image) from the live compile; a falsifiable end-to-end smoke (render a SECRET color, name it) cornered the drop and now pins the stamp

- feat(ui): bell items link back to their source session — a notification said something happened but not where. Each notification and agent question in the bell panel now carries a monospace session-id chip in its header; clicking it closes the panel and opens that session

- feat(harness): ARC-2 review fixes — five field failures from the 136b0064 campaign, each fixed at its mechanism. **Budget is not failover**: a per-session LLM time-limit error no longer triggers a model failover (switching models cannot buy time on a spent clock) — previously the fallback's own quota-403 masked the budget error so the BUDGET_EXHAUSTED soft-landing never ran; plus a quota circuit breaker (`provider_quota_cooldown_s`, default 600s) refuses failover to a model that 403'd on an exhausted key while the original error survives. **RLM cancel fidelity**: a child death during an active cancel reports "cancelled", not "failed: Broken pipe". **RLM orphan surfacing** (migration 29, `rlm_runs.surfaced_at`, history backfilled): a terminal run whose tool result was abandoned by a turn teardown is announced once as a system note on the next turn, with the partial answer and a continue_from pointer. **Plain checkpoint names**: bare `STATUS.md`/`NOTES.md`/`NEXT.md` join the `*_STATUS.md` scout-surfacing convention (a checkpoint file sat invisible next to the feature built to surface it). **StuckDetector Signal 13**: ≥8 consecutive repl/bash results that each falsify another candidate fit queue a one-time hypothesis-CLASS escalation hint (per-element → relational → sequential/recursive → global). **Distill grounding guard**: a <10%-grounded enumerated candidate gets its claim prefixed UNVERIFIED and high weight clamped to normal, and recall headers render the marker — "solved it" at 1% grounding was being recalled as authoritative

- fix(llm): the openai provider honors `vision_model_overrides` — supports_vision was derived purely from OpenAI name prefixes, so a self-hosted multimodal model behind vLLM was treated as text-only and images were dropped from the compiled context; the ollama path already honored the override list

- fix(settings): raise the edit bounds for deep work — max_tool_rounds 100→1000 and rlm_max_iterations 100→1000. The settings API silently reverted Calvin's 500s ("rejected by the server... out of range"); both ceilings dated from the pre-campaign era, and the floor plus round-cap-continuation machinery keep runaway risk bounded

- feat(harness,ui): the second ARC-3 sweep batch — six approved fixes. **Workers get kernel steering**: the worker charter now carries the hold-live-state-in-repl guidance (nine solver workers ran repl:0, paying the cold-start tax every probe). **Round-cap continuation**: a turn that exhausts max_tool_rounds while healthy (tools ran, no error, no stuck spiral) receives `round_cap_auto_continue` (default 1) fresh round budgets with a transcript notice and an LLM-wall extension — the cap had become the binding constraint agents burned cognition managing. **Grind detector sees bash**: Signal 12 now counts file paths inside cat/sed/grep/head/tail commands, so the rlm_process hint can fire on how agents actually read big files. **Reflect guards**: a grade missing its verdict coerces to retry (matching the invalid-verdict precedent — absent data must not read as approval), and a contentless "pass" on a substantial turn (≥5 tool calls) gets one re-prompt, then an explicit low-confidence marker. **Questions become an audit trail** (migration 28): answering marks answer/answered_at and keeps the row; the pending queue filters answered rows; retention pruning unchanged. **Bash log-noise collapse**: runs of identical output lines (6+) collapse to the first three plus a count — 54 identical API-key banners drowned one session's solver output. **UI: active dots blink again** — the sidebar's activity check still read the legacy `state` column, which stopped updating when state_v2 landed; activity now derives from state_v2 (processing/scouting/finalizing/awaiting_workers) with the legacy fallback for RLM view sessions

- fix(tools): the bash timeout error now points at job_start. First A/B field case from the harness-v2 cn04 retest: the agent ate a 600s and a full 1800s solver timeout without ever considering the job tools — the scout-time LONG COMPUTE rule doesn't reach the moment of need, so the pointer now rides the timeout error itself (mirroring the RLM truncation hint), gated on jobs_enabled

- feat(harness): the ARC-3 findings implementation — four phases from the 13-session friction study (approved "do all", vision experiment deferred). **Background jobs (Phase A)**: job_start/job_status/job_tail/job_kill make detached long compute first-class — output captured to a log, completion durable via an exit-code sidecar (survives server restarts; zombie-aware liveness), wall-clock capped by coreutils timeout, whole-group kill, per-session concurrency cap, same rlimits as bash; job_status/job_tail register idempotent=False so time-varying polls never dedup-cache; scout gains a LONG COMPUTE rule and bash's docstring points heavy compute at job_start (field: 15+ bash timeouts across the campaign, nohup+pkill hand-rolls condemned by the agents' own reflects). **Knowledge reuse (Phase B)**: new `stateful-env-reverse-engineering` skill encoding the probe→model→replay-validate→checkpoint loop the reflect verdicts kept re-teaching; scout now surfaces `*_STATUS.md`/`*_NOTES.md`/`*_NEXT.md` workspace checkpoint files (the convention the agents invented but successors never found — one lost a whole retry re-deriving a recorded fact). **Capability pointers (Phase C)**: StuckDetector Signal 12 queues a one-time rlm_process hint after 5 windowed reads of the same file (1 RLM use in 13 sessions while 12 ground huge sources through 50KB windows); the truncation footer names rlm_process for whole-file analysis; the kernel appends a memory-watermark warning to cell results past kernel_rss_warn_bytes (4GB default) instead of crashing at the 8GB cap with no warning. **Paper-cuts (Phase D, approved)**: /tmp joins the file-tool roots (bash always wrote there — the jail bought no containment); the `python -c ...exec(` denylist narrows to obfuscated payloads (base64/fromhex/hex-escapes); `rm -rf` allows paths inside the agent workspace (bare globs and the workspace root itself stay blocked)

- fix(orchestration): an unwatched worker's finished result no longer lands nowhere. A parent spawned a worker without `auto_resume_parent`, planned to await it, then hit the round cap and returned to idle with an empty watch-set — `_on_watched_worker_done` early-returned for unwatched workers, so completion triggered no resume and no notification and the finished transcript just sat there (field case: cd82 parent 41e10cf3c7bd, worker ef7758503a20). An unwatched worker completing while its parent watches nothing and sits IDLE_READY now routes through the documented Gap-1 idle-resume (synthesis turn with the standard per-worker status message). Parents mid-turn, awaiting the user, or deliberately watching other workers keep the old semantics

- fix(kernel): the idle-reaper no longer kills a kernel whose session is mid-turn. "Kernel idle" is not "session idle": a long bash call (a 10-minute solver run) kept the agent away from the repl past `kernel_idle_seconds` (1500s), the reaper snapshotted + shut the kernel down, and the live game env — a socket-backed object dill cannot carry — was gone on revival; the agent paid a NameError and a full env rebuild (field case: cd82 session 41e10cf3c7bd). Kernels of sessions in state_v2='processing' are now never reap or cap-eviction candidates regardless of repl idleness; `any_reapable` stays cheap on the event loop and the authoritative check runs off-loop in reap_idle. On a lookup error the reap proceeds (snapshot still preserves picklable state) so kernels can always be collected

- fix(ui,api): RLM runs surface correctly in the UI (reported by Calvin 2026-08-25). `/api/sessions/{id}/workers` returned every child session, so an RLM run's view session rode the workers list and the activity strip drew it as a teal worker chip named "RLM: ..." — mismatching the pink RLM legend key — and never retired it (a finished view session parks at state 'idle', not 'idle_ready'). The router now keeps only session_type='worker' children (the manager's rlm-child lookup is untouched); the strip's RLM chips come exclusively from `/api/rlm/runs` (pink, self-retiring), with a defensive type/state skip client-side. Second fix: the sidebar nested only one level, so an RLM run owned by a worker fell to the flat orphan list at the bottom — grandchildren now render under their worker with a second indent, the summary line counts them ("1 worker · 1 RLM run"), and the time group force-opens when a nested run is active. Also: file-panel's `sessionTypeDot` learned the rlm/snooze/canary types instead of falling back to the chat color

- fix(agent,reflect,scout): the harness stops scaring its own agent out of finishing (field case 17683100ecf8, the ARC-AGI-3 attempt). Three fixes from one post-mortem. **Budget display**: `[RESOURCE STATUS]` divided lifetime session spend — which re-counts the re-sent context on every LLM call — by the context window, so any long tool loop read "over budget" within a dozen rounds; the field session showed "1,299% of budget" while its largest prompt filled 36% of the window, and the agent narrated budget panic for ~20 straight messages before quitting 2-3 rounds short of a verified win with 84 of 100 rounds unused. The status line now shows context fullness against the window (the only number the window constrains, auto-compacted), lifetime spend as an explicitly informational count, and names tool rounds as the only binding limit — and the system prompt tail says exactly that. **Deferred verdicts get an effector**: deferred reflect is observe-only, so its "retry" verdict — with a correct 3-round finish strategy — sat unread in post_mortems while the session idled; a non-pass deferred verdict now raises a notification carrying the strategy ("no retry ran — reply in the session to have the agent act on it"). **Scout steers to the kernel**: the agent drove a live game environment through ~20 cold bash heredocs (fresh process, new anonymous API session, state re-fetched every round) while the persistent repl kernel sat unused; scout gains a STATEFUL ENVIRONMENTS rule (gated on `session_kernel_enabled`, like the RLM rule) telling approach_guidance to hold live objects in the kernel across rounds

- fix(retention): the sweeps see every session, and two record types that were never pruned now are. The canary-session and dream-journal pruners walked `list_sessions(500)` — the 500 most recently updated rows — so once the table passed 500 the OLDEST sessions, the ones due for pruning, were invisible (161 outside the window on the live box; a journal already past its 14 days with no way to be deleted). They now query by type and age. New: `worker_session_retention_days` (30; workers a parent is still waiting on are kept) — worker sessions had no pruner at all (36 on the box, eleven older than a month, 7 MB of messages); `dream_hypothesis_retention_days` (90) for terminal statuses only (refuted/expired/archived/promoted; pending and validated are work) — hypotheses grew ~57/day with nothing pruning them and the readers cap scans at 500 rows. Both run with the canary retention sweep (snooze Activity 12c). Normal sessions and the adaptive audit trail are deliberately untouched

- fix(tools,reflect,candor): a policy refusal is not a tool failure. A scheduled job's allow-list refused bash/glob/discover_tools 15 times in a week; each refusal was a `was_error` tool result, so tool_summary counted it as a failure — reflect read "bash: 7 failures", candor emitted `tool_ok(bash)=False`, telos minted "discover_tools failed 1/1" about tools that never ran. The executor now tags refusals (allow-list, retry exclusion, disabled tool, approval gate) and `record_tool_outcome` counts them as `refusals` with their own previews; `failures` means the tool ran and failed. Reflect shows them as "policy refusal(s)" and the rubric says what they mean. Second rubric guard from the same session: a non-pass verdict that attributes a requirement to the user must quote the user's words — a requirement found only in the scout plan cannot justify retry/escalate (a deferred escalate on a correct reply cited two memory entries "the user explicitly called out" that only the scout plan named; 4 of the 22 deferred non-pass verdicts that week had the same shape)
- feat(dream,adaptive): memory corrections apply on promotion and the caps go up. A validated contradiction / stale-memory finding is additive (the corrective entry sits beside the disputed ones, nothing is deleted), so the veto window never protected anything — yet 280 hypotheses queued behind a 12-row per-producer share and a 10-a-day drain. Promotion now mints the proposal row for the audit trail and applies it immediately (`auto_applied`, provenance "auto-applied on validation — dream finding, no veto window"), bypassing the queue caps; each correction is narrated in the dream journal and the operator gets one notification per day; the same (kind, file) correction is not re-applied within a week. Defaults raised across the board: `adaptive_max_pending_proposals` 40→200, `adaptive_max_pending_per_producer` 12→60, `adaptive_max_auto_approvals_per_day` 10→40, `adaptive_max_auto_applies_per_day` 6→24, `adaptive_max_entries_per_kind` 12→24, `dream_max_pending` 60→200, `dream_hypotheses_per_cycle` 3→6, `dream_validation_replays_per_day` 4→8, promote limit 3→10 per step
- feat(llm): a local CPU embedding fallback, and a sweep that survives a poison batch. After `embedding_fallback_after_minutes` (30) of continuous remote failure the active embedding model becomes `local:BAAI/bge-small-en-v1.5` (fastembed/ONNX, pulled once into `data/models/fastembed`): queries embed on the box's CPU, snooze re-embeds the corpus under that name over the next idle cycles, and search reads whichever model is active — the two vector spaces never mix. The snooze sweep probes the remote and switches back only after it has answered for `embedding_fallback_recover_minutes` (60), so a flapping server cannot trigger a corpus re-embed every few minutes. Both switches notify. `fastembed` is in requirements; without it the fallback is inert and behaviour is unchanged. Also: `embed_pending` skips a failed batch and carries on (three in a row means the server — stop) instead of parking the whole backlog behind one poison entry, and its log line reports the real backlog rather than the 256-row slice it fetched; the query-path backoff's zero-sentinel bug is fixed
- fix(memory): distill no longer restates — or invents a variant of — what the agent already saved. 35 seconds after the agent wrote a merged decision list to `pernix.decisions`, the automatic distill pass wrote a second `pernix.decisions` entry on the same topic with a fabricated top-6. Entries the agent saved during the session (read back from the SAVED/UPDATED tool results) are now shown to the distiller as authoritative, and any candidate that restates one (same file with modest word overlap, or strong overlap anywhere) is dropped. Enumerated candidates whose word trigrams barely occur in the transcript are tagged `unverified-distill`

- fix(adaptive): the two adaptive notifications say what happened and how to undo it. Asked to explain four of them, the agent on the live box (session dce9a6de7f81) mapped every proposal id to the wrong thing: the notices gave it bare ids, the queue-full text named the global 40-cap while the 12-row per-producer share was what had tripped, and the auto-approve text promised "roll back the batch in the Adaptive panel" for memory corrections — which create no batch and nothing the panel can roll back. The auto-approve notice now carries one line per proposal (producer, what it was, where it landed, the real undo path: batch rollback / delete the tagged memory entry / nothing); the queue-full notice names the cap that actually refused the insert and counts the canary proposals that never auto-approve; corrective memory entries say `auto-approved after the 24h veto window` instead of claiming a human approved them. `/api/adaptive/proposals` documents its status enum, answers an unknown status with a 400 that names it (it returned `[]`, which read as "the rows vanished"), serves `status=all` and `?id=` plus `/proposals/{id}`, and annotates every row with `summary`, `auto_approve_exempt` and `auto_approve_after`. The agent's `[SERVER CONTEXT]` gains a SELF-INSPECTION paragraph — the sessions.db path, `GET /openapi.json` before guessing an endpoint, the code root to read before asserting why, and "write 'not retrieved' rather than rebuilding an id→content mapping from timestamps"
- feat(reflect): a mechanical grounding check rides with the evidence. The same session's 5-row id→policy table passed reflect at 0.95 with every token real and every pairing invented; reflect had the tool results and the answer side by side and nothing asked it to check that any result showed a row's two halves together. The evidence blob now ends with a GROUNDING CHECK that lists identifiers the final response cites that appear in no tool result (nor the user's message), and markdown table rows whose id ↔ name pairing no single tool result shows within ~1500 chars. It is a flag, not a verdict: the rubric treats flagged rows as factually false under the materiality bar unless the cells are labelled inferred/not retrieved, one incidental token is not a retry, and a reconstructed mapping cannot be graded above 0.6 while a row is flagged. The flags are stored on the post-mortem (`payload.grounding`) and quoted back into the next attempt's retry context, which is the feedback channel the agent said would actually move its behaviour. Replayed on the live transcript, all five rows flag

- feat(adaptive): the proposal queue is a veto window, not an approval gate. A pending proposal older than `adaptive_auto_approve_after_hours` (default 24h) is approved by the system itself — the same apply path as a human approval (journaled, post-batch-swept, rollback-able), resolved as `auto_approved` so the audit trail keeps who-decided — oldest first, capped at `adaptive_max_auto_approvals_per_day` (10), idle-window only. The old shape held a structural contradiction: dream hypotheses are evidence-judged *before* they mint a proposal, the validation that measures anything real (tripwire drift, canary sweeps) can only run *after* application, yet application waited on a scarce human click — so on the live box 12 validated proposals sat pending for days with 39 more hypotheses parked behind them, all headed for TTL lapse. Reject inside the window to veto, roll back the batch to overrule; `0` restores the human-only gate. Canary-suite proposals never auto-approve — materializing a canary keeps its human invariant (I6) and has its own graduated-autonomy path (`canary_auto_admit`). The queue-full notification now explains the drain instead of demanding review

- fix(llm): batch embedding no longer dies 30 minutes after startup. `embed_texts` acquired the Ollama scheduler as pseudo-session `_embeddings`, which opted it into the 1800s wall-clock session budget — a stamp only SessionManager ever clears, and only for real sessions. The clock started at the first post-restart embed and never reset, so from 30 minutes of uptime every embed raised `LLMSessionTimeoutError` until the next restart (the live box embedded nothing for days at a stretch; new memories silently degraded to lexical-only recall). The acquire now uses the scheduler's documented background-caller contract (`session_id=""`, no budget); `session_created_at=inf` already ordered it behind all real sessions
- fix(maintenance): the daily backup survives restarts. It ran in the 24h tier, keyed on `tick % 1440` — and the tick counter starts at zero every process start, so a box that deploys daily never reached tick 1440 and silently took no backup at all (the live box: none between Aug 11 and Aug 15, across a four-day deploy streak). Due-ness now comes from the newest snapshot's own name-encoded timestamp (`hours_since_last_backup`), checked hourly, so the schedule is anchored to the data rather than to process uptime; the 24h tier's mutating sweeps keep running with a backup at most ~24h old since the hourly check shares the tick-1440 boundary and runs first
- fix(adaptive,dream): a full review queue is one warning, not a third of the log. Producers re-derive and re-offer the same findings every cycle while the queue is at cap, and every refusal logged at WARNING (plus dream's per-row INFO "deferring") — ~2,270 pairs in 21h on the live box, 32% of all log lines, drowning the actual signal. Refusals now warn once per producer per fill episode (a successful insert re-arms the warning; repeats log at DEBUG), and dream's promote pass emits a single per-cycle summary — "N validated hypotheses waiting on proposal review" — which is the line a human can act on
- fix(llm,memory,dream,telos): background JSON calls tolerate the model's bad output days. The memory file-split, dream hypothesize and telos soup parsers were three ad-hoc fence-strippers, and the qwen3.8 MTP tag broke all three at once (81 file-split parse failures in two days on the live box): its output sometimes arrives fenced or wrapped in prose, and sometimes the engine early-stops mid-generation leaving `[` or a cut-off object — a known MTP-variant failure mode in other engines too. The recoverable shapes now go through one extractor, `core/llm/jsonx.extract_json` (think-block strip, closed and truncated fences, balanced-scan for JSON embedded in prose), and each site retries the call once on an unparseable response — a resample usually lands — logging the head of what actually came back instead of a bare "could not parse"

- feat(llm): reasoning mode on Ollama is a setting, per role — `ollama_think` (Primary) and `ollama_think_background` (Background), both off, which is the behavior that was previously hardcoded. `think` was pinned to `False` on both native paths, so a reasoning-tuned model (the qwen3 family, nemotron3, …) ran with its reasoning suppressed and no way to say otherwise: invisible from outside, and it reads as the model simply being weaker under Pernix than in a terminal. Measured on `qwen3.8:27b-mtp-q8_0`, same prompt: `think=false` → 0 reasoning chars, 173 output tokens, 3.2s; default → 596 reasoning chars, 228 tokens, 4.0s. Roles are told apart by which settings key names the model, so **when Primary and Background are the same model the two cannot be distinguished and Primary's setting applies to both** — point Background at a different (ideally smaller) tag to run the tiers differently. Backup follows Primary. The reasoning chain is never surfaced or stored, matching the OpenRouter adapter; models without a thinking mode ignore the flag
- perf(scout): scout's AVAILABLE MODELS block is rendered from the registry instead of a live provider listing. Every field it prints — id, provider, context length, vision — was already in the registry, but it called `list_models()`, which goes out to `/api/tags` plus an `/api/show` per uncached model on **every scout run**: 4.7s of an 18.3s scout when warm, up to 20s cold, and as the slowest gatherer it set the floor for the whole gather phase. Related: a failed `/api/show` was neither cached nor logged, so the same handful of models were re-requested by every caller, through a 5-connection pool, re-creating the timeouts that caused the misses (three back-to-back `/api/models` calls measured 23.4s → 2.0s → 0.38s as the cache slowly filled). Failures are now negative-cached for 5 minutes and logged at warning, the guess is still never stored as metadata so it can never become a `num_ctx`, and the pool is sized to the fan-out
- perf(context): the tiktoken BPE cache lives under `data/` and is warmed at startup. tiktoken downloads cl100k_base (1.7MB) from a CDN on first use into an ephemeral temp dir, so every container rebuild re-downloaded it — 40s on the first turn after a deploy, measured, on the request path — and it was built lazily on the first compile, which put that download in front of a user's message. It now caches on the persistent volume (downloaded once, ever; `TIKTOKEN_CACHE_DIR` still wins if set) and is warmed in the background during startup. The load also caught only `ImportError`, so on an offline box the network error raised out of the constructor and took the turn with it instead of falling back to the character heuristic
- fix(llm): Ollama no longer 500s on the volatile system tail. On models using Ollama's newer chat renderer (the qwen3.8 family and anything else off the new template path), `/api/chat` rejects a system message that is not first — `chat prompt error: "system message must be at the beginning"` — before the model is reached. The compile emits later system messages by design: the compaction summary, trim notices, and the volatile clock/resource/telos tail, appended last so the cache-busting content sits in the prompt suffix. `normalize_for_openrouter()` has always rewritten those into user-role carriers, but only for the strict OpenAI providers, on the premise that "Ollama is more permissive and gets the raw compile output". The result was that every local-model turn failed three times (10s and 15s backoff, ~45s burned) and then failed over to the backup model, so the local model was never actually used and sessions looked hung. `_to_native_format()` — the single funnel for both Ollama chat paths — now carries mid-conversation system messages as user-role text
- perf(ui): opening Settings no longer downloads the OpenRouter catalog once per curated model. `/api/models/validate` fetched `openrouter.ai/api/v1/models` in full on every call, so a modal open fired a dozen multi-megabyte requests that queued behind one another (~18s on the reference box). The catalog is cached for 5 minutes behind a single-flight lock. Relatedly, the lazy registry repopulate added above uses `populate` rather than `refresh`, keeping each provider's per-model metadata cache: learning one newly pulled model cost an `/api/show` for every model on the host (~35 × 2s, on the turn's critical path), and a name that stays unknown after a repopulate now backs off to 15 minutes instead of retrying every minute
- fix(llm): a model pulled onto the host after startup no longer wedges every session. The model registry is populated once at boot, so a newly pulled model is absent from it; `derive_model_budget` is a catalog lookup with no network, returned `None`, and the agent fell back to the manual `context_budget` — on the reference box 16,384, which is smaller than the fixed cost of a turn (system + tool schemas + margin + history floor + a minimal completion), so every turn died in the compiler with `ContextBudgetError` until the registry was refreshed by hand. `GET /api/models` showed the model's real 262,144 window throughout, because that endpoint queries providers live and never writes back. The active model missing from the registry now triggers one refresh before derivation (rate limited per model, so a typo'd name cannot storm the provider list endpoints), at turn start, on an in-turn `switch_model`, and in `/api/context/{id}`. When the fallback is taken anyway it is stated rather than inferred: a one-shot warning per model, and the `ContextBudgetError` names the manual fallback as the budget's source instead of reading as though the model itself had a 16k window
- prune(workflows): the workflow engine is removed — `run_workflow`, `cancel_workflow`, `get_workflow_schema`, `create_workflow`, `discover_workflows`, `delete_workflow`, `validate_workflow`, the `/api/workflows` route family, the Explorer's Workflows tab, `workflows_dir`, and `WORKFLOW.md` parsing (seven agent-facing tools — 90 registered → 83 — and ~2,000 lines of dedicated code plus workflow-shaped conditionals in `reflect.py`, `scout/runner.py`, `context/compiler.py`, `snooze.py` and `retention.py`). It was never used: across the reference deployment's entire message history, from first boot to removal, `run_workflow` was called **zero** times and `workflow_runs` never held a row, while six workflows were parsed and registered at every boot for two months. The structural reason is that a workflow declares its step graph before the work starts — the one assumption an agent lets you drop — so when a step surprised it the only moves were retry, skip, or halt; everything built since (goals, gates, heartbeats, worker specs) went the other way. The capability survives as its parts: write the procedure as a **skill**, run steps with `spawn_worker` / `await_workers`, pass data through workspace files, enforce hard pass/fail with **gates**, schedule with `schedule_job`, and bound long autonomous runs with **goals**. `data/workflows/` is left untouched (nothing reads it — keep it as reference or delete it), and no migration runs: `workflow_runs` and the `workflow_name`/`run_id` proposal columns stay in the schema, simply never written. Rewrite any cron prompt or skill that calls `run_workflow` by name. See docs/upgrade.md
- fix(skills): skill-improvement proposals moved off the workflows router to `/api/skills/proposals*`, with the apply logic now at `core/skills/proposals.py`. They were only ever there by accident of where post-run reflect lived — proposals target `SKILL.md` files and `core/refine.py` writes them on ordinary sessions — so the workflow removal would otherwise have silently taken a live feature with it. External callers must re-point; the Explorer already does. Regression-pinned, including the route-ordering trap that makes `GET /api/skills/proposals` resolve as a missing skill if it is declared after `/api/skills/{name}`

- fix(core): correctness pass over the machinery the last four feature waves added — the semaphore's session identity no longer collapses distinct sessions onto one slot; blocking DB and filesystem work on the hot paths moved off the event loop; the adaptive tripwire anchors its comparison window on when a batch **applied** (earliest non-rollback journal event, ascending) rather than when it was queued, and reads the "after" window oldest-first so a batch is judged against the turns that actually followed it instead of a moving newest-first target; RLM's sub-call limiter is one semaphore shared across every recursion depth (peak concurrency was `max_concurrent ** max_depth`); kernel result binding became an exclusion set so `bash`/`grep`/`glob` — the three tools most likely to return megabytes — bind, and tools added later bind by default; the telos binding ladder blends the goal's real `token_usage` into budget share and only un-suspends goals it suspended itself; and Backup failover permits a **different model on the same provider**, so an Ollama-primary/Ollama-backup config finally has failover instead of silently having none
- feat(reflect): cross-retry circuit breaker — when reflect asks for another retry and the last two attempts of the turn failed identically (same `failure_cause`, reasoning ≥ 0.7 similar), the retry is refused rather than spending budget to reproduce the same failure a third time; emits `reflect.circuit_breaker`, writes a transcript notice, and notifies. Reflect also gains its first mechanical effector, `retry_without_tools`: up to five registry-validated tool names withheld from the retry attempt, enforced both in the schema slice and at the executor
- feat(adaptive): memory-correction effector — approving a dream contradiction/stale-memory proposal now writes something. `apply_memory_correction` appends a high-weight `CONTRADICTION RESOLVED` / `STALE-INFO CORRECTION` note (`source=dream_fix`) to each cited file, additively; the disputed entries stay in place, so the correction is reviewable and the original record survives. Previously these proposals were review-only and the overwhelming majority of a long-running install's pending queue had no effector at all. Adds `DELETE /api/adaptive/entries/{id}` (soft, journaled, rollback-able) as a release valve for a per-kind cap that producers can only ever fill, splits mixed-tier batches into one proposal per risk tier so approval isn't all-or-nothing, retires candor routing hints when the tool recovers, and makes tripwire dismissal durable via `cleared_at`
- refactor(config): three model roles instead of six — `scout_model`, `reflect_model`, `critical_model`, `rlm_root_model` and `rlm_sub_model` are gone. **Primary** (`llm_model`) runs agent turns and everything quality-critical (compaction, reflect, eval, RLM root); **Background** (`background_model`) runs the cheap tier (scout, titles, distill, snooze, dream, telos, RLM sub-calls); **Backup** (`fallback_model`) catches any Primary or Background failure, including a one-shot `chat_with_backup` retry around every non-streaming call site. Copy a distinct `scout_model` to `background_model`; stale keys are ignored, not fatal
- refactor(caps): defaults modernized for current models — `max_tool_rounds` 10 → 50 (ten was a weak-local-model-era value that manufactured its own `round_ceiling` failures and forced goal continuations to paper over them; goal budgets and the stuck detector are the real guards), view pruning is budget-gated instead of an unconditional stub-everything-over-300-chars hardcode (`view_prune_pressure`/`_keep_recent`/`_min_chars`, emits `context.view_pruned`), the context budget is derived per session from the model registry with `context_budget` demoted to a fallback, and the result caps were retuned to match (memory search 10, grep 400 matches, glob 300 paths, 24h spill-file TTL)
- refactor(snooze): `core/snooze.py` split — memory-store surgery to `core/memory/sweeps.py`, retention pruners to `core/retention.py`; the module keeps the lifecycle, idle gate and ladder. Prunes: Activity 2b and `core/snooze_reflect.py` deleted (folded into the Activity 13 refine pass), and the `schedule_workflow` tool deleted — it was a scheduling wrapper whose metadata never persisted; use `schedule_job` with a prompt that calls `run_workflow`
- feat(telos): actuation ports — anomaly-minted questions bind to the session's real `session_goals` row (mirrored as `g_db_<id>`) instead of collapsing onto `g_root`, which made the binding monitor structurally unfireable; `supported` claims with evidence and confidence ≥ 0.65 queue adaptive routing hints; open questions and live alarms render into the compile's volatile tail (suffix-only, so the prompt-prefix cache survives); and the L1→L2→L3 ladder monitors suspended goals so L2 is not terminal, with an ack silencing the notification while keeping the ladder's place
- feat(autonomy): goal budgets are checked every third tool round mid-turn (`goal.budget_exceeded`) rather than only between turns, where a single turn could overshoot without bound; cron sessions carry and auto-continue goals; goal continuations are snooze-transparent so an unattended overnight goal doesn't starve the box of maintenance; and no-op heartbeat ticks return before inserting a `cron_runs` row (a 30s heartbeat was writing ~2,880 rows/day recording that nothing happened)

- fix(telos): binding-monitor escalation is time-anchored — the L1→L2→L3 ladder climbs per ~20h the Goodhart signature persists, not per monitor run, so raising `telos_schedule` to a 4-hourly cron cannot compress "persists 2 windows" from ~2 days into ~8 hours (the spec §8 deep-push false positive)
- feat(telos): the teleological layer (`telos_enabled`, off by default) — a non-convergent drive with correction machinery derived from docs/dev/telos-spec.md. Fast loop: turn anomalies (surprise scaled by Candor priors) mint first-class Questions; at idle the SOUP generates cross-domain hypotheses at three analogical distances (band mix 50/30/20) with a testability gate (falsifier + cost + EIG floor) — rejected hypotheses join a recombinable speculation pool, admitted ones are judged against recorded evidence and commit claims under hard epistemic-class confidence caps (self_report 0.60, escapable only via trace corroboration). Slow loops (daily cron + weekly watermarks): Ordo Pass re-ranks the goal DAG and suspends orphans (never deletes), Binding Monitor detects Goodhart signatures (budget share + stalled parent question) with an L1 log → L2 freeze → L3 operator ladder, Hevel Audit scores goal discharge and marks never-discharging classes vapor (discounted, never banned), weekly reconciliation diffs the agent's compiled autobiography against the append-only JSONL trace ledger (trace > autobiography, always; divergence alarmed), and Entropy Control raises soup temperature when exploration goes cold. All state is markdown+YAML under `data/telos/`; 15% serendipity budget structurally reserved for non-goal questions; canary-isolated; four agent tools (`telos_status/ask/goal_add/goal_complete`), `/api/telos/*`, Explorer Telos tab, settings section, docs/internals/telos.md

- feat(llm): native OpenAI-compatible provider — `openai_base_url` (api.openai.com, vLLM, LM Studio, llama.cpp), env-only `OPENAI_API_KEY`, `openai_models` whitelist routing bare names (`gpt-4o`) away from the Ollama heuristic, same rate-limit fallback to the local model; router generalized from two hardcoded providers to a provider map
- feat(llm): prompt-cache breakpoints for `anthropic/*` models via OpenRouter (`openrouter_cache_control`, default on) — `cache_control` markers at the static-prefix and turn-section boundaries; cache read/write shown in the session cost tooltip
- feat(memory): semantic retrieval — set `embedding_model` (Ollama) and memory search turns hybrid BM25 + vector with RRF fusion; embedding runs as background snooze sweeps, empty setting = lexical-only, vectors are a rebuildable sidecar
- feat(memory): wiki-links — `[[file-name]]` / `[[file-name@epoch]]` in entry content expand one hop at recall (results labeled `source=link`); consolidation preserves the refs
- feat(kernel): session kernel — persistent per-session Python REPL (`repl` tool, `session_kernel_enabled`): variables survive rounds, turns, compaction, and (via dill snapshots) restarts; tool results over `large_result_bind_threshold` auto-bind as `tool_result_<n>` variables with head/tail stubs in context; `GET /api/kernel/status`
- feat(autonomy): gates, goals, heartbeats (three flags, all off) — deterministic shell gates that clamp Reflect's verdict before the post-mortem (with watch_paths reuse guard); persistent goals with token/time/continuation budgets and auto-continuations on round-ceiling/budget-exhausted turns (`goal_complete` refused while gates fail); heartbeats steered into running work at round boundaries, user/agent namespaces separated (`/api/sessions/{id}/heartbeat`, `/goal`, `/gates`)
- feat(canary): golden-task canary suite (`canary_enabled`) — `data/canaries/<name>/CANARY.md` (gates + optional `files:` fixtures) run headlessly through the full pipeline in isolated temp workspaces, excluded from FTS/distill/candor/memory-writes and snooze-transparent; scheduled/post-batch/manual triggers, `canary_run`/`canary_status` tools, `/api/canary*`; refine proposes new canaries from failed turns, human approval materializes + vets them; 90-day staleness nudge
- feat(adaptive): adaptive layer (`adaptive_enabled`) — governed machine-editable policy store (prompt_note/routing_hint auto-apply low-risk; policy/worker_spec proposal-gated) fed by refine/snooze-reflect/dream/candor with mandatory evidence; full event history, exact rollback, apply-on-approve proposals, canary-delta + post-mortem-drift tripwire flagging suspect batches; `worker_spec` templates consumed by `spawn_worker(spec=...)`; `/api/adaptive/*`, read-only `data/adaptive/ADAPTIVE.md` mirror
- feat(scout): learned model routing — post-mortems now record the turn's real model + task category; synthesized into `model_route` counters and a `[MODEL ROUTING INTEL]` exception brief steering scout's `recommended_model`
- feat(ui): Explorer gains Adaptive and Canary tabs (entries/events/proposals with rollback; suite pass rates with run buttons); settings modal gains Autonomy, Canary Suite, and Adaptive Layer sections; goal/gate/kernel state in the session header
- feat(voice): voice input for chat — a mic button with four engines selectable in Settings → Voice Input, each with an explicit where-does-my-audio-go disclaimer: `local_whisper` (faster-whisper on the Pernix server, audio never leaves the box; optional dep, 501 with install hint when absent), `remote_whisper` (OpenAI-compatible `/audio/transcriptions` endpoint, key via `VOICE_STT_API_KEY`), `model_direct` (recording attaches to the message and rides the existing ffmpeg→WAV pipeline into an audio-capable model), and `web_speech` (browser dictation — flagged as sending audio to the browser vendor). Whisper/model unavailability falls back to browser dictation only when the user enables the fallback toggle, which carries its own acknowledgment text. New `GET /api/voice/status` + `POST /api/voice/transcribe`; The mic button and Ctrl/Cmd+Shift+M (Discord/Teams muscle memory) share one gesture model — tap to toggle, press-and-hold for push-to-talk (release stops and transcribes); starts are instant (cached engine status instead of a blocking refetch), key auto-repeat can't flicker the mic, Esc cancels a live recording, send stops dictation, 5-minute hot-mic auto-stop, and an opt-in auto-send fires the message once dictation produces a non-empty transcript (never on silence; model_direct voice notes stay manual). A Test button in the settings section round-trips the saved engine (record → transcribe → show what it heard), and the language hint is a curated dropdown instead of free-form ISO codes
- feat(ui): clipboard paste lands in chat — a copied screenshot or file pasted anywhere outside another input becomes a pending attachment chip (same pipeline as drag & drop); generic clipboard names (`image.png`) get timestamped. Plain-text paste keeps native behavior

- fix(tools): `--dangerous` now suppresses the approval ritual end-to-end — `approve_dangerous_tool` is not registered, the `delete_skill`/`delete_workflow` descriptions drop the ask_user + approve sequence, and the system prompt states that approvals are bypassed. Previously the model had no way to know the gate was off and kept running the full ritual for tools that were never even gated (session 0dbee64fcd43: 13 ask_user + approve rounds for caution-level `bash`)
- fix(dialog): `ask_user(question_type="statement")` no longer parks the session in AWAITING_USER — statements land in the question panel as FYIs while the agent keeps working; only real questions pause the turn

- fix(ui): the sidebar legend wraps instead of clipping — five session types (RLM made it five) no longer fit the 270px sidebar on one line, and the centered no-wrap row clipped at both edges, leaving the Session toggle unreachable
- feat(rlm): live run visibility — every `rlm_process` run now gets a read-only view session nested under its parent in the sidebar (migration v20, `rlm_runs.ui_session_id`); selecting it opens a trace viewer that tails the run live (root reasoning per iteration, collapsible REPL cells with stdout/stderr, sub-call rows, caps progress, final answer, nested-run navigation) via `GET /api/rlm/runs/{id}/trace?after=` byte-offset paging. The launching session's stream carries `rlm.started/activity/heartbeat/done` (engine `progress_fn` seam + 10s heartbeat from the broker's live counters), the activity strip shows a live RLM chip next to worker chips, and `rlm_runs` counters update mid-run so lists/orphans stop reading `0 it · 0 calls`. Read-only gating is now a shared predicate (`sessions/policy.py`) with `read_only`/`read_only_reason` on session payloads — dream journals and RLM views use the same path; deleting a view session purges the finished run's dir + rows, and retention deletes view sessions with their runs
- feat(dream): Phase 4a deep probes — an RLM run over the whole memory corpus (every active entry with file@epoch markers + hypothesis list + Candor brief) hunts cross-file contradictions the one-file-per-cycle dream step cannot see; maintenance-tracked outside the cycle, evidence resolved to content-hash refs at ingest, same filters as cycle output, visible in the RLM runs panel (`dream_rlm_probe`, off by default)
- feat(memory): claim-origin provenance — distilled entries carry `@origin: external` when the session used web tools (`internal` otherwise), the origin survives moves/fuses (external taints), and dream evidence packs mark and discount web-derived entries
- feat(memory): the advertised `@tags:` filter now works — `recall("deploy @tags: alpha")` compiles to a real FTS5 column filter; inferred tags now reach the markdown too instead of silently dying on the next reindex
- feat(snooze): the hang backstop scales 4x when the background model is local (Ollama) — slow local inference is free and user activity preempts instantly; remote models keep the configured cap. Dream journal sessions now prune past `dream_journal_retention_days`
- fix(api): localhost-gated endpoints accept IPv4-mapped-IPv6 loopback (`::ffff:127.0.0.1`) via a shared `is_local_client` helper; docker-bridge sources stay rejected
- chore: removed the stale SCOUT SIGNALS block from the scout prompt (nothing has produced that section since signals became UI-only) and the dead `memory_recall_min_score` setting; added a full-lifespan boot smoke test

- feat(snooze): cycles now run until the activity ladder completes instead of being killed by a 60s wall clock — user activity (prompt/cron/shutdown) aborts even in-flight LLM awaits immediately via a per-cycle cancel event, interrupted activities resume next cycle via their watermarks, and `snooze_max_cycle_seconds` (default now 900) is demoted to a hang backstop; `run_cycle` returns ran/yielded/backstop/error, and the admin trigger reports `idle_blockers` when the idle gate refuses

- feat(dream): idle-time introspection add-on, Phases 0–2 of docs/dev/dream-plan.md — snooze Activity 14 generates typed hypotheses over memory, Candor evidence, and post-mortems (labeled evidence packs, fc329cb claim filter, dedup vs refuted), validates them (Candor re-predict, evidence judge, counterfactual scout replay with a per-day budget), and writes periodic reports to workspace/dreams/; sidecar tables via migration v19, all state in dream_* keys; off by default (`dream_enabled`)
- feat(snooze): `run_cycle(force=)` skips only the cadence gate and returns the gate outcome; localhost-only `POST /api/admin/snooze-cycle` triggers a cycle for testing/ops
- fix(memory): consolidation integrity — fused entries keep entry_type/tags/weight, bypass the dup gate that silently blocked them, key hit counts to the real post-collision epoch, and supersede their target contributor; entries a merge verdict omitted are rescued to the target instead of stranded in the archived source (previously unbounded silent data loss); archive stats count only real retirements; `updated` and non-default `weight` now roundtrip through markdown

- refactor(context): SOUL.md/RULES.md/SESSIONS.md now injected whole by the context compiler's fixed-prefix directives block — the single reader. Scout no longer echoes them (submit_report drops `identity`/`rules`/`instructions`, 14 fields → 11), the three duplicate `[:4000]`/`[:1200]` fallback loaders are gone, RULES.md stops losing its tail to silent truncation (32K guard warns loudly), and the byte-stable placement extends the prompt-prefix cache across turns
- fix(scout): the final round keeps `submit_report` instead of losing every tool, and revisions are only spent on issues the post-scout sanitizer can't fix — the old pairing made a round-5 revision unwinnable by construction (17 revisions logged, 0 second submits)
- fix(scout): a scout that never submits no longer hands the agent a blank report — it degrades to the deterministic fallback (identity, rules, memory, real tool list) instead of stripping the turn down to CORE_MINIMUM; degraded reports are kept out of the cache and no longer short-circuit the fallback model
- feat(rlm): recursive long-input processing add-on (arXiv 2512.24601) — sandboxed child REPL engine, sub-call broker, `rlm_process` tool, settings + model roles, migration v18 (`rlm_runs`), snooze retention, scout/nudge/prompt discoverability wiring; off by default

## v2.9.0 — 2026-06-11

- docs: accuracy audit pass — correct tool names, defaults, routes, and safety claims

## v2.8.0 — 2026-06-11

- chore: gitignore data/tool_approvals.json (runtime state)
- feat(skills): media-cast youtube subcommand + linkedin formatter voice guidance
- feat(ui): group session model picker by provider
- fix(ui): mobile usability pass — viewport, overflow, touch affordances
- feat(model_mgmt): add scope param to switch_model (turn|session)
- fix(state): harden turn workflow — reaper guards, multi-compaction, turn-scoped reflect
- feat(ui): state timeline upgrades + chat session UX enhancements
- chore: remove dead API/DB helpers
- chore: remove dead code across core, config, and sessions
- chore(memory): remove dead recall_enhanced
- fix(memory): length-normalize BM25 scores so documented thresholds hold
- fix(memory): temporal results pad hybrid search instead of displacing matches
- feat(memory): show entry age and provenance at recall time
- refactor(memory): single home for routing vocabulary and name canonicalization
- fix(memory): stop hit-tracking on automated recall paths
- feat(memory): point duplicate-skipped saves at the entry to supersede
- fix(memory): neutralize bare '---' lines in entry content
- fix(memory): enforce unique (file, epoch) entry identity
- fix(memory): stop reindex from resurrecting archived files
- chore(pwa): bump shell cache to v2 for the wave-3 UI changes
- feat(ui): de-emphasize cron sessions in the sidebar
- fix(ui): login hint covers the QR path, not just the server console
- fix(ui): stop infinite SSE reconnects to a deleted session
- perf(ui): skip sidebar/health polling while the tab is hidden
- fix(ui): model-generated links open in a new tab; strip inline styles
- fix(ui): offline banner instead of blocking modal; gate restart by host
- feat(sessions): user-facing pause/resume for any session
- feat(ui): worker activity strip — live view of the fleet during fan-outs
- feat(ui): surface per-session cost
- feat(ui): upload progress, client-side size precheck, and no silent partial sends
- feat(ux): make the product's capabilities discoverable
- feat(ui): sidebar session search over the existing FTS index
- fix(mobile): Enter inserts a newline instead of sending
- fix(ui): stop-button errors, visibility recovery, notification stream, rejected bubbles
- fix(agent): LLM scheduling fairness was silently disabled (created_at AttributeError)
- feat(pwa): precache the app shell in the service worker
- fix(ui): vendor marked.js and add a Monaco load timeout (LAN-only deployments)
- perf(ui): incremental streaming render — stop re-parsing the whole answer per tick
- feat(ui): paginate session history — bounded initial load + load-earlier
- perf(scout): make the report cache hittable + bypass conversational follow-ups
- perf(scout): gather baseline searches concurrently
- perf(db): move hot DB work off the event loop
- fix(tools): run long-poll tools on a dedicated executor (pool starvation)
- fix(state): marshal tool-thread state transitions onto the event loop
- fix(events): marshal subscriber-queue delivery onto the event loop
- style: black formatting for orchestration gate and db tests
- fix(snooze): dedup must not archive entries that carry unique information
- fix(orchestration): exact sentinel match in worker quality gate + visible truncation
- fix(reflect): hard do-not-repeat guard for executed side-effecting tools
- feat(ux): gesture-driven notification enable + cron schedule presets
- feat(ux): first-run setup card when no model is configured
- fix(ux): require confirmation before destroying sessions
- fix(ux): show status text on mobile as a floating strip instead of hiding it
- fix(ux): pinned scrolling — stop yanking readers to the bottom, follow the stream
- feat(ux): emit tool.start so the UI shows live progress during tool execution
- perf(db): bound the tail-reader get_messages calls
- perf(context): memoize attachment base64 encoding across tool rounds
- perf(context): cache token counts instead of re-encoding history every round
- perf(db): reuse SQLite connections per thread
- perf(context): keep volatile state out of the system head (prompt cache)
- fix(db): run each migration in an explicit transaction (crash no longer bricks startup)
- fix(db): stop leaking messages_fts rows when sessions are deleted
- fix(boot): sweep all non-terminal session states at startup, not just 2 of 8
- fix(scheduling): create cron job sessions with session_type="cron"
- fix(ui): resync when server event_seq goes backwards (restart left UI dead)
- fix(tools): enforce dangerous-tool approval scope and require an answered ask_user
- fix(web): validate every redirect hop in http_get (SSRF bypass)
- fix(sessions): don't reaper-unstick PROCESSING sessions with a live agent task
- fix: invalidate bash cross-round dedup after file mutations
- feat: add paperclip attach button to chat input (desktop + mobile)
- docs: reconcile SPEC_v2 with current code state (audit pass)
- feat: refine pass — whole-session skill/lesson extraction at snooze tail
- fix: scope reflect verdict to ask_user answer when user replied to a question
- chore: identify as Pernix.cc to OpenRouter attribution
- fix: salvage XML-style tool calls that leak as text

## v2.7.0 — 2026-05-19

- chore: black auto-format core/llm/providers/ollama.py
- feat: default browser_enabled=True; pin browse_web independence
- feat: per-session memory recall dedup ledger
- fix: skip primary scout retries on wall-clock timeout
- fix: gate audio inlining on supports_audio, mirror vision strictness
- feat: audio attachments — pass WAV to Ollama, transcode others via ffmpeg
- fix: quote every FTS5 query word as a literal phrase
- fix: strip `:` `.` `/` `=` from FTS5 search queries

## v2.6.0 — 2026-05-11

- test: pin internal_recall contract + search_web augmentation
- feat: surface internal memory + prior-session hits alongside search_web
- fix: subscribe SSE client to all server-emitted event types
- docs: sync SPEC_v2 with 2026-05-08–11 commits
- fix: search_sessions actually finds rows — escape %, log FTS errors, resolve id prefixes
- feat: trim-aware short-term memory recovery (pin user msg, trim notice, current-session search)

## v2.5.0 — 2026-05-08

- fix: route injected messages to prompt() outside live-loop states
- fix: prevent RuntimeError in Ollama stream generator on HTTP 500 + retry
- feat: UTC+local temporal context, session-history guidance, docs update
- feat: inject current datetime into scout context; gate web tools on settings flags
- fix: SSE watchdog reconnect now replays missed events via query param
- docs: reflect that memory is no longer strictly append-only
- feat: cap retry budget gate + add update_memory / forget tools

## v2.4.0 — 2026-05-08

- fix: patch bugs found in post-merge validation
- feat: reflect emits turn_digest, sees per-attempt transcript with verbatim tool results
- fix: raise scout memory preload cap, uncap active recall, scrub spec links from public docs
- feat: folder delete + mobile swipe-to-delete in Explorer
- docs: tighten quickstart, contributing, and workflows examples
- docs: write changelog and upgrade guide
- docs: write internals reference (extensions, reflect+snooze)
- docs: write deployment guides
- docs: write authoring guides + refresh writing-skills staleness
- docs: write user-facing guides
- docs: write contributing guide
- docs: write quickstart and FAQ
- docs: update state-machine references to 10-state model
- docs: reorganize into role-based /docs/ tree
- fix: persist ask_user dialog on refresh and unblock agent on dismiss
- feat: remove DuckDuckGo fallback, gate search_web on Tavily API key
- fix: approve_dangerous_tool finds ask_user in tool_calls column

## v2.3.0 — 2026-05-07

- chore: exclude agent-created custom tools from formatters and git
- fix: inject custom tool into active schema immediately after create_tool
- feat: workspace Explorer column headers, folder metadata, and sort fix
- feat: custom tool venv routing, requirements tracking, and release prep

## v2.2.0 — 2026-05-07

- feat: make inline file paths in chat clickable to open Explorer
- fix: expand fallback reasons, gate workflow retry on budget, and misc cleanup
- fix: offload remaining sync blocking ops to asyncio.to_thread
- fix: keep event loop responsive during snooze consolidation
- fix: unify log format by routing uvicorn through root logger
- docs: correct stale facts in AI tooling.md and add check.sh + companion docs
- fix: stop FTS5 syntax errors from punctuation in BM25 queries
- docs: update SPEC_v2 for 2026-05-05 changes
- feat: overhaul cross-session tools with rich metadata
- docs: update state_v2.py to reflect Stage 1 completion
- refactor: extract _run_scout_and_process, migrate to v2 state machine
- feat: overhaul memory search — query fix, ripgrep fallback, deep_recall agent
- feat: add delete_skill and delete_workflow tools
- feat: seed SESSIONS.md with spec-informed default structure
- refactor: rename PROJECT.md to SESSIONS.md
- refactor: rename agent instruction files for clarity, add proactive behavior rules
- fix: recover from orphaned skill dirs and stale registry on load_skill
- chore: disable clean-room-release skill, remove test-mini-flow workflow
- fix: block internal message roles from reaching LLM providers
- feat: enhance state timeline modal with richer workflow context
- fix: scout bypass fires incorrectly on short first-turn messages
- fix: improve kimi native-token error hint and surface reasoning deltas
- fix: close three stuck-loop gaps exposed by session b8e40e45925e
- feat: raise workspace upload limit from 10MB to 250MB
- fix: exclude reflect role from LLM context, unbreak OpenRouter multi-turn
- feat: filter disabled skills/tools across every agent surface
- ui: align UI brand styling with pernix-website
- docs: patch SPEC_v2 for 2026-05-03–04 commits
- fix: state badge and soft-reload streaming sync
- fix: prevent infinite orphan re-queue loop on consecutive ask_user answer chains
- fix: yes/no quick-answer buttons, mobile tab wrapping, ask_user session stuck bug
- fix: harness prompt nudges from session edb605c3e045 audit
- fix: 7 issues from session cb41c12d92fe audit
- docs: add playwright install chromium step to README, INSTALLATION, and Docker outline
- docs: update AI tooling.md — venv restart caveat, playwright setup, cron gate exception
- fix: bypass dangerous gate for cron sessions, hint file_read on stale edit, update playwright pin
- fix: preserve notification input, mobile layout, auto-close on empty
- docs: add Amendment 21 — Explorer panel search coverage + focus-safe pattern
- feat: add search to Skills, Workflows, and Jobs Explorer panels
- fix: retain search focus in Explorer → Tools by rendering list in-place
- docs: update spec, security, architecture, and install docs for recent changes
- feat: Security tab in Settings + Run Dangerously mode via --dangerous flag
- fix: per-invocation dangerous tool approval with persistent scope memory
- fix: implement per-session dangerous tool approval via ask_user handshake
- fix: state machine correctness — 4 session lifecycle bugs + graph completeness
- docs: modernize model recs, add Swagger UI references, add ARCHITECTURE.md
- chore: gitignore docs/spec and release tooling

## v2.1.0 — 2026-05-02

- docs: add comprehensive end-user documentation; cleanup .gitignore and dead stubs
- chore: add black/ruff/flake8 linting pipeline with check.sh
- feat: rename --fresh to --rebuild with confirmation; set tool safety defaults
- refactor: merge evaluate_one into evaluate; remove validate_workflow_content
- refactor: remove VCS extension — all 6 git tools replaced by bash
- feat: bring Tools tab in line with other Explorer tabs
- feat: show and edit tool safety level in Explorer → Tools UI
- fix: revert bash to safety_level=caution
- fix: ensure agent always sees tool output — close 3 silent-failure paths
- fix: second-pass safety level corrections (3 tools)
- refactor: tool audit — safety levels, merged pairs, orphan registration
- feat: add TEAM skill — 16 specialist agents for technical enablement
- chore: post-change cleanup from signals refactor
- refactor: replace scout signals with simple tool/skill performance tracking
- fix: recover orphaned user messages and harden tool call parsing
- perf: set PRAGMA synchronous=NORMAL on all SQLite connections
- fix: track injected message element before the await, remove on failure
- fix: persist model_divider and fix from/to fields on override restore
- fix: switch_model — validate model exists before accepting override
- fix: session 444e33b3968e — allow self-loopback through SSRF in network mode
- fix: session 220a71bb post-mortem — 6 reliability fixes
- docs: sync SPEC_v2 with 2026-04-27–30 reliability & observability wave
- feat: persist model switches in chat as pill dividers + reflect-model chip
- feat: surface reflect model in reflect.done event and card
- fix: switch_model now actually moves the LLM mid-turn
- feat: post-mortem fixes from session 7b5c19c78dde
- feat: detect max_tokens truncation and continue in-turn instead of retry
- feat: harden agent system prompt with three new behavioral rules
- fix: force dark background on workspace sort select and its options
- feat: expose bulk session cleanup in Settings; fix select dark-mode
- feat: add Name / Date / Size sort to the workspace file list
- fix: stop sidebar group toggle from un-collapsing on every SSE redraw
- fix: stop reflect lessons from leaking into the queued-popped turn
- fix: persist reflect-skipped marker so it survives page reload
- fix: tighten auto-eval prompt + surface reflect-skipped to the UI
- fix: make auto-eval discoverable and self-closing
- feat: render eval results as a card instead of raw JSON
- fix: eval tools now reach the running event loop
- fix: parent no longer auto-resumes when worker pauses on ask_user
- fix: cap consecutive stuck-loop nudges so an unresponsive LLM stops the turn
- fix: idempotency_key dedup actually persists the key now
- fix: cascade cancel no longer respawns workers via auto-resume race
- fix: stop turn-scoping filter from dropping /api/chat/inject messages
- chore: add test-mini-flow workflow as a harness regression fixture
- fix: reaper now unsticks SCOUTING sessions when the agent task is gone
- fix: record dropped queued messages on session cancel
- fix: workers-complete notification used to truncate ids the agent then copied verbatim
- fix: render assistants in logical-turn order via parent_user_msg_id metadata
- fix: scope agent + reflect view to the current turn's user message
- fix: collapse rapid-fire user messages into one queued turn
- fix: surface scout's planned approach to reflect to stop skill false-negatives
- feat: surface skill provenance + cancellation marker in session transcript
- fix: align scout system prompt round counts with SCOUT_MAX_ROUNDS
- feat: notify user when a session's LLM time budget is exhausted
- test: AWAITING_USER answer also resets LLM time budget
- fix: reset LLM time budget on each user prompt (no more locked sessions)
- feat: per-step model override in WORKFLOW.md + ai-tech-daily-brief refinements
- fix: workflow stale_threshold=300s + reflect coerces invalid verdict to retry
- chore: parallel pytest by default (-n auto via pytest-xdist)
- feat: per-call timeout override for bash tool + workflow guidance
- test+fix: workflow harness handles AWAITING_USER, cron rerun, and LLM outage
- fix: harness recovers when pass-verdict worker writes deliverable to a different path
- fix: correct has_started check in await_workers (Task != None ≠ turn started)
- fix: drain queued worker_ids appends in await_workers + extend asyncio.timeout fix
- fix: harness remediation from 2026-04-27 ai-tech-daily-brief audit
- fix: stop reflect-retry leak from suspended turn into synthesis turn
- chore: add _finalize_step tests + gitignore TLS keys, .AI tooling/, *.bak
- feat: handle worker.failed event in frontend + bundle system-message markdown
- feat: persist v2 state and watched_worker_ids for restart recovery
- fix: close AWAITING_WORKERS deadlock paths in worker orchestration
- fix: cron orchestrator sessions dying mid-flight from sticky LLM session timeout
- fix: ai-tech-brief workflow hallucination + reflect/finalize fidelity
- fix: harden workflow orchestration after session 7b97cf7 post-mortem
- fix: scout LLM priority starvation and cancelled-worker manifest fidelity
- fix: schedule_workflow failed to persist job to cron_jobs.json
- fix: add refresh button to Jobs panel and fix new-job visibility
- fix: clarify cron times are UTC in Jobs UI
- feat: expose llm_session_timeout in Settings UI
- feat: session-aware LLM scheduling with FIFO, priority, and timeout
- fix: prevent AWAITING_WORKERS deadlock when early workers complete before suspend
- feat: harness improvements from session-42550cc spiral post-mortem
- chore: switch DuckDuckGo search to renamed ddgs package
- feat: extend self-improvement loop to non-workflow sessions
- fix: rescan skills dir on GET /api/skills
- fix: wrap file panel tabs on narrow screens
- fix: harden workflows feature — state machine, context isolation, self-healing
- chore: add linkedin-post-formatter skill, ignore whisper summaries
- chore: add initial workflow definitions
- fix: align workflow editor with workspace/skill editor pattern
- fix: add workflow tools hint to BASE_SYSTEM_PROMPT
- fix: add create_workflow tool and fix agent workflow creation failures
- feat: workflow visualization, validation, and agent self-healing
- feat: skill workflows — reusable multi-step skill pipelines
- feat: move Scout Signals into Explorer panel as dedicated tab
- feat: retry LLM stream errors with backoff and cross-infra fallback
- fix: skip post-hooks when session is AWAITING_USER to prevent spurious reflect retry
- feat: tool activity icons and model switch indicator in status bar
- chore: remove redundant status text and model pulse covered by state badge
- fix: always live-query Ollama for model dropdown so new models appear
- fix: persist user message immediately and scope UI state per session
- feat: show tool call tally in state timeline graph tab
- fix: always show expand toggle when tool output overflows 150px clip
- fix: replace opacity pulse with brightness to prevent badge bleed-through on mobile
- fix: skip reflect when session is AWAITING_USER
- fix: prevent reflect retries from re-doing completed work
- feat: inject server URL into agent system prompt
- chore: add MCP tool permissions + remove youtube-video-gallery skill
- feat: youtube-whisper — add timeline, diarization, and speaker labels
- refactor: clean and consolidate skills — 5 skills → 4
- refactor: status bar — consolidate CSS, add divider, abbreviate scout info
- feat: async multi-agent communication — AWAITING_WORKERS state + 4 gaps
- fix: four state-machine v2 correctness issues found in post-merge review
- feat: state-timeline modal — Mermaid graph + tool-call interleaving
- feat: state-machine v2 (Stage 1) — wire 9-state machine alongside legacy
- fix: harness remediation from session a79b9ebdc7ba deep dive
- fix: treat touch-primary devices as mobile so iPad gets the drawer
- fix: harness remediation from session b493 post-mortem
- feat: crawl4ai browse-fallback skill + skill-index hardening
- fix: worker-reliability remediation + 24h audit follow-ups
- feat: session id badge in sidebar — hover to view, click to copy
- fix: worker reliability — round-budget warnings, reflect retry, scout recovery
- fix: vision self-awareness + stuck→ask_user + tool cohort expansion
- fix: scout signals modal mobile layout + type filter active state
- fix: honest ctx indicator + surface compaction state to UI
- style: align signals-btn with settings-btn styling + force text flag glyph
- fix: stop inlining base64 attachments in the DB; expand at compile-time
- fix: bound memory and CPU on file_read / file_edit for large files
- fix: harden file tools against silent edits, binary corruption, and wrapper bypass
- feat: graceful frontend offline mode with reconnect pinger
- fix: stop SSE consumer task leaks at shutdown and on disconnect
- docs: spec alignment pass for 24h feedback-loop baseline
- feat: post-mortem TTL cleanup with configurable retention
- feat: snooze gates on idle state and runs every 15 min (was 1 hour)
- fix: deduplicate retry post-mortems in synthesis (one signal update per session)
- feat: search_post_mortems scout tool for targeted failure lookup
- feat: scout self-check gets two revision attempts instead of one
- fix: scout session brief uses per-session context budget
- refactor: remove workers from execution_mode enum (unfulfilled, deferred)
- docs: clarify synthesis attribution for cache vs fallback scout
- Phase 0-4 baseline: scout signals, synthesis, post-mortems, metrics
- Add scout retry with detailed error logging for transient Ollama failures
- Fix scout fallback: handle native tool-calling models on last round
- Fix scout: restore pre-gathered baseline context for hybrid approach
- Convert scout from single-shot to multi-turn tool-calling agent
- Audit and fix agent prompt ↔ source code alignment
- Add multimodal image support for user attachments
- Fix false positive in curl pipe security check
- Suppress expected CancelledError tracebacks during graceful shutdown
- Add pytest suite: 782 tests, 68% code coverage (up from 21%)
- Fix 9 agent reliability issues with full test coverage
- Fix inter-session queueing: raise LLM semaphore timeout from 60s to 1800s
- Fix notification bell panel clearing user input on SSE updates
- Fix clean shutdown by replacing BaseHTTPMiddleware with pure ASGI
- Fix push notifications for reflect escalations and job failures
- Fix VAPID private key format for pywebpush
- Suppress /api/notifications polling logs
- Fix Web Push for network mode: auth + SW scope
- Add Web Push (VAPID) background notification support
- Style bell icon to match status bar theme + add mkcert setup docs
- Add standard mobile-web-app-capable meta tag alongside Apple-specific one
- Add notification bell with unified panel for questions + notifications
- Add notify_user tool and browser push notification support
- Add PWA support, revoke-access button, SSE task cancellation fix, localStorage auth
- Fix DB integrity, memory routing, reroute clusters, suppress polling logs
- Add ask_user chat-flow display and multi-client modal sync
- Fix DB integrity gaps + update SPEC_v2.md for memory routing changes
- Balance reroute file creation: allow clusters, block single-entry new files
- Fix _REROUTE_PROMPT: restrict moves to existing files only
- Add snooze Activity 3c: re-route misplaced memory entries
- Fix memory distillation routing: rich file catalog + explicit routing rules
- Fix H4/H5/H7/H8/H9/M1/M5/M11/M12: concurrency and safety hardening
- Mobile UI polish: fixed viewport, compact input, modal grab handles
- Fix H2/M2/M3/M13/M14: security guards, symlink fix, swipe UX, job flicker
- Mark M6/M7/M8/M9/M10/H6 as fixed in REVIEW_ISSUES.md
- Fix M6/M7/M8/M9/M10/H6: thread safety, cancel race, state machine
- Update SPEC_v2.md: document 10 commits since last amendment
- Fix auth cookie reload bug, epoch collision loop, and dedup threshold
- Fix token-in-URL security: use cookies for SSE auth, gate QR on --qr flag
- Add workspace/ to gitignore
- Enhance memory system: consolidation, ingestion, improved distillation and snooze
- Fix mobile breakpoint mismatch and unblock browse_web tool
- Add auth middleware, remote client onboarding, and critical bug fixes
- Add LogAct-inspired reliability features: diagnostic reflect, tool safety levels, execution metadata, worker cross-pollination
- Replace recursive workspace scan with directory-level navigation and search
- Milestone: Mobile UI, scout grep awareness, settings Network tab, youtube skill
- Add network access with HTTPS support and graceful server restart
- Update SPEC_v2.md: align with milestone commit (15 features documented)
- Milestone: App branding, Jobs UI overhaul, and quality-of-life improvements
- Move source image and video assets to assets/ folder
- Add app icon with animated GIF, favicon, and touch icons
- Add per-provider concurrency, Ollama model list, and Settings reorganization
- Overhaul tool call display with three-tier progressive disclosure
- refactor gallery-7HKVvcNOQb0: apply AI tooling Sonnet improvements
- Fix live SSE message ordering so assistant text and tool calls interleave correctly
- Add memory provenance, usage tracking, staleness pruning, and skill cooccurrence
- Fix stream GeneratorExit crashes, unstick dead PROCESSING sessions, and update youtube skill
- Restrict ticker scroll to live activity, not idle previews
- Fix stale activity ticker persisting for cron sessions after completion
- Make truncation visible and actionable for LLMs
- Add Monaco editor, file dates, and save button state management to Explorer
- Fix workers silently dying on LLM errors and misrouted model names
- Update SPEC_v2.md: align with 34 commits from past 72 hours
- Fix ReferenceError: pathChildren is not defined in file viewer
- Eliminate artifact abstraction, unify Explorer into Workspace
- Harden APIs, fix stream persistence, add glob tool, rewrite gallery skill
- Unify workspace/artifacts, protect skills, fix session deletion and shutdown
- Guard against empty base URLs clobbering defaults
- Fix settings: restore URL fields and refresh models on whitelist change
- Enforce workspace venv for all Python package installs (never system pip)
- Harden security and stability across 4 waves (27 fixes)
- Add mid-turn message injection: send messages while agent is processing
- Fix Explorer crash on large/binary files: add media preview, size guards
- Fix auto-title: route Ollama chat through native API with think=False
- Fix session cancellation: cascade to workers, kill processes, skip post-hooks
- Fix thinking-process title display and restore ticker scrolling
- Rename Chat to Session, add live activity ticker, fix thinking-process titles
- Rename Files panel to Explorer, add Skills tab with full CRUD management
- Fix settings tooltip clipping by using fixed positioning above modal overlay
- Add settings section tooltips and wire auto-evaluation into post-task hooks
- Fix snooze distillation crash when memory file parent directory doesn't exist
- Add typed failover errors, compaction-retry, deterministic tool ordering, session lane queuing
- Add disk-backed truncation, shlex bash security, structured tool metadata, atomic writes
- Fix send/stop button state bugs, modernize input area, add /cancel and /retry commands
- Polish UI: sidebar toggle, scrollbar placement, wider content, modal improvements, responsive fonts
- Add skill resource management: delete tools, overwrite feedback, executable fix, file_write access
- Fix toolmaker callable check, scout truncation, stuck detection drift, skill path messaging
- Add Skills system — filesystem-based capability packages with progressive disclosure
- Fix worker diagnostics, LLM slot starvation, memory bugs
- Fix reflect reading tool names from DB-stored tool_calls
- Fix tool name mapping for both tool_calls JSON formats
- Preserve browse_web enriched card on page refresh
- Pulse sidebar dots for active sessions (scouting/processing)
- Fix Playwright greenlet thread-affinity crash
- Update SPEC_v2.md: SSE drift prevention, reflect retry fix, model dedup
- Add SSE drift prevention: gap detection, reconciliation, health monitor
- Fix reflect retry state transition crash and model warning spam
- Fix asyncio task leaks and streaming state recovery after refresh
- Update SPEC_v2.md: single-stream SSE, stop button, snooze user insights
- Refactor to single-stream SSE: eliminate dual-stream race condition
- Add user insight extraction to Snooze idle-time agent
- Add stop button, fix stale question cleanup race
- Add ask_user notification system, reflect UI card, browse_web discovery
- --fresh now wipes memory files; filter polling endpoints from access logs
- Fix asyncio task leak, add tool arg visibility, add env settings tab
- Update SPEC_v2.md: Reflect hardening, DB hygiene, maintenance schedule
- Add DB hygiene: prune orphaned rows, WAL auto-checkpoint
- Skip duplicate user message save on Reflect retries
- Fix Reflect retry: use bounded while loop, verify each attempt
- Fix Reflect lifecycle: eliminate double hooks, off-by-one, add context
- Document Reflect in SPEC_v2.md: section 14.6, config, amendment
- Add Reflect SSE events and frontend handling
- Integrate Reflect into session lifecycle with retry support
- Implement Reflect: post-execution verification agent
- Add Reflect configuration: settings, session state, and UI section
- Improve scout intent matching: respect user's explicit tool preferences
- Always register browse_web, gate on browser_enabled at call time
- Add browser status bar indicator and enriched browse_web tool card
- Emit live browse.start/browse.done events from browse_web tool
- Add web access guidance to RULES.md and tool co-occurrence links
- Harden browse_web: SSRF protection, context isolation, resource limits
- Update SPEC_v2.md: audit fixes, browse_web tool, state machine cleanup
- Add Web/Browser section to settings modal
- Add Playwright browser cleanup to app lifespan shutdown
- Implement browse_web tool with Playwright + trafilatura
- Add trafilatura to optional requirements for browse_web content extraction
- Add browser settings: browser_enabled, browser_headless, browser_timeout
- Fix search_web type coercion bug for num_results parameter
- Show 'offline' in status bar when backend is unreachable
- Fix question modal auto-reopening after dismissal
- Fix shutdown event initialization race with threading lock
- Log cron protection list read failures instead of silently swallowing
- Lower compaction compression ratio gate from 50% to 35%
- Move compaction metadata to dedicated column, optimize session listing
- Remove dead RESETTING state from session state machine
- Protect emit_event sequence counter with threading lock
- Add thread-safe locking to scout cache
- Fix Phase C trimming: preserve _pinned messages during last-resort drops
- Fix worker spawn: remove deprecated asyncio.get_event_loop() fallback
- Add scheduler shutdown to app lifespan cleanup
- Fix scheduler init race condition with double-checked locking
- Fix markdown XSS: sanitize iframe, object, embed, form, style elements
- Fix FTS5 silent deletion failures: log errors instead of swallowing
- Fix done_sent flag: eliminate duplicate stream.done events
- WIP: snapshot before audit fixes
- Replace session badges with colored type dots and add legend footer
- Add sidebar session organization: time grouping, badges, tooltips, worker nesting
- Add jobs management system, global event bus, and scheduling enhancements
- Fix cross-session search: include user messages, add multi-query decomposition and context expansion
- Add scout cross-session FTS5 search and deep memory decomposition
- Auto-restore default model after agent-initiated switches
- Fix model switch registry gap and add cross-session artifact discovery
- Add model registry, fix OpenRouter streaming tool calls, normalize tool call formats
- Add tabbed settings modal with OpenRouter model management
- Redesign scout window: collapsed by default, show full report
- Add temporal context, tool call grouping, artifact viewer, code copy buttons, UI tweaks
- Add Snooze: idle-time memory consolidation and self-optimization
- Add tool output toggle, ephemeral scout visibility, and semantic dedup
- Fix event loop threading for tool extensions and harden scout fallback
- Add model-aware workers and scout-driven model selection
- Second-pass hardening: 9 fixes from verification audit
- Harden codebase: fix 15 critical/high issues from audit
- Fix tool chaining and final response after tool calls
- Add question notification and answer modal
- Fix bash fork error, ask_user aliases, and argument resilience
- Preserve settings.json on --fresh start
- Add --fresh flag and clean shutdown handling
- Fix 500 error on stale session SSE connection
- Fix Ollama native API tool message format
- Add OpenRouter model filtering and improve web search
- Add settings modal to frontend
- Fix duplicate token streaming and Qwen3 thinking mode
- Implement CAI v2 core system (Phases 1-10)
- Add project specification, docs, and configuration
