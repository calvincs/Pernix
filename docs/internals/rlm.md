# RLM — Recursive Long-Input Processing

The RLM extension (`core/extensions/rlm/`) lets the agent process inputs far
beyond any model's context window. It is an implementation of **Recursive
Language Models** (arXiv [2512.24601](https://arxiv.org/abs/2512.24601), MIT
CSAIL), adapted from the MIT-licensed reference implementation at
[github.com/alexzhang13/rlm](https://github.com/alexzhang13/rlm) — extracted
and rewritten for Pernix rather than taken as a dependency (each adapted file
carries an attribution header).

Off by default. Enable in Settings → General → RLM (Recursive Processing);
the `rlm_process` tool registers at startup (restart after toggling), and
models are chosen in Settings → Models → Model Roles.

## The idea

Instead of stuffing a huge input into the model's window (impossible) or
paginating it (loses whole-input understanding), the input becomes a `context`
**variable in a Python REPL**. The root model never sees the raw input — it
sees metadata (type, size) and iteratively writes ```repl``` code blocks that
slice, search, and transform the variable, delegating semantic work on chunks
to `llm_query()` / `llm_query_batched()` sub-calls. Results accumulate in REPL
variables; the run ends when the model's code sets `answer["ready"] = True`.

## Architecture (one run)

```
tool thread (long-poll pool)                 child process (sandboxed)
┌─────────────────────────────┐   exec.sock  ┌──────────────────────────┐
│ RLMEngine loop:             │ ───────────▶ │ child_runner.py          │
│  root LLM → parse ```repl```│  code cells  │  persistent namespace    │
│  → execute → feed stdout    │ ◀─────────── │  context variable        │
│  back → repeat              │   results    │  llm_query() stubs       │
└──────────┬──────────────────┘              └───────────┬──────────────┘
           │ run_coroutine_threadsafe                    │ llm.sock
           ▼                                             ▼
   main event loop ◀──────────────────────── broker (ThreadingUnixStreamServer)
   (LLMClient.chat, scheduler)               ledger · semaphore · model allowlist
```

- **Engine** (`engine.py`) — the iteration loop. Runs on a tool-executor
  thread, never the event loop (guarded). Root/sub LLM calls are injected
  callables, so tests run scripted models against real child processes.
- **Child** (`child_runner.py`, spawned by `child_env.py`) — one persistent
  stdlib-only subprocess per run holding REPL globals in RAM. Spawned with the
  bash tool's sandbox pattern: `setsid` + `RLIMIT_AS`/`RLIMIT_FSIZE`, cwd =
  run dir, env built from scratch (no inherited variables, **no API keys**).
- **Broker** (`broker.py`) — serves the child's sub-LLM requests over a unix
  socket in the run dir. Every budget decision is parent-side: sub-call count
  (`rlm_max_subcalls`, one ledger shared across recursion depths), concurrency
  (`rlm_max_concurrent_subcalls`), model allowlist, recursion depth. Child
  frames are data, never trusted.
- **Recursion** — at `rlm_max_depth` ≥ 2, `rlm_query()` runs a nested engine
  (own child, `sub/<id>` run dir, shared ledger and deadline) on the broker
  handler thread it already occupies. Past the cap it silently degrades to a
  plain `llm_query`.

## Kill discipline

Every blocking wait is bounded by the run deadline (`rlm_timeout_seconds`).
A cell with no result AND no in-flight sub-calls for 300s gets SIGINT
(→ `KeyboardInterrupt` in the cell, namespace preserved); SIGKILL to the
process group after a grace period if unresponsive. The child is registered as
`session._active_process`, so session cancel and executor dispatch-timeout
kill paths work unchanged; the child also self-reaps on socket EOF and
`PR_SET_PDEATHSIG` if the server dies. Runs never return empty-handed: the
iteration cap triggers one answer-synthesis call, and every abnormal exit
carries the best partial answer, labeled as such.

## Security posture — read this

**This is defense-in-depth, not a security boundary** — the same stance as the
`bash` tool. The child is a same-UID subprocess without namespaces: model code
can read the workspace, open network connections, and burn CPU within its
rlimits. What the design does guarantee:

- model-written code **never executes in the server process**;
- the child env contains **no secrets** (verified by test) — all LLM access is
  brokered, budgeted, and model-allowlisted by the parent;
- resource limits (8 GB address space, 2 GB file size) and group-kill;
- the restricted-builtins table inside the child is a behavioral guardrail
  only, and is documented as trivially escapable.

The Docker container Pernix runs in is the actual containment layer.

## Data lifecycle ("extract and discard")

The durable output is the tool result in the session transcript. Everything
else is transient residue in `data/workspace/rlm/<run_id>/` (manifest.json,
trace.jsonl of every turn/cell/sub-call, staged context copies, child.log,
answer.txt — workspace-visible so the agent can `file_read` its own trace),
indexed by a lightweight `rlm_runs` DB row (migration v18). Snooze activity
12a purges dirs + rows older than `rlm_run_retention_days` (default 30) —
along with the run's view session (below) — and a startup sweep marks rows
orphaned by a restart.

## Live visibility (run views)

A run used to be a black box between `tool.start` and the tool result. Three
layers now surface it, all reading from the same trace:

- **Engine progress seam** — `RLMEngine(progress_fn=...)` receives every
  trace event plus a periodic `heartbeat` (iteration, sub-call count, broker
  in-flight/quiet time) so long root calls and cells don't read as frozen.
  Heartbeats are SSE-only; trace.jsonl records signal, not the passage of
  time. Nested engines and the dream probe pass no `progress_fn`.
- **Parent-session SSE + strip chip** — the tool glue forwards events as
  `rlm.started` / `rlm.activity` / `rlm.heartbeat` / `rlm.done` on the
  calling session's stream (the worker-event path), updates the `rlm_runs`
  counters per iteration/sub-call, and the UI renders a live chip
  (`RLM · it 7/20 · 6 calls · 4m10s`) in the activity strip next to worker
  chips.
- **View session + trace viewer** — each run gets a message-less
  `session_type='rlm'` pseudo-session under its parent (linked via
  `rlm_runs.ui_session_id`, migration v20): a sidebar anchor with the run's
  state dot, nested like a worker. Selecting it renders the read-only trace
  viewer (`static/js/components/rlm-viewer.js`) in place of the chat:
  iteration cards, collapsible REPL cells with stdout/stderr, sub-call rows,
  the final answer, and nested-run navigation. While the run is live the
  viewer polls `GET /api/rlm/runs/{id}/trace?after=<byte offset>` (~2s); the
  engine flushes whole lines, so the file tails cleanly and the same view
  serves post-hoc inspection. Chat into a view session is rejected by the
  shared read-only predicate in `sessions/policy.py` (dream-journal
  precedent). Deleting the view session purges the finished run's dir + rows
  (`manager._purge_rlm_artifacts`); retention deletes the view session with
  the run — symmetric, no orphans. Dream probes (`session_id="dream"`) get no
  view session; their runs remain inspectable from the Jobs tab.

## How the agent discovers it

Five wiring points, all gated on `rlm_enabled`: the tool description playbook;
a `RECURSIVE PROCESSING` block in the base system prompt; a scout RULES entry
(stage sources → recommend `rlm_process` in approach guidance); a harness
nudge that fires on truncated-output signatures (`⚠ TRUNCATED`, `⚠ Large
file`, `[truncated at N bytes]`); and registry synonym/co-occurrence entries.

## Settings

See [configuration.md](../configuration.md#rlm-recursive-processing-add-on).
