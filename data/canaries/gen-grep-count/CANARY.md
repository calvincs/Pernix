---
name: gen-grep-count
generated: true
timeout: 300
tags: [sentinel, generated, holdout]
flaky: false
last_reviewed: 2026-09-04
---

GENERATED sentinel — `generate.py` builds 2-4 log files with a fresh line
mix from a fresh random seed on every run, so the expected ERROR count moves
too. Both traps from the static `grep-count` canary are guaranteed present
in every fixture: a lowercase `error` line that must NOT count
(case-sensitive) and an `ERRORS-SUMMARY` line that MUST count (substring
semantics).

The count is derived from the rendered log lines rather than tallied as they
are planned, so the gate can only disagree with the fixture if the fixture
itself is wrong.

`holdout` — never referenced in refine or dream prompts and never the target
of a proposal-derived edit, so nothing the system learns can be trained
against this task. The run's seed is recorded in `gate_results_json`; pass
it to `generate(seed)` to reproduce a failure exactly.
