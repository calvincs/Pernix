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
tags: [coding, debug]  # 'sentinel' = rides along on every post-batch probe
covers: []           # change surfaces this canary tests, e.g. [skill:foo, kind:prompt_note]
flaky: false         # flaky canaries inform, never trip the tripwire
parked: false        # parked = off the heartbeat; still coverage/full/manual-run
max_runs: 0          # probe: auto-retire after N total runs (0 = never)
expires: ""          # probe: auto-retire after this ISO date
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
— flaky canaries inform but never feed the tripwire. `covers:` is the
targeting index: change-driven triggers (below) select canaries whose
`covers` matches what changed. Invalid files log a warning and are skipped;
one bad canary never sinks a sweep.

### How a run works

Each canary runs as a headless `session_type="canary"` session through the
**full pipeline** — scout → agent → gates → reflect — because a canary that
skips what real turns exercise measures nothing. The workspace is a temp
directory per run (seeded from `files:`), the gates materialize as
canary-scoped rows for the run and are deleted after, and the score is the
gates re-run against the final workspace state. A run that triggered reflect
retries scores the final attempt; the retry count is recorded.

Every run records an **outcome**: `pass`, `gate_fail` (the agent ran and
the work was wrong), `timeout` (killed at the wall clock), `error` (the
harness broke), or `noop` (zero tokens, sub-second — the agent never
executed). Only `gate_fail` is evidence about the agent; the rest are
suite-health trouble, and the tripwire ignores them. Results land in the
`canary_runs` table (gate results, outcome, error, pass/fail, retries,
tokens, duration) and in the Explorer's **Self-tuning → Self-checks (Canary)**
tab.

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
- **Tool-allowlisted** — every canary session runs under
  `CANARY_TOOL_ALLOWLIST` (computation and reads only: file/search/repl
  tools, memory *recall*, read-only skill and tool discovery), enforced at
  the same three points as scheduled-job charters. Canary prompts carry
  machine-authored content — auto-admitted tasks, injected SKILL.md bodies
  during skill-verify runs — so workers, jobs, notifications, and every
  skill/tool/memory mutation are fenced off for the whole session type.

### Triggers — change-driven, not wall-clock

Canaries run when something they cover **changes**; the only standing
schedule is a small heartbeat. This replaced the original nightly-full-suite
+ full-post-batch design after a live audit showed 80% of run volume
re-testing tasks nothing had touched, at a 99% pass rate.

| Trigger | When |
|---|---|
| `scheduled` | The nightly **heartbeat** (`canary_schedule`, default `0 3 * * *`): the `canary_heartbeat_per_night` (2) least-recently-run non-parked canaries. Keeps every active canary's history warm enough that a post-change failure is provably the change's fault. |
| `post_batch` | Enqueued after every adaptive apply (auto **or** approved proposal), tagged with the batch id — the tripwire's active probe. **Targeted**: canaries whose `covers` matches the batch's edit kinds first, `sentinel`-tagged ones riding along, capped at `canary_post_batch_max` (4), resolved at execution time (the suite may have changed during the deferral to the next idle window) — a batch with neither a coverage nor a sentinel match falls back to the active non-flaky canaries, so the tripwire is never left blind by omission. A non-flaky `gate_fail` is immediately **confirm-rerun once** in the same sweep — two rows is what the tripwire calls confirmed. Enqueued for the next idle window, never dispatched inline. |
| `manual` | The `canary_run(name)` tool, `POST /api/canary/run`, the Self-checks tab's run buttons, or a coverage-triggered targeted sweep (e.g. a skill edit). `canary_status` reads recent results. |
| `full` | The-world-changed sweeps: a model swap (both switch paths), a deploy (the boot version stamp), or the tab's "Run all". Runs **everything including parked canaries** and carries `must_run`, so a sweep already in flight defers it instead of eating it (the lock is otherwise skip-not-queue). |

One sweep runs at a time; Snooze prunes runs past
`canary_retention_days` (30).

### Growing the suite

Start with a small hand-written seed covering your daily-driver categories.
From there the suite grows the way a regression-test suite does — from real
failures: while `canary_enabled` is on, the refine pass may **propose** a new
canary distilled from a genuinely failed turn (name, prompt, gates,
fixtures, rationale). Proposals wait in the Learning (Adaptive) tab for a
human. Approving materializes the `CANARY.md` (validated by a parse round-trip)
and
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

**Long-green canaries are parked, never removed.** After
`canary_park_after_passes` (25) consecutive passes, maintenance writes
`parked: true`: the canary leaves the heartbeat rotation but stays in the
suite — coverage triggers, full sweeps and manual runs still fire it, and
**any red run auto-unparks it** (the one mutation allowed while a canary is
red, because it amplifies the alarm instead of silencing it). It is never
deleted: the per-task tripwire only lets a canary testify against a batch
when its trailing runs were green, so removing the stable canaries would
disarm exactly the signal they feed. (This replaced cadence demotion, which
replaced retirement — same invariant, third mechanism.)

**Full lifecycle control** lives in the Self-checks tab and the API: create
(raw CANARY.md or structured spec — gate commands are checked against the
auto-admission allowlist proof and the verdicts returned as *warnings*,
never blockers), edit (`PUT`, validated round-trip), park/unpark
(`PATCH`), mark reviewed, and retire (`DELETE` — the directory moves to
`.retired/` and is purged only after `canary_purge_after_days`, so a
retirement is reversible for the whole window).

**One-off probes**: a canary with `max_runs: N` or an `expires:` date is a
probe — "occasionally test something" without suite residue. Maintenance
retires an exhausted probe with a pass/fail tally notification;
retirement-with-the-tally IS the probe's report, so this pass is
deliberately exempt from the Goodhart lock (nothing is silenced — the red
runs are in the tally). The tab has a one-click probe template.

**Skill verify blocks** (`core/canary/skill_verify.py`): a skill may embed
its own behavioral test in SKILL.md frontmatter —

```yaml
verify:
  prompt: |
    Use the technique this skill teaches on the seeded fixture...
  gates:
    - name: check
      command: python -m pytest tests/test_expected.py -q
  files: { ... }       # optional fixtures
  timeout: 600         # optional
```

Maintenance materializes it as the MANAGED canary `skill--<name>` with
`covers: [skill:<name>]`, resyncs it whenever the skill changes, and
retires it when the block (or the skill) goes away. A sha256 watermark over
each SKILL.md (`snooze_state['skill_hash:<name>']`, the `skill_reqs_hash`
precedent) detects every mutation path including hand edits; a changed
skill fires one targeted sweep of its covering canaries at the next idle
window. **Security boundary**: verify-gate commands execute on the host and
SKILL.md is machine-editable, so every gate must pass the same allowlist
proof as canary auto-admission — a skill whose gates fail the proof gets a
once-per-content notification and no canary.

Staleness is curated, not automated away: 90 days past a canary's
`last_reviewed` date, Snooze nudges you with a notification. Bump the date
(the tab's *Reviewed ✓* button) after reviewing; the nudge re-arms when it
goes stale again.

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

(`worker_spec` was carved in v3.1: fully-built consumption, zero live rows
ever, and no reachable producer — its high-risk gating meant a human would
have had to approve YAML that refine would first have had to spontaneously
emit.)

### The actionability floor (v3.1)

The consumers of this store are prompts, and prompts act on instructions —
but the live audit found the policy slots full of narrative complaints
("Despite high-confidence verifications, the agent repeatedly fails
to..."), auto-approved unread through the veto window. Three layers now
stop that at the mouth:

- **The mechanical lint** (`core/adaptive/lint.py`), applied inside
  `queue_producer_edits` — under all four machine producers. Narrative
  shapes are refused; negative tool claims pass only with the fix clause
  (Candor's "prefer an alternative or verify; see why_reliability(...)"
  template is the model citizen); policy/routing_hint content must contain
  an actionable directive. Human authorship uses the direct create path
  and is deliberately unlinted — the human is the authority the lint
  substitutes for.
- **Dream's actionability gate** (`core/dream/promote.py`): both promotion
  channels (`lesson_ineffective→policy`, `tool_pattern→routing_hint`) pass
  through one bounded judge call that rewrites the validated finding into
  an imperative rule or rules honestly that none exists —
  `reported:not-actionable` is terminal, and the finding still reaches the
  dream report. A tool_pattern restating a live Candor hint is a terminal
  duplicate.
- **Refine's contract** gained the Do-NOT-capture rules, a worked bad→good
  example, and a confidence field (floor and 2-edit cap enforced
  mechanically in the parser). Telos hints dropped the "Supported
  hypothesis (...)" framing and must pass the lint or stand as claims only.

### The usefulness signal (v3.1)

The layer could always detect harm (the tripwire); it could never detect
benefit — so retirement ran on 90-day clocks and the store converged on
what was *recent*, not what *worked*. Now both consumption paths report
usage: rendered entries carry their ids, scout echoes the hints that
shaped its plan (`used_hints`, counted once at the fresh-report seam),
reflect sees an id-carrying `ACTIVE ADAPTIVE POLICIES` section in its
evidence and may cite up to five in `cited_policies`. Both flow through
post-mortems into synthesis and land as `adaptive_entry` rows in
`scout_signals`. Counters surface in the Learning tab (zero-use
highlighted) and drive:

- **Value-based retirement** — `retire_unused_entries` (Activity 15):
  entries with zero recorded uses over `adaptive_usage_retire_days` of
  *instrumented* life are retired (journaled soft-deletes, one aggregate
  daily notification, one-click rollback). The usage epoch is stamped on
  the sweep's first run so pre-instrumentation entries get a full observed
  window before they can be judged. `prompt_note` — previously the kind
  with no retirement loop at all — also gets a TTL backstop.
- **Failure-dominated retirement** — the same sweep also reads the OUTCOME
  half of the signal (successes/failures attributed by synthesis): an
  entry with ≥ `adaptive_harmful_retire_min_uses` attributed outcomes
  whose success share sits below `adaptive_harmful_retire_max_success`
  retires *even though it is used*. Usage-only retention had the perverse
  edge: a harmful hint cited every turn was immortal precisely because it
  was cited, while an uncited good one died at the window. No age/epoch
  gate — the outcomes themselves are the observed window. Exempt sources
  (candor, user) and the journaled-delete/rollback path are shared with
  the unused sweep.
- **Capped, ranked rendering** — the scout hints block ranks by observed
  outcome share (Laplace-smoothed `(s+1)/(n+2)`, so unattributed entries
  sit at a neutral 0.5), then usage, and caps at 12 lines/1.6k chars with
  a truncation marker (which finally makes `search_adaptive`'s trigger
  real); the agent block caps at 12 policies/12k chars with deterministic
  source-priority selection (user > refine > candor > telos > dream) —
  stable bytes between idle applies, prompt-cache safe.

### Authorship (v3.1)

`SOURCES` always declared `user` and `agent`; no path ever minted either.
Now: **you** author directly (`POST /api/adaptive/entries`, the Adaptive
tab's *New entry* form — immediately active, journaled, unlinted), and
**the agent** authors through the `adaptive_note` tool
(`adaptive_agent_notes_enabled`): prompt_note/routing_hint only, the lint
applies, 2 mints/day, normal batch pipeline + tripwire — an agent never
writes `policy` about itself.

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

### Task taxonomy and resource channels (v3.1)

A related signal lives beside the adaptive store rather than in it: `model_route`
rows in `scout_signals`, one per (model, task category), fed by reflect's
post-mortems and read back by scout as the `[MODEL ROUTING INTEL]` brief —
"models absent here have no known problem," steering `recommended_model`
away from a listed pair when an alternative exists. Three v3.1 fixes make it
worth trusting:

- **A real task taxonomy.** Scout classifies each turn's `task_type`
  (`research | coding | data_analysis | writing | ops | conversational`,
  `core/scout/runner.py` `TASK_TYPES`) as a statistics label only — it never
  changes how the turn runs. Reflect stamps it onto the post-mortem as
  `task_category`, falling back to the legacy `execution_mode` stamp for
  older reports. Before this, `model_route` was keyed by `execution_mode`,
  whose two live values made almost everything read as `inline`; keying by
  the real task type is what makes the brief's exception rows mean anything.
  The brief drops rows with `n < 5` observations, a `≥70%` pass rate, or no
  counter movement in 45 days (`_ROUTE_STALE_DAYS`, `core/synthesis.py`) —
  legacy-keyed subjects age out on their own, no migration needed.
- **Decoupled resource channels.** Reflect stamps `turn_metrics` (tokens,
  LLM calls, wall-clock, retries included, anchored on the turn's user
  message) into every post-mortem; synthesis accumulates per-(model,
  category) averages into the `model_route` signal's payload via
  read-merge-write, and the brief renders them as context — `avg ~Nk tok,
  ~Ns/turn` — never as a routing rule. The pass/fail rate stays the only
  thing that steers `recommended_model`.
- **Fallback-burn watch** (`core/llm/burnwatch.py`): a standing snooze check
  for the 2026-08-19 incident where a dead primary provider key silently
  rerouted every call to the paid fallback for days. When `fallback_model`
  served ≥ `fallback_burn_alert_share` (0.25) of the trailing 24h's tokens
  with at least `fallback_burn_min_tokens` (50000) of volume, one
  high-urgency notification fires per day naming the share, the volume, and
  the fix (the primary's key, at the compose `.env` level). Watch-only —
  it mints a notification and touches nothing else; `fallback_burn_alert_share
  = 0` disables it.

`cost_estimate` (`token_usage.cost_estimate`, previously a dead column) is
now actually written: the stream ladder prices each usage frame from
`model_prices` (`{model_id: {"in": $/Mtok prompt, "out": $/Mtok completion}}`,
exact-id match only — a partial match would silently price the wrong model)
via `estimate_cost()` in `core/llm/stream_ladder.py`. Unpriced and local
models keep `cost_estimate` NULL; per-session cost sums light up on their
own once a model has an entry in `model_prices`.

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
- **Caps** — `adaptive_max_entries_per_kind` (24),
  `adaptive_max_auto_applies_per_day` (24), `adaptive_edit_cooldown_hours`
  (24) per entry.
- **Notification** — auto-applies are never silent ("adaptive: 2 routing
  hints applied — review"), and neither is a cap rejection ("Adaptive layer:
  entry cap reached").
- **Structural immutability** — the apply path writes only `adaptive_*`
  rows; it has no file-write capability. `SOUL.md`/`RULES.md` and the base
  prompt stay machine-untouchable.
- **Release valve** — `DELETE /api/adaptive/entries/{id}` (the *Delete*
  button on each entry in the Learning tab) soft-deletes one entry as actor
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
(`/api/adaptive/proposals`, Explorer → Self-tuning → Learning): approving
executes the batch through the same apply engine as auto-applies — and mints
the same
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

**Memory corrections skip the queue (2026-08-21).** A validated dream
contradiction / stale-memory finding is additive — approving it writes a
corrective entry beside the disputed ones, deletes nothing — so the veto
window protected nothing and only delayed it: 280 hypotheses queued behind
a 12-row per-producer share and a 10-a-day drain. Promotion now mints the
proposal row for the audit trail and applies it at once (`auto_applied`,
provenance "auto-applied on validation — dream finding, no veto window"),
bypassing `adaptive_max_pending_*`. Every applied correction is narrated in
the dream journal; the operator gets one notification per day. The same
(kind, file) correction is not re-applied within a week. Undo = delete the
entry tagged `dream:<id>` in the memory file. Policy and routing-hint
promotions still go through the queue.

**Rollback is exact.** Every apply is an append-only event with full
before/after snapshots; `rollback(batch_id | event_id)` walks the events in
reverse and restores each entry byte-for-byte (or deletes what the batch
created). Rollback is itself an event. One click in the Learning tab, or
`POST /api/adaptive/rollback`.

**The tripwire** watches every batch with two signals, both anchored on when
the batch actually **applied** — not when it was queued. `created_at` is
stamped at queue time, and a batch can sit pending in the proposal queue for
days, so the anchor is the earliest non-rollback journal event for the batch
(events are read ascending), falling back to `created_at` only when nothing
landed.

- *Primary (active)*: **per-task verdicts** from the batch's post-batch
  canary sweep. A non-flaky canary whose trailing runs before the apply
  (up to `canary_baseline_runs` (5), floored at 3 — fewer than 3 recorded
  runs and the task can't testify either way) were all green — the *green
  precondition* — and which records `gate_fail` post-batch with no pass,
  has regressed;
  because the sweep confirm-reruns every gate_fail, a **confirmed**
  regression shows two gate_fail rows for the (batch, task) pair, while a
  single row (the rerun itself died) flags but can never auto-roll-back.
  Timeouts, errors, noop runs and pre-v30 legacy rows neither trip nor
  certify — with nothing usable to judge, the signal reports unavailable
  rather than issuing a false all-clear. (The original aggregate
  pass-rate-delta form had a structural dead zone — one failure among 8
  canaries was a 12.5% drop against a 15% delta, so it never fired once in
  production — and was replaced in v3.1.)
- *Secondary (passive)*: organic post-mortem reflect-retry drift over the
  `adaptive_tripwire_window_turns` (20) turns after the apply, compared
  against the 20 organic turns immediately before it (canary-stamped
  post-mortems excluded, so the probe can't contaminate the signal); drift
  ≥ `canary_regression_delta` (0.15) flags.

The "after" window is fetched **oldest-first from the apply timestamp**, and
the sweep simply waits until that many organic turns exist. Slicing the
newest-first feed instead would compare the newest turns *overall* — a moving
target that drifts further from the batch the longer the system keeps
running, so a batch could be judged on turns that had nothing to do with it.

Either signal flags the batch **`suspect`** — surfaced in the Learning tab,
cleared by human dismiss (`POST /api/adaptive/batches/{id}/dismiss`, the
*Dismiss flag* button), a subsequent clean sweep, or — for flags raised by
the passive signal alone — an automatic expiry after
`adaptive_suspect_ttl_days` (7): the passive comparison windows are frozen
at the apply, so such a flag can never self-clear, and four batches sat
suspect for 12 days on the live box waiting for clicks nobody owed them.
Canary-confirmed flags never expire. **Dismissal is durable**:
it stamps `cleared_at`, and the sweep skips any batch that has one, so the
same evidence can never re-raise a flag you already looked at. Only a
**confirmed** primary (canary) verdict can promote to automatic rollback,
and only with `adaptive_auto_rollback` on (off by default) — an unconfirmed
gate_fail and the passive post-mortem signal never roll anything back on
their own. Leave auto-rollback off until the metric has earned that trust
on your suite.

Neither the adaptive layer nor the canary suite emits SSE. Both surface
through the polled REST endpoints, plus a high-urgency notification row when
the tripwire flags or auto-rolls-back a batch.

---

## Burn-in — the recommended order

The pair is safe *because* measurement precedes actuation. Turn things on in
this order:

1. **`canary_enabled` first, alone, for at least a week.** Heartbeats build
   each canary's green history (the tripwire's per-task precondition); you
   learn which canaries are flaky before anything depends on them.
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
