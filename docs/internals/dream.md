# Dream — Idle-Time Introspection

> **Notifications.** Every "notification" this page mentions goes through `core/notices.py` and lands in the tier its category is registered under — most self-maintenance receipts are *log* tier (the bell's Activity tab, never a badge); only things that need you interrupt. See [guides/notifications.md](../guides/notifications.md) for the full list.

The Dream subsystem (`core/dream/`) gives Pernix an idle-time faculty that
examines its own memory and post-mortems; generates typed
hypotheses about itself; and then **tries to falsify them** against recorded
outcomes. Nothing a dream produces influences live behavior until it has been
validated — and the only live effect of a validated conclusion is an
additive memory correction (see [Memory corrections](#memory-corrections)).

Off by default. Enable in Settings → Autonomy & idle work → Dream
(Introspection); all settings apply hot. Runs as the final activity of the
[Snooze ladder](reflect-and-snooze.md),
so it only ever spends idle time.

## The idea

Memory today is a Polaroid — written once, never falsified. An entry that says
"X always fails" keeps saying it long after X was fixed. Post-mortems record
outcomes continuously; dreaming closes the loop by asking, offline and unhurried, whether the beliefs still square with the
evidence:

- **Contradictions** between memory entries.
- **Stale memory** — claims that recorded outcomes have since overtaken.
- **Ineffective lessons** — lessons that scout recalls but that demonstrably
  don't change the plan.
- **Tool patterns** — no longer generated. They were evidenced and
  re-checked by Candor, which was retired in 3.2; any still-pending
  `tool_pattern` row expires on its next validation pass
  (`method: candor_retired`). The kind stays valid for historical rows.

A hypothesis is not a belief. It sits as a row in a sidecar table
(`dream_hypotheses`, migration v19) doing nothing until a validation pass
confirms or refutes it. Refuted hypotheses are kept and deduplicated against,
so the dreamer cannot resurrect an idea that already failed.

## What a dream step does

One step per snooze cycle, one bounded background-model call:

1. **Observe** — assemble a small, quoted, delimited evidence pack: new
   post-mortems since the last cursor, one memory file
   sampled by rotation, recently-recalled lessons with their ages.
2. **Hypothesize** — ask the model for at most `dream_hypotheses_per_cycle`
   typed hypotheses, each required to cite evidence refs from the pack.
   Refs are pinned by content hash — if consolidation later moves or rewrites
   an entry, the hypothesis expires rather than guessing.

   A second, targeted mint path bypasses the model call: Snooze watermarks
   every enabled skill's `SKILL.md` + scripts with a sha256 hash
   (`snooze_state['skill_content_hash:{name}']`), and when one changes — a
   proposal applied in the Skills tab, an in-session agent edit, or a human edit on disk
   — memory entries that mention the skill are cited directly into
   `memory_stale` hypotheses (hash-guarded refs, capped at 6 per skill,
   deduped against what's already pending, one changed skill per cycle), so
   claims like "the script lacks a CPU flag" get re-judged by the validator
   below instead of contradicting the now-fixed skill for months.
3. **Validate** (pending hypotheses, oldest first) — the check matches the kind:
   - *Tool patterns* (historical rows only) expire without a check.
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
background task outside the snooze cycle, shows up in the Automation → Jobs
tab (Active while running, History when done) like any other run, and its
candidates go through the same dedup and filters as
cycle-generated hypotheses — no special write powers.

## What it deliberately doesn't do

- **No prompt or scout influence.** Validated conclusions never reach scout
  or the live prompt. What promotion does depends on the kind:
  - `contradiction` / `memory_stale` with cited memory files: the
    correction is written at once (see [Memory corrections](#memory-corrections)),
    narrated in the dream journal, with at most one log-tier notification per
    day. `promoted_ref` is `correction:<files>`.
  - The same finding without a citable file: `reported:no-effector`.
  - The same files corrected for the same kind within the last 7 days:
    `reported:duplicate-evidence`.
  - `tool_pattern` / `lesson_ineffective`: `reported:report-only`. Until 3.2
    these minted adaptive routing hints and policies; the adaptive layer is
    retired, so the finding reaches the dream report and nothing else.

  Only *validated* hypotheses promote at all, and every outcome above is
  terminal, so a validated row never sits waiting (a row that does for
  `_STALL_DAYS` raises `dream.promotion_stalled`).
- **No self-modification.** Skills, prompts, and code are untouched.
- **Strict write-permission rule.** The dream may write its own tables, files
  under `workspace/dreams/`, and corrective memory entries marked
  `source="dream_fix"` — and may delete only what it authored. Demoting a user- or
  distill-authored entry is proposal-only, applied by a human.
- **Kill switch is total.** `dream_enabled = false` removes the activity from
  the cycle entirely; the sidecar tables are safe to drop.

## Memory corrections

`apply_memory_correction()` (`core/memory/ingest.py`) is **additive and
non-destructive**. For each cited memory file (capped at 3, drawn from the
hypothesis's pinned evidence) it appends one new entry —
`entry_type="note"`, `weight="high"`, `source="dream_fix"`, tagged
`correction,<kind>` — prefixed `CONTRADICTION RESOLVED` or `STALE-INFO
CORRECTION` with the provenance `(auto-applied on validation — dream finding,
dream:<hypothesis id>)`, and ending with an instruction to treat the note as
overriding conflicting older entries in that file. **The disputed entries are
left in place.** Nothing is edited or deleted, so the correction is itself
reviewable and the original record survives. Undo one by deleting the entry
tagged `dream:<id>` in that file; `scripts/dream_fix_audit.py` traces those
tags back to their hypotheses.

Before 3.2 these corrections were minted as adaptive proposals and applied
through the proposal machinery; that indirection went with the adaptive layer.

## Settings

See [configuration.md](../configuration.md#dream-introspection-add-on).
