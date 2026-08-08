# TELOS — The Teleological Layer

`core/telos` gives Pernix a **non-convergent drive with correction machinery**: a root objective that is a *question with no satisfaction predicate* (it can never be completed, only re-expressed with operator co-sign), a fast loop that turns turn-time anomalies into falsifiable hypotheses, and a set of slow loops that audit the goal hierarchy for the classic failure modes — goals drifting loose of their purpose, proxy metrics consuming budget while the real question stalls, completions that discharge nothing, a self-story that diverges from the record, and exploration going cold.

The full derivation from the source material is in [docs/dev/telos-spec.md](../dev/telos-spec.md). This page describes what shipped.

Off by default. Master switch: `telos_enabled` (`config.py`). While off the layer is fully inert: no directories are created, snooze Activity 16 is skipped, the cron never installs, and the post-task hook never fires. All call sites gate at runtime (hot toggle) except tool registration, which follows the Candor pattern (restart).

## State: markdown as database

Everything lives under `data/telos/` as markdown files with YAML frontmatter — greppable, diffable, no schema migration:

```
data/telos/
  config/telos.yaml          the layer's own provenance record (readable tier)
  config/state.yaml          runtime knobs actuated by Entropy Control
  questions/q_*.md           first-class Questions with provenance
  soup/h_*.md                hypotheses: gated, speculation pool, resolved
  goals/g_*.md               root, dreams, milestones, tasks
  claims/c_*.md              committed claims with epistemic-class caps
  alarms/a_*.md              binding | hevel | divergence | acedia
  ledgers/first_person/      weekly autobiography (agent-derived)
  ledgers/trace/             append-only JSONL, one file per UTC day
```

The trace ledger is the authority (spec §5.4): `TelosStore.trace_append` (`core/telos/store.py`) is the only writer in the codebase, nothing exposes a rewrite path, and the agent-facing tools get no trace write access. The autobiography is tested *against* it, never the reverse.

## The fast loop

**Turn end** (`sessions/hooks.py` → `core/telos/anomaly.py`): mechanical, no LLM. Every turn appends a trace event. Anomalies — a normally-reliable tool failing, a reflect retry, a round-ceiling termination — mint Questions, with surprise scaled by the violated prior (Candor's calibrated p when available). A slice of minted questions is tagged `serendipity`: high-surprise, deliberately unbound from any goal, so the layer cannot become a pure exploiter of its current goal set (§3.2).

**Idle** (snooze Activity 16, `core/snooze.py` → `core/telos/__init__.py:run_step`): one bounded unit per cycle, round-robined like Dream:

- **Generate** (`core/telos/soup.py`): the scheduler pulls the next question (85% goal-linked / 15% serendipity, deterministic counter). SOUP samples memory at three analogical distances — near / mid / far, default 50/30/20 — and one LLM call produces structure-mapped hypotheses, each with a falsifier (observable + decision rule), an EIG estimate, and a cost estimate. The **testability gate** admits a hypothesis to execution iff the falsifier is defined, cost fits `telos_max_eval_tokens`, and EIG ≥ `telos_eig_floor` — where EIG is first multiplied by the generator's **calibration discount** (`calibration.py`). Both halves of that metric are in the trace: the predicted `eig` rides the `hypothesis` event, and whether evaluation actually resolved anything rides `hypothesis_resolved` / `hypothesis_pooled`. The headline score is the Brier the spec asks for; the discount keys on the *reliability component* (mean claimed EIG minus realized resolve rate), because the Brier total is blind to the exact failure it was commissioned to catch — a constant 0.4 against all-inconclusive outcomes scores 0.16, "better" than an honest 0.5. Over-claiming is discounted, under-claiming is never inflated, the correction is floored at 0.25×, and the rolling window means a bad patch ages out rather than latching the gate shut. `telos_status` shows the current figure. Rejected hypotheses are *not deleted* — they enter the speculation pool (`status: soup`): searchable, with zero execution rights. The spec's recombination-by-future-SOUP-passes is **not implemented**; nothing currently reads `status == "soup"`, so treat the pool as a retained record rather than a live feedstock.
- **Evaluate** (`core/telos/evaluate.py`): the oldest gated hypothesis has its falsifier applied by one LLM judge against gathered evidence (memory, trace, Candor brief). Supported/refuted verdicts commit a claim; two inconclusive attempts return the hypothesis to the pool. Resolving hypotheses narrows their parent question — committed knowledge changes what counts as an anomaly, which closes the loop.

## The slow loops

Installed by `ensure_telos_schedule()` (`core/extensions/scheduling/__init__.py`) as a transient daily cron (`telos_schedule`, default 04:00 UTC), recreated each boot from settings. Weekly and monthly blocks are watermarked in `snooze_state` so cadence changes never double-run them.

| Loop | Cadence | What it does |
|---|---|---|
| **Ordo Pass** (`ordo.py`) | daily | Walks the goal DAG from `g_root`. Orphans (no parent chain to root) are suspended and listed for operator review — never deleted. Active siblings re-ranked by advancement × discharge history × claim support, with a 0.5 discount for vapor classes. The correction is a re-ranking, not a purge. |
| **Binding Monitor** (`binding.py`) | daily | The Goodhart detector. Alarm iff, over 7 days: budget share > `telos_budget_share_max` AND the subgoal's own activity is climbing AND its parent questions aren't narrowing AND new claims are below floor. L1 log + immediate ordo → L2 freeze pending re-justification (persists 2 windows) → L3 operator escalation. Escalation is time-anchored: a window advances only after ~20h of the signature holding, so a faster `telos_schedule` cron re-checks more often without climbing the ladder faster. |
| **Hevel Audit** (`hevel.py`) | on completion + weekly rollup | Discharge `D(G) = α·entropy_reduction + β·new_questions`. Classes with mean D < 0.1 across ≥ 3 completions are marked **vapor**: discounted at the ordo re-rank, never banned. (The spec's third term, a γ-weighted re-open rate, is not implemented: TELOS has no goal-reopen path, so `goal_reopened` is never emitted and the penalty was structurally always zero. Build the path, then restore the term.) |
| **Reconciliation** (`reconcile.py`) | weekly | Compiles a first-person autobiography from the trace (one LLM call; every claim must cite trace refs), then mechanically diffs it: each cited event is **opened** and tested for shared evidence with the claim — the event's own type token appears in the claim, or the claim names an id (`g_`/`q_`/`h_`/`c_`) present in the event JSON, or enough content words overlap. Supported claims commit as `observation_of_self`; unsupported ones are repaired (`confabulation_repaired`) and capped at the `self_report` ceiling. Divergence > `telos_divergence_max` raises an alarm. The coherence time series in `state.yaml` *is* the identity metric. The overlap test is a deliberately crude proxy for entailment — but a proxy a paraphrase can fail, which the previous "is the ref number in range" check could not. |
| **Entropy Control** (`entropy.py`) | weekly | The acedia detector. If novelty entropy over executed hypotheses < 0.2 or the far band's realized share < 0.10, shift the band mix toward far and bump the serendipity budget until the floor recovers; decay back toward defaults when healthy. "Executed" excludes `gated` candidates in both metrics, and novelty buckets on the memory files the SOUP actually sampled (`context_files`) rather than the model-authored `source_domain` label — falling back to the label only for hypotheses that carry no sampled-file record. |
| **Dream register review** (`ordo.py`) | monthly | Every dream must fail the capability test (`capability_gap: true`, not completable). Violations and fully-milestoned dreams are flagged for the operator — reclassification is operator work. |

## Humility layer

Claims commit through `TelosStore.commit_claim` with hard confidence caps by epistemic class (spec §6): observation 0.99, inference 0.95, testimony 0.90, analogy 0.70, **self_report 0.60**. A self-report corroborated by the trace is reclassified `observation_of_self` and escapes the cap — the path to confident self-knowledge runs through the external record, not introspection. `data/telos/config/telos.yaml` is the *readable* provenance tier: the agent can inspect who installed its drive and what it is aimed at. The substrate tier (model weights) stays opaque, deliberately (spec §9).

## Surfaces

- **Agent tools** (`core/extensions/telos/`): `telos_status`, `telos_ask`, `telos_goal_add`, `telos_goal_complete`. Deliberately absent: trace writes, root re-expression, alarm clearing.
- **API** (`api/routers/telos.py`): `GET /api/telos` (overview), `/questions`, `/hypotheses`, `/goals`, `/claims`, `/trace` (read-only ledger window), `POST /api/telos/run` (manual slow-loop pass), `POST /api/telos/alarms/{id}/ack`.
- **UI**: the **Telos** tab in the Explorer panel; settings section "Telos (Teleological Layer)".

## Isolation and safety properties

- Canary sessions never mint questions (`anomaly.py` early-return) — synthetic turns must not seed the drive.
- Every entry point is guarded; a TELOS failure logs a warning and the turn/cycle completes normally.
- The post-task hook is delta-tracked per turn (reflect retries never double-trace) and bounded by a 10s wait.
- Open questions are capped (120) and per-turn minting is capped (2), with near-duplicate rejection.
- The root cannot be completed, the trace cannot be rewritten, and the agent cannot clear alarms — three invariants that hold by construction, not policy.

## Actuation ports (2026-08 refactor)

TELOS is wired into the executing system in four places — the difference
between "telos noticed" and "telos changed behavior":

1. **Goal attribution** — anomaly-minted questions bind to the session's
   active `session_goals` row, mirrored into the store as `g_db_<id>`
   (`store.ensure_db_goal`). The binding monitor blends the goal's real
   `token_usage` (7-day window) into budget share, so the Goodhart detector
   measures actual spend, not just TELOS's own soup/evaluate tokens.
   Serendipity questions stay bound to `g_root` by design.
2. **Adaptive output** — a `supported` claim with recorded evidence and
   judge confidence ≥ 0.65 queues a `routing_hint` through
   `queue_producer_edits("telos", …)`; the adaptive engine's risk gating,
   caps, and rollback apply unchanged.
3. **Context port** — up to three open questions (by surprise) plus a live
   alarm count render into the **volatile tail**: a single pinned system
   message appended as the *last* message of every compile
   (`_build_telos_drive_block`, composed into `_build_volatile_tail`
   alongside the to-the-second clock, resource status and goal burn). Keeping
   all per-call churn in the suffix is what lets the provider's prompt-prefix
   cache stay valid across turns — a clock in the system head would
   invalidate it on every single call. The block is the empty string when
   `telos_enabled` is off, so the compiled output is byte-identical.
4. **Escalation that terminates** — the binding ladder monitors suspended
   goals too, so L2 is not a terminal state: a persisting signature climbs
   L2 → L3 (operator notification at `high` urgency; L2 notifies at
   `normal`), and a signature that stops holding clears the alarm and
   un-suspends the goal — but **only if this monitor was what suspended it**
   (the alarm id is matched inside `suspended_reason`). Suspensions imposed
   by the ordo pass or by the operator survive.

   Acknowledging an alarm silences its notification but leaves it live
   (`open` and `acknowledged` are both live states), so the ladder keeps its
   place instead of minting a fresh L1 on the next pass and restarting the
   climb from zero. An acked alarm is forced back to `open` when the ladder
   actually climbs, because a climb is new evidence.
