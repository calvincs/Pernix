# Autonomy — Gates, Goals, Heartbeats, and the Session Kernel

Four subsystems that together let Pernix run long, unattended tasks without
lying to itself about progress: **gates** (deterministic checks Reflect cannot
overrule), **goals** (persistent cross-turn objectives with budgets),
**heartbeats** (recurring instructions steered into running work), and the
**session kernel** (a persistent per-session Python REPL whose state survives
everything shorter than the task itself).

All four are off by default. Enable them in Settings → Autonomy (Gates,
Goals, Heartbeats, Kernel); each flag registers its tools at startup, so
flipping one takes a restart. Each is useful alone; the last section explains
how they compose into an autonomous task.

## Gates — a finish line Reflect can't argue with

[Reflect](reflect-and-snooze.md) is an LLM judging an LLM — good at catching
missed intent, but persuadable. A **gate** is not persuadable: it is a
user-authored shell command whose exit code is the verdict. `pytest -q`
either passes or it doesn't.

With `gates_enabled`, the agent (or a canary, or a worker spec) registers
gates via three tools:

- `add_gate(name, command, watch_paths?, cwd?)` — register a check on this
  session. Creating one is a `caution`-level action: a gate is shell you
  authored that will run automatically from now on.
- `list_gates` / `remove_gate` — inspect and retire them.

Gates run in the post-turn hook chain, immediately before Reflect, and their
results are fed into Reflect's evidence. The critical mechanic is the
**clamp**: if any gate fails, a `pass` verdict from the reflect model is
mechanically clamped to `retry` before the post-mortem is written — so the
recorded history shows the clamped truth, and no amount of confident prose
from the agent can close a turn whose tests are red. Gate output also reaches
the retrying attempt (via reflect's lessons channel), so the next attempt
knows *which* check failed and how.

Two guardrails keep this honest and cheap:

- **The unchanged-workspace guard.** Gates re-run on every retry attempt —
  unless nothing under the gate's declared `watch_paths` changed since the
  last failure, in which case the failure is reused and the transcript says
  so ("gate `tests` not re-run: no changes under watch_paths"). This scopes
  by the gate's own paths, not the whole workspace, because the shared
  workspace churns from unrelated sessions and cron jobs.
- **The honesty rule.** A passing gate verifies only what that gate checks.
  Green tests do not mean the feature works; they mean the tests pass.

With `gates_enabled = false` there is zero behavior change.

## Goals — intent that outlives a turn

A turn is minutes; some work is hours. With `goals_enabled`, a session can
carry one **persistent goal**: an objective (≤4000 chars), optional budgets,
and a status that survives turns, compactions, and restarts.

- `goal_create(objective, token_budget?, time_budget_s?, continuation_budget?)`
  — goals come from **explicit user intent only**; the agent is instructed
  never to infer one from conversation.
- `goal_status` — live burn: tokens (worker spend included — workers spawned
  during an active goal bill to it), wall clock, continuations used, gate
  states.
- `goal_update` — pause/resume, adjust budgets, reword the objective.
- `goal_complete` — **the only path to `complete`**, and it is refused while
  any goal-scoped gate fails. The gates are the completion criteria; the
  agent cannot declare victory over a red check.

**Continuations** are what make a goal long-running. With
`continuation_budget > 0`, a turn that ends because it ran out of room — the
tool-round ceiling or an exhausted LLM time budget — is automatically
re-entered: a synthetic prompt ("The goal is still active. Continue, or
report blockage with host-observable evidence — do not end the goal
yourself") starts the next turn, up to the budget. Continuations are never
enqueued after a cancellation, an error, or while the session is waiting on
the user — human intervention is a legitimate stop, not a failure. When a
budget runs out, the goal moves to `budget_limited` and you get a
notification; nothing fails silently. The default `continuation_budget` is 0:
goals are user-driven unless you opt in.

## Heartbeats — steering without interrupting

Cron spawns *new* turns. A **heartbeat** nudges the *current* one: a
recurring instruction delivered into a running session. With
`heartbeats_enabled`:

- **`steer`** (default) injects the instruction as a system row picked up at
  the next round boundary of the running turn — mid-flight course correction
  without spawning a competing turn.
- **`follow_up`** queues it as a normal prompt for the next idle moment.
- A session parked where no round boundary can arrive (awaiting workers,
  awaiting user input) degrades `steer` to `follow_up` automatically.
- A heartbeat whose previous firing is still undelivered coalesces — they
  never stack.

There are two strictly separated namespaces. The agent's tools
(`set_heartbeat`, `clear_heartbeat`, `list_heartbeats`) operate only on its
own `agent`-owned heartbeats for its own session. **Your** heartbeat — one
per session — is set only via the UI or the API
(`GET`/`PUT`/`DELETE /api/sessions/{id}/heartbeat`), and the agent can
neither see nor clear it. `every` accepts durations (`30s`, `5m`, `2h`;
floor 30 s) or a 5-field cron expression. Heartbeat jobs persist in
`data/cron_jobs.json` and survive restarts; heartbeat rows are machine text
and are excluded from reflect/distill evidence.

## The session kernel — state that survives the context window

With `session_kernel_enabled`, each session can hold a **persistent Python
REPL** — a sandboxed child process (the same containment posture as
[RLM](rlm.md): resource limits, scrubbed env, defense-in-depth with the
VM/container as the real boundary) driven by the `repl` tool.

What persists, and through what:

- **Across tool rounds and turns** — it's one live process; variables,
  imports, and function definitions simply remain.
- **Across compaction** — compaction is a view transform over message
  history; the kernel isn't in the message history. Working state survives
  context resets by construction.
- **Across restarts** — idle or shutting-down kernels snapshot their
  namespace with `dill`, one variable at a time, to
  `data/kernels/<sid>/` (capped by `kernel_snapshot_max_bytes`).
  Unpicklable values (sockets, file handles) are skipped and reported. The
  first `repl` call after a restart revives the namespace and prepends
  `[kernel revived: N names restored, M skipped]`.

The kernel runs with cwd = the shared workspace and the workspace venv as its
interpreter, so `repl` and `bash` see the same files and the same installed
packages. `exec`/`eval`/`input` are blocked as a guardrail; an in-cell
traceback aborts only the cell, never the kernel — iterative debugging is
normal REPL use, not a tool failure.

**Result binding** is the kernel's quiet superpower. When any tool (all of
them except `repl`, `rlm_process`, and the conversational ones — binding is
an exclusion list, so new tools get it by default) returns more
than `large_result_bind_threshold` chars (default 20,000), the full payload
is loaded into the kernel as `tool_result_<n>` and spilled to a sidecar file,
while the model sees only a head/tail stub plus
`[full 812KB payload bound as tool_result_7 — use repl to slice/search it]`.
The agent slices megabytes with code instead of re-reading them into context
— which is exactly what keeps a many-hour goal from drowning in its own tool
output.

Lifecycle: kernels idle-reap (with snapshot) after `kernel_idle_seconds`
(1500 s — deliberately below the 1800 s session reap, so a kernel never
outlives its session as an orphan process), and at most
`kernel_max_concurrent` (3) are alive at once, LRU-reaped beyond that.
`GET /api/kernel/status` shows live counts.

## How the pieces compose

A long-running autonomous task is not one feature — it is:

> **a goal with `continuation_budget > 0` + goal-scoped gates +
> (optionally) a steer heartbeat.**

The goal carries intent and budgets across turns; continuations drive
re-entry when a turn runs out of rounds or clock; gates are the deterministic
finish line Reflect cannot overrule and `goal_complete` cannot bypass; the
heartbeat steers course mid-flight without spawning competing turns; and the
kernel carries working state across every compaction and restart in between.

`AWAITING_USER` blocks continuations *by design* — a question to the human is
a legitimate block. The push-notification path alerts you, the goal stays
`active`, and work resumes when you answer. During an active goal the agent
is guided to prefer `notify_user` for progress reports and reserve `ask_user`
for genuine decisions.

The [canary suite](canary-and-adaptive.md) is built to coexist with this:
canary sweeps are snooze-transparent and workspace-isolated, so an overnight
autonomous goal and the nightly measurement baseline can share the box.

## Settings

See [configuration.md](../configuration.md#autonomy-gates-goals-heartbeats-session-kernel).
