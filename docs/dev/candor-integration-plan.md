# Candor integration plan — operational memory as a toggleable add-on

Status: **phases 0–2 implemented** (2026-07-29) — see `core/extensions/candor/`,
`sessions/hooks.py:_maybe_candor`, snooze Activity 12b, and the scout
`_gather_candor_intel` preload. Phase 3 (backfill, settlement loop, `do:`
interventions) remains future work. One deviation from the original proposal:
no scout-side tool was added — `_exec_scout_tool` runs on the event loop, so
scout is served by the async preload brief only; the interactive tools are
agent-facing.

Candor (`~/Desktop/Candor`) is a calibrated memory substrate: attributed
observations in an append-only ledger, probabilities earned from outcomes,
drift located to dates, per-source trust learned from settled predictions.
This plan wires it into Pernix as an off-by-default add-on that (a) ingests
operational outcomes at turn end and during snooze, and (b) gives scout a
calibrated intelligence brief before every turn.

Candor already knows Pernix: `Candor/bench/ingest_pernix.py` and
`bench/run_realworld.py` were built by replaying Pernix's operational history
(they found the 2026-04-30 tool repair and the 2026-04-22 search collapse).
The seeding and ingest patterns below are lifted from that bench code.

---

## 1. Why — and division of labor vs MemoryStore

Pernix already has episodic/semantic memory (`core/memory/store.py`: markdown
+ FTS5). Candor does not replace any of it. The two answer different
questions:

| | MemoryStore (existing) | Candor (new) |
|---|---|---|
| Question | *What happened? What do I know about X?* | *How reliably does X behave, and why do we believe that?* |
| Content | prose entries, user profile, lessons | counts, calibrated probabilities, guards, regime changes |
| Retrieval | text search (BM25/hybrid) | `predict()`, `distribution()`, `questions()`, `why()` |
| Trust | none (all entries equal) | per-source, earned via settled claims |

Concrete failures Candor would have caught by name (all from hkb history):
web-search reliability collapse 93%→38% (located to a date), the dead
OpenRouter key silently degrading scout for 2 days (`llm_ok` regime change),
yt-dlp missing from the box image (`tool_failure_mode` = not_found at 100%).
Today these are discovered by manual session audits; Candor turns them into
queryable, dated beliefs that scout can inject before the agent trips on them.

Anti-goals (from Candor's own docs): it is not a vector store (MemoryStore
keeps semantic search), not distributed, has no continuous channel (latencies
must be bucketed), and `recall()` prose retrieval stays unused in v1 —
MemoryStore already owns that job.

---

## 2. Hard constraints the design must respect

From the Candor codebase (verified, not from docs):

1. **One writer per store, enforced by `flock`.** A second `CandorSystem` on
   the same dir raises `LedgerError`. There is no read-only open. → exactly
   one instance, inside the Pernix process.
2. **Not thread-safe** — plain `sqlite3.connect()` (thread-bound) plus
   unguarded ledger state. `asyncio.to_thread` (Pernix's usual off-loop
   idiom) uses a *pool* of threads and will crash it. → all Candor calls go
   through a **dedicated single-thread executor** owned by the bridge.
3. **`observe()` before the fact is admitted loses the evidence forever**
   (`core/apply.py:177` files it with `fact_id=NULL`; replay never
   re-attaches). → seed facts first; buffer observations for unknown
   predicates until the next gate run.
4. **`assert_()` only creates a candidate; `run_gate()` admits.** The gate is
   a full O(facts × observations) sweep whenever new observations exist —
   never call it in the hot path. → gate runs live in snooze.
5. **Default quotas** (3000 obs / 500 candidates per actor per day) will
   throttle real traffic. → `set_actor_quota()` at store init, including for
   `agent:curiosity`.
6. **`predict()` on a never-admitted fact returns a dressed-up prior** rather
   than raising. → guard reads with `fact_id_for(stmt) is not None`.
7. **`close()` must be called** (no context manager) — release the flock on
   shutdown.
8. **Construction folds the whole ledger** (O(ledger), three passes). → open
   lazily off-loop; `checkpoint()` periodically from snooze to keep reopen
   fast.
9. **`ctx` prefixes `do:` and `derived:` are reserved**; actors are
   `class:name` (`tool:web_search`, `agent:pernix`, `verifier:reflect`).
10. **No packaging** — `pip install -e` fails today; import path is
    `from candor.system import CandorSystem` (never `from candor import …`).

Pernix-side constraints (from the architecture map):

- Post-turn hooks run in `FINALIZING` under a background ref and **block turn
  completion** — turn-end work must stay in the milliseconds-to-low-seconds
  range. Heavy work belongs in snooze.
- Snooze activities can be **cancelled at any point** (generation counter,
  cycle cut at `snooze_max_cycle_seconds`); work must be idempotent and poll
  `_is_cancelled()` between units.
- Every hook/gather/activity must be failure-isolated: `try/except` +
  `logger.warning` + continue. A Candor failure must never break a turn.

---

## 3. Architecture

```
                        ┌─────────────────────────────────────────┐
                        │  core/extensions/candor/                │
                        │                                         │
 sessions/hooks.py ───▶ │  bridge.py — CandorBridge (singleton)   │
 (_maybe_candor,        │   • settings.candor_enabled gate        │
  turn end)             │   • 1-thread executor (owns the store)  │
                        │   • lazy open, close() on shutdown      │
 core/snooze.py ──────▶ │   • pending-obs buffer (unknown preds)  │
 (activity: gate,       │   • vocab seeding + quota provisioning  │
  drain, checkpoint)    │                                         │
                        │  emit.py  — turn → observation dicts    │
 core/scout/runner.py ─▶│  intel.py — brief for scout preload     │
 (_gather_candor_intel) │  tools.py — scout/agent tools (ph. 2)   │
                        └───────────────┬─────────────────────────┘
                                        │ single thread, serialized
                                        ▼
                            CandorSystem(data/candor/store)
```

### The bridge (`core/extensions/candor/bridge.py`)

One module-level singleton, mirroring `get_memory_store()`:

```python
class CandorBridge:
    def __init__(self):
        self._exec = ThreadPoolExecutor(max_workers=1,
                                        thread_name_prefix="candor")
        self._system = None            # created on first use, on _exec
        self._known_facts: set[str] = set()   # stmt-key → admitted

    async def _call(self, fn, *args):  # every Candor touch goes through here
        return await asyncio.get_running_loop().run_in_executor(
            self._exec, fn, *args)
```

- **Enabled gate:** every public method starts
  `if not settings.candor_enabled: return None` — hot toggle, no restart
  (the `eval_auto` pattern). Turning the flag off mid-flight simply makes
  the bridge inert; the store stays on disk.
- **Lazy open:** first enabled call schedules `CandorSystem(root)` on the
  executor thread (the fold can take a while on a big ledger — it never
  touches the event loop). On open: `set_actor_quota` for `agent:pernix`
  (obs 100k/day), `human:calvin` and `agent:curiosity` (candidates 10k/day),
  then load the admitted-fact set via `fact_id_for` checks.
- **Shutdown:** `api/app.py` lifespan teardown calls
  `await bridge.close()` → `system.close()` on the executor, then
  `_exec.shutdown()`. Releases the flock.
- **Failure isolation:** `_call` wraps in try/except; on repeated failures
  the bridge trips a circuit breaker (logs once, goes inert until next
  process start) so a corrupted store can't spam every turn.
- **Store location:** `data/candor/store/` (inside the persisted `data/`
  volume on box). The pending buffer lives beside it at
  `data/candor/pending.jsonl`.

### Toggle & config (`config.py`)

```python
# --- Candor (operational memory add-on) ---
candor_enabled: bool = False          # master switch (hot)
candor_store_dir: str = "data/candor" # store + buffer root
candor_scout_brief: bool = True       # inject intel brief into scout preload
candor_max_obs_per_turn: int = 200    # safety valve on turn-end emission
```

`load()` tolerates unknown keys, so this is forward/backward compatible.
Optional follow-ups: a `Memory` section entry in
`static/js/components/modals/settings.js` and a row in
`docs/configuration.md`. Tool registration (phase 2) is gated inside
`register()` like the `web` extension — that part needs a restart, which
matches how every other extension behaves.

---

## 4. Data model — v1 predicate vocabulary

Fixed, mechanical, no LLM extraction. Everything below is derivable from
state Pernix already has at turn end. Dual-granularity seeding (the bench
trick): an aggregate `(pred, ["*"])` fact carries `ctx target=<arg>` so the
sweep can find cross-target structure, while per-target facts get their own
changepoint treatment.

| Statement | Type | Observed from | ctx |
|---|---|---|---|
| `tool_ok([<tool>])` + `tool_ok(["*"])` | frequency | `session.last_tool_summary` (per tool: calls, failures) | `model`, `kind` (chat/cron/worker), `target` (aggregate only) |
| `tool_failure_mode([<tool>])` | categorical | error strings from `last_tool_summary`, bucketed mechanically (timeout / auth / not_found / rate_limit / invalid_args / other) | `model`, `kind` |
| `turn_ok(["*"])` | frequency | `termination_reason == "complete"` | `model`, `kind`, `is_retry` |
| `reflect_verdict(["*"])` | categorical | reflect verdict (pass / retry / escalate) | `model`, `kind` |
| `llm_ok([<model>])` | frequency | stream/scout fallback errors vs successful completions | `provider`, `role` (scout/agent/reflect) |

Actors: observations from tool outcomes are `tool:<name>` is wrong — the
*reporter* is Pernix itself, so use `agent:pernix` for turn-end emission;
reflect verdicts are reported as `verifier:reflect`. (Actor = who is
speaking, not what is spoken about.)

Latency is continuous → not modeled in v1 (Candor has no continuous
channel). If wanted later: bucket into `fast/slow/timeout` as a categorical.

Volume estimate: a busy day ≈ a few hundred tool calls + tens of turns —
well under the raised quotas, and `observe()` is sub-ms on the bridge thread.

---

## 5. Write path

### Turn end — `sessions/hooks.py` (phase 1)

New `_maybe_candor(session_id, session, session_obj)` appended after
`_maybe_reflect` in `run_post_task_hooks()` (so the verdict is available),
gated `if not settings.candor_enabled: return`. It:

1. Builds observation dicts **mechanically** from `session.last_tool_summary`
   (calls/failures/errors per tool), `termination_reason`, the reflect
   verdict, and the session's model/kind — via `emit.py`, pure function,
   unit-testable. Each dict carries `ts` = now (ms).
2. Hands them to `bridge.record(observations)`:
   - fact already admitted → `observe(..., ts=...)` directly (sub-ms each);
   - unknown predicate/arg (first sighting of a new tool, etc.) → append to
     `pending.jsonl`. Because `observe()` accepts backfill timestamps,
     buffering is **lossless** — the observation enters later with its true
     event time.

Cost: a handful of ms serialized on the bridge thread; nothing on the event
loop. Auto-suppressed when messages are queued (hooks already skip then).

### Snooze — `core/snooze.py` (phase 1)

One new activity (template: `_refine_one_session`), non-LLM, watermarked via
`db.set_snooze_state("candor:last_gate", iso_ts)`:

1. **Seed:** for each new predicate/arg in the pending buffer, `assert_`
   candidates (source=`pernix:runtime`, actor=`agent:pernix`).
2. **Gate:** `run_gate()` — admits candidates, runs the curiosity sweep,
   audits guards. This is the expensive O(facts × obs) step, exactly where
   it belongs: idle time, cancellable between steps.
3. **Drain:** replay `pending.jsonl` from a durable byte-offset cursor
   (`snooze_state["candor:pending_cursor"]`), in chunks, updating the cursor
   after each chunk; truncate when fully drained. Worst case on a
   mid-chunk cancel is one chunk double-observed — statistically negligible
   and preferable to loss.
4. **Checkpoint:** every N cycles (e.g. 20), `checkpoint()` so process
   restart doesn't refold the whole ledger.

Each step polls `self._is_cancelled()`. The activity is cheap when idle:
`run_gate()` is O(1) when no new observations exist.

---

## 6. Read path — scout as the consumer

### Phase 1: deterministic intel brief

Add `_gather_candor_intel()` to the preload `asyncio.gather` in
`core/scout/runner.py:1400` (gated on `candor_enabled and
candor_scout_brief`). It calls `bridge.intel_brief()` which renders, from
pure reads (`predict`, `distribution`, `questions`, direct index query for
admitted guards — wrapped in ONE bridge function since `index.query` is a
private surface):

```
[OPERATIONAL INTEL]  (calibrated; from outcome history, not vibes)
- web_search: 38% success (was 93% until 2026-04-22), unstable
- fetch: works when method=crawl4ai (admitted guard)
- ffmpeg_probe: 3 obs only — low confidence
- open question: tool_ok(yt_dlp) dispersed; suggested: record ctx 'network'
```

Only degraded/flagged items are included (p below threshold, `unstable` /
`under_specified` / `regime_mixed` caveats, admitted guards, top 2
questions), char-capped like every other preload part
(`scout_preload_memory_char_limit` pattern). Healthy tools say nothing —
the brief is an exception report, not a dashboard. Prompt-framing lesson
from commit `fc329cb` applies: the brief carries *facts found*, never
conclusions about what is missing.

### Phase 2: on-demand tools

- Scout tool `check_reliability(pred, arg)` → `predict()` +
  `distribution()` summary, added to `_SCOUT_TOOLS` + `_exec_scout_tool`.
- Agent tools via the extension's `register(reg)`:
  `predict_reliability`, `why_belief(fact)` (audit chain), `open_questions()`.
  Registered only when `candor_enabled` (restart-gated, standard for
  extensions).

---

## 7. Phases

**Phase 0 — enablement (small)**
- Add minimal `pyproject.toml` to Candor; `pip install -e ../Candor` for dev.
  For box: add Candor to the image (pinned copy or submodule) — decision for
  Calvin (§9). Zero runtime deps make either trivial.
- `core/extensions/candor/` skeleton: bridge with executor, lazy open,
  close-on-shutdown wired into `api/app.py` lifespan teardown, config flags,
  circuit breaker. Extension listed in `BUNDLED_EXTENSIONS` (registers
  nothing yet).
- Tests: toggle-off is a no-op; open/close lifecycle on a tmp store; second
  instance on same dir raises and is handled.

**Phase 1 — write + brief (the core)**
- `emit.py` (turn → observations) + `_maybe_candor` hook.
- Snooze activity: seed / gate / drain / checkpoint.
- Vocabulary seeding on first enable (assert + gate the v1 predicates for
  all currently registered tools).
- `_gather_candor_intel` scout preload brief.
- Tests: emission mapping from a fabricated `last_tool_summary`; pending
  buffer drain with simulated cancellation; brief rendering thresholds.

**Phase 2 — interactive intelligence**
- Scout tool + agent tools (above).
- `ScoutReport` learns nothing new — the brief and tool results ride
  existing fields; only if that proves lossy, add a dedicated field +
  `to_system_prompt_section()` part.

**Phase 3b — user-fact attestations (SHIPPED)**
Closes the "Candor gap" surfaced in live testing: Candor tracked only
operational facts, while user-fact provenance lived solely in markdown
`source=` tags. Now every mutation of a `user.*` memory file emits
`user_fact(<area-slug>)` attestation observations (add → True, update →
False+True, forget → False) via `MemoryStore._candor_attest` →
`bridge.record_nowait` (fire-and-forget, safe from any thread). Prose never
enters the ledger — slugs and outcomes only — so PII stays in the editable
markdown store. Deliberately NOT full user-facts-in-Candor: Candor's own
anti-use-cases exclude general knowledge storage, and without settlement
events its per-fact numbers would be priors dressed as confidence. What this
gives honestly: `p(user_fact(area))` = earned stability of that user-model
area, `why_reliability` = the attestation chain, corrections = negative
evidence. `move_entries` (snooze file-org) is deliberately unhooked so
consolidation doesn't pollute attestation stats.

**Phase 3 — trust, curiosity, backfill (each independent)**
- **Backfill:** one-time script reusing `bench/ingest_pernix.py` to replay
  existing post_mortems / session history into the store with real
  timestamps — the store starts life already knowing the 2026 history.
- **Settlement loop:** `register_oracle("verifier:reflect", ...)`; scout's
  viability verdict becomes a `claim()` settled by the reflect verdict via
  `resolve()` — scout's calibration is then *earned and auditable*, and
  reflect's judge-quality is measurable per Candor's source_reliability
  pattern.
- **Curiosity → action:** surface `questions()` suggested measurements as
  snooze work items (e.g. "start recording ctx key `network` on yt_dlp
  observations").
- **`do:` interventions:** when Pernix acts on the world it monitors
  (toolmaker rewrites a tool, settings change, model swap), log
  `ctx={"do:<action>": "yes"}` so Goodhart-style coupling collapses get
  found by name.

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Thread misuse corrupts/crashes the store | single-thread executor; *only* the bridge imports candor |
| Candor failure breaks turns | gate + try/except + circuit breaker at every entry point; hooks already isolate failures |
| `run_gate()` grows with history | snooze-only; checkpoint keeps reopen fast; if it ever exceeds the snooze budget, gate in slices (it's resumable — the sweep is recomputed, not incremental) |
| Observation loss on unadmitted facts | pending buffer + ts backfill; seeding at enable time makes the buffer rare |
| Double-observe on snooze cancel | chunked drain with durable cursor; bounded to one chunk |
| Inode growth (`ledger/payloads/`, one file per distinct payload) | v1 volume is low (hundreds/day); revisit before any high-volume ingest |
| `retrieval.sqlite3` unbounded growth | `recall()` unused in v1 |
| Private `index.query` surface drifts with Candor updates | confined to one bridge function; covered by a test |
| Brief misleads the agent (fc329cb lesson) | exception-report framing, facts-only wording, char cap |

## 9. Decisions for Calvin

1. **How Candor ships to box:** editable install from a sibling checkout
   (easy on desktop, awkward in Docker) vs. vendored copy vs. git submodule
   pinned in the image. Recommendation: add `pyproject.toml` upstream +
   pin a copy in the box image, same as other box-local additions.
2. **Should Candor get a settings-UI section**, or stay API/file-toggled
   like snooze? (Plan assumes API-only initially.)
3. **Backfill now or later?** The bench ingest already proved it works;
   doing it at phase 1 gives scout useful intel from day one instead of
   waiting weeks for observations to accumulate.
