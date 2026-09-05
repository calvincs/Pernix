---
name: gen-file-create
generated: true
timeout: 300
tags: [sentinel, generated, holdout]
flaky: false
last_reviewed: 2026-09-04
---

GENERATED sentinel — the fixture is built by `generate.py` from a fresh
random seed on every run, so the target filename, the exact line, and the
near-miss decoy in `reference/sample.txt` are all different each time. There
is no answer in this file to memorise, which is the whole point: the static
`file-create` canary has been green for months and can no longer tell
"followed the instruction" from "has seen this before".

`holdout` — never referenced in refine or dream prompts, and never the
target of a proposal-derived edit. If this fails, something fundamental
broke (tool dispatch, workspace override, or instruction following); treat
a regression here as a pipeline problem, not a model problem.

The run's seed is recorded in `gate_results_json` — pass it to
`generate(seed)` by hand to reproduce a failure exactly.
