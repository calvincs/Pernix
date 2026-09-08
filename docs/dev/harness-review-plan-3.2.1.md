# Harness review 3.2.1 remediation plan (2026-09-08)

Source: `atra_findings_3.2.1.md`, a coding/long-research harness audit filing
20 findings (H01-H20) against baseline `f16007f`, which is exactly the tip
this plan starts from. The report states its findings are "regression
specifications ... not claimed production reproductions" and asks a fixing
agent to reproduce each one first.

**Every one of the 20 was reproduced against HEAD before this plan was
written.** All 20 are confirmed. Nine turned out to be broader than filed.
None were refuted. H04 is the only one whose *headline* is a design limit
rather than a defect, and reproducing it surfaced a real defect underneath.

## Verified status

| ID | Filed | Verified | Note |
| --- | --- | --- | --- |
| H01 | P1 | Confirmed, broader | A round-exhausted worker also reaches its parent with no INCOMPLETE header, and reflect's ceiling-loop guard can never fire |
| H02 | P1 | Confirmed | 3 cycles grant headroom `[1799s, 0s, 0s]`; one prior `spawn_worker` makes even the first goal extension a no-op |
| H03 | P1 | Confirmed, worse | ~70% of every compaction slice is dropped un-summarized in steady state; `tool_calls` never serialized; ratio gate 4.8x lenient |
| H04 | P1 | Partial + new defect | Cannot-compact is a deliberate design limit and evidence stays id-addressable; but on small `max_output` configs the long first turn dies with `compaction_failed` |
| H05 | P1 | Confirmed, broader | Also fires same-round: `file_write(fix)` + `bash(rerun)` in one response serves the stale pre-fix failure |
| H06 | P1 | Confirmed | "not found" matches any app message; reuse drops the `broken` flag so a broken gate flips to failing on attempt 3 |
| H07 | P1 | Confirmed | Six tools, three roots. A gate false-passes on work the foreground correctly fails |
| H08 | P1 | Confirmed, worse | An unscoped legacy `summary.md` can be served as an unrelated worker's result |
| H09 | P1 | Confirmed, worse | Hydrated workers also escape the goal budget check shipped this morning in `b257532` |
| H10 | P1 | Confirmed | Plus a fourth path: `message_worker` re-prompt leaves the prior artifact in place |
| H11 | P2 | Confirmed | The downgrade marker is clipped out of every view the parent sees |
| H12 | P1 | Confirmed | Debit is durable, dispatch is memory-only; no boot path sweeps orphans |
| H13 | P1 | Confirmed, worse | Result mode is exactly `0600` every time, dropping group/other and setgid, not just `+x`; 60/60 concurrent trials lost an edit |
| H14 | P1 | Confirmed | All three fuzzy strategies; `multiedit` reports `Applied 1/1 edits` |
| H15 | P1 | Confirmed, worse | The artifact header states the clipped size as the source total |
| H16 | P2 | Confirmed | All five sub-claims; the file-read dead end reports a 3001-line file as having 1 line |
| H17 | P2 | Confirmed | 15s registration against a 60s internal deadline; inner thread runs 5s past the reported timeout |
| H18 | P1/P2 | Confirmed, worse | Policy bypass in both modes; `job_kill` kills nothing at all, and no TERM resistance is needed |
| H19 | P2 | Confirmed | Past ~40k chars every distillation ships byte-identical input forever |
| H20 | P2 | Confirmed | Paused/awaiting-input/uncollected all pruned; the parent is told the worker "produced no output" |

## Scope decision

The report's "Recommended Long-Task Architecture" section is explicitly
proposed design, not defects, and it says so. This plan implements the 20
contract fixes and does **not** build a general task/planner layer. Where a
finding's full recommendation is architectural, the plan takes the smallest
change that makes the observed behaviour honest, and records the rest under
follow-ups. That is the report's own instruction: "Prefer the smallest
correct change and existing mechanisms."

## Fix designs

One commit per finding, each with a dated regression test in
`tests/regressions/test_2026-09-08_<slug>.py` whose docstring tells the
failure story. House style is `git show d620c23`.

### Group 1 - tool truth

**H05** `fix(tools): report what a shell command actually did`. bash returns
structured metadata through the executor's existing `(str, dict)` channel:
`exit_code`, `timed_out`, `cwd`, truncation/completeness. `_execute_single`
derives `was_error` from that metadata rather than string prefixes, keeping
prefix classification only as the fallback for tools that return a bare
string. Exit status is always displayed. `bash` registers
`idempotent=False` so cross-round dedup never serves a cached shell result.
Keep the distinction between command failure and infrastructure failure for
tool-health metrics. A nonzero exit stays a *fact*, not an automatic retry:
`grep` exits 1 on no-match and a reproduction test is meant to fail.
`tests/regressions/test_2026-09-03_dedup_cache_answered_polls_and_reruns.py`
must keep passing.

**H06** `fix(gates): stop reading a real failure as a gate that never ran`.
`_looks_unrunnable` keys on launch evidence, not arbitrary output: exit 127
with a shell-shaped `command not found` naming the gate's own command, exit
126 with a permission message about the command path, and cwd errors from
the launcher. Drop the bare `not found` substring. Keep three states
(`passed` / `failed` / `unavailable`) and carry the full state through the
reuse path at `gates.py:232-244`, which today drops `broken`. A required
unavailable check stays explicitly unverified and never reads as success.

### Group 2 - path contract

**H07** `fix(paths): give every tool the same relative-path root`. One
documented default: the session's `workspace_home()` when it has one, else
`workspace()`. Apply it to grep, glob, `job_start`'s `job_cwd`, and gate
cwd resolution, which today all use `workspace()` unconditionally. Keep the
containment root separate from the default cwd so this stays a correctness
fix and does not become a new cross-space restriction. Preserve, explicitly
and with tests: the prefer-existing/global fallback at `paths.py:307-360`,
the doubled-prefix guard from `ef1d4c9`, the `parent_exists` rule from
`7e1be5f`, and `build_shell_env` keeping venv and PATH on the global
workspace while HOME follows the space. Results display the effective root.
Jobs pass the root explicitly into the detached process rather than relying
on ambient thread state.

### Group 3 - file mutation

**H13** `fix(tools): keep a file's mode and stop silent lost updates`. One
shared atomic-write primitive that stats the target and `fchmod`s the temp
file to the existing mode before `os.replace`, keeping the current
fsync-before-replace. New files keep today's safe default. Do not follow
symlinks or copy ownership. Guard the read-transform-replace interval with a
canonical-target lock held across the whole edit, so two cooperating editors
serialize instead of both reporting success. An in-process lock cannot
coordinate an external shell edit; that residual race is documented, not
claimed fixed.

**H14** `fix(tools): stop reporting a partial fuzzy replace as done`.
`_apply_edit` accumulates non-overlapping replacements for the fuzzy
strategies when `replace_all=True` and reports an explicit replacement
count; where the semantics cannot be established safely it refuses rather
than editing one occurrence and returning success. Block-anchor stays
single-match by definition and says so. `multiedit` stops printing
`Applied 1/1 edits` for a partial application. Also surface the mixed case
where an exact match exists alongside whitespace variants.

**H18** `fix(jobs): apply shell policy at launch and actually kill the job`.
`job_start` runs the same `_check_command_security` / strict allowlist
admission as foreground bash, honouring the configured mode and session
permissions. `job_kill` targets the workload's real process group rather
than the leader pid: record the group `timeout` creates and escalate on
group emptiness the way foreground `_kill_process_tree` does, and report
cleanup uncertainty when descendants cannot be proven stopped. Detached
jobs still deliberately survive ordinary turn cancellation. The existing
regression at `test_2026-08-25_long_compute_died_inside_blocking_bash_calls.py:53`
passes only because of a timing window and must be strengthened, not
preserved as-is.

### Group 4 - bounded execution

**H01** `fix(agent): renew the round budget before the last round, not after`.
Track total rounds consumed separately from the current window's remaining
rounds and continuations granted. Decide renewal *before* entering the
tools-disabled terminal round. Only force synthesis when no authorized
renewal remains, and carry a typed allowance-exhaustion reason
(`round_ceiling`) through the final prose response so reflect's ceiling
guard and the worker INCOMPLETE header both see it. Absence of tool calls
never by itself means the task finished. Fix the prompt text that always
says no further continuations follow. Renewal respects cancellation, pending
user direction, goal ceilings and a genuine no-progress stop, and an empty
model response never buys a new allowance.

**H02** `fix(llm): make a renewed phase budget grant real headroom`. Audit
the four callers by intent. Keep base-relative `extend_session_budget` where
an idempotent total cap is genuinely wanted. Use the existing clock-relative
`ensure_session_budget(session_id, min_remaining_seconds)` for a fresh
bounded phase window, which is what the goal continuation and round renewal
want. Headroom comes from the task's remaining authorized allowance
including workers; repeated ensures are not an unlimited bypass. Preserve
`0`/unlimited semantics and monotonic time. Also handle the narrower gap
found while reproducing: a continuation after `round_ceiling` or `complete`
gets no extension at all today, and a continuation that dies on its first
acquire is recorded as `scout_error`, so the goal stalls at `active` with a
burned allowance.

**H12** `fix(sessions): make a continuation survive the crash that debits it`.
A small SQLite continuation outbox at schema version 37: atomically check
status and allowance, allocate an ordinal, and insert the pending
continuation in one transaction; dispatch under a durable claim and settle
the result; recover abandoned claims at boot, which today runs no orphan
sweep at all. Durable dispatch is not exactly-once side effects: recovery
re-reads receipts and never blindly replays a shell command or file write.
Re-check user pause/cancel intent and queued user direction before
dispatching a recovered continuation.

### Group 5 - evidence coverage

**H03** `fix(compaction): never advance the marker past what was summarized`.
Serialization returns explicit coverage: which message ids actually reached
the summarizer. The boundary advances only across covered rows. Assistant
`tool_calls` are serialized as compact tool identity plus relevant arguments
and status, since paths and commands often exist only there. Clipping a body
is allowed but records the omission and keeps a retrievable pointer. The
compression-ratio gate measures the input actually supplied, not the whole
slice. Summary failure still must not advance the marker.

**H04** `fix(context): stop killing a long turn compaction cannot help`. The
verified defect, not the architecture: when compaction structurally cannot
run (a single live turn with nothing older to summarize) the turn must fall
through to the compiler's trim path, which works and already emits an
id-addressable notice, instead of terminating with `compaction_failed`.
Bound the trim notice's own growth so it cannot become the next context
consumer, and give a dropped plain assistant row the same 500-character
verbatim preview a dropped user row gets. A rolling intra-turn checkpoint is
a design item, recorded as a follow-up, and depends on H03 landing first.

**H19** `fix(memory): distill the whole session, not its first 40k characters`.
A durable covered-message watermark plus bounded chunks, so later turns
distill new material instead of re-sending a frozen prefix. Coverage commits
only after extraction and storage succeed. Corrections and final outcomes
get explicit treatment with source message references, and a later claim
supersedes or qualifies an earlier one rather than silently erasing it.
Avoid trading oldest-prefix bias for newest-tail bias.

### Group 6 - worker result integrity

**H08** `fix(workers): make a worker's report findable where it was written`.
Persist an authoritative report artifact reference per worker run at spawn
and revival, and use that one reference for the charter text, finalization,
retrieval and cleanup, so a space worker's report is not missed and
replaced by a fabricated global fallback. Never adopt a legacy global
`summary.md` without provenance that it belongs to the requested worker.
Return the artifact handle when previewing a capped report, and give
`get_worker_transcript` message-id pagination with tail selection so a long
final report is reachable. Version a superseded artifact rather than
deleting it.

**H09** `fix(sessions): rebuild what a parent knows about its workers`.
Reconstruct parent-child relationships from durable `parent_session_id`
rows when hydrating or when building the resume manifest, so a restarted
parent is not told it has zero workers while being ordered to collect them.
Query durable terminal metadata even when an incomplete in-memory worker
object exists. Restore inherited `active_goal_id` so a revived worker keeps
both attribution and the budget check from `b257532`. Cancellation authority
from `d69c641` stays intact: a recovered relationship must not trigger an
unwanted resume.

**H10** `fix(workers): stop an old verdict certifying new output`. Bind a
result and its verification to a durable run boundary. Reject a verdict that
precedes the current run when serving a result, and apply the same rule in
finalization, the parent resume summary and direct retrieval so they cannot
disagree. Trust state comes from records, never from a Markdown heading a
worker can author itself. Interrupted and unknown-verification states stay
visible. Also cover the `message_worker` re-prompt path, where the prior
artifact is left in place today.

**H11** `fix(reflect): keep "could not verify" out of the pass column`.
Separate retry disposition from verification state. A low-confidence
non-pass keeps `verdict` usable for control flow but carries an explicit
`verification=unknown` that survives into worker results, finalization and
the resume manifest, instead of a marker appended to reasoning that every
consumer clips away. Do not fix this by forcing low-confidence retries.

### Group 7 - acquisition, search, retention

**H15** `fix(tools): keep the evidence, not just its preview`. Separate
acquisition from presentation: stream raw captured data to a bounded durable
artifact before collapse and truncation, so the persisted artifact is raw
evidence rather than the readability transform. Return completeness metadata
(captured size, total when known, truncation reason, artifact handle) and
never state a clipped size as the source total. When acquisition
deliberately stops, say `source_complete=false`. Caps stay; honesty about
them is what changes.

**H16** `fix(tools): tell the truth about what a search returned`. Expose
scope, caps, partial/error status and accurate returned/omitted counts.
Distinguish a ripgrep error (exit 2) from an empty result rather than
reporting "No matches found." Fix glob's omitted-count arithmetic, which
today invents omissions below 300 and triples them above. Make the
truncation cursor exact so a cut line's remainder is reachable. Stop
counting synthetic markers as source lines, and remove the file-read dead
end where an oversized first line yields zero source lines, a total of 1,
and advice that loops back to itself.

**H17** `fix(web): align the fetch timeout with the fetch's own deadline`.
Registration, per-operation timeouts and the total acquisition deadline
agree, with each operation bounded by the remaining total allowance.
Propagate a cooperative deadline into the synchronous acquisition so a
cancelled dispatch does not leave a worker thread running past the reported
timeout. Do not simply raise the outer number while leaving unbounded inner
work.

**H20** `fix(retention): do not prune a worker whose result nobody has read`.
Protect non-terminal, paused, awaiting-input and live-task-referenced
workers, not only those watched by a parent currently in `awaiting_workers`.
Define result consumption or explicit abandonment before pruning the sole
record, preserve a result manifest durably before deletion, and make archive
failure prevent destructive pruning. Stop reporting a retention deletion to
the parent as "produced no output ... consider retrying". The existing
regression at `test_2026-08-21_pruners_only_saw_the_newest_500_sessions.py:48`
pins today's age-only behaviour and must be updated, not merely added to.

## Execution

Eight Opus agents, each in its own worktree branched from this plan's
commit, grouped so overlapping files are touched in different functions:

| Stream | Findings | Primary files |
| --- | --- | --- |
| W1 | H05, H06 | core_tools.py (bash), executor.py (classification), agent.py (dedup), gates.py (classification) |
| W2 | H07 | paths.py, grep_tool.py, glob_tool.py, jobs_tool.py (cwd), gates.py (cwd) |
| W3 | H13, H14 | file_edit.py, core_tools.py (file_write) |
| W4 | H18 | jobs_tool.py (policy, kill) |
| W5 | H01, H02, H12 | agent.py (round loop), semaphore.py, manager.py (continuation), db |
| W6 | H03, H04, H19 | compaction.py, compiler.py (notice), distill.py |
| W7 | H08, H09, H10, H11 | orchestration, manager.py (worker), reflect.py, state.py |
| W8 | H15, H16, H17, H20 | core_tools.py (capture, file_read), truncation.py, web, retention.py, db/models.py |

Merge order: W1, W2, W3, W4, W6, W8, W5, W7. Remove worktrees before
`./check.sh` (the compaction scanner walks worktree copies). Then push.

## Deploy

Per `pernix-box-deploy`: check for running turns, `git pull &&
docker compose up -d --build`, verify `--dangerous` survived, curl health,
grep the container for one new line per fix, and run live assertions inside
the container.

## Outcome (2026-09-08, same day)

All 20 fixes landed on `next-3.2-testing`, one commit each, plus one merge
reconciliation commit. Eight Opus agents implemented them in parallel
worktrees. Gate green at 4,055 tests; deployed to the box (build
c9a3a1a8de33); 19 live assertions pass inside the container and a real
tool-using turn completes.

**The merge was the risky part, and one conflict mattered on its own.**
Three streams each claimed schema version 37 and a fourth took 38. The
migration runner skips any version at or below the database's current one,
so a MIGRATIONS list that is not ascending silently drops an entry on every
existing database instead of failing loudly. Renumbered to 37-40 in list
order and verified on the live box: `schema_version = 40` with all four
columns and both new tables present on a real v36 database, not a fresh one.
The continuation-outbox test now pins that ordering invariant instead of its
own absolute number.

A second conflict was worth the care it got: W2 and W8 had independently
rewritten the same two search tools, and both had introduced a variable
named `scope` with different meanings. Git merged them cleanly while
silently discarding the path fix. It would have compiled and quietly dropped
a finding.

The rest was signature drift between streams: bash returns text plus
metadata now, the gate runner gained a base path, and worker trust rewrote a
tuple the round-renewal test pinned by source text. Nine tests needed
mechanical updates; two of them pinned brittle things and were rewritten to
pin the real invariant instead.

Things the implementation found that the audit did not:
- **H05 fires same-round.** `gate.admit()` dedups the whole round's batch
  before any of it executes, so `file_write(fix)` plus `bash(rerun)` in one
  response is served the stale pre-fix failure. The documented mitigation
  only covers an edit in a strictly earlier round.
- **H18 needs no termination-resistant process.** GNU `timeout` puts itself
  in its own process group, so an ordinary job survives its own kill, the
  exit sidecar is never written, and the concurrency cap counts the slot
  free.
- **H06's reuse path fails in the opposite direction** to the filing: a
  genuinely broken gate flips back to *failing* on attempt three.
- **H09 defeats a fix from the same morning.** A hydrated worker loses its
  inherited `active_goal_id`, so the budget check in `b257532` no-ops for
  every revived worker.

## Follow-ups (deliberately out of scope)

- The durable task/checkpoint/continuation layer, progress policies,
  event-driven dependency wakeups and task visibility from the report's
  architecture section.
- A rolling intra-turn checkpoint for H04, which needs H03 first.
- The report's own "Deferred Audit Leads": remaining typed-timeout and
  fallback paths, inactivity-based worker wait expiry, feature-registry
  provenance, exact reflect retry counts, context-transparency endpoints,
  pause/resume API refusal reporting, kernel snapshot freshness.
- Router-level silent fallback (`core/llm/router.py`), still carrying the
  budget mismatch fixed for the agent ladder in `311e1e9`.
