---
name: gen-json-transform
generated: true
timeout: 600
tags: [sentinel, generated, holdout]
flaky: false
last_reviewed: 2026-09-04
---

GENERATED sentinel — `generate.py` draws a fresh customer set and record
list from a fresh random seed on every run, so both expected values move.
The gate validates values, not formatting, so any JSON writer works.

The aggregation trap from the static `json-transform` canary is guaranteed
rather than incidental: at least one customer appears only on non-shipped
records, so deriving the customer list from the shipped subset produces the
right sum and the wrong list.

`holdout` — never referenced in refine or dream prompts and never the target
of a proposal-derived edit. The run's seed is recorded in
`gate_results_json`; pass it to `generate(seed)` to reproduce a failure.
