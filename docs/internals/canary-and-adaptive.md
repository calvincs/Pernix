# Canary Suite & Adaptive Layer — Measured Self-Improvement

Pernix has long had the *observation* half of self-improvement — post-mortems,
scout signals, [Candor](candor.md), [Dream](dream.md), the refine pass. These
two subsystems ship the other half:

- The **canary suite** (`core/canary/`) is the measurement substrate: golden
  tasks with deterministic gates, run headlessly through the full pipeline,
  answering the question no ledger of anecdotes can — *is the agent actually
  getting better or worse?*
- The **adaptive layer** (`core/adaptive/`) is the actuation substrate: a
  governed, machine-editable policy store with full history, exact rollback,
  and a tripwire wired to the canary numbers.

Both are off by default (`canary_enabled`, `adaptive_enabled`) and inert when
off: zero rows written, compiler output byte-identical. They are designed as
a pair — the adaptive layer without the canary suite is actuation without
measurement, which is why the recommended burn-in (last section) turns them
on in that order.

---

## The canary suite

### What a canary is

One directory per canary under `data/canaries/`, each holding a `CANARY.md`:
frontmatter defining the task, a markdown body of free-form notes for the
humans who review it. A full example:

```markdown
---
name: fix-failing-test
prompt: |
  The test in tests/test_math.py fails. Find the bug and fix it.
gates:
  - name: pytest
    command: python -m pytest tests/test_math.py -q
    watch_paths: [src/]
files:
  src/mathlib.py: |
    def add(a, b):
        return a - b   # the planted bug
  tests/test_math.py: |
    from src.mathlib import add
    def test_add():
        assert add(2, 2) == 4
model: ""            # optional model override
timeout: 600         # optional per-run wall clock (seconds)
tags: [coding, debug]
flaky: false         # flaky canaries inform, never trip the tripwire
last_reviewed: 2026-08-06
---
Checks that the agent can localize a one-line arithmetic bug from a failing
test and fix it without breaking the test file.
```

`name`, `prompt`, and a non-empty `gates` list are required — a canary
without gates cannot be scored. The optional `files:` map seeds the run's
workspace with deterministic fixtures (workspace-relative paths only), so
canaries are self-contained: fixtures over live URLs, per the flakiness
discipline. Gates locally deterministic; anything that can't be, tag `flaky`
— flaky canaries inform but never feed the tripwire. Invalid files log a
warning and are skipped; one bad canary never sinks a sweep.

### How a run works

Each canary runs as a headless `session_type="canary"` session through the
**full pipeline** — scout → agent → gates → reflect — because a canary that
skips what real turns exercise measures nothing. The workspace is a temp
directory per run (seeded from `files:`), the gates materialize as
canary-scoped rows for the run and are deleted after, and the score is the
gates re-run against the final workspace state. A run that triggered reflect
retries scores the final attempt; the retry count is recorded. Results land
in the `canary_runs` table (gate results, pass/fail, retries, tokens,
duration) and in the Explorer's **Canary** tab.

### Isolation guarantees

Canary sessions are deliberately hard synthetic tasks; letting them leak into
the stores that shape live behavior would poison the very signals they exist
to guard. The isolation is an enumerated predicate list, not a vibe:

- **No memory writes** — the memory-write tools are denied to canary
  sessions (reads stay: recall quality is part of what's measured).
- **Invisible to search** — canary messages are excluded from session FTS.
- **Excluded from distill/refine sweeps** and from Candor's reliability
  ledger.
- **Post-mortems are written but stamped** `session_type='canary'` and
  excluded from the passive tripwire window and model-routing aggregation.
- **Snooze-transparent** — canary sessions neither cancel a snooze cycle nor
  block its idle gate, so the nightly sweep and idle housekeeping coexist.
- **Hidden from the session sidebar** like Dream journals.

### Triggers

| Trigger | When |
|---|---|
| `scheduled` | The nightly sweep (`canary_schedule`, default `0 3 * * *`) — this builds the baseline. |
| `post_batch` | Enqueued after every adaptive apply (auto **or** approved proposal), tagged with the batch id — this is the tripwire's active probe. Enqueued for the next idle window, never dispatched inline. |
| `manual` | The `canary_run(name)` tool, `POST /api/canary/run`, or the Canary tab's run buttons — needed to vet a newly approved canary. `canary_status` reads recent results. |

At most `canary_max_concurrent` (1) canary sessions run at once; Snooze
prunes runs past `canary_retention_days` (30).

### Growing the suite

Start with a small hand-written seed covering your daily-driver categories.
From there the suite grows the way a regression-test suite does — from real
failures: while `canary_enabled` is on, the refine pass may **propose** a new
canary distilled from a genuinely failed turn (name, prompt, gates,
fixtures, rationale). Proposals wait in the Adaptive tab for a human;
**nothing writes `data/canaries/` without your approval**. Approving
materializes the `CANARY.md` (validated by a parse round-trip) and queues a
manual vetting run so you see it pass before it counts.

Staleness is curated, not automated away: 90 days past a canary's
`last_reviewed` date, Snooze nudges you with a notification. Bump the date
after reviewing; the nudge re-arms when it goes stale again.

---

## The adaptive layer

### What it stores — kinds and risk tiers

A governed store of machine-editable **policy** — distinct from memory
(facts) and skills (instructions). Four kinds:

| Kind | What | Consumed by | Risk tier |
|---|---|---|---|
| `routing_hint` | Tool/skill/model selection guidance | Scout only (`[ADAPTIVE ROUTING HINTS]` + `search_adaptive`) | **low → auto-apply** |
| `prompt_note` | Supplemental directive ≤400 chars | The agent's compiler block | **low → auto-apply** |
| `policy` | Behavioral rule with control-flow weight | The agent's compiler block | **high → proposal-gated** |
| `worker_spec` | Reusable worker template: instructions, model, gate set | `spawn_worker(spec=...)` via the `[WORKER SPECS]` catalog | **high → proposal-gated** |

Risk is computed at apply time, and two escalations gate otherwise-low-risk
edits: any **delete** of another producer's entry, and any **global-scope**
edit originating from Dream. Adaptive entries *supplement* `RULES.md`, never
override it — a producer whose edit contradicts a rule must route it as a
gated proposal with the conflict flagged. The store is SQLite
(`adaptive_entries` / `adaptive_events` / `adaptive_batches` /
`adaptive_proposals`, full before/after snapshots on every event); a
read-only rendered mirror at `data/adaptive/ADAPTIVE.md` is regenerated on
change and **never read back** — hand-editing a version-chained store would
corrupt rollback, so don't.

### Producers

Four subsystems emit adaptive edits, each batch carrying ≥1 evidence
reference (post-mortem ids, dream hypothesis ids, Candor ledger refs) — an
edit without evidence is refused:

- **Refine** and **snooze-reflect** — user corrections → `prompt_note`,
  technique/tool patterns → `routing_hint`, sequencing rules → `policy`
  (gated). Refine may also propose canaries (above).
- **Dream promotion** — the deferred phase from
  [dream.md](dream.md) now ships here: mechanically-validated tool patterns
  → `routing_hint`; counterfactually-validated ineffective lessons →
  `policy` proposals; contradiction/stale-memory findings → review-only
  proposals where approving *acknowledges* — memory edits stay human.
- **Candor** — calibrated reliability regressions → `routing_hint` with the
  ledger's audit chain as evidence (queued during Snooze's Candor
  maintenance activity).

### Apply discipline

- **Plan/apply split** — producers record each touched entry's version at
  planning time; apply re-reads and rejects entries that moved ("entry
  changed during planning") while the rest of the batch applies.
- **Idle windows only** — session-scoped edits apply when that session is
  idle; global edits apply only inside a snooze cycle that passed the idle
  gate, because a global edit invalidates every session's cached prompt
  prefix and must never land mid-turn.
- **Caps** — `adaptive_max_entries_per_kind` (12),
  `adaptive_max_auto_applies_per_day` (6), `adaptive_edit_cooldown_hours`
  (24) per entry.
- **Notification** — auto-applies are never silent ("adaptive: 2 routing
  hints applied — review").
- **Structural immutability** — the apply path writes only `adaptive_*`
  rows; it has no file-write capability. `SOUL.md`/`RULES.md` and the base
  prompt stay machine-untouchable.

### Proposals, rollback, and the tripwire

High-risk edits become **apply-on-approve proposals**
(`/api/adaptive/proposals`, Explorer → Adaptive tab): approving executes the
batch through the same apply engine as auto-applies — and mints the same
batch id and post-batch canary sweep, so batch-tagged measurement data
accumulates even with auto-apply off.

**Rollback is exact.** Every apply is an append-only event with full
before/after snapshots; `rollback(batch_id | event_id)` walks the events in
reverse and restores each entry byte-for-byte (or deletes what the batch
created). Rollback is itself an event. One click in the Adaptive tab, or
`POST /api/adaptive/rollback`.

**The tripwire** watches every batch with two signals:

- *Primary (active)*: the batch's post-batch canary sweep vs. the trailing
  `canary_baseline_runs` scheduled sweeps — a pass-rate drop ≥
  `canary_regression_delta` (0.15) is a regression.
- *Secondary (passive)*: organic post-mortem reflect-retry drift over the
  `adaptive_tripwire_window_turns` (20) turns after the batch
  (canary-stamped post-mortems excluded, so the probe can't contaminate the
  signal).

Either signal flags the batch **`suspect`** — surfaced in the Adaptive tab,
cleared by human dismiss (`POST /api/adaptive/batches/{id}/dismiss`) or a
subsequent clean sweep. With `adaptive_auto_rollback` on (off by default), a
canary regression promotes to automatic rollback; leave it off until the
metric has earned that trust on your suite.

---

## Burn-in — the recommended order

The pair is safe *because* measurement precedes actuation. Turn things on in
this order:

1. **`canary_enabled` first, alone, for at least a week.** Nightly sweeps
   build a stable baseline; you learn which canaries are flaky before
   anything depends on them.
2. **Then `adaptive_enabled` with `adaptive_auto_apply` off.** Producers
   emit, everything routes through proposals, approved applies mint batch
   ids and post-batch sweeps — batch-tagged data accumulates while you watch
   what the machine *wants* to change.
3. **Then `adaptive_auto_apply` on** (the default once enabled) for the
   low-risk kinds, with the tripwire now grounded in a week-plus of
   baseline. `adaptive_auto_rollback` last, if ever.

## Settings

See [configuration.md](../configuration.md#canary-suite) and
[configuration.md](../configuration.md#adaptive-layer). API surface:
[api.md](../api.md#canary-suite), [api.md](../api.md#adaptive-layer).
