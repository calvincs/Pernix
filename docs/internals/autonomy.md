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

- `add_gate(name, command, watch_paths?, cwd?, scope?)` — register a check on
  this session. Creating one is a `caution`-level action: a gate is shell you
  authored that will run automatically from now on. `scope` is `session`
  (default) or `goal`; a goal-scoped gate additionally blocks `goal_complete`.
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
yourself") starts the next turn, up to the budget, emitting
`goal.continuation {goal_id, ordinal, budget}`. Continuations are never
enqueued after a cancellation, an error, or while the session is waiting on
the user — human intervention is a legitimate stop, not a failure.

**Budgets are checked mid-turn, not just between turns.** The between-turn
check is still the authoritative settlement, but a single turn used to be
able to overshoot its token and time budgets without bound — the loop only
looked at the budget once it had already finished. The agent loop now
re-checks every third tool round while a goal is active and, when a budget is
blown, emits `goal.budget_exceeded {reason}`, sets
`termination_reason="budget_exhausted"` and ends the turn there. Every third
round is enough to bound the overshoot without paying for a DB read per
round.

**When a budget runs out**, the goal moves to `budget_limited` and a
high-urgency notification is written and broadcast (`Goal #N budget-limited`,
with the reason — e.g. `continuation budget spent (5/5)`). Token, time and
continuation exhaustion all take the same path, and it fires once: later
turns short-circuit on the goal no longer being `active`. Nothing fails
silently, and there is no separate `goal.*` event for exhaustion — it reuses
the notification channel so it reaches you even if no browser is attached.

**Cron sessions can carry goals.** Goal auto-continuation applies to
`normal` *and* `cron` sessions, so a scheduled job that picks up the active
goal keeps driving it. Goals are still only ever *created* by
`goal_create` from explicit user intent; a cron session attaches to whatever
goal is already active, it does not mint one.

**Goal continuations are snooze-transparent.** While a session is being
auto-driven by continuations it sets `goal_continuation_active`, which takes
it out of Snooze's idle-gate accounting: it neither blocks a snooze cycle
from starting, nor cancels one that is running, nor refreshes the activity
cooldown. Otherwise an unattended overnight goal would starve the system of
all its maintenance for as long as it ran. Contention for the actual model is
handled where it belongs — the LLM scheduler's priority tiers — rather than
by pretending the session is idle in any other sense.

The default `continuation_budget` is 0: goals are user-driven unless you opt
in.

### Retry discipline for unattended runs

Two mechanisms stop an unattended goal from burning its budget re-running the
same failure. Both live in the post-turn [Reflect](reflect-and-snooze.md)
hook:

- **The cross-retry circuit breaker.** When Reflect asks for yet another
  retry, the last two attempts of this turn are compared: same
  `failure_cause`, and reasoning text ≥ 0.7 similar. If they match, the retry
  is *refused* — `reflect.circuit_breaker {attempts, signature, reasoning}`
  is emitted, a notice row lands in the transcript, and you get a
  notification ("Retry stopped — same failure repeating"). A loop that has
  failed identically twice will not become correct on the third identical
  attempt; it needs a different approach or a human.
- **`retry_without_tools`.** Reflect can name up to five tools (validated
  against the registry, so hallucinated names are dropped) to withhold from
  the retry attempt. They are removed from the schema slice the model sees —
  overriding both the builtin set and the monotonic allowlist — and the
  executor refuses them as a second layer if the model calls one anyway. This
  is how "you kept reaching for the wrong tool" becomes a mechanical
  constraint instead of a suggestion. Both the exclusion set and the retry
  counter reset on every genuine user turn.

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
- **A no-op tick writes nothing.** A tick that finds no session, no
  instruction, or that coalesces against an already-steered turn or a still-
  queued prior tick returns *before* any `cron_runs` row is inserted. A 30-second
  heartbeat would otherwise write ~2,880 rows a day, almost all of them
  recording that nothing happened, and drown the real cron history.

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

**Result binding** is the kernel's quiet superpower. When any tool returns
more than `large_result_bind_threshold` chars (default 20,000), the full
payload is loaded into the kernel as `tool_result_<n>` and spilled to a
sidecar file under `data/kernels/<sid>/payloads/`, while the model sees only
a head/tail stub (2,000 chars / 800 chars) plus
`[full 812KB payload bound as tool_result_7 — use repl to slice/search it]`
and the durable copy's path. That path resolves: the kernel payload tree is a
registered **read** root, so the pointer is not dead on arrival — `file_read`
and `rlm_process(source=…)` can re-open the whole payload later.

Binding is an **exclusion set, not an allowlist**: everything binds except
`repl`, `rlm_process`, `ask_user`, `notify_user` and `notify_parent`. So
`bash`, `grep` and `glob` — the three tools most likely to produce a
multi-megabyte result — bind, and any tool added later gets binding by
default rather than being silently forgotten. Errored results are never
bound.

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
