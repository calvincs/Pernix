# Adaptation plan — prime-agent-inspired upgrades (kernel, gates, goals, adaptive layer)

Status: **Phases 1, 2 AND 3 IMPLEMENTED** (2026-08-05/06, branch
`next-phase-features`) — Phase 1: 1a/1c/1d/1e/1f/1g; Phase 2: §10.11
socket fix, 2a scaffold modes + snapshot/restore + lock, 2b SessionKernel
lifecycle, 2c repl tool + binding; Phase 3: 3a gates (migration v22,
reflect clamp before post-mortem write, watch_paths reuse guard,
skipped-reflect retry fallback, H2 post-mortem fields), 3b goals
(migration v23, goal_id-stamped budgets incl. worker spend,
FINALIZING-only continuations on complete/round_ceiling/budget_exhausted
with LLM-clock extension, budget_limited notifications), 3c heartbeats
(steer = system row at next round boundary, parked states degrade to
follow_up, per-turn + queued coalescing, user/agent namespaces separated,
kind=heartbeat jobs riding the 1c round-trip + claim discipline). Full
suite: 1805 passed, 0 failed (1 darwin skip). Deviations noted inline as
[IMPL] / in §10. Phases 3.5/4 remain PROPOSED.

**Decisions by Calvin:** (1) design doc before any code; (2) autonomy model for
adaptive-layer changes = **auto-apply with rollback** for low-risk kinds,
proposal-gated for the rest; (3) out of scope for now: MCP support, native
Anthropic provider, converting skills to executable packages; (4) in scope:
native OpenAI provider for broader compatibility.

**Revision log:** 2026-08-05 pass 2 — added semantic retrieval (1f), canary
suite (Phase 3.5), horizon items (§9). 2026-08-05 pass 3 — four-agent
adversarial review (citation ground-truthing, doc consistency, cold-read
implementability, integration analysis); this revision folds in all confirmed
findings: corrected citations, per-phase acceptance criteria, seam
corrections (heartbeat delivery, gate clamping, kernel cancel path,
cache_control), the workspace-scoping prerequisite (1g), new `ToolDef` flags
(`idempotent`, `denied_session_types`), canary isolation predicates, and the
rename of Phase 4 from "Adaptive Harness" to **Adaptive Layer** — `harness`
already names a live Pernix subsystem (`core/harness/nudges.py`) and the
collision was guaranteed to misroute a downstream agent. 2026-08-05 pass 4 —
long-running-autonomy check: continuation triggers widened to
`round_ceiling`/`budget_exhausted` with LLM session-budget extension (3b);
composition note added (3d).

**Phase ↔ section map** (subsection labels are phase-based everywhere):

| Phase | Section | Subsections |
|-------|---------|-------------|
| — Invariants | §0 | I1–I8 |
| — Sources | §1 | — |
| 1 Foundations | §2 | 1a–1g |
| 2 Session kernel | §3 | 2a–2d |
| 3 Gates, goals, heartbeats | §4 | 3a–3d |
| 3.5 Canary suite | §5 | — |
| 4 Adaptive Layer | §6 | 4a–4f |
| — Cross-cutting rules | §7 | — |
| — Sequencing & burn-in | §8 | — |
| — Horizon | §9 | H1–H4 |
| — Open questions | §10 | — |
| — File impact | §11 | — |

Source material: structural comparison against
`/Users/calvincs/Projects/prime-agent` (v0.7.0) on 2026-08-05. Prime-agent is
a CLI coding agent; Pernix is a self-hosted personal agent server — we take
*patterns*, filtered through Pernix invariants. File:line references were
re-verified against the working tree on 2026-08-05 (pass 3).

The idea, in one sentence: Pernix has the **observation half** of
self-improvement (post_mortems, scout_signals, Candor, Dream, refine) but
never shipped the **actuation half** — this plan ships it, plus the execution
substrate (persistent session kernel), the verification substrate (gates,
goals), and the measurement substrate (canary suite) that make actuation
trustworthy, using prime-agent's proven safety patterns: immutable base
prompt, full before/after snapshots, exact reverse-order rollback,
plan/apply conflict detection, claim-before-deliver scheduling.

---

## 0. Invariants we hold (non-negotiable)

| # | Invariant | Where it lives |
|---|-----------|----------------|
| I1 | Message history is append-only; compaction is a view transform, never a mutation | `core/context/compaction.py:63-84,122` |
| I2 | The state graph is closed and hand-authored: single `transition()` mutator (`sessions/state_v2.py:280`), no `force_state()` (`:14`), append-only `session_state_log` (`:16`) | `sessions/state_v2.py` |
| I3 | Memory source of truth is human-editable markdown; indexes are rebuildable | `core/memory/store.py:42,885` |
| I4 | SOUL.md / RULES.md / SESSIONS.md and the base system prompt are user-owned — the machine never writes them | `core/context/compiler.py:340,458` |
| I5 | Scout curates; the agent sees a small tool surface | `core/scout/runner.py`; active-tools slice `core/agent.py:502-532` |
| I6 | High-consequence self-modifications flow through human approve/deny proposals | `core/refine.py:12-13`; `db/models.py:1821` |
| I7 | Add-ons default off; sandboxing is defense-in-depth, not a boundary | `docs/security.md:18,29`; `config.py:185,195,210` |
| I8 | Prompt-cache-stable assembly: volatile content in the suffix, never the prefix | `core/context/compiler.py:670,574` |

**On "letting the state machine grow":** rejected as *topology* growth — I2
is why crash recovery works. Adaptation lives in **policy operating within
states**: adaptive-layer entries and scout guidance (Phase 4), heartbeat
steering (Phase 3). No phase requires a new state. A future state is a
hand-written graph change plus `docs/internals/state-machine.md` §0 update,
never machine-generated.

---

## 1. What we're taking from prime-agent (and what we're not)

Adopted: **(1)** persistent execution kernel as a first-class session surface
(not the one-tool thesis — I5 stays) → Phase 2; **(2)** per-variable state
snapshots (skip-and-report unpicklables) → Phase 2; **(3)** immutable-base +
versioned supplemental state + exact rollback + plan/apply conflict detection
(their "Continual Harness" → our **Adaptive Layer**) → Phase 4;
**(4)** deterministic verification gates with unchanged-workspace guard →
Phase 3; **(5)** persistent goals with budgets, explicit completion only →
Phase 3; **(6)** heartbeats distinct from cron, user/agent namespaces
separated → Phase 3; **(7)** claim-before-deliver scheduling + "uncertain,
not replayed" → Phase 1; **(8)** test discipline (regression pinning, faux
provider, honest coverage) → Phase 1.

Beyond both projects (added pass 2): **(9)** local semantic retrieval — both
projects are purely lexical → 1f; **(10)** golden-task canary suite —
neither project can measure whether self-improvement improves anything →
Phase 3.5; **(11)** horizon items with compatibility hooks → §9.

Deferred / rejected (per Calvin): **MCP** (later; their in-kernel-SDK
pattern is right for us and Phase 2 is its prerequisite), **native
Anthropic** (caching arrives via Phase 1 anyway), **executable skills** (we
keep instructions + scripts; tooling ideas only → 1d), **one-tool surface**
(scout curation is a strength), **fully-autonomous auto-refine** (we use
risk-tiered graduated autonomy).

---

## 2. Phase 1 — Foundations

### 1a. Native OpenAI provider

New `core/llm/providers/openai.py`. The internal format is already
OpenAI-compatible; the work is transport + **de-two-providering the router**.

- **The real provider surface** is `ProviderProtocol`
  (`core/llm/providers/base.py:16` — `chat`, `chat_stream`, `get_model_info`,
  `list_models`, `check_health`, `close`) **plus two members the Protocol
  omits but the router/registry require**: `available`
  (`core/llm/router.py:106`) and `clear_models_cache()`
  (`core/llm/registry.py:114`). Implement all eight.
- **`chat_stream` contract** (mirror `openrouter.py:163-327`): index-keyed
  tool-call delta accumulation with flush on `[DONE]` *and* in `finally`;
  `DONE` event carries `finish_reason` (consumed at `core/agent.py:1014`);
  `CONTEXT_OVERFLOW` raised as `FailoverError`, never yielded as ERROR;
  `GeneratorExit` handled.
- **Generalize the router to a provider map.** `ProviderRouter` is
  structurally two-provider: hardcoded instances + semaphores
  (`router.py:88-100`), `get_semaphore()` if/else (`:110`), fallback tested
  as `provider is self._openrouter` six times (`:163,:171,:177,:199,:210,
  :219`), `ModelRegistry.populate(ollama, openrouter)` fixed 2-arg
  (`registry.py:41`), `purge_session` iterating a literal semaphore tuple
  (`client.py:214`). Decision: refactor to `dict[str, Provider]` +
  `dict[str, SessionAwareLLMScheduler]` keyed by provider name, with
  `fallback_eligible(provider)` replacing identity checks. Do NOT copy-paste
  a third branch.
- **Fallback truth**: `FALLBACK_REASONS = {RATE_LIMIT, OVERLOADED, TIMEOUT,
  UNKNOWN}` (`core/llm/errors.py:25-32`) — four reasons, not two. OpenAI
  gets the same set. `sanitize_for_fallback` (`router.py:26,60-80`) applies
  unchanged.
- **Routing bare names**: `resolve_provider`'s heuristic
  (`registry.py:109`) routes `"/"`-less names to Ollama, so `gpt-4o` would
  misroute. Rule: membership in `openai_models` wins before the heuristic
  (same shape as the `openrouter_models` whitelist, `registry.py:63-72`).
  `resolve_model_id` (`registry.py:122`) does *suffix* matching — bare
  OpenAI IDs pass through unchanged; no work needed there.
- **`core/agent.py` must change** (missing from pass-2 plan):
  `normalize_for_openrouter()` (`compiler.py:1172`) — the strict
  OpenAI-format normalizer — is gated on `resolve_provider(model) ==
  "openrouter"` at `agent.py:774,951,1633,1710`, and `is_openrouter_model()`
  (`router.py:21`) gates fallback format decisions at `agent.py:940,1699`.
  Introduce `provider_needs_openai_normalization(model) -> bool` covering
  both providers; replace all six call sites.
- **Config**: `openai_base_url` (default `https://api.openai.com/v1`;
  overridable → vLLM/LM Studio; precedent `voice_remote_url`,
  `config.py:270`), `openai_models: list` (env `OPENAI_MODELS`, mirroring
  `config.py:461-465`), `openai_max_concurrent: int = 2`. **The API key is
  env-only** (`OPENAI_API_KEY`) — there is deliberately no key field on
  `Settings` because `settings.json` is plaintext on disk; add
  `OPENAI_API_KEY` to the `/api/settings/apikey` allowlist
  (`api/routers/health.py:311`; it's already in the existence check at
  `:409`).
- **Cache observability — no migration.** `token_usage` already has
  `cache_read_tokens`/`cache_write_tokens` fully plumbed
  (`db/database.py:145-146` → `core/llm/types.py:33-34` →
  `openrouter.py:146-147` → `agent.py:860-861` → `db/models.py:790-809,
  823-824`). Map OpenAI's `usage.prompt_tokens_details.cached_tokens` →
  `cache_read_tokens`. UI: there is **no usage panel today** —
  `GET /api/usage/{session_id}` exists (`api/routers/chat.py:450`) but
  nothing in `static/` renders it beyond a sidebar tooltip
  (`static/js/app.js:522`); add cache-hit display there (small UI task, in
  scope).

**Done when:** (1) `tests/test_llm_providers.py` gains OpenAI parse tests
mirroring the OpenRouter ones (streaming tool-call accumulation,
200-with-error body, usage/cache mapping); (2) a FauxProvider test scripts a
429 and asserts Ollama fallback + `sanitize_for_fallback`; (3)
`resolve_provider` returns `openai` for a whitelisted bare name and `ollama`
on collision; (4) the normalization helper has a regression test across all
six former call sites; (5) `settings.js` shows the new fields (§7 rule).

### 1b. Prompt-cache breakpoints for OpenRouter/Anthropic (optional — resized M, sequenced after 1a)

Pass-3 finding: the pass-2 mechanism was impossible. Both target boundaries
live *inside one string* — `system_prompt = "\n\n".join(system_parts)`
(`compiler.py:719`) emitted as a single `messages[0]`; and
`_strip_private_fields` runs unconditionally inside `compile_context`
(`:952`), before any provider is chosen, so a private marker never reaches
the provider.

Respec: (a) `compile_context` returns **boundary offsets** in the payload
(end of static system block, end of scout section) rather than marking
message dicts; (b) the OpenRouter provider, for `anthropic/*` models,
converts `messages[0]` into content-parts form and attaches `cache_control`
at those offsets inside `normalize_for_openrouter`; (c) `_count_text_cached`
treats the parts as concatenated text. `sanitize_for_fallback` already
rebuilds messages as bare `{role, content}` (`router.py:60-80`), so nothing
survives into the Ollama path — the pass-2 watch item is resolved by
existing code. Ship only after 1a's cache metrics exist, so the win is
measurable. Config `openrouter_cache_control` (default on).

**Done when:** a FauxProvider test asserts parts-form + `cache_control`
present for `anthropic/*` via OpenRouter, absent for everything else, and
absent after fallback sanitization; `cache_read_tokens` visibly nonzero
against a real Anthropic-via-OpenRouter model.

### 1c. Cron claim-before-deliver

Corrections from pass 3: statuses today are `running → completed | error`
(`db/models.py:962,968`; UI reads `running` at
`core/extensions/scheduling/__init__.py:452`) — keep those names, add
`claimed` and `uncertain`. There is **no persisted fire-time anywhere**:
APScheduler uses the default in-memory jobstore (`scheduling/__init__.py:37`)
and jobs rebuild from `data/cron_jobs.json` on boot (`_load_jobs`, `:58`), so
missed-tick coalescing is currently uncomputable.

- Migration **v21**: `cron_runs.fire_time` column (DDL lives in
  `db/database.py:192-203`, not `db/models.py`).
- Add `last_fired_at` to the persisted job schema in `data/cron_jobs.json`.
  Prerequisite fix (also needed by 3c/3.5): `_save_jobs`/`_load_jobs`
  currently **drop unknown fields on restart** (`:64-71,:92-107`) — make
  them round-trip `extra_meta` verbatim.
- Claim sequence, before `manager.prompt`: insert `cron_runs` row
  (`status='claimed'`, `fire_time`) + update `last_fired_at` + `_save_jobs`.
  Then `running → completed | error` as today.
- Startup reconcile: rows stuck `claimed`/`running` → `uncertain`, never
  replayed, user notified. **Must run before `init_scheduler()`**
  (`api/app.py:186-190`) so no job fires into a half-reconciled table.
- Coalescing: on boot, if `last_fired_at` is ≥2 fire-times behind, dispatch
  exactly one run carrying `[coalesced N missed runs since <ts>]`. Note
  interplay: APScheduler's `misfire_grace_time=300`
  (`scheduling/__init__.py:174`) already suppresses some in-process
  misfires; the new logic covers only downtime gaps.

**Done when:** a test kills the process between claim-write and
`manager.prompt`, restarts, asserts `uncertain` + no re-dispatch + a
notification; a 3-missed-tick test asserts exactly one coalesced dispatch;
extra_meta survives a save/load round-trip.

### 1d. Skills tooling

Correction: the health check **half-exists**. `SkillRegistry._validate()`
already runs `py_compile` over `scripts/*.py` at scan time
(`core/skills/registry.py:374-417`, from `scan()` at `:260`), and scout
already consults `is_valid()` (`core/scout/runner.py:669,1866`). But
`_invalid` is a `set[str]` — **the reasons are logged and discarded**
(`:264`), and `load_skill` never consults validity
(`core/tools/builtin/skill_tools.py:44-85`).

- Change `_invalid: set[str]` → `dict[str, list[str]]` (name → issues); add
  a `health` field to `SkillDef`. Add `bash -n` for `.sh` and a
  `requirements.txt`-satisfied check against the workspace venv
  (`config.py:343`, `core/tools/paths.py:113`).
- `load_skill` on a broken skill returns the concrete reason + remediation;
  L1 catalog keeps listing it; UI badge on the skills panel.
- **No network installs inside `scan()`** — it runs in the startup path
  (`api/app.py:80`) and on every skill PUT/PATCH (`registry.py:295`).
  Hash-triggered `requirements.txt` install runs as a snooze activity
  (ladder position per §7) with the hash persisted in `snooze_state`
  (keyed `skill_reqs_hash:<name>`).
- Frontmatter `scripts: [{path, purpose, usage}]` rendered into the L2
  injection; verify `core/skills/parser.py` tolerates the new key (unknown
  frontmatter keys must warn, not fail); `add_skill_script` learns to write
  it.

**Done when:** a skill with a deliberately broken `scripts/fetch.py` still
appears in `discover_skills`, `load_skill` returns the SyntaxError line +
remediation, and `tests/test_skills_registry.py` pins both; a requirements
change triggers exactly one install in the next snooze cycle.

### 1e. Test discipline

- Remove `core/extensions/*` from coverage omit (`pyproject.toml:32`);
  **in the same commit**, lower `fail_under` (`:40`) to the measured number
  so CI stays green, then ratchet (schedule in §10.6).
- `tests/regressions/test_<date-or-issue>_<slug>.py`, one file per shipped
  defect.
- **FauxProvider**: there is no provider registration seam today —
  `ProviderRouter.__init__` constructs its providers directly
  (`router.py:88`). 1a's provider-map refactor IS the seam: FauxProvider
  registers into the map under tests. Relationship to the existing
  `FakeLLMClient` (`tests/conftest.py:54-140`): FauxProvider does not
  replace it — `FakeLLMClient` stays for agent-loop tests; FauxProvider
  covers router/failover/semaphore paths that a client-level fake cannot
  reach. State this in the test README.

**Done when:** coverage gate includes extensions and CI is green; one
regression file exists as the pattern exemplar; a FauxProvider-scripted 429
exercises the real router fallback path end-to-end.

### 1f. Local semantic retrieval (hybrid BM25 + embeddings)

Fifth model role: `embedding_model` (`config.py:60-63` pattern; empty = off;
setting the role is the switch). Degrades to today's lexical behavior when
unset/unavailable (the APScheduler precedent,
`scheduling/__init__.py:42`).

- **Provider plumbing (was unscoped)**: `OllamaProvider` has no embed
  endpoint today (`ollama.py` implements `/api/chat`, `/api/show`,
  `/api/tags` only). Add `embed(model, texts) -> list[vec]` to the provider
  (native `/api/embed`, derived via the existing
  `base_url.replace("/v1","")` pattern, `ollama.py:94`) and
  `LLMClient.embed()`. **Every embed call goes through the Ollama
  scheduler at `PRIORITY_BACKGROUND`** (`semaphore.py:14-16`) — a direct
  httpx call would bypass `llm_max_concurrent=1` (`config.py:64`) and,
  worse, evict the chat model from VRAM mid-turn (multi-second stall). The
  scheduler sorts background behind live turns (`semaphore.py:27-33`), so
  starvation of turns is structurally impossible.
- **Storage (I3-compliant)**: `vectors` table inside the existing
  `data/memories/_index.db`, keyed **`(file_name, epoch)`** — epochs are
  unique only per file (`store.py:180-184`; `memory_hits` already uses the
  composite key, `:785-789`). Columns: `file_name, epoch, model, dim,
  content_hash, vec BLOB, PRIMARY KEY(file_name, epoch)`. The memory DB has
  **no migration ladder** — schema goes into `_MEMORY_SCHEMA`
  (`db/database.py:767`) with an idempotent `CREATE TABLE IF NOT EXISTS`
  plus a `meta` table recording embedding model + dim; model mismatch marks
  all rows stale.
- **Write path (the sync/async bridge — was the 1f blocker)**:
  `MemoryStore.add_entry()` is synchronous and lock-held
  (`store.py:97-217`); an inline async embed is impossible. Design:
  writes only *mark* (the vectors row is simply absent or hash-stale);
  embedding is **batch work** — primarily snooze Activity 5
  (index reconciliation, `core/snooze.py:430,1688`) embeds all
  missing/stale rows; optionally a fire-and-forget nudge on write via the
  single-thread-executor pattern of `_candor_attest`
  (`store.py:57-78`). Search never blocks on embedding: entries without
  vectors simply don't participate in the vector channel yet.
- **Retrieval**: extend the existing `search_hybrid`
  (`core/memory/search.py:296`) — same name, same callers, new optional
  vector channel: BM25 top-K ∪ cosine top-K (brute-force over an in-memory
  float32 matrix, loaded lazily, invalidated on write; numpy only, no ANN
  dep), fused with RRF (k=60). Existing signals unchanged: `search_recent`
  stays inside `search_hybrid` (`:311-319`); `search_lessons` age decay is
  a separate path (`store.py:797-835`) — give it the vector channel in a
  second step. `reindex()` (`store.py:885`) deletes/rebuilds FTS only; it
  leaves `vectors` alone except pruning rows for entries that no longer
  exist — **it does not re-embed**, so `health_check(fix=True)` (which
  calls reindex synchronously at first store access, `store.py:1043,
  1077-1086`) stays fast and sync-safe. Re-embedding is always snooze work.
- **Surfaces, in order**: (1) memory `search_hybrid`; (2) `search_sessions`
  (`core/tools/builtin/session_search.py`); (3) `ToolIndex`/`SkillIndex`
  optional semantic channel, demoting `SYNONYMS`/`SKILL_SYNONYMS`
  (`core/tools/registry.py:22`, `core/skills/registry.py:21`) to fallback —
  retirement criterion: two weeks of scout tool-selection parity on the
  canary suite; (4) Phase 4 `search_adaptive` free.
- Config: `embedding_model`, `embedding_batch_size` (16).

**Done when:** a fixture corpus (~50 entries, 10 labeled query→entry pairs)
shows recall@5 ≥ lexical baseline; with `embedding_model` unset every
existing memory test passes unchanged; killing Ollama mid-search degrades to
lexical with a warning; a test asserts `reindex()` leaves `vectors` in the
defined state (prune-only); an Ollama-slot test asserts embeds never
preempt a live turn.

### 1g. Session-scoped workspace override (new — prerequisite for Phases 2, 3, 3.5, H1)

Pass-3 finding: there is exactly **one global workspace**
(`config.py:334`; every file-tool root resolves from it,
`core/tools/paths.py:28-46`). Three later features silently assumed
otherwise. Decision: **the shared workspace stays the default** (it is the
user's file area; sessions sharing it is a feature), and we add optional
per-session overrides:

- `AgentSession.workspace_override: Path | None`; `paths.py` resolvers gain
  a session-aware entry point (`workspace_for(session) -> Path`) falling
  back to the global. File tools receive the session via the existing
  `_context` (`executor.py:136` already passes `session_id`).
- Users: canary runs (temp dir per run, §5), kernel snapshot/payload dirs
  (`data/kernels/<sid>/`, §3 — note the kernel's *cwd* stays the shared
  workspace so `repl` and `bash` see the same files), H1 checkpointing.
- Gate fingerprinting (3a) does NOT use this — it scopes by per-gate
  `watch_paths` instead (the global tree churns from unrelated
  workers/cron; a whole-tree fingerprint is meaningless).

**Done when:** a session with an override reads/writes only inside it via
`file_read`/`file_write`/`glob`/`grep`/`bash` cwd; a session without one
behaves byte-identically to today (regression test).

---

## 3. Phase 2 — Session kernel (generalize RLM's ChildREPL)

`ChildREPL` (`core/extensions/rlm/child_env.py:127`) already provides the
sandboxed child: `setsid` (`:166`), RLIMIT_AS/FSIZE (`:167-168`),
scrubbed env (`:111-124`), `start/execute_cell/interrupt/kill/cleanup`,
AF_UNIX socket (`:158`); PDEATHSIG lives in the child at
`child_runner.py:362-369`. Phase 2 promotes it to a per-session persistent
workspace. New module `core/kernel/`; shared pieces may extract to
`core/kernel/child.py` with RLM importing back.

### 2a. Runner divergence and protocol extension

- `ChildREPL`/`child_runner` gain a **`scaffold` mode** (`"rlm" | "plain"`),
  negotiated at handshake. Plain mode omits the RLM scaffolding —
  `llm_query`, `rlm_query`, `SHOW_VARS`, the `_AnswerDict` `answer`, and
  `_restore_scaffold()` re-installation (`child_runner.py:205-221,280`) —
  which would otherwise hang on a nonexistent broker socket. Keep
  `_SAFE_BUILTINS` (note: `exec`/`eval` are `None`, `child_runner.py:
  171-179` — document this in the `repl` tool description so the agent
  isn't surprised).
- **New frame types** `snapshot` / `restore` in `child_runner.py:388-408`
  (today: `exec`, `load_context`, `shutdown` only). Snapshot serializes the
  namespace with `dill` **one top-level name at a time**, skipping and
  reporting unpicklables; writes `data/kernels/<sid>/kernel-state.dill` +
  `manifest.json` atomically; cap `kernel_snapshot_max_bytes` (256 MiB).
  This file is shared with RLM — every change lands with RLM regression
  tests. (Historical note: RLM deliberately *removed* dill in favor of a
  persistent child, `child_env.py:10`; we reintroduce it only for
  cross-restart revival, which RAM cannot provide. The persistent child
  remains the primary mechanism.)
- `ChildREPL` is synchronous with **no concurrency guard** (bare `_conn` +
  `_exec_id`, `child_env.py:148-150`); add a lock around round-trips before
  anything but RLM's single-driver broker touches it. Session-side calls
  run via `asyncio.to_thread`.
- Child interpreter = the workspace venv (`settings.workspace_venv_python`,
  `config.py:341-343`) so `install_package` results import — note this
  **changes the current default** (`_build_child_env` includes venv bin only
  conditionally, `child_env.py:171`; RLM's child runs `sys.executable`).
  Apply to plain mode only; RLM keeps its current env.

### 2b. Lifecycle, cancel path, reaping (pass-3 critical fixes)

- **Own slot: `session._kernel`, never `session._active_process`.** That
  slot is a per-tool-call convention set/cleared in `finally` by `bash`
  (`core_tools.py:642,658`) and RLM (`rlm/__init__.py:512,640`), and its
  consumers kill unconditionally: `_kill_tool_subprocess` on *any* dispatch
  timeout in the session (`executor.py:85-90`) and the Cancel button's
  process-group SIGTERM (`api/routers/sessions.py:234-240`). A kernel
  parked there dies the first time an unrelated bash times out. The kernel
  gets `session._kernel` with a `cancel_cell()` (SIGINT → grace → SIGKILL,
  RLM's discipline) and a `shutdown(snapshot: bool)`; `repl` registers only
  the *cell* for interruption; both kill paths explicitly skip `_kernel`.
- **Reap timing**: `kernel_idle_seconds` default **1500** — strictly below
  `reap_idle_sessions(max_idle=1800)` (`maintenance.py:188`), because
  session reap pops the `AgentSession` (`sessions/manager.py:529-533`) and
  the `setsid`-ed child would outlive it as a ~100 MB orphan (PDEATHSIG
  only fires on server death). Belt and braces: `SessionManager.remove()`
  gains a kernel-cleanup hook (snapshot + kill).
- **Snapshots run off the tick's critical path**: `maintenance._tick()` is
  bounded by `TICK_TIMEOUT=30` (`maintenance.py:18,106`); a 256 MiB dill
  round-trip inside it would freeze the loop. Follow the `_run_snooze`
  outside-the-tick precedent (`maintenance.py:121-147`): the tick only
  *schedules* reaps; snapshot+kill runs in `asyncio.to_thread`.
- `kernel_max_concurrent` (default 3): live-kernel registry owned by
  `SessionManager`; LRU-idle reap (with snapshot) beyond the cap.
- Revival: first `repl` call after restart restores and prepends
  `[kernel revived: N names restored, M skipped (name: reason)]`.

### 2c. The `repl` tool and result binding

- ToolDef: `repl(code, timeout?)` — `timeout=300`, **`max_timeout=1800`**
  (mandatory when the schema exposes `timeout`, else the override silently
  no-ops — `registry.py:119-124`, `executor.py:48-65`); `parallel_safe=False`
  (default); `long_poll=False` — the 16-thread long-poll pool is for
  99%-blocked orchestration waits, not active compute
  (`executor.py:274-281`); `safety_level` matching `bash`'s registration
  (same power, same posture); `worker_allowed=True` (workers get their own
  kernels; flat hierarchy unchanged).
- **New `ToolDef.idempotent: bool = True` flag, `False` for `repl`.**
  Cross-round dedup (`agent.py:1249-1258` + `_CROSS_ROUND_DEDUP_EXCLUDED`,
  `:358`) would otherwise stub out a repeated identical cell
  (`next(pages)` twice) with a fabricated "already executed" result and the
  kernel would never advance. Check the flag at `agent.py:1249` instead of
  growing the hardcoded set.
- **Never return empty string**: an assignment-only cell produces no
  stdout, and `executor.py:304` classifies empty results as errors —
  feeding `record_failure`, StuckDetector's `tool_failure_loop`
  (`agent.py:219-224`), and Candor. Emit `(no output)` (the literal
  `_LOW_INFO_RESULT_RE` at `agent.py:130` already expects). In-cell Python
  tracebacks are **not** tool errors (iterative debugging is normal REPL
  use); `Error:` is reserved for kernel-level failures.
- **Result binding (prompt-as-variable)**: post-pass in
  `execute_tool_round` (outside per-call timeouts). When a result from a
  binding-eligible tool (`file_read`, `http_get`, `browse_web`,
  `session_read`) exceeds `large_result_bind_threshold` (20_000 chars) and
  the kernel is enabled: **spill the full payload to
  `data/kernels/<sid>/payloads/<n>.txt`**, record
  `{bound_var, payload_path, size}` in the tool message's metadata, load it
  into the kernel as `tool_result_<n>` (generalize
  `stage_context`/`load_context`, `child_env.py:71,198` — arbitrary var
  names), and store head+tail + `[full 812KB payload bound as
  tool_result_7 — use repl to slice/search it]` as the message content.
  The sidecar keeps the transcript reconstructible (I1 is about history
  integrity — a bound-and-spilled payload is a view transform with a
  durable sidecar, not a discard). Reflect impact is benign (it already
  caps bodies at 5000 chars, `core/reflect.py:332`), but update
  `REFLECT_PROMPT`'s "verbatim from the actual tool result" clause
  (`reflect.py:44,50`) to acknowledge stubs.
- Scout: `repl` discoverable via 1f's semantic channel; add `SYNONYMS`
  entries **only if** `embedding_model` is unset (don't grow a map 1f is
  retiring). Kernel state is compaction-proof by construction (I1) —
  document in the tool description and `docs/architecture.md`.
- Not in v1: `llm_query` in session kernels (RLM broker stays RLM-only),
  kernel sharing, forkserver prewarm.

Config: `session_kernel_enabled` (off), `kernel_idle_seconds` (1500),
`kernel_snapshot_max_bytes` (256 MiB), `large_result_bind_threshold`
(20_000), `kernel_max_concurrent` (3).

**Done when:** define `x=1` in `repl`, force `compact_with_llm`, assert `x`
resolves; snapshot a namespace containing a socket → manifest lists it
skipped, other names revive; bind a 1 MB `file_read` → model-visible result
< 2 KB, `tool_result_1` sliceable, payload file exists and is referenced in
message metadata; repeat an identical `repl` call → two real executions;
cancel-mid-cell → SIGINT, namespace survives; unrelated bash dispatch
timeout → kernel untouched; RLM's full test suite green.

---

## 4. Phase 3 — Gates, goals, heartbeats

### 3a. Deterministic gates

- **Definition**: `add_gate(name, command, watch_paths?, cwd?)` /
  `list_gates` / `remove_gate`. Migration **v22**: `gates` table
  (`id, session_id, scope ∈ session|goal|canary|step, name, command,
  watch_paths_json, cwd, enabled, created_at`). One ownership model: gates
  are *always* rows; canary frontmatter and `StepDef.gate` (a gate *name*
  defined in workflow frontmatter, resolved to a scoped row at run start)
  materialize into it. Tools live beside `evaluate`
  (`core/extensions/evaluation/__init__.py:218`); note the feature tools
  (`add_feature` etc.) are in the *planning* extension
  (`core/extensions/planning/__init__.py:30,92`) — leave them there.
- **Where gates run**: inside the post-hook chain — `_finalize_turn`'s
  retry loop calls `_run_post_hooks` per iteration
  (`sessions/manager.py:1179-1180`) → `run_post_task_hooks`
  (`sessions/hooks.py:42`); gates execute immediately before
  `_maybe_reflect` (`hooks.py:64`). **Gates therefore re-run on every
  reflect retry attempt** — that is intended and is what the
  unchanged-workspace guard exists for. Results keyed by
  (`_turn_id`, `_retry_index`) (`sessions/state_v2.py:344-347`).
- **Clamp location**: inside `reflect_on_session`, **before**
  `_write_post_mortem` (`core/reflect.py:882`) — so the post-mortem records
  the *clamped* verdict (Phase 4's tripwire reads post-mortems; an
  unclamped record would poison it). While in there: add the **turn's
  model** and `execution_mode`-derived task category to the post-mortem
  payload (`reflect.py:899-940` currently records only the judge's
  `reflect_model` — this is H2's enabling hook, one line each).
- **When reflect is skipped** (`reflect_enabled` off, AWAITING_USER,
  retry cap, min-messages — `hooks.py:319,333,396`): a failing gate sets
  `reflect_retry_requested` directly, subject to the same
  `reflect_max_retries` cap.
- **Feedback channel to the retrying agent** (was unwired): gate output is
  filtered from the agent's context (`role="eval"` is dropped at
  `compiler.py:789` *and* by reflect's own transcript builder,
  `reflect.py:380-384`). Two wires: (1) into the reflect prompt via the
  existing `extra_evidence` kwarg (`hooks.py:513`) as a `GATE EVIDENCE`
  section; (2) into the retry itself via `build_retry_context`
  (`hooks.py:558`) → `session.reflect_lessons`, which is the only channel
  the next attempt's scout message actually carries
  (`manager.py:1518-1519`).
- **Persistence**: reuse `role="eval"` rows with a payload discriminator
  (`{"kind": "gate", ...}`) — `hooks.py:808`'s existing eval payload gets a
  `kind` too; UI switches on it.
- **Unchanged-workspace guard, rescoped**: fingerprint only the gate's
  declared `watch_paths` (git plumbing when inside a repo, else mtime
  scan) — the global workspace churns from unrelated sessions, and turns
  whose deliverable isn't a file (a chat answer, a scheduled job) would
  otherwise pin to a stale fail forever. Never applied on the first retry.
  Message on reuse: "gate `tests` not re-run: no changes under
  watch_paths since last failure."
- **Execution**: cwd = gate's `cwd` or the session's workspace (1g);
  bounded output (~4 KB tail); per-gate timeout (default 120 s);
  sequential. Gates run for `normal` and `worker` sessions; a gate is
  user-authored shell executed automatically — creating one is
  `safety_level="caution"`.
- Honesty rule, documented: a passing gate verifies only what it checks.
- Workflows: `StepDef` gains the optional field
  (`core/workflows/parser.py:19-34`) — touch parser, validator
  (`core/workflows/validator.py`), and the WORKFLOW.md format doc together.

**Done when:** a gate scripted to exit 1 produces verdict=retry though the
reflect model said pass, and the post-mortem records the clamped verdict; a
retry with no watch_paths change reuses the failure and the transcript says
so; gate output reaches the retry attempt via reflect_lessons (asserted in
the scout message); `goal_complete` with failing goal-scoped gates is
refused; with `gates_enabled=false`, zero behavior change.

### 3b. Persistent goals

- Migration **v23**: `session_goals` (`id, session_id, objective` ≤4000,
  `status ∈ active|paused|budget_limited|complete|error`, budgets, usage
  counters, timestamps) **plus `token_usage.goal_id`** (nullable, stamped at
  write time in `add_token_usage`, `db/models.py:784`) — because
  `token_usage` has no parent rollup and workers bill to their own session
  ids (`db/database.py:138-153`): a goal ambitious enough to budget is
  exactly one that fans out. Workers spawned during an active goal inherit
  the `goal_id` stamp. Exempt rows with a live `goal_id` from
  `prune_orphaned_token_usage` (`maintenance.py:237`, 30-day TTL).
- Tools: `goal_create`, `goal_status`, `goal_update` (sets `paused`/
  resumes/edits budgets), `goal_complete` (the only path to `complete`;
  refused while goal-scoped gates fail). Prompt guidance: goals come from
  explicit user intent, never inferred. (Note: an orphaned
  `max_continuations: int = 5` exists at `config.py:85`, referenced
  nowhere — delete it in this phase to avoid confusion with
  `continuation_budget`.)
- **Compiler, cache-safe split**: static goal fields (objective, budget
  ceilings, status) render after the scout section; **live burn goes in
  `_build_volatile_tail`** (`compiler.py:574`) — `compile_context` runs
  once per *round* (`agent.py:686`), so burn in the head would invalidate
  the cached prefix every round. Regression test: system-prompt head
  byte-identical across two rounds of one turn with an active goal.
- **Continuations, exact enqueue rules** (pass-3 F5; pass-4 autonomy
  check): trigger on `termination_reason ∈ {complete, round_ceiling,
  budget_exhausted}` (`sessions/state_v2.py:68-74`) — `round_ceiling` and
  `budget_exhausted` are precisely the long-running case (the turn ran out
  of tool rounds or LLM session budget mid-goal, not out of work); never on
  `cancelled` / `error` / `compaction_failed` (user intervention or infra
  faults need a human). Enqueue only after the reflect-retry `while` loop
  breaks (between `manager.py:1231` and `:1376`), only when the state is
  FINALIZING; as a synthetic `PendingMessage(msg_id=None)` so
  `_process_pending`'s existing budget-reset rule handles it
  (`manager.py:1869-1885`). **For `budget_exhausted` triggers the enqueue
  must also extend the session's LLM time budget via the scheduler's
  existing `extend_session_budget` seam** (`core/llm/semaphore.py`) —
  synthetic messages deliberately don't get the reset real user messages
  get, so without the extension the continuation inherits the exhausted
  budget and dies immediately, stalling the goal forever; once a goal is
  active, the goal's own token/time budgets are the governing limit.
  **Refuse** when the turn ended
  `AWAITING_USER` or `pending_messages` is non-empty
  (`_run_post_hooks` early-returns on pending, `:1974`, and a queued
  retry is dropped by `:1188-1205` — a mis-timed continuation silently
  disables reflect). Content borrows their language: *"The goal is still
  active. Continue, or report blockage with host-observable evidence — do
  not end the goal yourself."* Append the continuation ordinal
  (`continuation 2/3`) — **also defeating scout's 5-minute prompt cache**
  (`core/scout/runner.py:1014-1036`), which would otherwise replay an
  identical plan for byte-identical synthetic prompts.
  Budget exhaustion → `budget_limited` + notification, never silent.
  Default `continuation_budget=0` (opt-in per goal).
- Startup reconcile: goals `active` on boot with no live session task →
  stay `active`, note appended; goals of deleted sessions → `error`.

**Done when:** `continuation_budget=2` yields exactly two continuations then
a `budget_limited` notification; default 0 yields none; a continuation is
refused in AWAITING_USER; worker spend appears in `goal_status` totals; the
head-stability I8 regression test passes.

### 3c. Heartbeats

- **Delivery seam (pass-3 correction)**: NOT the harness-nudge seam — that
  is a stateless regex over tool-result bodies
  (`core/harness/nudges.py:134`, invoked `agent.py:1481-1486`), fires only
  when a tool ran, and appending to a tool row would permanently pollute
  the persisted result. The real seam: the scheduler writes a
  `role="system"` row with `metadata={"heartbeat": true}` via
  `db.add_message`; `compile_context` re-runs at the top of every round
  (`agent.py:685-696`) and picks it up (mid-conversation system rows are
  converted to user-role carriers by `normalize_for_openrouter`,
  `compiler.py:1177-1187` — already exercised by `agent.py:1191`). If the
  session is idle: queue as a normal prompt.
- **`steer` degradation**: a session parked in `AWAITING_WORKERS` inside
  a long-poll `await_workers` (up to 30–60 min, `executor.py:274-290`)
  reaches no round boundary — `steer` delivery there (and in
  `AWAITING_USER`) degrades to `follow_up` explicitly.
- Heartbeat rows are excluded from reflect/distill evidence the way
  `notice` rows are (they are machine text, not user intent).
- Two namespaces, enforced in storage: job records carry
  `owner ∈ user|agent`; the agent-facing tools (`set_heartbeat`,
  `clear_heartbeat`, `list_heartbeats`) operate only on `owner="agent"`
  rows for their own `_context["session_id"]`; the user heartbeat is set
  via UI/API only. `every` accepts `"5m"`-style durations or cron
  expressions (`parseAgentCronSchedule` precedent from prime-agent noted;
  implement with existing croniter/APScheduler parsing). A heartbeat whose
  previous firing is still undelivered coalesces (no stacking).
- Persistence rides `data/cron_jobs.json` with `kind: heartbeat` in
  `extra_meta` — **depends on 1c's round-trip fix** (today unknown fields
  are dropped on restart). Fires through 1c's claim-before-deliver.

Config: `gates_enabled`, `goals_enabled`, `heartbeats_enabled` (all default
off — pass-3 fix: Phase 3 previously shipped flagless).

**Done when:** a steer heartbeat lands mid-turn at the next round boundary
(asserted via transcript order); one parked in AWAITING_WORKERS arrives as
follow_up after the turn; an agent cannot see or clear the user's heartbeat;
heartbeat jobs survive a restart; all three flags off → zero behavior change.

### 3d. Long-running autonomy — how the pieces compose

The plan's equivalent of prime-agent's autonomous mode is not one feature
but a composition; stating it explicitly so no piece gets built in a way
that breaks the whole:

- **A long-running autonomous task = a goal with `continuation_budget > 0`
  + goal-scoped gates + (optionally) a steer heartbeat.** The goal carries
  intent and budgets across turns; continuations drive re-entry on
  `round_ceiling`/`budget_exhausted` (3b) with the continuation prompt
  demanding host-observable evidence of blockage; gates are the
  deterministic finish-line Reflect cannot overrule; the heartbeat steers
  course mid-flight without spawning competing turns.
- **`AWAITING_USER` blocks continuations by design.** Human input is a
  legitimate block, not a failure — the existing push-notification path
  (`core/push.py`, `core/notify.py`) alerts the disconnected user, the goal
  stays `active`, and work resumes when they answer. Prompt guidance during
  an active goal: prefer `notify_user` for progress reports; reserve
  `ask_user` for genuine decisions (StuckDetector's ask_user nudge is
  correct even here — a stuck autonomous run *should* stop and ask, loudly).
- **State survives everything shorter than the goal.** The kernel (Phase 2)
  carries working state across compactions and — via snapshots — restarts;
  result binding (2c) keeps context pressure low enough for a goal to span
  many compactions; claim-before-deliver (1c) + the startup reconciles +
  goal reconcile (3b) mean a crashed box resumes without replaying anything.
- **The measurement layer stays out of the way**: canary sweeps are
  snooze-transparent and workspace-isolated (§5), so an overnight
  autonomous goal and the scheduled canary baseline can coexist on the box.

---

## 5. Phase 3.5 — Golden-task canary suite

Active measurement for self-improvement: canned tasks + deterministic gates,
run headlessly. The Phase 4 tripwire's primary signal.

- **Task format**: `data/canaries/<name>/CANARY.md` — frontmatter (`name`,
  `prompt`, `gates: [{name, command, watch_paths?}]`, optional `model`,
  `timeout`, `tags`, `flaky`, `last_reviewed`) + markdown body. Scanned
  like skills (own small parser in `core/canary/`; reuse
  `core/skills/parser.py`'s frontmatter helper).
- **Execution**: headless `session_type="canary"` sessions via
  `manager.create_session` + `manager.prompt` (the cron precedent,
  `scheduling/__init__.py:227-238`), full pipeline (scout → agent → gates →
  reflect — canaries must exercise what real turns exercise). Workspace =
  temp dir via 1g override. Gates materialize as `scope="canary"` rows,
  deleted after the run. A run that triggers reflect retries scores the
  *final* attempt's gates; retry count is recorded.
- **Isolation is a predicate list, not a vibe** (pass-3: the claimed
  "journal precedent" does not exist — FTS indexes by role whitelist
  regardless of session type, `db/models.py:409-415`, and distill/refine
  filter only `!= 'worker'`, `:1152-1215`). Enumerated exclusions, all
  landing **before the first sweep**:
  - `search_messages_fts` (`db/models.py:641-712`): exclude
    `session_type IN ('canary')` via join (indexing stays cheap; exclusion
    at query time).
  - `get_unreviewed_sessions` / `get_unproposed_sessions`
    (`db/models.py:1163,1207`) and the snooze distill sweep
    (`core/snooze.py:679`): add `'canary'` to the type exclusion.
  - `_maybe_candor` (`sessions/hooks.py:216-260`): early-return for
    canaries in v1 — deliberately-hard synthetic tasks would poison the
    reliability ledger that Phase 4 consumes. (§10.9 revisits a separate
    ledger namespace — canaries would make a good calibration set.)
  - Post-mortems: written but stamped `session_type='canary'` in the
    payload and **excluded from the passive tripwire window and H2
    aggregation** — else the post-batch sweep contaminates the very signal
    it guards (pass-3 F12/F13).
  - Memory **writes** disabled; reads stay (recall quality is measured).
  - Sidebar/session lists: filtered like snooze journals
    (`core/dream/journal.py:88` precedent — a UI filter, correctly
    understood as UI-only).
- **Tool gating, generalized**: replace `ToolDef.worker_allowed` with
  `denied_session_types: set[str]` (default `set()`;
  `worker_allowed=False` ≙ `{"worker"}` — preserve semantics for the three
  orchestration call sites, `orchestration/__init__.py:2468,2621`,
  `rlm/__init__.py:729`); enforce in `_execute_single`
  (`executor.py:159-173`). Memory-write tools add `"canary"`.
- **Snooze transparency** (pass-3 F3 — three deadlock/starvation paths):
  `manager.prompt()` calls `get_snooze().request_cancel()` unconditionally
  (`sessions/manager.py:624`) and `_execute_cron_job` does too
  (`scheduling/__init__.py:205`); `_is_idle`/`has_active_work`/the
  activity cooldown iterate all sessions untyped (`core/snooze.py:109-113,
  130-135`; `manager.py:2120-2124`). Add
  `SNOOZE_TRANSPARENT_TYPES = {"canary"}`: transparent sessions neither
  cancel snooze nor block its idle gate nor refresh the cooldown.
  **Post-batch sweeps are enqueued for the next idle window, never
  dispatched inline from a snooze activity** — inline dispatch would
  cancel the cycle that produced the batch.
- **Triggers**: `scheduled` (a `kind: canary` cron job, default
  `canary_schedule="0 3 * * *"`, `canary_max_concurrent=1`), `post_batch`
  (enqueued by a Phase 4 apply — **including approved-proposal applies**,
  so batch-tagged data accumulates even with auto-apply off), `manual`
  (`canary_run(name)` tool + API — needed to vet a newly approved canary).
- **Scoring**: migration **v24**: `canary_runs` (`id, task, trigger ∈
  scheduled|post_batch|manual`, **`batch_id` nullable FK** — not smuggled
  into a string, the tripwire joins on it —, `session_id, gate_results_json,
  passed, retries, tokens, duration_s, created_at`). Reflect verdict
  recorded as secondary, never authoritative.
- **Flakiness discipline**: gates locally deterministic (fixtures over live
  URLs) unless tagged `flaky`; flaky canaries inform, never trip the
  tripwire.
- **Growing the suite**: refine/snooze_reflect may *propose* canaries from
  real failed turns (proposal-gated, I6) — the regression-test convention
  at the behavior level. Seed: ~6–10 hand-written covering daily-driver
  categories. Staleness: snooze nudge at 90 days past `last_reviewed`
  (§10.8).
- Config: `canary_enabled` (off), `canary_schedule`,
  `canary_max_concurrent` (1), `canary_retention_days` (30; pruned via the
  existing retention-activity pattern, `core/snooze.py:483-505`),
  `canary_baseline_runs` (3), `canary_regression_delta` (0.15).

**Done when:** `canary_enabled=false` → zero rows, zero behavior change; one
seed canary runs end-to-end producing a `canary_runs` row with per-gate
results; canary messages absent from `search_sessions`/FTS; `remember`
inside a canary returns the denied-tool error; a canary sweep during snooze
neither cancels nor blocks it (asserted on snooze logs); two consecutive
scheduled sweeps produce a stable baseline.

---

## 6. Phase 4 — The Adaptive Layer (the centerpiece)

A governed store of machine-editable **policy**, distinct from memory
(facts, I3) and skills (instructions, I6). This is Dream's deferred
promotion phase (`docs/dev/dream-plan.md:20-21`, deferred 2026-07-31,
revisit ~2026-08-07), shipped with prime-agent's safety rails. **Naming:**
module `core/adaptive/`, tables/flags `adaptive_*` — `core/harness/`
already means the nudge machinery and is untouched by this phase.

### 4a. Data model (migration v25; DB-first, unlike memory)

Machine-managed, version-critical → SQLite with full history, not markdown
(hand-editing a version-chained store corrupts rollback). Read-only rendered
mirror `data/adaptive/ADAPTIVE.md`, regenerated on change, **never read
back**.

- `adaptive_entries`: `id` (slug PK), `kind ∈ prompt_note | routing_hint |
  policy | worker_spec`, `scope ∈ global | session:<id>`, `title`,
  `content`, `risk ∈ low | high` (**computed at apply time from (kind,
  scope, action, source)**, stored for audit), `version` (monotonic),
  `status ∈ active | rolled_back | deleted`, `source ∈ refine |
  snooze_reflect | dream | candor | user | agent`, timestamps.
- `adaptive_events` (append-only): autoincrement `id` (**the rollback
  ordering key** — `created_at` text timestamps are not monotonic within a
  batch), `entry_id`, `action ∈ create|update|delete|rollback`,
  `before_json`, `after_json` (full snapshots), `evidence_json` (≥1 ref
  required: post_mortem ids, dream hypothesis ids, Candor refs, session
  ids), `actor`, `proposal_id?`, `batch_id`, `created_at`.
- `adaptive_batches`: `batch_id` PK, `producer`, `status ∈ pending | applied
  | suspect | rolled_back`, `flagged_reason`, `cleared_at`, `created_at` —
  the tripwire's `suspect` flag needs a home and a lifecycle (cleared by
  human dismiss or a subsequent clean sweep).
- `adaptive_proposals`: **a new table, not the skills one** —
  `skill_improvement_proposals` is skill-shaped (`skill_name NOT NULL`,
  `db/database.py:509-528`) and its approve endpoint is a status flip whose
  docstring says "user will edit skill manually"
  (`api/routers/workflows.py:84-89`). Harness-, er, adaptive-proposals need
  **apply-on-approve**: approving executes the batch through the same apply
  engine as auto-applies (and mints the same `batch_id` + post-batch canary
  sweep). Columns: proposal id, batch payload (the edits JSON), evidence,
  producer, status, resolved_at.

### 4b. Kinds and risk tiers (Calvin's autonomy decision)

| Kind | Consumed by | Tier |
|------|-------------|------|
| `routing_hint` — tool/skill selection guidance | Scout | **low → auto-apply** |
| `prompt_note` — supplemental directive ≤400 chars | Compiler block | **low → auto-apply** |
| `policy` — behavioral rule with control-flow weight | Compiler block | **high → proposal-gated** |
| `worker_spec` — reusable worker template (task shape, model, gate set) | Orchestration | **high → proposal-gated** |

Escalations to proposal-gated regardless of kind: any **delete** of another
producer's entry; any **global-scope** edit originating from Dream. Named
caps: `adaptive_max_entries_per_kind=12`,
`adaptive_max_auto_applies_per_day=6`, `adaptive_edit_cooldown_hours=24`.

### 4c. Apply discipline

- **Immutability (I4) structural**: the apply path writes only `adaptive_*`
  rows; it has no file-write capability.
- **Plan/apply split**: producers carry each touched entry's `version` as
  baseline; apply re-reads and rejects moved entries ("entry changed during
  planning") while the rest of the batch applies.
- **Exact rollback**: `rollback(batch_id | event_id)` walks events in
  reverse **autoincrement-id** order — `before` present → restore; absent →
  delete. Rollback is itself an event.
- **Application windows, scope-aware** (pass-3 F11):
  `scope=session:<id>` applies when that session is `IDLE_READY`;
  `scope=global` applies **only inside a snooze cycle that passed
  `_is_idle()`** (`core/snooze.py:109-113`) — "target session idle" is
  meaningless for global entries, and applying one while any session is
  mid-turn busts that session's prefix mid-turn (I8). Deferred otherwise.
- **Where it runs**: new snooze **Activity 15** (after Dream's 14,
  `core/snooze.py:551-568`), following the ladder pattern (cancel-gate +
  LLM-availability check): drain pending auto-applies → enqueue post-batch
  canary sweeps → evaluate tripwire on completed sweeps.
- **Cache honesty**: the block sits in the stable prefix, so every global
  apply invalidates every session's cached prefix once. Bounded by the
  daily cap (6) and the idle-window rule; the block is **omitted entirely
  when empty** so first deploy shifts nothing.

### 4d. Producers (the contract, previously unspecified)

Refine (`core/refine.py:218-300` — one `client.chat`, parsed by
`_parse_refine_output` into proposals/lessons) gains a third output array
`adaptive_edits`, same call, same parse function:
`[{action, kind, scope, title, content, evidence: [refs],
baseline_version|null}]`. A producer pass mints one `batch_id`. Low-risk
edits → pending auto-apply; high-risk → `adaptive_proposals`. Identical
contract for `snooze_reflect`. Mappings: user corrections → `prompt_note`;
technique/tool patterns → `routing_hint`; sequencing rules → `policy`
(gated). Refine may also propose canaries (§5).

Dream promotion: validated hypotheses map by kind — `tool_pattern`
(validated mechanically against Candor, `core/dream/validate.py:192-241`) →
`routing_hint`, auto-eligible; `lesson_ineffective` (counterfactual scout
replay, `validate.py:9`) → `policy` proposal, gated. **`contradiction` /
`memory_stale`** (the correct kind name — `core/dream/hypothesize.py:52`)
have *no existing proposal path* (pass-3 correction: none exists anywhere) —
they route through `adaptive_proposals` as memory-edit proposals rendered
for human review. Dream's content-hash-pinned refs
(`core/dream/observe.py:40-43`) slot into `evidence_json`.

Candor: reliability regressions → `routing_hint` with ledger refs
(`why_reliability`'s audit chain as evidence).

### 4e. Consumption

- `_build_adaptive_block()` between the directives block and the skills
  catalog (`compiler.py:707-711`): `prompt_note` one-liners, `policy`
  rendered fully. **`routing_hint`s render only into the scout's prompt** —
  beside the `[OPERATIONAL INTEL]` block (the named seam:
  `core/scout/runner.py:125-129`) — they're planning signals, keeping the
  agent prompt lean (I5).
- Scout gains `search_adaptive` (standalone, riding 1f; §10.3 records the
  memory-facet alternative as rejected-unless-proven-annoying).
- Conflict rule in the block header: adaptive entries *supplement* RULES.md,
  never override; a producer whose edit contradicts a RULES.md line must
  route it as a gated proposal with the conflict flagged.

### 4f. Rollback UX and the tripwire

- UI: Adaptive panel — entries by kind, event journal, per-event/batch
  rollback, before/after diff. API: `GET /api/adaptive/entries`,
  `GET /api/adaptive/events`, `GET /api/adaptive/proposals`,
  `POST /api/adaptive/proposals/{id}/approve|reject` (approve = apply),
  `POST /api/adaptive/rollback`. Auto-applies notify ("adaptive: 2 routing
  hints applied — review").
- **Tripwire, two signals**: *primary (active)* — post-batch canary sweep
  vs. the trailing `canary_baseline_runs` scheduled sweeps, joined on
  `canary_runs.batch_id`, regression = pass-rate drop ≥
  `canary_regression_delta`; *secondary (passive)* — organic post-mortem
  reflect-retry drift over `adaptive_tripwire_window_turns=20` turns after
  a batch (canary-stamped post-mortems excluded, §5). Either sets
  `adaptive_batches.status='suspect'`. `adaptive_auto_rollback` (off
  initially) promotes a canary regression to automatic rollback once the
  metric earns trust.

Config: `adaptive_enabled` (off), `adaptive_auto_apply` (on when enabled —
per Calvin), the 4b caps, `adaptive_tripwire_window_turns` (20),
`adaptive_auto_rollback` (off).

**Done when:** applying a 3-edit batch and rolling back restores
`adaptive_entries` byte-for-byte against the `before_json` snapshots,
including delete-when-`before`-absent; an edit whose entry version moved
during planning is rejected while the rest applies; `adaptive_enabled=false`
→ compiler output byte-identical to today; an edit without evidence is
refused; approving a proposal applies it and enqueues a batch-tagged sweep;
a global apply is deferred while any session is mid-turn.

---

## 7. Cross-cutting engineering rules (read before implementing any phase)

1. **Migration ledger** (sessions DB, `db/database.py` `MIGRATIONS`, last is
   v20 at `:653`): v21 = cron (1c), v22 = gates (3a), v23 = goals +
   `token_usage.goal_id` (3b), v24 = canary_runs (3.5), v25 = adaptive_*
   (4a). The memory DB has **no migration ladder** — schema changes go in
   `_MEMORY_SCHEMA` (`db/database.py:767`) with idempotent DDL + an
   explicit upgrade branch (the `memory_fts` precedent).
2. **Config → UI**: every new setting gets an entry in
   `static/js/components/modals/settings.js`'s explicit field list
   (`:213`); model-role settings use the model-select type. A setting
   absent there is invisible to the user.
3. **Session-type registry**: `session_type` is an open string compared in
   ~50 hardcoded places. New types (`canary`) and new exclusions route
   through named predicates/constants (beside `sessions/policy.py:11-19`),
   not new inline string comparisons.
4. **Flag-off proof**: every phase lands with a test asserting that, with
   its flag(s) off, `compile_context` output and the registered tool set
   are unchanged. (Phases 1c/1d/1e are flagless behavior fixes — their
   guard is tests + git revert; §8's "off by default" claim applies to
   Phases 2, 3, 3.5, 4 and flags 1f via the role being unset.)
5. **Naming**: the new policy store is "adaptive layer" / `adaptive_*`
   everywhere; "harness" continues to mean `core/harness/` nudges. The
   kind is `routing_hint` (singular) in all prose and code.

---

## 8. Sequencing, sizing, burn-in

| Phase | Contents | Size | Risk |
|-------|----------|------|------|
| 1 | OpenAI provider + router generalization, cache metrics, cron claim, skills health, coverage + FauxProvider, semantic retrieval, workspace override (1g); 1b optional after metrics | M–L | Low–Medium |
| 2 | Session kernel, snapshots, binding, dedup/idempotent flag | M | Medium |
| 3 | Gates + clamp + post-mortem model/category fields, goals, heartbeats | M | Medium |
| 3.5 | Canary format/runner, isolation predicates, snooze transparency, triggers, seed suite | M | Medium (predicates touch hot paths) |
| 4 | Adaptive tables, apply/rollback engine, producer contract, proposals + apply-on-approve, UI, Dream promotion | L | Highest (lands last, by design) |

Dependencies pass 3 surfaced: **1g before 2/3/3.5**; **1c's extra_meta
round-trip before 3c and 3.5's cron kinds**; **3.5's isolation predicates
before its first sweep** (else the baseline week contaminates Candor and
post_mortems); **canary baselines ≥1 week before Phase 4 autonomy**. Phase 4
ships with `adaptive_auto_apply=off` for its first week — approved-proposal
applies still mint `batch_id`s and sweeps, so batch-tagged data accumulates
before autonomy turns on (this replaces pass-2's self-contradictory
"proposal-only mode regardless of the flag").

Burn-in per the Dream/Candor precedent; watch items live in §10 (not the
status header). Current watch items: Ollama VRAM thrash from embedding-model
residency (1f); dill reintroduction tension with RLM's removal rationale
(2a); `role="eval"` payload discriminator vs. UI (3a).

---

## 9. Horizon (not scheduled; hooks preserved)

- **H1. Workspace checkpointing** — git-backed shadow repo over the
  workspace, auto-commit at turn boundaries (`_finalize_turn`,
  `sessions/manager.py:1061`), tagged by `turn_id`; enables safe parallel
  file-mutating workers and H3. *Hooks:* 1g's session-scoped workspace;
  3a's git-plumbing fingerprints share machinery.
- **H2. Learned model routing** — Candor's idea applied to models: an
  exception-report brief to scout informing `recommended_model`. *Corrected
  premise (pass 3):* post_mortems today record only the judge's
  `reflect_model` (`db/database.py:369`) — the turn's model and task
  category are **added by Phase 3a's clamp work**; H2 is then "just a new
  producer" against the existing `routing_hint` kind.
- **H3. Speculative best-of-N** — with H1 giving isolated discardable
  workspaces, serial reflect-retry becomes parallel selection; Reflect
  judges, `# AUTO-STAMPED (reflect=...)` headers
  (`sessions/manager.py:1456`) arbitrate.
- **H4. Memory wiki-links** — `[[entry-title]]` with link-aware recall;
  compounds with 1f. *Hooks:* `sanitize_entry_content`
  (`core/memory/format.py:42`) and consolidation/re-routing (Activities
  3b/3c) must not mangle `[[...]]` — add the guard test when 1f touches
  those files.

---

## 10. Open questions

1. **Kernel + embedding memory pressure** on the box: N kernels × ~100 MB
   RSS + `nomic-embed-text` residency alongside the chat model.
   `kernel_max_concurrent=3` + LRU reap + background-priority embeds are
   the v1 answers; measure during Phase 2 burn-in.
2. ~~cache_control vs. fallback sanitization~~ — resolved:
   `sanitize_for_fallback` rebuilds `{role, content}` (`router.py:60-80`);
   nothing survives. Kept for the record.
3. `search_adaptive` standalone vs. memory facet — standalone chosen (4e);
   revisit only if scout tool-count pressure appears.
4. Goal ↔ workflow overlap — separate in v1; revisit on convergence.
5. `prompt_note` vs. memory dedup — producer prompts draw the line: memory
   = what's true; adaptive = how to behave.
6. Coverage ratchet schedule after 1e's measured re-baseline.
7. Embedding index residency — brute force until row count says otherwise;
   record the threshold when measured.
8. Canary authorship drift — `last_reviewed` + 90-day snooze nudge; who
   curates gates as daily-driver tasks evolve is Calvin.
9. Should canaries feed Candor as a **separate ledger namespace** (a
   calibration set) rather than being excluded? Revisit after Phase 4
   burn-in.
10. Skills requirements-install ladder position (1d) — RESOLVED: landed as
    Activity 2c (after 2b's skill proposals; no LLM; one skill per cycle).
11. Pre-existing on Calvin's box: `tests/test_rlm_engine.py` fails wholesale
    with "AF_UNIX path too long" (macOS 104-char sun_path limit vs. deep
    pytest tmpdirs). Identical on a clean tree — not introduced by this
    branch. Fix candidate: point the ChildREPL socket at a short mkdtemp
    under /tmp instead of the run dir in tests.
12. [IMPL] 1f deviations, deliberate: (a) query-time embedding is a bounded
    SYNC httpx call (5s timeout + 60s failure backoff) rather than routed
    through the async scheduler — search runs in sync tool threads and
    bridging onto the loop risks deadlock; batch embedding follows the
    scheduler rule. (b) Coverage gate RATCHETED UP to 63 (measured 66% with
    extensions included) instead of the anticipated lowering. (c) 1d's
    SkillDef gained scripts_meta + registry.validation_issues() instead of a
    stored health string (single source of truth, no state duplication).

---

## 11. File impact (fully-qualified; inclusion rule: every new file, table, migration, doc, and UI surface)

| Area | Files |
|------|-------|
| Providers (1a) | ➕ `core/llm/providers/openai.py`; ✏️ `core/llm/router.py` (provider map, fallback predicate), `core/llm/registry.py` (populate signature, whitelist routing), `core/llm/client.py` (purge tuple), `core/agent.py` (normalization helper ×6 sites), `config.py`, `api/routers/health.py` (key allowlist), `static/js/app.js` + `static/js/components/modals/settings.js` |
| Cache breakpoints (1b) | ✏️ `core/context/compiler.py` (boundary offsets), `core/llm/providers/openrouter.py` (parts + cache_control) |
| Cron (1c) | ✏️ `core/extensions/scheduling/__init__.py` (claim, round-trip extra_meta, last_fired_at), `db/database.py` (v21 + DDL), `db/models.py`, `api/app.py` (reconcile before init_scheduler) |
| Skills (1d) | ✏️ `core/skills/registry.py` (`_invalid` dict, health), `core/skills/parser.py` (scripts key), `core/tools/builtin/skill_tools.py` (load_skill reason), `core/extensions/skillmaker/__init__.py`, `core/snooze.py` (install activity), skills-panel UI |
| Tests (1e) | ✏️ `pyproject.toml`; ➕ `tests/faux_provider.py`, `tests/regressions/` |
| Semantic retrieval (1f) | ✏️ `core/llm/providers/ollama.py` (embed), `core/llm/client.py` (embed), `core/memory/search.py` (vector channel in search_hybrid), `core/memory/store.py` (mark-stale, prune-on-reindex), `db/database.py` (`_MEMORY_SCHEMA` vectors+meta), `core/snooze.py` (Activity 5 embedding), `core/tools/registry.py` + `core/skills/registry.py` (semantic channels), `config.py` |
| Workspace (1g) | ✏️ `core/tools/paths.py` (session-aware resolvers), `sessions/state.py`/`manager.py` (`workspace_override`), file tools' context plumbing |
| Kernel (2) | ➕ `core/kernel/` (+ `data/kernels/<sid>/` snapshots + payload sidecars); ✏️ `core/extensions/rlm/child_env.py` + `child_runner.py` (scaffold mode, snapshot/restore frames, lock), `core/tools/executor.py` (binding post-pass, `_kernel` skip), `core/tools/registry.py` (`idempotent` flag), `core/agent.py` (dedup flag check), `core/reflect.py` (REFLECT_PROMPT stub clause), `sessions/manager.py` (remove() hook, kernel registry), `maintenance.py` (off-tick reap), `api/routers/sessions.py` (cancel skips kernel), `docs/architecture.md`, `docs/security.md` |
| Gates/Goals/Heartbeats (3) | ✏️ `core/extensions/evaluation/__init__.py` (gate tools), `core/reflect.py` (clamp before `_write_post_mortem`; +turn model/category), `sessions/hooks.py` (gate execution, extra_evidence, build_retry_context, eval-payload `kind`), `sessions/manager.py` (continuation enqueue rules), `core/context/compiler.py` (goal block + volatile burn), `core/extensions/scheduling/__init__.py` (heartbeats, owner field), `core/scout/runner.py` (synthetic-prompt cache discriminator), `core/workflows/parser.py` + `validator.py` + workflow docs, `db/database.py` (v22 gates, v23 goals + `token_usage.goal_id`), `db/models.py` (accessors, goal_id stamp), `maintenance.py` (prune exemption), `config.py` (3 flags; delete orphaned `max_continuations`) |
| Canaries (3.5) | ➕ `core/canary/` (parser, runner, scoring), `data/canaries/` seed suite; ✏️ `core/tools/registry.py` + `core/tools/executor.py` (`denied_session_types`), `db/models.py` (FTS/distill exclusions), `sessions/hooks.py` (Candor early-return, post-mortem stamp), `core/snooze.py` (transparency set), `sessions/manager.py` + `core/extensions/scheduling/__init__.py` (transparency at request_cancel callers; canary job kind), `db/database.py` (v24), session-list UI filter |
| Adaptive Layer (4) | ➕ `core/adaptive/` (store, apply, rollback, tripwire), `data/adaptive/ADAPTIVE.md` (generated), `api/routers/adaptive.py`, Adaptive panel UI; ✏️ `core/refine.py` + `core/snooze_reflect.py` (adaptive_edits output), `core/dream/` (promotion mapping), `core/scout/runner.py` (routing_hint injection at `:125-129`; `search_adaptive`), `core/context/compiler.py` (`_build_adaptive_block`), `core/snooze.py` (Activity 15), `db/database.py` (v25: entries/events/batches/proposals) |
