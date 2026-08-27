# Workers — parallel sub-agents

A **worker** is a sub-agent the main session can spawn to do work in parallel, on a different model if needed. Workers run in their own session, share the same workspace and memory, and report results back.

Use cases:

- Have a powerful but slow model do high-level planning, while delegating individual file edits or research tasks to fast cheap models.
- Run three workers in parallel to investigate three angles of a problem, then synthesize.
- Use a vision-capable model for one task and a code-focused model for another, all in the same conversation.

Workers are flat: a worker can't spawn its own workers. Only main sessions spawn workers.

---

## Spawning

The agent calls `spawn_worker` with a prompt and optional model override:

```python
spawn_worker(
    task_description="Research the latest changes to X. Output a 200-word summary.",
    title="research-x",                   # optional label
    model="anthropic/claude-haiku-4.5",   # optional model override
    kind="research",                      # optional typed kind (see below)
)
```

The call returns a `worker_id`. The parent session enters `AWAITING_WORKERS` if it `await_workers`-blocks, or continues running if it just dispatches and moves on.

### Typed kinds

`kind` selects a named bundle instead of a hand-written charter: a role
preamble, an **exclusive tool allowlist** (enforced in the schema and again at
the executor, same as scheduled-job charters), a default model, and
verification criteria the quality gate grades against. Built-ins:

| Kind | Shape | Gate |
|---|---|---|
| `research` | web + memory reads, no file edits | claims must name sources |
| `code` | file tools + shell + repl + jobs | states which check ran and its result |
| `explore` | read-only workspace survey | findings carry file:line citations |
| `debug` | file tools + shell + repl + jobs | reproduction shown, root cause stated |
| `transform` | file tools + repl, no network | output files named with parse proof |

Every kind can write files enough to produce its summary deliverable. `research`
and `explore` also have a cheap deterministic gate in `get_worker_result`: a
summary with zero citations comes back prefixed with a `# KIND GATE` warning.

Operators can override a built-in or add new kinds by dropping
`data/worker_kinds/<name>.json` with any subset of the fields
(`description`, `role_instructions`, `tool_allowlist`, `model`,
`verification`). `"model": "background"` resolves to the Background role at
spawn time. Files are re-read on every spawn — no restart needed.

The kind persists on the worker's session row, so a rehydrated worker (restart,
reap, `resume_worker`) keeps its allowlist and model.

In a chat, you can ask the parent agent to do this naturally:

> *"Spin up two workers — one to summarize the SEC filing, one to draft questions a reporter would ask. Then merge their output."*

The agent picks reasonable models for each based on the task.

---

## Coordinating

Several control tools are always available to the parent:

| Tool | What it does |
|---|---|
| `check_workers` | List all workers and their current state |
| `get_worker_result` | Fetch a finished worker's final response |
| `message_worker` | Send a follow-up message into a running worker (mid-turn injection) |
| `set_worker_state` | Pause (`paused=true`) or resume (`paused=false`) a worker at the next round boundary |
| `resume_worker` | Release a paused worker — or **revive** a cancelled/errored/round-capped/reaped one from its persisted state, with an optional guidance note |
| `await_workers` | Block the parent until specified workers complete |

The parent can dispatch fan-out work, do other things while workers run, and collect results when ready. The orchestration extension lives in `core/extensions/orchestration/`.

---

## When workers complete

Each worker finishes with a final response. The parent can pull it via `get_worker_result(worker_id)` and fold it into its own context.

The parent receives a `worker.done` event when a worker settles. The UI shows a worker badge on the parent session.

If a worker fails to start, the parent receives `worker.failed` (the other events are `worker.started` and `worker.done`). Errors don't crash the parent — they're just one more tool result to handle.

---

## Limits

- `max_concurrent_workers` (default 5) caps how many workers a parent can have running simultaneously. Subsequent `spawn_worker` calls fail until earlier workers complete.
- `await_workers` flags a worker as stalled if it shows no activity for longer than its `stale_threshold` argument (default 120 seconds). Stalled workers are surfaced in the UI.
- Workers respect the same dangerous-tool gate as their parent. **Cron-spawned workers** (where the parent is itself an unattended cron session) skip the gate; chat-spawned workers do not.

---

## Pausing and resuming

Useful when you want the agent to "wait, don't keep going, let me think":

```
set_worker_state(worker_id, paused=true)
```

The worker observes the pause at its next round boundary and parks in the `PAUSED` state. It stops consuming LLM time. Resume later with `set_worker_state(worker_id, paused=false)`, or use the REST endpoints `POST /api/sessions/{id}/workers/{worker_id}/pause` and `.../resume`.

Paused workers are not reaped for inactivity. Two independent safety nets apply: 24 hours of idleness, or the parent session being deleted — either one force-cancels the paused worker.

### Reviving a dead worker

Workers are sessions, and sessions persist — so a worker that was cancelled,
errored, hit the round ceiling, was reaped from memory, or was lost to a server
restart can be brought back instead of re-run from scratch:

```
resume_worker(worker_id, note="skip part one, it's already verified")
```

Revival rehydrates the persisted state (message history — compacted if long —
plus the typed kind's allowlist and the pinned model), validates the model
still exists (falling back to the default with a visible note if not), clears
the stale summary stamp so the old `# CANCELLED` header can't shadow the new
result, re-attaches the worker to its parent, and starts a continuation turn.
The parent receives a `worker.resumed` event. `retry_worker` remains the right
call when the prior work is *not* worth keeping — it spawns a fresh
replacement.

The REST mirror is `POST /api/sessions/{id}/workers/{worker_id}/resume` with an
optional `{"note": "..."}` body; it revives terminated workers the same way the
tool does.

---

## When NOT to use workers

Workers add overhead — they spin up a new session, scout phase, and tooling context. If you can do the work with a single agent loop, do that. Reach for workers when:

- The work is genuinely parallelizable (independent sub-tasks).
- A different model is better for one of the sub-tasks (e.g., vision for one part, code for another).
- The parent is going to do additional work after the sub-task and shouldn't block on it inline.

A single-shot lookup ("What's the weather in NYC?") doesn't need a worker. A research project with three independent investigations and a synthesis step does.

---

## Inspecting a worker mid-flight

The UI shows worker progress in the parent's timeline drawer. You can also click into a worker session in the sidebar and watch it directly.

Programmatically: each worker has its own session ID. Subscribe to `GET /api/sessions/{worker_id}/events` to stream that worker's events independently.
