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
fixtures, rationale). Proposals wait in the Adaptive tab for a human.
Approving materializes the `CANARY.md` (validated by a parse round-trip) and
queues a manual vetting run so you see it pass before it counts.

**Auto-admission.** `canary_auto_admit` defaults to **true**, so a proposal
that clears the allowlist proof (`core/canary/propose.py`) is materialized
into `data/canaries/` immediately, without waiting for you. The proof is
what makes that safe, and it is deliberately narrow: every gate command must
parse, resolve to a closed set of known-safe binaries, run
`python -m pytest` / `python -m unittest` only, carry no shell metacharacters
(no pipes, redirects, substitution, chaining), and reference only
workspace-relative paths — plus no model override, a timeout under the auto
cap, and room left under `canary_max_suite`. Anything outside that set falls
back to human approval, and you get a notification either way.
Auto-admitted canaries land tagged `vetting` + `flaky: true`, so
they inform but cannot trip the tripwire until `canary_vetting_runs`
consistent passes promote them. Set `canary_auto_admit=false` if you want
every canary to pass under your eye first.

**Long-green canaries are demoted, not retired.** After
`canary_retire_after_passes` consecutive passes a canary's `cadence` doubles
(capped at 12), so it runs on every Nth *scheduled* sweep instead of every
one. It stays in the suite deliberately: the tripwire's baseline pass rate is
computed from scheduled runs of these same tasks, so deleting the stable
canaries would shrink the denominator of the only signal allowed to
auto-roll-back a batch. Post-batch and manual sweeps ignore `cadence` and run
everything.

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

Producers emit adaptive edits, each batch carrying ≥1 evidence reference
(post-mortem ids, dream hypothesis ids, Candor ledger refs) — an edit without
evidence is refused:

- **Refine** — user corrections → `prompt_note`, technique/tool patterns →
  `routing_hint`, sequencing rules → `policy` (gated). Refine may also
  propose canaries (above). (A separate `snooze_reflect` producer existed
  briefly and was folded back into Refine; the module is gone.)
- **Dream promotion** — the deferred phase from
  [dream.md](dream.md) now ships here: mechanically-validated tool patterns
  → `routing_hint`; counterfactually-validated ineffective lessons →
  `policy` proposals; contradiction/stale-memory findings → proposals
  carrying the **memory-correction effector** (below). Every dream edit is
  global-scope and every global-scope dream edit escalates to high risk, so
  all of them are proposal-gated — none auto-applies.
- **Candor** — calibrated reliability regressions → `routing_hint` with the
  ledger's audit chain as evidence (queued during Snooze's Candor
  maintenance activity). The same activity also **retires** hints it no
  longer needs (below).
- **Telos** — a hypothesis evaluated `supported` with real evidence and
  confidence ≥ 0.65 queues a global `routing_hint` under producer `telos`,
  so a validated claim about how the agent should work actually reaches
  scout instead of dying in the journal.

#### Hint retirement — every producer, not just Candor

Minting alone is a ratchet: every entry consumes a slot in the per-kind cap
and nothing ever gives one back. Once a kind fills, every further edit is
rejected at apply time — a failure mode indistinguishable from a producer
with nothing to report. All three programmatic producers now retire:

| Producer | Retires when | Where |
|---|---|---|
| Candor | the tool recovered above the degradation threshold | `core/snooze.py` (Candor maintenance) |
| Dream | the originating hypothesis is gone or unpromoted, its cited Candor facts recovered, or the entry passed a 90-day TTL | `core/dream/retire.py` (per dream step) |
| Telos | the cited hypothesis is missing or no longer `supported`, its question was abandoned, or the entry passed a 90-day TTL | `core/telos/retire.py` (daily slow loop) |

The TTLs are the honest part: Dream and Telos verdicts are terminal by
construction, so the evidence-withdrawn criteria rarely fire on their own.
Without a TTL those passes would be decorative. A still-true claim re-mints
cheaply; a slot held forever cannot.

A cap rejection also raises an **operator notification** now, so "the shelf
is full" is visibly different from "the loop had nothing to say".

Candor's rule in detail: a `routing_hint` is retired when all three hold: its
`source` is `candor` (a producer deleting its own entry stays low-risk, so
the cross-producer escalation doesn't fire), its id has the
`tool-<name>-degraded` shape, and the tool has **recovered** — it no longer
appears in `degraded_tools()` because calibrated reliability climbed back
above threshold. Evidence reads `candor:tool_ok recovered (<id>)`. Mints and
retires are capped at 2 each per pass and queued as one batch. Dedupe on the
mint side only checks *live* hints, so a hint can legitimately come back if
the tool degrades again.

#### The memory-correction effector

Dream's contradiction and stale-memory findings used to produce review-only
proposals: approving them merely acknowledged the finding and wrote nothing,
which is why the great majority of pending proposals on a long-running
install had no effector at all.

Approving such a proposal now runs `apply_memory_correction()`, which is
**additive and non-destructive**. For each cited memory file (capped at 3,
drawn from the hypothesis's pinned evidence) it appends one new entry —
`entry_type="note"`, `weight="high"`, `source="dream_fix"`, tagged
`correction,<kind>` — prefixed `CONTRADICTION RESOLVED` or `STALE-INFO
CORRECTION` and ending with an instruction to treat the note as overriding
conflicting older entries in that file. **The disputed entries are left in
place.** Nothing is edited or deleted, so the correction is itself reviewable
and the original record survives. The approve response reports
`corrections_written`.

This path is handled before the normal batch machinery: `memory_correction`
is a payload action, not one of the three entry actions.

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
  hints applied — review"), and neither is a cap rejection ("Adaptive layer:
  entry cap reached").
- **Structural immutability** — the apply path writes only `adaptive_*`
  rows; it has no file-write capability. `SOUL.md`/`RULES.md` and the base
  prompt stay machine-untouchable.
- **Release valve** — `DELETE /api/adaptive/entries/{id}` (the *Delete*
  button on each entry in the Adaptive tab) soft-deletes one entry as actor
  `human`: status flips to `deleted`, the version increments, and a `delete`
  event with full before/after snapshots is journaled, so it rolls back like
  any other change. This exists because producers can only ever *fill* the
  per-kind cap; without a human way to free a slot, a cap wedged full of
  stale machine entries stays wedged. The event is intentionally minted
  outside any batch, so it is reversible by `event_id` rather than by batch.

### Per-tier proposals

When a producer's batch mixes risk tiers and `adaptive_auto_apply` is off,
the queue mints **one proposal per tier** — the rationale is suffixed
`— high-risk tier` / `— low-risk tier`, and the result carries both
`proposal_id` (the first) and `proposal_ids` (all). Folding low-risk edits in
with high-risk ones would make approval all-or-nothing across tiers, forcing
you to accept a `policy` change to get a `routing_hint`.

### Proposals, rollback, and the tripwire

High-risk edits become **apply-on-approve proposals**
(`/api/adaptive/proposals`, Explorer → Adaptive tab): approving executes the
batch through the same apply engine as auto-applies — and mints the same
batch id and post-batch canary sweep, so batch-tagged measurement data
accumulates even with auto-apply off.

The pending queue is a **veto window, not an approval gate**. A proposal
still pending after `adaptive_auto_approve_after_hours` (default 24h) is
approved by the system itself in snooze Activity 15 — oldest first, capped at
`adaptive_max_auto_approvals_per_day`, resolved as `auto_approved` so the
audit trail distinguishes it from a human `approved`. The reasoning: dream
hypotheses are evidence-judged *before* they mint a proposal, and the
validation that actually measures anything — tripwire drift, post-batch
sweeps — can only run *after* application; a queue that waits on a scarce
human click just converts validated lessons into TTL lapses. Reject inside
the window to veto; roll back the batch afterward to overrule. Canary-suite
proposals are the exception and never auto-approve: materializing a canary
keeps its human invariant (I6), and graduated autonomy for canaries lives in
`canary_auto_admit` instead.

The two notifications this produces are written for whoever has to act on
them — including the agent, when a user pastes one back and asks what it
meant (box session dce9a6de7f81 is the case that shaped them). The
**auto-approved** notice lists each proposal: what it was, where it landed,
and how to undo it — "roll back batch ab-…" for edit batches; "delete the
entry tagged `dream:<id>` in `<file>`" for memory corrections, which create
no batch and nothing the Adaptive panel can roll back; nothing for
review-only rows. The **queue-full** notice names the cap that actually
refused the insert — the per-producer share
`adaptive_max_pending_per_producer` trips far more often than the global
cap — and says how many pending rows are canary proposals waiting on a
human. Corrective memory entries carry their approver in the preamble
(`human-approved via adaptive review` vs `auto-approved after the 24h veto
window`), so "what did a human actually approve" stays answerable. For the
same reason `/api/adaptive/proposals` documents its status enum, serves
`status=all` and `?id=`, and answers an unknown status with a 400 rather
than an empty list that reads as "those rows are gone".

**Rollback is exact.** Every apply is an append-only event with full
before/after snapshots; `rollback(batch_id | event_id)` walks the events in
reverse and restores each entry byte-for-byte (or deletes what the batch
created). Rollback is itself an event. One click in the Adaptive tab, or
`POST /api/adaptive/rollback`.

**The tripwire** watches every batch with two signals, both anchored on when
the batch actually **applied** — not when it was queued. `created_at` is
stamped at queue time, and a batch can sit pending in the proposal queue for
days, so the anchor is the earliest non-rollback journal event for the batch
(events are read ascending), falling back to `created_at` only when nothing
landed.

- *Primary (active)*: the batch's post-batch canary sweep vs. the trailing
  `canary_baseline_runs` scheduled sweeps that ran *before* the apply — a
  pass-rate drop ≥ `canary_regression_delta` (0.15) is a regression. Canaries
  detected as flaky are excluded.
- *Secondary (passive)*: organic post-mortem reflect-retry drift over the
  `adaptive_tripwire_window_turns` (20) turns after the apply, compared
  against the 20 organic turns immediately before it (canary-stamped
  post-mortems excluded, so the probe can't contaminate the signal).

The "after" window is fetched **oldest-first from the apply timestamp**, and
the sweep simply waits until that many organic turns exist. Slicing the
newest-first feed instead would compare the newest turns *overall* — a moving
target that drifts further from the batch the longer the system keeps
running, so a batch could be judged on turns that had nothing to do with it.

Either signal flags the batch **`suspect`** — surfaced in the Adaptive tab,
cleared by human dismiss (`POST /api/adaptive/batches/{id}/dismiss`, the
*Dismiss flag* button) or a subsequent clean sweep. **Dismissal is durable**:
it stamps `cleared_at`, and the sweep skips any batch that has one, so the
same evidence can never re-raise a flag you already looked at. Only the
primary (canary) signal can promote to automatic rollback, and only with
`adaptive_auto_rollback` on (off by default) — the passive post-mortem signal
never rolls anything back on its own. Leave auto-rollback off until the
metric has earned that trust on your suite.

Neither the adaptive layer nor the canary suite emits SSE. Both surface
through the polled REST endpoints, plus a high-urgency notification row when
the tripwire flags or auto-rolls-back a batch.

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
