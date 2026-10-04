# System audit remediation — 2026-10-04

Baseline: `next-3.2-testing`, commit `11eba77`. The audit collected 4,211 tests;
4,197 passed and 14 failed, with 79.23% Python line coverage. Thirteen failures
were settings saves crossing filesystems; the remaining test incorrectly
required GNU timeout's killed workload to produce exit code 143 on every host.

## Plan and decisions

- [x] Journal each skill application's exact backup and before/after revisions.
  Refuse legacy timestamp guesses and rollback over subsequent edits.
- [x] Serialize skill file mutations; claim pending proposals atomically and
  return a conflict when approval/rejection races a claimed application.
- [x] Require successful backups before apply and rollback. Preserve a durable
  `applying` record if finalization fails, allowing explicit recovery.
- [x] Check strict idle state, including goal continuations, before each apply.
- [x] Remember custom registry roots. Cache validation against script,
  requirements and workspace-package revisions; publish complete scan maps.
- [x] Move the skills list scan off the event loop and stop writing temporary
  Python bytecode during syntax checks.
- [x] Isolate malformed cron entries and atomically replace persisted schedules.
- [x] Bound RLM answer preview reads to 200,000 bytes.
- [x] Create settings-save temporary files on the destination filesystem.
- [x] Page pending proposals oldest-first without the 500/1,000-row blind spots;
  use a database count when review requires no per-proposal validation.
- [x] Remove the four verified redundant tests, isolate scheduler tests from
  scout providers, and restore mutable settings after every test.
- [x] Tighten permissive API smoke assertions and assert the job-kill sidecar's
  real exit code while retaining the process-termination checks.
- [x] Remove unused merge/holdout/token helpers and unused imports/variables.
  Retain `content_revision`: it is now used by proposal recovery. Enable unused
  name linting in production with explicit aliases for compatibility exports.
- [x] Finish regression checks, full coverage run, lint and final diff review.

## Recovery contract

Migration 44 adds `backup_name`, `before_revision`, and `after_revision` to
skill proposals. An application records these and claims status `applying`
before replacing the file. A rejected proposal cannot be claimed. Once claimed,
a later reject/approve request gets HTTP 409 rather than a false success.

A rollback verifies both the exact backup and the current skill revision.
Newer edits must be undone first. Historic proposals without this journal need
manual recovery; no migration can infer which timestamped backup belonged to
which old application. File locks coordinate writers within this server,
including the editor and file tools; external shell/editor processes remain
outside that lock and are checked by revision before replacement.
An external write between that check and replacement remains a race; these locks
coordinate this server's writers, not separate processes.

## Validation

New regressions live in
`tests/regressions/test_2026_10_04_system_audit.py`. They exercise races with real
SQLite and threads, real registry reloads, backup failure, interrupted apply,
active goals, more than 1,000 proposals, event-loop responsiveness, cache
invalidation, malformed schedules, failed atomic replacement and bounded reads.

Coverage remains a Python line measure. Shared executed lines alone do not
justify deleting distinct behavioral regression tests. No broad orchestrator
rewrite or speculative mass test deletion is part of this remediation.

### Completed results

- Full suite: **4,227 passed in 112.63 seconds**, using four workers and
  per-test coverage contexts. Baseline: 4,197 passed / 14 failed in 197.26 seconds.
  The observed runtime reduction is about 43%; this is a local comparison,
  not a production throughput benchmark.
- Python line coverage: **79.18%** (26,691 / 33,708 statements), versus 79.23%
  at baseline. The 63% gate passes. The four removed tests had redundant
  assertions/coverage; this small aggregate percentage change spans the changed
  production code and test isolation and is not a claim of branch equivalence.
- Added 20 regression cases, including API recovery and JavaScript rollback
  action/error behavior. The final focused set passed 266 tests. No live-browser
  visual check was performed.
- Black, Ruff, Flake8 and `git diff --check` all passed.
- Local allocation probe: a 16 MiB answer preview peaked at about 0.4 MiB
  rather than 32 MiB. A repeat scan of 40 skills took about 15 ms versus the
  baseline's approximately 100 ms, without generating temporary `.pyc` files.
- Removed redundant cases: `test_models_list` and `test_settings_get` in
  `test_api_extended.py`, `test_prune_stale_empty_store` in
  `test_snooze_activities.py`, and
  `test_a_new_bare_name_still_defaults_into_the_space_home` in the space-prefix
  regression file. Distinct behavioral and concurrency tests remain.

Validation artifacts for this run are in `/tmp/pernix-final-full.log`,
`/tmp/pernix-final.coverage`, and `/tmp/pernix-final-coverage.json`.
Migration 44 runs through the normal application migration path. Older applied
proposals intentionally require manual recovery because their exact backup
association cannot be reconstructed safely. No commit or deployment was made.
