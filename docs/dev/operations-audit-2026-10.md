# Pernix box operations audit — 2026-10-04

Reviewed deployment: `box.ventibean.com`, branch `next-3.2-testing`, current
commit `85824d6e`. This records the read-only findings at that baseline.
Follow-up implementation and deployment: [operations remediation](operations-remediation-2026-10.md).

## Scope and evidence limits

Requested window: September 4, 2026 19:32 UTC through October 4, 2026.
Session analysis used the consistent SQLite backup made October 4 at 19:29:43
UTC before deployment. Logs were copied through approximately 19:32 UTC;
selected live status checks followed. Log timestamps below are America/Chicago
(UTC−5 during this window); database timestamps are UTC.

The retained application logs begin **September 8 at 06:07**, so September
4–8 cannot be fully audited from logs. Docker logs cover only the new container.
The four retained application files contain 240,132 timestamped records, of
which 97,324 are HTTP access records. The database contains 12,255 messages in
the requested window across 456 sessions, including 80 ordinary user sessions.
There are 523 post-mortems; these are grades/attempts, not independent user tasks.
Analysis used aggregate queries and targeted inspection of failures, corrections,
tool results and recent session transcripts. Historical deletions and retention
mean these counts describe surviving records, not all activity ever performed.

Current code was cross-checked for every finding. No live provider experiments,
session prompts, settings changes, repairs or additional deployment were made.
One short synthetic cancellation reproduction ran locally. No full test suite
was needed for this read-only pass.

## Unresolved findings

### O1 — High: maintenance repeatedly times out, and timeout does not stop its worker

**Observed:** 78 cycles hit the 900-second backstop from October 2 at 22:36:32
through October 4 at 14:24:52. From the cycle beginning October 2 at 22:21:32,
the log records 80 completions: 78 `backstop`, two `yielded`, and zero `ran`.
That represents 19.5 hours inside timed-out cycle windows, not a measurement of
CPU time. The ordinary health endpoint still reported healthy.

Every timed-out cycle's last memory/maintenance progress was in the early memory
ladder. The consolidation watermark remained `2026-10-02T03:03:24Z`; the first
failing cycle began just after its 24-hour interval became due. This strongly
implicates consolidation, but the logs do not persist per-rung start/end timings,
so the exact production hot loop is **not proven**. Aggregate inventory found
705 memory Markdown files totaling approximately 104.5 MB; no corpus contents
were copied or profiled.

**Confirmed mechanism:** `core/snooze.py:385` cancels `cycle_task` on backstop
without changing the cancellation generation. `_is_cancelled()` therefore
continues returning false to an already-running thread. The same risk applies
to shutdown cancellation. `core/memory/sweeps.py:320` and `:326` run signature
building and clustering on asyncio's default executor. Clustering checks for
cancellation only in the outer file loop; `score_pair` can compare every pair
of entry fingerprints without a cancellation check (`core/memory/consolidate.py:170`).
Cancelling the awaiting coroutine does not stop those threads. A later cycle can
submit another scan, and the unfinished watermark causes repeated attempts.

A local reproduction shortened the backstop to 50 ms and substituted a harmless
worker that polls the real `_is_cancelled()`. Result: `outcome=backstop`, worker
still running, cancellation flag false. The reproduction then explicitly released
its worker. It used no production data or LLM calls.

**Impact:** later maintenance rungs, including splitting, retention, refinement
and skill proposal processing, are repeatedly starved. Abandoned CPU work may
also compete with API reads in the default executor; production executor
exhaustion was not measured. Today's earlier fixes did not change this mechanism.

**Recommended fix:** immutable per-cycle cancellation signals; cooperative
checks within costly inner loops; bounded background execution with at most one
scan in flight; resumable/budgeted scans; failure cooldown; persist rung timing,
last successful cycle and a degraded-maintenance health signal. Regression-test
that a timeout stops work and subsequent cycles can reach later rungs.

### O2 — Medium: memory splitting retries the same failing operation indefinitely

**Observed:** 1,078 split starts, 478 successful split log records and 1,233
parse-failure warnings in retained logs. Of those warnings, 1,005 contained an
empty response and 582 were second-attempt failures. These are attempt/log counts,
not an exact success-rate denominator: cancellation and malformed results can
produce other exits. September 21 and 22 each logged 72 starts, 144 parse failures
and no successful split. Failures continued through October 2 at 22:01:58.
Their subsequent absence coincides with O1 preventing the split rung from running.

**Current mechanism:** `core/memory/sweeps.py:934` always selects the largest
eligible file, asks for free-form JSON, and retries once with the same prompt and
2,000-token limit (`:1004`). Failure returns without a per-file retry watermark,
cooldown or alternative candidate. It cannot distinguish an empty completion,
truncated JSON or prose-only answer in scheduling decisions. A 150-entry cap
exists, but does not prevent this live failure pattern.

**Impact:** repeated provider work without maintenance progress; one failing
file monopolizes splitting. No dollar-loss estimate is supported by the records.

**Recommended fix:** record failure and finish reasons; enforce structured output
where supported; adapt batch/output limits; back off failing file revisions and
service other files. Do not merely increase the global timeout. Test repeated
empty/truncated outputs and verify both bounded retries and progress elsewhere.

### O3 — High: the grader can send factually false corrective instructions

**Observed:** in session `c0ef636348b2`, the October 4 01:38 UTC post-mortem and
01:38:37 notification said product ID `264623` was IVV, told the user not to
reuse it, and asserted the requested fund should have approximately 100 holdings.
The transcript instead shows an initial failed lookup using **239726**
(messages 70386–70387), followed by a different successful fetch using **264623**
whose CSV identified itself as the requested dividend-growth fund
(messages 70408–70409). The grader merged evidence from different requests.

Independent verification: the issuer identifies
[264623 as DGRO](https://www.ishares.com/us/products/264623/ishares-core-dividend-growth-etf)
and [239726 as IVV](https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf).
The DGRO page reports 390 holdings as of October 1. This confirms that the
grader's product-ID correction and approximately-100 assertion were wrong;
it does **not** establish that every calculation or claim in the original
answer was correct.

**Current mechanism:** the deferred-grade notification forwards `strategy`,
`missing` or `reasoning` as corrective advice (`sessions/hooks.py:718`). There is
no requirement that factual accusations map to the exact tool call/result that
supports them. Existing prompt instructions and grounding checks did not catch
this case. The error is present after September's grading fixes.

**Impact:** a successful recovery during a task is misgraded as failure, and
the user receives instructions to undo the correct part. Such grades also feed
later scout/review behavior. No automatic retry ran in this observed case.

**Recommended fix:** require tool-result/message references for factual failure
claims, preserve request/response identity, distinguish abandoned attempts from
the final artifact, and report unsupported contradictions as uncertainty. Add
this corrected-fetch sequence to deterministic evidence tests and grader
hold-outs; verify that the notification cannot promote an unsupported correction.

### O4 — Medium: hold-out accuracy hides ungradable cases

**Observed:** the latest persisted grader report has nine cases: seven correct,
one wrong and one JSON parsing failure. It publishes `accuracy=0.875`, `n=8`.
The trust panel labels this **Hold-out accuracy: 88%, 8 fixtures**, omitting the
failed ninth case from the headline. End-to-end success on the intended suite
was 7/9, or 77.8%.

**Current mechanism:** `core/reflect_holdout.py:182` records exceptions but
continues before incrementing `graded`; `:193` divides by graded cases only.
`static/js/components/modals/trust.js:99` presents that conditional accuracy
without completion coverage. This is a misleading completeness metric, not an
arithmetic error. The latest missing case is `verifier-blindness`, which is
particularly relevant to O3.

**Recommended fix:** publish total, attempted, graded, correct, failed and
ungradable counts; show conditional accuracy separately from whole-suite
success/completion. Preserve failure diagnostics and test partial/all-ungradable
runs. A skipped hard case must not improve the headline reliability score.

### O5 — Medium: completed detached jobs remain marked running for weeks

**Observed:** seven jobs started within the window remain `running` in SQLite.
Read-only checks on the box found durable exit sidecars for all seven: six exit
0 and one exit 124. None of their recorded PIDs exists in the new container.
They were started September 10–28. Four additional stale records predate the
window. The sidecars prove the seven jobs have terminated, independently of the
container restart or PID observations.

**Current mechanism:** detached-job reconciliation is lazy: `_refresh()` in
`core/tools/builtin/jobs_tool.py:338` runs when job tools poll or inspect the row;
there is no periodic/startup sweep. It also stamps `finished_at` with the time
of that eventual inspection (`:352`), overstating duration for old jobs.
These are detached shell jobs, distinct from the scheduled jobs in `/api/jobs`.

**Recommended fix:** bounded startup/periodic reconciliation of running jobs,
completion timestamp persisted with the exit sidecar, and explicit unknown
completion time for legacy records. Test completion without a later tool poll,
restart recovery and PID reuse without signalling unrelated processes.

## Historical issues excluded from the new list

- The 163 fallback-burn watcher `_bump()` exceptions ended before the October 2
  fix (`247c2a4`); do not reopen that resolved defect.
- Adaptive tripwire warnings and much of the canary churn concern behavior
  removed October 2. The current four-task suite is a different population;
  a month-wide canary pass rate would obscure that change.
- Old `browse_web` approval failures occurred around the known box launch-flag
  mismatch. The current launch preserves `--dangerous`.
- The localhost VAPID-contact warning was historical: live configuration now
  uses `https://pernix.cc`.
- All 74 recorded provider transport errors were connection failures; the last
  was September 17. They are historical infrastructure incidents, not evidence
  of a current provider regression.
- The newest one-round scout has four observed primary retries and one backup
  escalation across the small post-prune sample. This is the already-documented
  scout watch item, not enough evidence to claim a broad new regression.
- All 57 surviving scheduled-run rows in the requested window say completed;
  the seven RLM runs comprise five completed and two iteration-cap outcomes.
  These state labels alone do not prove substantive task success.

Ordinary sessions still show behavioral misses (omitted requested links and
delayed use of explicitly requested search). They merit replay cases, but the
stored grader verdicts are not unquestioned ground truth, as O3 demonstrates.

## Observability and next steps

Address O1 first, then O2 and O3; O4 is a small, independently testable fix, and
O5 should accompany lifecycle maintenance work. Add per-rung timing before
claiming the exact production cause of the consolidation stall. Increasing the
900-second limit alone would conceal the defect and lengthen each failed cycle.

The log policy (`run.py:150`, `api/app.py:40`) keeps three 10 MB rotations plus
the current file. It does not guarantee a 30-day audit trail. If monthly review
is desired, retain compressed time-based application logs for at least 35 days
and separate/filter routine access polling. Do not infer a clean month from the
missing September 4–8 logs or the absence of errors after an activity stopped.

Local analysis artifacts: `/tmp/pernix-ops-audit/findings-evidence.json`,
`/tmp/pernix-ops-audit/reports/summary.txt`, and the bounded synthetic
`/tmp/pernix-ops-audit/repro.py`. Raw copied logs/session snapshot remain in that
temporary directory; this repository report contains only selected evidence.

Automatic approval review rejected exporting the separate memory corpus because
it could expose private content beyond the logs-and-sessions request. That export
was not performed. Exact corpus-level performance profiling remains unverified;
the report does not depend on such profiling to establish the cancellation bug.
