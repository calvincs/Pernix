# Dream integration plan — introspection over memory and Candor as a toggleable add-on

Status: Phases 0–2 IMPLEMENTED (2026-07-30) — `core/dream/`, migration v19,
snooze Activity 14, §13 defects #1–#5 fixed. Phases 3–4 not started; their
settings flags will be added with their phases. Deviations from the proposal:
(a) bridge gained async `predict` + `health_snapshot` only — `conjectures()`/
`events_tail` deferred until used (ledger events carry payload hashes, not
semantics; the intel brief is the semantic Candor source for evidence packs);
(b) the evidence pack samples ONE memory file per step, so v1 contradiction
discovery is intra-file; (c) scout replay uses a fresh-session `SessionBrief`
carrying the original session id (exclusion still works, and the historical
brief is unrecoverable anyway — this also keeps the counterfactual clean of
the failure transcript); (d) the report's first enable only starts the clock.
Every constraint cited below was verified against the working tree (file:line), not
recalled from docs. Companion precedent: `docs/dev/candor-integration-plan.md`.

The idea, in one sentence: give Pernix an idle-time faculty that examines its own
memory, Candor evidence, and post-mortems; generates typed hypotheses about itself;
validates them against recorded outcomes; and only then — through gates — lets the
conclusions influence live behavior.

Design criterion (from Maes' definition of computational reflection): self-knowledge
must be **causally connected in both directions**. Candor already gives us the
inbound mirror (outcomes flow in continuously). Memory today is a Polaroid — written
once, never falsified. Dreaming closes the return path: derived conclusions flow
back out to scout, but only after validation, and only through additive, flagged,
human-visible channels.

---

## 1. Goals and non-goals

Goals:

- **Belief falsification.** Detect contradictions between memory entries, and between
  memory/lessons and Candor's outcome evidence. Nothing in the system does this today
  (verified: the only lifecycle signals on a memory entry are retrieval frequency and
  calendar age — `core/memory/store.py:763-781`, `store.py:797-824`, `core/snooze.py:1779-1975`).
- **Hypothesis generation over operational history.** The venue where drawing
  conclusions is safe: offline, unhurried, checkable. The fc329cb rule (facts-not-
  conclusions, `core/extensions/candor/intel.py:8-11`, `core/scout/runner.py:155`)
  stays intact for the live intel brief; dreaming is where conclusions are allowed to
  exist — as rows in a sidecar table, not as prompt text.
- **Counterfactual validation.** Re-run scout offline against past failures to test
  whether a lesson actually changes the plan.
- **Gated promotion.** Validated conclusions reach scout via existing recall
  machinery (lesson entries) and, optionally, a new flagged preload section.
- **A dream report.** A periodic human-readable artifact: contradictions found,
  hypotheses raised/refuted, lessons that demonstrably work or don't.

Non-goals (explicitly out of scope):

- No self-modification of skills, prompts, or code. The existing hard rule — skill
  edits are never auto-applied (`core/snooze_reflect.py:9-12`) — is the outer wall
  and this plan does not approach it.
- No automatic deletion or archival of anything the dream did not itself write.
  Demotion of user/distill-authored entries is proposal-only, human-approved.
- No changes to existing snooze activities, the memory format, scout's existing
  sections, the Candor bridge's existing entry points, or the reflect pipeline.
  Every touch is an addition; flag off ⇒ byte-identical behavior.
- No fixes to pre-existing defects found during recon (§13) — listed for separate
  triage so this plan stays reviewable.

---

## 2. Hard constraints the design must respect (verified)

1. **Snooze cycles are short, serial, and budgeted.** `run_cycle` wraps `_do_cycle`
   in `asyncio.wait_for(..., timeout=settings.snooze_max_cycle_seconds)` — default
   **60 s** (`core/snooze.py:183-186`, `config.py:218`). Activities run as a
   hardcoded serial `if`-ladder (`snooze.py:209-419`), with two one-LLM-call-per-
   cycle budget booleans (`did_llm`, `did_maintenance_llm`, `snooze.py:214-217`).
   ⇒ Dreaming must be **incremental**: one small step per cycle, watermarked, never
   a long nightly pass inside the cycle. Long work (RLM probes) must run outside
   `_do_cycle` via `MaintenanceRunner.track_task` (`maintenance.py:81`).

2. **Cancellation is cooperative and advisory.** Poll `self._is_cancelled()` at every
   boundary; pass it as a callable into helpers (pattern:
   `bridge.run_maintenance(self._is_cancelled)`, `snooze.py:2278`); dispatch
   uncancellable write-pairs via `asyncio.to_thread` (`snooze.py:910-928`).

3. **The memory markdown format is roundtrip-fragile.** `format_entry` silently drops
   fields it doesn't emit — the `updated` field is already lost on every
   `move_entries` + `reindex` (`core/memory/format.py:52-61`, `store.py:655-663`).
   Consolidation can silently skip fused entries and strand omitted ones in archived
   files (§13). ⇒ **Dream state never lives in markdown metadata.** It lives in new
   sidecar DB tables; memory is touched only through `store.add_entry` /
   `store.delete_entry` with self-contained prose.

4. **Entry references are unstable.** Consolidation moves entries across files,
   splitting relocates them, `repair_epoch_collisions` renumbers epochs
   (`store.py:929`). ⇒ Hypotheses reference evidence by `(file_name, epoch)` **plus a
   content-hash prefix**; a failed re-resolution expires the hypothesis, it never
   guesses.

5. **All Candor access crosses the bridge, single-threaded.** One
   `ThreadPoolExecutor(max_workers=1)` owns the `CandorSystem`
   (`core/extensions/candor/bridge.py:63`); `asyncio.to_thread` is explicitly unsafe
   (`bridge.py:3-7`). New reads are added as `_impl(system, ...)` + an async
   `_submit` wrapper — the circuit breaker and `candor_enabled` gate come free
   (`bridge.py:104-143`). Loop-safe sync calls do not exist; `_submit_sync` raises on
   the event loop (`bridge.py:143`).

6. **Candor already has the hypothesis primitives.** Verified in
   `/home/calvincs/Desktop/Candor/src/candor/system.py`:
   `conjecture(goal, sim_budget, commit=False)` — `commit=False` is documented as
   "the read-only path … nothing is appended, nothing moves" (`system.py:1071+`);
   `commit=True` files a CLAIM under a separately-calibrated `conjecture/v1`
   predictor class whose track record Candor scores on settlement. Also unexposed
   and useful: `events_since(cursor)` (`system.py:1763`) for incremental evidence
   reads, `health()` (`system.py:1779`), `recall(query, budget)` (`system.py:1034`).

7. **Scout prompt assembly is additive and sectioned.** Preload parts are gathered
   in fixed order and joined (`core/scout/runner.py:1505-1518`); the agent system
   prompt is an ordered `system_parts` list (`core/context/compiler.py:653-703`)
   with a cache-sensitive fixed prefix. A new preload part in scout's **user
   message** (like the Candor intel brief, `runner.py:1483-1504`) touches nothing
   existing and has no prompt-cache impact.

8. **Scout runs offline without side effects.** `run_scout` performs no DB writes;
   all memory searches pass `_track_hits=False`; scout tools are read-only
   (verified sweep of `runner.py:397-519`, `1353-1504`). The `scout.done` message
   row is written by the session manager, not by `run_scout`
   (`sessions/manager.py:1495-1517`). Caveats for replay: the module report cache
   lives in `run_scout` (bypass it by calling `_run_scout_llm` directly), and
   cross-session exclusion keys off the session id — replay must exclude the
   replayed session so scout can't "remember" the failure through FTS (verify the
   exact parameter at implementation time).

9. **Post-mortems alone cannot drive replay.** A `post_mortems` row holds no user
   message and no transcript (`core/reflect.py:899-939`); replay needs
   `db.get_messages(session_id)` for the `role='user'` message and the lossy
   `role='scout'` projection, and attempt↔message alignment is inferential on
   multi-turn sessions. ⇒ v1 replay restricts itself to sessions where alignment is
   unambiguous (single user turn, or the latest turn).

10. **Settings, migrations, tests have exact precedents.** Flags: the `candor_*` /
    `rlm_*` blocks (`config.py:182-204`), bounds in `_SETTING_BOUNDS`
    (`api/routers/health.py:121-148`), UI `SECTIONS` (`settings.js:101-122`).
    Schema: append a versioned entry to `MIGRATIONS` (next free: **v19**), applied
    once inside `BEGIN IMMEDIATE` (`db/database.py:635-669`). Tests: pure-rules
    layer + end-to-end watermark/idempotency driver (`tests/test_synthesis.py`),
    per-test temp isolation + `FakeLLMClient` (`tests/conftest.py`).

11. **LLM conventions for background work.** Model = `settings.background_model or
    settings.llm_model`; `client.chat` with no `session_id` (inherits
    `PRIORITY_BACKGROUND`); free-text output, `/no_think` sentinel, fence-strip,
    defensive parse returning empty on failure (`core/snooze_reflect.py:319-340`,
    `snooze.py:646-666`). No JSON mode exists anywhere — do not introduce one here.

---

## 3. Design overview

```
                        ┌─────────────────  idle (snooze)  ─────────────────┐
 evidence sources        │  OBSERVE      HYPOTHESIZE      VALIDATE   PROMOTE │   live (scout)
 ────────────────        │                                                   │
 post_mortems ──cursor──▶│ evidence      typed rows in    evidence    lesson │──▶ search_lessons
 candor events ─cursor──▶│ pack (small,  dream_hypotheses checks  ─▶  entries│    (existing path)
 memory sample ─rotate──▶│ quoted,       (sidecar, never  + scout     source │
 candor conjecture() ───▶│ delimited)    prompt text)     replay      ="dream"──▶ optional flagged
                         │                                                   │    preload section
                         └────────────── dream report (weekly .md) ─────────┘
```

Principles:

- **Sidecar, not annotation.** All dream state lives in two new tables plus
  `snooze_state` watermark keys. No markdown metadata, no new columns on existing
  tables, no changes to existing rows.
- **Phase separation (the Genera lesson).** Live sessions: read-only. Idle cycle:
  the single writer. There is no mid-session dream mutation in any phase of this
  plan.
- **Write-permission rule.** The dream may create rows in its own tables, write
  files under `workspace/dreams/`, and add memory entries with `source="dream"`.
  It may delete/replace **only entries it authored** (`source == "dream"`).
  Everything else — demoting a user/distill entry, editing a skill — is
  proposal-only, surfaced in the report and applied by a human.
- **Hypotheses are not beliefs.** A hypothesis row does nothing until validated;
  a validated row does nothing until promoted; promotion has evidence thresholds
  and its own flag. Refuted hypotheses are kept and matched against so the dreamer
  cannot resurrect them (dedup against *seen*, not against *confirmed*).
- **Untrusted content discipline.** Memory entries may contain distilled web
  content (no claim-origin provenance exists — `MemoryEntry`, `format.py:10-27`).
  Dream prompts wrap all evidence in explicit delimiters with a "data, not
  instructions" preamble, and the dream LLM is chat-only — no tool schemas are
  ever passed to it.

---

## 4. Data model (migration v19)

```sql
CREATE TABLE IF NOT EXISTS dream_hypotheses (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,      -- contradiction | lesson_ineffective | tool_pattern
                                      -- | memory_stale | open_question | conjecture
    statement     TEXT NOT NULL,      -- self-contained prose claim
    evidence_json TEXT NOT NULL,      -- [{type: pm|candor|memory|conjecture, ref, content_hash?, quote?}]
    status        TEXT NOT NULL DEFAULT 'pending',
                                      -- pending | validated | refuted | expired | promoted | archived
    confidence    REAL NOT NULL DEFAULT 0.0,
    validation_json TEXT,             -- {method, results, checked_at[]} — appended per pass
    promoted_ref  TEXT,               -- memory ref of the promoted entry, if any
    origin        TEXT NOT NULL DEFAULT 'dream_cycle',   -- dream_cycle | candor_conjecture | rlm_probe
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dream_hyp_status ON dream_hypotheses(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dream_hyp_kind   ON dream_hypotheses(kind, status);

CREATE TABLE IF NOT EXISTS dream_reports (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    path         TEXT NOT NULL,       -- workspace-relative markdown path
    stats_json   TEXT NOT NULL        -- counts by kind/status, spend, durations
);
```

`snooze_state` watermark keys (generic KV, no migration needed — `db/database.py:271-275`):
`dream_pm_cursor` (post_mortems `created_at` high-water), `dream_candor_cursor`
(ledger seq for `events_since`), `dream_mem_cursor` (memory file rotation),
`dream_last_report` (ISO), `dream_replays:{YYYY-MM-DD}` (per-day replay counter).

New settings (all following `config.py:182-204` precedent; defaults chosen so the
feature is fully inert until switched on):

| Field | Default | Purpose |
|---|---|---|
| `dream_enabled` | `False` | master switch — activity absent from the cycle when off |
| `dream_hypotheses_per_cycle` | `3` | cap on rows created per step |
| `dream_validation_replays_per_day` | `4` | scout-replay LLM spend guard |
| `dream_report_interval_days` | `7` | report cadence |
| `dream_scout_inject` | `False` | Phase 3: promoted-conclusions preload section |
| `dream_promotion_min_passes` | `2` | validation passes ≥ `dream_promotion_min_gap_days` apart |
| `dream_promotion_min_gap_days` | `3` | anti-confabulation spacing |
| `dream_rlm_probe` | `False` | Phase 4: deep probes (also requires `rlm_enabled`) |

Bounds for the numerics go into `_SETTING_BOUNDS` (`api/routers/health.py:121-148`);
a "Dreaming" section goes into `settings.js` `SECTIONS`.

New module: `core/dream/` — `observe.py` (evidence-pack assembly, pure where
possible), `hypothesize.py` (prompt + parse, pure parse layer), `validate.py`
(per-kind checks + state machine, pure rules layer), `promote.py`, `report.py`,
`__init__.py` (the activity driver called from snooze). Mirrors the
`synthesis.py` pure-rules/driver split so the test suite convention applies
directly.

---

## 5. Phase 0 — Substrate (no behavior change)

1. Settings fields + bounds + UI section (above).
2. Migration v19 (above) + `db/models.py` accessors:
   `add_dream_hypothesis`, `list_dream_hypotheses(status, kind, limit)`,
   `update_dream_hypothesis(id, *, status, confidence, validation_json, ...)`,
   `add_dream_report`, `list_dream_reports(limit)`.
3. Candor bridge read extensions (each an `_impl` + `async` wrapper via `_submit`,
   per `bridge.py:128-132`; all return `None` when disabled/broken):
   - `events_tail(cursor: int, kinds: set[str] | None) -> list[dict] | None`
   - `conjectures(goal: dict, sim_budget: float) -> list[dict] | None` — always
     `commit=False` in this phase; committing is a Phase 3 open question (§14).
   - `health_snapshot() -> dict | None`
   - `predict(pred: str, args: list) -> dict | None` (async twin of the existing
     off-loop-only `predict_sync`, `bridge.py:388`).
4. `core/dream/` package skeleton with the state machine and evidence-ref
   resolution (`(file_name, epoch, content_hash)` → entry lookup via
   `store.read_file` + `parse_entries_from_markdown`; mismatch ⇒ `expired`).
5. Tests: migration applies on a fresh DB (`init_db()` per-test already exercises
   this); accessor roundtrips; bridge wrappers no-op when `candor_enabled=False`;
   state-machine transitions (pure).

Risk: none in practice — nothing calls dream code yet; the migration is one
versioned transaction with the v12/v18 precedents.

## 6. Phase 1 — The dream step (observe + hypothesize) and the report

**Registration.** New Activity 14 block at the tail of `_do_cycle` (after refine,
`snooze.py:403-419` is the template): gated on `not self._is_cancelled() and
settings.dream_enabled`, its own `can_llm` check, its own budget (independent of
`did_llm`, like refine — comment must document the extra call), a
`bus.emit({"type": "snooze.activity", "activity": "dream", "detail": ...})` line
(the UI renders it with zero JS changes — `file-panel.js:1866-1873`), and
`await self._dream_step()` which delegates to `core/dream/__init__.py:run_step()`.
Stats via `self._stats.setdefault("dream_hypotheses", 0)` etc.

**One step = one bounded LLM call.** `observe.py` assembles a small evidence pack
from the cursors: new post-mortems since `dream_pm_cursor` (verdict, failure_cause,
scout_summary — a few rows), Candor events since `dream_candor_cursor` (when
enabled), one memory file sampled by rotation (`dream_mem_cursor`), the lessons
that scout has recently recalled plus their ages, and `conjectures()` output as
pre-made candidates. Everything quoted, delimited, capped (~6–8 k chars total).
`hypothesize.py` prompts the background model for at most
`dream_hypotheses_per_cycle` typed hypotheses, each required to cite evidence refs
from the pack; parse defensively per convention; drop anything whose refs don't
resolve; dedup by `SequenceMatcher` (threshold ~0.8, same-kind) against **all**
non-archived hypotheses including `refuted`. Advance cursors only after the write
(mark-on-failure is deliberately *not* copied here: a failed LLM call leaves the
cursor so the evidence is retried — it's a cursor, not a per-session watermark, so
there's no head-of-line blocking).

Additionally, filter at generation time: hypotheses of the form "X is missing /
unconfigured" are rejected outright — the fc329cb class of conclusion stays banned
even inside the sidecar, because it is exactly the class that validated poorly.

**The report.** When `dream_last_report` is older than the interval and there is
material: `report.py` writes `workspace/dreams/DREAM-<date>.md` (sections:
contradictions, hypotheses raised, refuted this period, open questions, store
health notes), inserts a `dream_reports` row, and emits the activity event. The
file is visible in the existing file explorer with zero UI work. Nothing else
happens — Phase 1's entire observable output is a markdown file and sidecar rows.

Spend: ≤ 1 background-model call per idle cycle (≈ the cost of one existing
distill/insight step), only when idle-gated conditions already pass.

Tests: pure parse + dedup + fc329cb filter; driver with `FakeLLMClient` (step
creates rows, advances cursors, idempotent on rerun, honors cancellation between
items, no-op when flag off); report writer (interval gating, file content).

## 7. Phase 2 — Validation

`validate.py` picks up to N pending hypotheses per cycle (oldest first) and applies
the check matching their kind:

- **`tool_pattern`** — pure evidence check, no LLM: re-read Candor
  (`predict`/`raw_counts` via the Phase 0 wrappers) for the implicated statements;
  confirm direction and effect size still hold; record the numbers in
  `validation_json`.
- **`contradiction` / `memory_stale`** — re-resolve both refs (content-hash guard);
  one LLM judge call with both entries quoted, asked only "do these still both
  claim to be true and conflict?"; refute on any hedge.
- **`lesson_ineffective` / lesson-effectiveness** — **counterfactual scout replay.**
  Select a qualifying post-mortem (failing verdict, unambiguous turn alignment per
  §2.9); rebuild the brief via `build_session_brief`; call `_run_scout_llm`
  directly (bypasses the report cache); ensure the replayed session is excluded
  from cross-session search; diff the produced plan against the recorded
  `role='scout'` projection + `post_mortems.scout_summary`; one LLM judge call:
  "given failure_cause F, does the new plan avoid the recorded failure?" Debit
  `dream_replays:{date}`; hard-stop at `dream_validation_replays_per_day`.
- **`open_question` / `conjecture`** — no validation path; they exist for the
  report and for Candor's own claim machinery later.

Transitions: `pending → validated | refuted | expired`. A `validated` row is
re-checked on a later pass before promotion (§8) — `validation_json.checked_at`
accumulates. Refuted rows keep their evidence and the refutation reason; the
report's "refuted this period" section is deliberately prominent (a high refute
rate early is the system working, not failing).

Phase 2 still writes nothing outside the sidecar and the report.

Tests: per-kind pure rules with canned evidence; replay driver with `FakeLLMClient`
(cap enforcement, cache non-pollution, exclusion of the replayed session); state
machine property tests (no transition out of `refuted` except `archived`).

## 8. Phase 3 — Promotion (closing the loop)

Gate (`promote.py`): status `validated`, `confidence ≥ 0.7`, and at least
`dream_promotion_min_passes` validation passes spaced
`dream_promotion_min_gap_days` apart with consistent results. Then:

1. **Primary channel — a lesson entry.** Write one self-contained entry via
   `store.add_entry(content=..., entry_type="lesson", tags="dream,validated,...",
   weight="normal", source="dream")`. It now surfaces through the existing
   `search_lessons` recall path (`store.py:783-824`, scout `runner.py:926-968`)
   with **zero scout changes**. Content must carry its own evidence line
   ("validated over N observations across M days; refutes/confirms: ...").
   Notes: dream entries are not `user.*` files so no Candor attestation fires
   (`store.py:57-76` gates on the `user.` prefix); lesson age-decay applies, which
   is desirable — a conclusion that is never revalidated fades. Revalidation
   refreshes by `delete_entry`(old, dream-authored only) + `add_entry`(new) under
   the §3 write-permission rule.
2. **Optional channel — scout preload section**, behind `dream_scout_inject`
   (default off; mirrors `candor_scout_brief`). A ninth gathered part
   (`_gather_dream_conclusions`) rendering the top-K promoted conclusions with
   their evidence counts under a distinct header, e.g.
   `[VALIDATED OPERATIONAL NOTES]`, with consumer rules in the scout prompt
   modeled on the intel-brief rules (`runner.py:131`): advisory heuristics; each
   line carries its evidence basis; never grounds for refusing a task. This does
   **not** resurrect the withdrawn SCOUT SIGNALS PREFER/AVOID block — the stale
   prompt text about it (`runner.py:123-128`) is left untouched (§13).
3. **Demotion proposals.** For contradictions where the dream-side entry is *not*
   the loser: the report lists the proposal with quoted evidence; a minimal
   API pair (`GET /api/dream/hypotheses`, `POST /api/dream/hypotheses/{id}/resolve`
   with `action: demote|dismiss`) lets the user apply it. Demote executes via a
   new **public** `MemoryStore.archive_entries(file_name, epochs)` implementing
   the established archive pattern (`snooze.py:929-983`) as store-owned code —
   deliberate small duplication rather than refactoring snooze's private copy;
   unify later, outside this plan.
4. **UI (cheap, optional):** a dreams section in the file panel jobs tab cloned
   from the RLM runs section (`file-panel.js:1927-1973`) listing recent reports
   and pending demotion proposals.

Tests: gate thresholds (pure); promotion writes exactly one entry and stamps
`promoted_ref`; scout preload section respects flag/off; demotion route touches
only the targeted epochs; write-permission rule enforced (attempt to delete a
non-dream entry raises).

## 9. Phase 4 — Deep probes and in-session hooks (optional, separately flagged)

**9a. RLM dream probe** (`dream_rlm_probe` + `rlm_enabled`). For accumulated
material a single-call step can't chew (cross-file pattern mining), run the RLM
engine directly — not the tool — as a maintenance-tracked background task
(`maintenance.track_task`, `maintenance.py:81`), never inside the 60 s cycle:

- Stage **copies** of the evidence (exported memory files, hypothesis JSON, candor
  brief text) via `stage_context` into the run dir; the child never sees live DBs
  or `data/` (recon: the child REPL can `open`/`__import__` freely —
  `child_runner.py:16-19,137-138` — containment is env/rlimits, so we simply don't
  point it at anything live).
- Inject chat callables per the test precedent (`engine.py:48`, `broker.py:32`)
  bound to the background model; small caps (`RLMCaps(timeout_seconds=300,
  max_iterations=8, max_subcalls=12)`); `cancel_check` wired to snooze
  cancellation + shutdown.
- Record via `runs.record_start(session_id="dream", ...)` so the existing RLM runs
  panel and retention sweep (`snooze.py:2200-2241`) cover it for free.
- Output: hypothesis candidates fed through the same Phase 1 dedup/filter — the
  probe gets no special write powers.

**9b. In-session read-only introspection** (future, listed for completeness): a
post-tool-failure hint via the `internal_recall` pattern ("have I failed like this
before?"), and a write-behind observation buffer folded by the dream step —
the Candor `pending.jsonl` discipline (`bridge.py:15-17`) applied to dream
observations. Neither is required for the loop to close; both stay out of scope
until Phases 1–3 have burned in.

---

## 10. Failure modes and guards

| Failure mode | Guard |
|---|---|
| Confabulated patterns | Hypotheses inert until validated; evidence refs mandatory and re-resolved (content hash); promotion needs ≥2 spaced consistent passes; refute-first framing in validation prompts |
| Self-reinforcement drift | `source="dream"` labels every write; dream evidence packs exclude dream-authored entries from the *hypothesis* sample (they may only appear as validation subjects); promoted count capped by gate strictness; report makes volume visible |
| Prompt injection via distilled web content | Delimited data-not-instructions framing; dream LLM is chat-only (no tools); conclusions influence scout only after validation against *outcome records* (post-mortems/Candor), which injected prose cannot fabricate; fc329cb-class conclusions filtered at generation |
| Resurrection of refuted ideas | Dedup against all seen (incl. refuted), not just active |
| Cycle overrun / cancellation | One LLM call per step; `_is_cancelled` polled between items; sidecar writes are single-row commits; markdown writes go through `store.add_entry` (flock discipline, `store.py:162-202`) |
| Candor thread-safety | All access via new bridge wrappers on the confined executor; circuit breaker inherited |
| Stale refs after consolidation/split | Content-hash mismatch ⇒ `expired`, never guessed |
| Replay cost runaway | Per-day counter in `snooze_state`; replay only for unambiguous turns; judge is one call |
| Report spam | Interval-gated; only written when there is material |
| Kill switches | `dream_enabled` removes the activity entirely; `dream_scout_inject` severs the only live-prompt influence; dropping the two tables is safe (sidecar); promoted lesson entries are identifiable (`source="dream"`) and deletable in bulk |

Spend summary: Phase 1 ≤1 background call/idle-cycle; Phase 2 adds ≤1 judge call
per validated item and ≤`dream_validation_replays_per_day` scout replays; Phase 3
adds nothing recurring; Phase 4a is explicitly budgeted per probe.

## 11. Test plan

Per the house convention (`tests/test_synthesis.py`, `tests/conftest.py`): every
module ships a pure-rules layer testable without I/O (parsing, dedup, state
machine, promotion gate, report rendering) and an end-to-end driver layer against
the per-test temp DB with `FakeLLMClient` (watermark idempotency, cancellation,
flag-off no-op, crash-mid-step retry safety). Bridge wrappers get the
`test_candor_extension.py` treatment (disabled/broken/enabled paths). One
integration test per phase runs a full snooze `_do_cycle` with dreaming enabled
and asserts existing activities' outputs are unchanged.

## 12. Rollout

1. Land Phase 0+1 together; enable `dream_enabled` on the box only; run ≥1 week.
   Watch: hypotheses/day, kinds distribution, dedup hit rate, step duration vs the
   60 s cycle, report readability. Success = reports a human finds informative;
   failure = noise ⇒ tune evidence pack before Phase 2.
2. Phase 2 next; watch the refute rate (expect high early — that is the feature),
   replay cap adherence, judge-call spend.
3. Phase 3 with `dream_scout_inject` **still off**: promotion via lesson entries
   only. Only after promoted lessons prove non-disruptive in scout recall, flip
   the inject flag. Every step has a same-day revert: flag off.
4. Phase 4 on demand.

## 13. Pre-existing defects found during recon (out of scope, listed for triage)

Dream validation quality depends on memory integrity, so these matter, but fixing
them is not part of this additive plan:

1. Consolidation fuses entries with hard-coded `entry_type="finding"`, no tags, no
   weight — and the fused write is often silently skipped by the duplicate gate
   while stats still count it (`consolidate.py:512-535`, `store.py:122-133`).
2. `entries_to_archive` from merge verdicts is never actually archived
   (`consolidate.py:541`).
3. Whole-file archival strands any source-file entry the merge LLM omitted from
   its verdict — unbounded silent data loss (`consolidate.py:537-539`,
   `store.py:580-610`, `format.py:118-119`).
4. `updated` timestamp is dropped on every `move_entries` + `reindex`
   (`format.py:52-61`).
5. `weight="low"` diverges between FTS and markdown (`format.py:73-74`).
6. The advertised `@tags:` FTS filter is stripped to an ordinary token
   (`search.py:56-80` vs `RULES.md`, `memory_tools.py:505`, `runner.py:1378`).
7. Stale scout-prompt text describes a SCOUT SIGNALS block nothing injects
   (`runner.py:123-128`).
8. Dead config: `memory_recall_min_score` (`config.py:180`).

## 14. Open questions

1. **Candor loopback.** Should promoted conclusions be filed as Candor CLAIMs
   (`conjecture(commit=True)` / `claim`+`resolve`) so Candor scores the dream
   engine's calibration on its own separately-tracked curve? Elegant (the
   scorekeeping is built and isolated by design), but it makes the dream a Candor
   *writer* — proposal: revisit after Phase 2 with real refute-rate data.
2. **Demotion autonomy.** Does human-approved demotion ever graduate to automatic
   for the narrow case where Candor evidence is overwhelming (e.g. p < 0.2 over
   50+ observations)? Default: no; revisit with data.
3. **Claim-origin provenance in memory.** Distinguishing web-derived from
   self-derived content at distill time would materially harden the injection
   story, but touches the fragile format roundtrip (§2.3) — separate initiative?
4. **Report surfacing.** Is the file-explorer artifact + jobs-panel section
   enough, or should the report land as a push notification
   (`core/push.py` exists)?
