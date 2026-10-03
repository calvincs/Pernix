---
name: link-digest
generated: true
timeout: 600
tags: [generated, holdout, web]
serve: [article.html]
tools: [http_get]
flaky: false
last_reviewed: 2026-10-02
---

GENERATED — "read this link and summarise it", the most common real
request, with no internet involved. `generate.py` builds an article with a
random title, author and key figure (plus a revised decoy figure) from a
fresh seed on every run. The runner publishes it under
`data/workspace/.canary-serve/<run-token>/` for the length of the run
(`serve:`), so the agent has to fetch it from Pernix's own `/workspace`
route with `http_get` (`tools:`); the article is not in the task workspace.
In network mode the URL is https and `http_get` verifies it against
Pernix's own certificate.

Gates: the title, the author, the key figure and the fetched URL all appear
in `summary.md`. The run's seed is recorded in `gate_results_json`; pass it
to `generate(seed)` to reproduce a failure.
