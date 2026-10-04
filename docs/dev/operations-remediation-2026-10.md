# Operations remediation — October 2026

Baseline: `85824d6e`, `next-3.2-testing`. Scope is O1–O5 in
`operations-audit-2026-10.md`, plus the audit-trail retention gap.

## Implementation plan

- [x] O1: make cycle cancellation permanent for its workers; bound and isolate
  consolidation work; let slow rungs yield without starving the remaining ladder;
  expose rung timing, failures and last successful cycle.
- [x] O2: persist revision-aware split backoff, reduce failed batch sizes,
  validate grouping before mutation, and let other files progress.
- [x] O3: preserve tool-call/result provenance in grading evidence, require
  verifiable support for corrective factual claims, and withhold unsupported
  corrections from notifications and future retry guidance. Add a recovered-fetch
  hold-out and behavioral regression coverage.
- [x] O4: report intended/attempted/graded/correct/ungradable counts and display
  whole-suite success separately from conditional accuracy.
- [x] O5: periodically reconcile detached jobs, record durable completion times,
  and preserve unknown times for legacy records.
- [x] Keep at least 35 daily application-log archives, compress rotations, and
  separate routine access logs without deleting existing history.
- [x] Run focused regressions, full suite with coverage, formatting and lint.
- [ ] Commit and push the branch; back up the box, deploy, verify the code,
  lifecycle repairs, service health and a completed maintenance cycle.

## Constraints

Preserve the existing deployment settings and user data. Do not copy the private
memory corpus; use deterministic local fixtures and bounded live maintenance
verification. Existing unrelated `atra_findings*` files remain uncommitted.
Historical behavior already removed is not reintroduced. No automatic model
switch or speculative increase in the global maintenance timeout is planned.

## Implementation details

- Consolidation scans use a persistent filename-pair cursor and at most 24
  fingerprint samples per file for candidate discovery. Original entries still
  undergo the existing merge validation. Each activity records duration and
  failure; partial cycles are visible as degraded maintenance in health stats.
- Split failures retain a revision-aware backoff, including interrupted calls.
  Invalid output cannot trigger a move, and omitted entries remain in place.
- Factual corrections require paired tool-call IDs and matching source quotes
  for the same subject, with a later verification result. Unsupported corrections
  are withheld from notifications and retry lessons; hold-outs count withheld
  grades as ungradable. These checks establish attribution, not arbitrary truth.
- Job reconciliation uses conditional updates to avoid overwriting a concurrent
  kill. New wrappers atomically publish exit status and a completion timestamp.
- Logging uses UTC daily rotations with 35 compressed archives per stream;
  existing numbered logs are preserved.

## Deployment record

Pre-deployment backup: `data/backups/pre-operations-remediation-20261004T201513Z`
on `box.ventibean.com`. SQLite integrity check returned `ok`; no processing or
queued sessions were present. Eleven jobs still had legacy running states.
Final validation and live deployment results are recorded below when complete.

Final local validation: **4,247 tests passed in 112.92 seconds**, with **79.28%
coverage** (63% required). Black, Ruff, Flake8 and `git diff --check` passed.
The focused maintenance/reflect/consolidation/job suite also passed (276 tests).
