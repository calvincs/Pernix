# Harness review remediation plan (2026-09-08)

Source: an external read-only harness review (`atra_findings_3.2.md`, 11
findings, 6×P1 + 5×P2). Every finding was re-verified on `next-3.2-testing`
at ef1d4c9 with isolated reproductions against the real modules before this
plan was written. Nine hold as written, two (F03, F06) are real but narrower
than rated, none were refuted.

## Verified status

| ID | Finding | Verified | Adjusted severity |
| --- | --- | --- | --- |
| F01 | Queued tools execute after cancellation | Confirmed (needs a saturated pool) | P1 |
| F02 | Cancelled queued messages resurrected by orphan recovery | Confirmed, broader: the cancelled *running* prompt is re-run too when no assistant row exists yet | P1 |
| F03 | Worker completion overrides in-progress cancel | Partial: only detached (reaper/boot) resumes; the worker-driven path instead parks the parent in AWAITING_WORKERS with `cancel_requested=True` and nothing unsticks it for 30 min | P2 + new P2 |
| F04 | Rapid-fire combine loses an in-flight correction | Confirmed (3 s window) | P1 |
| F05 | Admission rejection feedback dropped from the next model context | Confirmed, broader: dedup stubs and cross-round "already executed" stubs vanish the same way; live since v2.1.0 | P1 |
| F06 | Workers do not enforce the inherited goal budget | Confirmed mechanism; goals default off, worker bounded elsewhere | P2 |
| F07 | Stuck detection rejects the recovery it asks for | Confirmed, worse: Signals 7/11 per-tool counters push every later response ≥0.4; neutralises d620c23 for workers | P2 (fix early) |
| F08 | Non-object JSON tool arguments crash admission | Confirmed, plus an earlier crash in Signal 12 of `StuckDetector.evaluate` | P2 |
| F09 | Semantic dedup drops distinct `call_model` requests | Confirmed; `images` key never existed, tests certify the drift | P2 |
| F10 | Sticky fallback compiles for the primary's budget | Confirmed; docs recommend the bad topology (cloud primary, local fallback) | P2 |
| F11 | Queued turns keep a stale `session.error`, skip reflect/eval | Confirmed; also disables round-cap continuation, leaks into `/status` and `worker.done` | P2 |

## Fix designs

One commit per finding, each with a dated regression test in
`tests/regressions/test_2026-09-08_<slug>.py` whose docstring tells the
failure story. Commit subject `fix(<scope>): <lowercase sentence>`, body in
prose (see d620c23 for the house style).

### F05 — rejected calls must reach the model (do first)

`_ToolCallGate` writes a tool-role row keyed to the rejected call id, but the
persisted assistant row lists only admitted calls, so `exclude_orphans`
strips the rejection from every compiled request. Preferred fix: persist the
assistant row with **every proposed call** (admitted and rejected) so each
tool-role row has a parent and the pair survives compilation unchanged.
Arguments on a rejected call are normalised to an object (non-object /
unparsable payloads become `{"_raw_arguments": "<text>"}`) so every provider
adapter accepts them. Audit every reader of persisted `tool_calls` (dedup
cache, turn ledger, reflect summaries, stuck detector, `_record_round_results`)
so a rejected call is never counted as executed. If that audit shows the
pairing approach is invasive, fall back to carrying the rejection as a
system-row note (the compiler already converts mid-conversation system rows
into user-role carriers). Either way the compiler's orphan filter stays as
is; `tests/test_compaction.py` / `tests/test_compiler.py` keep passing.

Test: unknown tool, missing required parameter, non-object arguments,
intra-round duplicate, cross-round duplicate, semantic duplicate. Assert on
the **next compiled request**: the corrective text is present and every tool
result is paired with an assistant call.

### F08 — non-object arguments become an ordinary rejection

`isinstance(parsed, dict)` guard in `_parse_and_validate` returning the
normal admission rejection ("arguments must be a JSON object; received
`42`"). Same guard in Signal 12 of `StuckDetector.evaluate`, which runs
before the gate and calls `.get()` on the parsed value. Parameterised test
over `null`, `42`, `true`, `"path"`, `[]`, `["path"]`, plus an object control;
assert the turn stays alive and the feedback is in the next request.

### F09 — dedup only genuinely redundant `call_model` calls

`_is_near_duplicate_call` compares a phantom `images` key. Compare the live
schema: same `model`, same `image_path`, same `system`, and a normalised-equal
`prompt` (strip, casefold, collapse whitespace). Different prompts or images
are distinct work. Replace the tests that use `images`.

### F07 — let a compliant recovery run

Two mechanisms hold the detector at the threshold: `has_unresolved_failure`
blocks the decrement for a zero-score response, and Signals 7/11 add 0.4 for
every response after three executed failures of one tool, whether or not the
new response touches that tool. Fix both: per-tool failure counters
contribute only when the current response calls that tool / path; and in the
loop, a response that is a *recovery move* (contains `ask_user`, or every
call targets a tool with no failure history this turn and differs from the
repeated signature) executes instead of being discarded, bounded to two
recovery passes per turn. Real repetition stays nudged then stopped.

Test drives `StuckDetector.evaluate` → `_handle_stuck_signals` across rounds
(the d620c23 test hard-coded `repeats=3` and could not see this): unresolved
failure at threshold followed by a distinct `ask_user`, a different tool, a
worker's `file_write` deliverable, and the same failing call again.

### F10 — compile for the model that will be called

Once `_tried_fallback` is set the loop must resolve `effective_model` (and
the budget / max-output / capabilities derived from it) to
`settings.fallback_model` for the rest of the turn, in the tool loop, the
final-answer path, and overflow recovery (recompile + compaction
`history_budget`). Fix the comment at `can_help_with_overflow` that assumes
the fallback is larger. Fake-provider integration test: primary fails, fallback
returns a tool call, next round compiles with the fallback's budget and
model name; an overflow on the fallback recompacts to the fallback's budget.
The router's own silent swap on rate limits has the same mismatch; out of
scope here, recorded under follow-ups.

### F06 — budget check by goal id

`_goal_budget_exceeded` looks the goal up by owning session. Add
`db.get_goal(goal_id)` and resolve by the session's `active_goal_id`
regardless of owner; keep the ownership check for goal mutation only. Test:
parent-owned goal inherited by a worker with an exhausted token budget →
the worker's next `_pre_round_gate` breaks with `budget_exhausted`.

### F02 — cancelled messages get a durable disposition

`messages.metadata` (JSON) carries `{"cancelled": true}`. On cancel (manager
`cancel_session` and the HTTP route — factor the shared part into one
manager helper), stamp every dropped pending row **and** the running turn's
own user row when no assistant row has been persisted yet.
`get_orphaned_user_messages` skips stamped rows. The transcript keeps the
text; the UI can render the stamp later. Tests for both cancel paths: queue
an operation, cancel, prompt again → only the new prompt dispatches; cancel
before any assistant row → the cancelled prompt does not re-run; survives a
manager restore.

### F11 — queued turns start clean

`_process_pending` clears `session.error` and `session.termination_reason`
after the `_turn_in_flight` check and before `create_task`, the same fields
`prompt()` and `_resume_from_workers` clear. Test: turn A fails with B
queued, B succeeds → error cleared, `_maybe_reflect` and `_maybe_evaluate`
pass their guards; three queued turns in a row.

### F03 — cancel stays authoritative through a resume

In `_resume_from_workers`, re-check `parent.cancel_requested` and the current
state after the `to_thread` awaits, inside the lock, immediately before
clearing the flag and dispatching; if cancelled, return without launching.
Then the side defect: on the worker-driven path the cascade's
`CancelledError` escapes `_finalize_turn`'s `except Exception` and leaves the
parent parked in `AWAITING_WORKERS` with `cancel_requested=True`, an empty
watch set and no notice. Make the AWAITING_WORKERS cancel path (both routes)
drive the parent to IDLE_READY with a cancel notice and clear the watch set,
and have `_finalize_turn` treat cancellation of the resume the same way.
Barrier-controlled tests for both the detached and worker-driven paths: no
synthesis turn, flag still set, parent not stuck.

### F04 — a correction that arrives after the last compile still gets read

`TurnState` gains `user_row_version`; the rapid-fire combiner bumps it under
the session lock; the agent records the version it compiled. On the text-only
final-answer path, after persisting the answer, if the version moved since
the last compile, run one more round (bounded to one extra pass) instead of
returning, so the model sees the combined row and its own answer and can
address the correction. Test: barrier after compile, correction during the
barrier, final answer streams → a second model request carries the
correction.

### F01 — a cancelled dispatch never starts its queued tool

In `_execute_single`: `_runner` checks a per-dispatch cancel flag (and
`_session_cancel_requested`) before calling `execute_sync` and returns a
cancelled marker if set; the `CancelledError` handler sets the flag and calls
`fut.cancel()`. Test: single-thread pool, queue a side-effecting tool, cancel
the dispatch, release the pool → the tool never runs; also cancel at the
queue-to-running boundary.

## Execution

Six Opus agents, each in its own worktree branched from HEAD, grouped so
that no two agents edit the same region:

| Stream | Findings | Files |
| --- | --- | --- |
| W1 | F05, F08, F09 | core/agent.py (gate, dedup), provider adapters if needed |
| W2 | F07 | core/agent.py (StuckDetector, `_handle_stuck_signals`, discard site) |
| W3 | F10 | core/agent.py (loop model selection, overflow recovery) |
| W4 | F06 | core/agent.py (`_goal_budget_exceeded`), db/models.py (goals) |
| W5 | F02, F11 | sessions/manager.py (cancel, `_process_pending`, orphan sweep), db/models.py (orphan predicate), api/routers/sessions.py |
| W6 | F03, F04 | sessions/manager.py (`_resume_from_workers`, rapid-fire), core/agent.py (final-answer path), sessions/state |
| — | F01 | core/tools/executor.py (folded into W4, disjoint file) |

Merge order into `next-3.2-testing`: W1, W2, W4, W3, W5, W6 (cherry-pick,
resolve conflicts, re-run the touched tests). Remove the worktrees before
`./check.sh` (the compaction scanner walks worktree copies). Then push.

## Deploy

Box runbook (`pernix-box-deploy`): check `/api/sessions?limit=500` for
running turns first; `git pull && docker compose up -d --build`; verify
`docker inspect pernix --format '{{json .Args}}'` still shows `--dangerous`;
curl `/api/health`; grep the container for one new line per fix; run the
live assertions (repro scripts adapted to the box where state permits).

## Follow-ups (out of scope)

- Router-level fallback swap on rate limits also feeds a primary-sized
  prompt to the fallback (`core/llm/router.py`).
- A rehydrated worker has `active_goal_id=None`, so its spend is not
  attributed at all.
- Docs recommend cloud primary + local Ollama fallback without noting the
  context-window mismatch (`docs/faq.md`, `docs/configuration.md`).
- Tests in `tests/test_compaction.py` and `tests/test_compiler.py` still
  assert orphan dropping; correct behaviour, but they should be joined by
  the F05 pairing test so the two contracts are visible together.
