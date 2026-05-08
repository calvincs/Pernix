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
    prompt="Research the latest changes to X. Output a 200-word summary.",
    model_override="anthropic/claude-haiku-4.5",
    session_timeout=600,  # optional, seconds
)
```

The call returns a `worker_id`. The parent session enters `AWAITING_WORKERS` if it `await_workers`-blocks, or continues running if it just dispatches and moves on.

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
| `pause_worker` / `resume_worker` | Park or unpause a worker at the next round boundary |
| `await_workers` | Block the parent until specified workers complete |

The parent can dispatch fan-out work, do other things while workers run, and collect results when ready. The orchestration extension lives in `core/extensions/orchestration/`.

---

## When workers complete

Each worker finishes with a final response. The parent can pull it via `get_worker_result(worker_id)` and fold it into its own context.

The parent receives a `worker.done` event when a worker settles. The UI shows a worker badge on the parent session.

If a worker errors out, the parent receives `worker.error`. Errors don't crash the parent — they're just one more tool result to handle.

---

## Limits

- `max_concurrent_workers` (default 5) caps how many workers a parent can have running simultaneously. Subsequent `spawn_worker` calls fail until earlier workers complete.
- `stall_threshold` (default 120 seconds) flags a worker as stalled if it shows no activity for this long. Stalled workers are surfaced in the UI.
- Workers respect the same dangerous-tool gate as their parent. **Cron-spawned workers** (where the parent is itself an unattended cron session) skip the gate; chat-spawned workers do not.

---

## Pausing and resuming

Useful when you want the agent to "wait, don't keep going, let me think":

```
pause_worker(worker_id)
```

The worker observes the pause at its next round boundary and parks in the `PAUSED` state. It stops consuming LLM time. Resume later with `resume_worker(worker_id)`.

Paused workers are not reaped for inactivity. The only safety net is a 24-hour timeout if the parent session is deleted.

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
