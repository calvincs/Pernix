# Dream — Idle-Time Introspection

The Dream subsystem (`core/dream/`) gives Pernix an idle-time faculty that
examines its own memory, Candor evidence, and post-mortems; generates typed
hypotheses about itself; and then **tries to falsify them** against recorded
outcomes. Nothing a dream produces influences live behavior until it has been
validated — and validated conclusions reach live behavior only through the
[Adaptive Layer](canary-and-adaptive.md)'s governed promotion path (itself
off by default).

Off by default. Enable in Settings → Dream (Introspection); all settings apply
hot. Runs as the final activity of the [Snooze ladder](reflect-and-snooze.md),
so it only ever spends idle time.

## The idea

Memory today is a Polaroid — written once, never falsified. An entry that says
"X always fails" keeps saying it long after X was fixed. Candor gives the
inbound mirror (outcomes flow in continuously); dreaming closes the loop by
asking, offline and unhurried, whether the beliefs still square with the
evidence:

- **Contradictions** between memory entries, or between lessons and Candor's
  outcome records.
- **Stale memory** — claims that recorded outcomes have since overtaken.
- **Ineffective lessons** — lessons that scout recalls but that demonstrably
  don't change the plan.
- **Tool patterns** — regularities in operational history worth writing down.

A hypothesis is not a belief. It sits as a row in a sidecar table
(`dream_hypotheses`, migration v19) doing nothing until a validation pass
confirms or refutes it. Refuted hypotheses are kept and deduplicated against,
so the dreamer cannot resurrect an idea that already failed.

## What a dream step does

One step per snooze cycle, one bounded background-model call:

1. **Observe** — assemble a small, quoted, delimited evidence pack: new
   post-mortems and Candor events since the last cursors, one memory file
   sampled by rotation, recently-recalled lessons with their ages.
2. **Hypothesize** — ask the model for at most `dream_hypotheses_per_cycle`
   typed hypotheses, each required to cite evidence refs from the pack.
   Refs are pinned by content hash — if consolidation later moves or rewrites
   an entry, the hypothesis expires rather than guessing.
3. **Validate** (pending hypotheses, oldest first) — the check matches the kind:
   - *Tool patterns* are re-checked against Candor's numbers directly, no LLM.
   - *Contradictions / stale memory* get one LLM judge call over the
     re-resolved, content-hash-verified entries; any hedge refutes.
   - *Ineffective lessons* get the strongest test: a **counterfactual scout
     replay** of a past failed turn, with the original session excluded from
     search so scout can't "remember" the failure. Capped at
     `dream_validation_replays_per_day`.

Memory entries distilled from web content carry `@origin: external`
provenance, and dreaming discounts them as evidence — injected prose can't
fabricate the outcome records that validation checks against.

## The journal

Each day of dreaming narrates itself into a **Dream journal session** — a
day-keyed session that appears in the sidebar under its own "Dream" category
(purple dot, titles like "Dream Jul 31"). It records signal — hypotheses
raised, verdicts, report writes — not every heartbeat step. Journal sessions
are read-only in chat ("Pernix writes it while dreaming"), excluded from
search and distillation, and pruned after `dream_journal_retention_days`.

## The report

Every `dream_report_interval_days` (when there's material), the dream writes
`workspace/dreams/DREAM-<date>.md`: contradictions found, hypotheses raised,
refuted this period, open questions, store health notes. It lands in the
workspace, so the file explorer shows it with everything else. A high refute
rate early is the system working, not failing.

## Deep probes (RLM)

The per-cycle step samples one memory file at a time, so it can only find
intra-file contradictions. With `dream_rlm_probe` enabled (requires
`rlm_enabled`), the dream periodically runs an [RLM](rlm.md) probe over
**staged copies** of the whole memory corpus, hunting cross-file
contradictions — at most once per `dream_rlm_probe_interval_days`, with caps
sized from your observed completed RLM runs. The probe runs as a tracked
background task outside the snooze cycle, shows up in the Jobs tab
(Active while running, History when done) like any other run, and its
candidates go through the same dedup and filters as
cycle-generated hypotheses — no special write powers.

## What it deliberately doesn't do

- **No direct promotion — and the proposal queue is a veto window, not an
  approval gate.** Validated conclusions never reach scout or the live
  prompt from here; they route through the
  [Adaptive Layer](canary-and-adaptive.md) (when `adaptive_enabled`). A
  validated contradiction / stale-memory finding applies immediately on
  promotion: the proposal row is still minted for the audit trail
  (resolution `auto_applied`), but the correction itself is additive — a
  corrective note written beside the disputed entries, nothing edited or
  deleted — narrated in the dream journal, with at most one operator
  notification per day. Other dream proposals wait in the queue, where a
  pending row self-approves after `adaptive_auto_approve_after_hours`
  (default 24 h) unless you veto it first; every application is journaled
  and rollback-able. Only *validated* hypotheses promote at all — dream is
  the most speculative producer in the stack. With the adaptive layer off,
  the dream's entire observable output remains the journal, the report,
  and sidecar rows.
- **No permanent shelf space.** Promoted entries are retired again when
  their evidence stops holding — the originating hypothesis is gone or
  unpromoted, the cited Candor facts recovered above the degradation line,
  or the entry outlived its TTL (`core/dream/retire.py`). Minting without
  retiring silently wedges the per-kind entry cap.
- **No self-modification.** Skills, prompts, and code are untouched.
- **Strict write-permission rule.** The dream may write its own tables, files
  under `workspace/dreams/`, and (once promotion ships) memory entries marked
  `source="dream"` — and may delete only what it authored. Demoting a user- or
  distill-authored entry is proposal-only, applied by a human.
- **Kill switch is total.** `dream_enabled = false` removes the activity from
  the cycle entirely; the sidecar tables are safe to drop.

## Settings

See [configuration.md](../configuration.md#dream-introspection-add-on).
