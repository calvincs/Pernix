# Autonomy — Gates, Goals, Heartbeats, and the Session Kernel

Four subsystems that together let Pernix run long, unattended tasks without
lying to itself about progress: **gates** (deterministic checks Reflect cannot
overrule), **goals** (persistent cross-turn objectives with budgets),
**heartbeats** (recurring instructions steered into running work), and the
**session kernel** (a persistent per-session Python REPL whose state survives
everything shorter than the task itself).

All four are off by default. Enable them in Settings → Autonomy & idle work →
Autonomy (Gates, Goals, Heartbeats, Kernel); each flag registers its tools at
startup, so flipping one takes a restart. Each is useful alone; the last
section explains how they compose into an autonomous task.

## Gates — a finish line Reflect can't argue with

[Reflect](reflect-and-snooze.md) is an LLM judging an LLM — good at catching
missed intent, but persuadable. A **gate** is not persuadable: it is a
user-authored shell command whose exit code is the verdict. `pytest -q`
either passes or it doesn't.

With `gates_enabled`, the agent (or a canary, or a worker) registers
gates via three tools:

- `add_gate(name, command, watch_paths?, cwd?, scope?)` — register a check on
  this session. Creating one is a `dangerous`-level action: a gate is shell you
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

A different, always-on mechanism shares the name: the **tool-call gate**
(`_ToolCallGate` in `core/agent.py`) type-checks every tool call's arguments
against its JSON schema before dispatch — coercing numeric strings, `"true"`,
and bare scalars where a string is expected (tagged
`[note: coerced parameter type(s): …]` on the result), dropping unknown
parameters with a note, and rejecting an uncoercible mismatch in-round with
the expected type spelled out, enum membership included (checked *after*
coercion, so `"5"` still matches an integer enum) and per-element array item
types. It runs on every call regardless of `gates_enabled` — dispatch
hygiene, not a user-authored check — and emits
`tool.call.intercepted {name, action, reason}` so the UI and the adaptive
layer can see what it corrected.

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

**Round-cap continuation is separate and always on.** Even with no goal
active, a turn that hits the `max_tool_rounds` ceiling while healthy — tools
ran, no error, no stuck repeat spiral — gets `round_cap_auto_continue`
(default 1) fresh rounds and an extended LLM session-timeout budget, with a
system message telling the model this is its last continuation and to wrap
up honestly. This is what stops the 100-round ceiling from being the thing
that ends deep work instead of the work itself; it composes with, but does
not require, `continuation_budget`.

**The forced follow-up nudge** catches the opposite failure: a turn that
*ends* with no tool calls and a tail announcing work it never did ("Next,
I'll update the settings file."). `_announces_future_work` (`core/agent.py`)
scopes the check to the reply's last two sentences on purpose — an "I'll"
three paragraphs up followed by a completed deliverable must not trigger —
and a trailing question or a courtesy closer ("let me know if…", "happy to
help") means the model handed the turn back deliberately, so neither fires
it. When it does, the agent gets one bounded in-turn nudge naming the
unfinished item — the active goal's objective if one is running, otherwise
the model's own announced intent — instead of the turn ending and paying for
a Reflect retry later. Gated on `forced_followup_enabled` (default `true`),
capped per turn by `forced_followup_max_per_turn` (default 1, 0–5), and
emitted as `turn.forced_followup`. Whether the nudge actually produced tool
calls or the agent re-idled anyway is recorded as one aggregate
`scout_signals` row (`forced_followup`/`global`) plus a
`turn.forced_followup_outcome` event, so a week of live traffic answers
whether the feature is earning its keep with one query.

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
eleven names — `repl` (its output is already kernel-side), `rlm_process`
(a synthesized answer, nothing to slice), the conversational tools
(`ask_user`, `notify_user`, `notify_parent`), and readers whose output is
already managed context and must be read whole (`load_skill`,
`read_skill_instructions`, `read_skill_resource`, `discover_tools`,
`get_worker_result`, `get_worker_transcript`). So `bash`, `grep` and
`glob` — the three tools most likely to produce a multi-megabyte result —
bind, and any tool added later gets binding by default rather than being
silently forgotten. Errored results are never bound.

The agent slices megabytes with code instead of re-reading them into context
— which is exactly what keeps a many-hour goal from drowning in its own tool
output.

Lifecycle: kernels idle-reap (with snapshot) after `kernel_idle_seconds`
(1500 s — deliberately below the 1800 s session reap, so a kernel never
outlives its session as an orphan process), and at most
`kernel_max_concurrent` (3) are alive at once, LRU-reaped beyond that.
`GET /api/kernel/status` shows live counts.

## Background jobs — compute that outlives the turn

A blocking `bash` call ties a tool round to the lifetime of the process it
spawned — a 30-minute solver means a 30-minute round, or more often a
timeout. With `jobs_enabled` (default **on**), the agent instead gets:

- `job_start(command, ...)` — spawn a detached process group. Output goes to
  a log file; an exit-code sidecar makes completion durable, so a finished
  job is still reported correctly after a server restart.
- `job_status` / `job_tail` — poll state and read the log tail. Both are
  exempt from result-dedup caching, so repeated polls always see fresh data.
- `job_kill` — kill the whole process group, not just the leader.

Jobs are wall-clock capped via coreutils `timeout`
(`jobs_default_timeout_s`, 2 h default; callers can raise it to
`jobs_max_timeout_s`), capped at `jobs_max_concurrent` (3) running jobs per
session, and run under the same rlimits as `bash`. The pattern composes with
everything above: start the solver as a job, keep working the goal, read the
log when the gate is ready to check it.

Scheduled cron jobs have a separate, unrelated validate-and-dry-run
mechanism (`POST /api/jobs/{name}/validate`, `POST /api/jobs/{name}/test`) —
see [../guides/scheduling-cron.md](../guides/scheduling-cron.md). This
section is the ephemeral in-turn `job_start`/`job_status`/`job_tail` tool,
not that.

## Typed workers — spawning with a shape, surviving death

A worker delegated mid-goal is still autonomous work, and v3.1 gave workers
two things long-running tasks need: a repeatable shape, and a way back after
they die.

`spawn_worker(kind=...)` (`core/extensions/orchestration/kinds.py`) selects a
named bundle instead of a hand-written charter: role instructions, an
*exclusive* tool allowlist, a default model, and verification criteria
Reflect grades against. The allowlist rides the same two-point enforcement
scheduled-job charters use — schema intersection at the tool-schema builder,
refusal at the executor — so there is no new enforcement path to audit. Five
built-ins ship: `research`, `code`, `explore`, `debug`, `transform`;
`research` and `explore` also get a cheap deterministic check on their own
output (a `# KIND GATE` warning from `get_worker_result` when a research
summary names zero sources, or an explore summary carries zero file:line
citations). Operators add or override kinds by dropping a JSON file at
`data/worker_kinds/<name>.json` — re-read on every resolve, so an edit
applies to the next spawn without a restart; `"model": "background"`
resolves to the Background role at spawn time. `retry_worker` replacements
inherit the original's kind.

`resume_worker(worker_id, note?, auto_resume_parent?)` unifies "bring a
worker back" under one tool: a paused worker is released (unchanged); a
worker that died — cancelled, errored, round-capped, reaped from memory, or
lost to a server restart — is **revived**: its history rehydrates from the
database (compacted if long), its kind allowlist and pinned model are
restored (the migration-v31 `sessions.model_override` / `sessions.worker_kind`
columns — previously in-memory only, so a rehydrated worker silently lost
both), a model that no longer resolves falls back to the default with a
model-visible note, the stale summary stamp is cleared so an old
`# CANCELLED` header can't shadow the new result, and a continuation turn
starts carrying `note`. `auto_resume_parent` (default false) adds the
revived worker back to the parent's watch-set — same contract as
`spawn_worker` — so the parent auto-wakes on its completion; reviving also
extends the parent's LLM wall-clock budget (up to 2x the session timeout,
capped at 24h) so a parent that revives-then-awaits doesn't die on its own
next acquire. Emits `worker.resumed`.
`POST /api/sessions/{id}/workers/{worker_id}/resume` (optional `{"note"}`
body) does the same over the API and checks parentage against the database
row, so it still works after a restart. See
[../guides/workers.md](../guides/workers.md) for the full worker model.

## The turn ledger — closing the awareness gap

Every turn on a `normal` or `cron` session opens with a
`[SINCE YOUR LAST TURN]` block (`_build_turn_ledger` in
`core/context/compiler.py`, gated on `turn_ledger_enabled`, on by default):
workers and background jobs that finished since the agent last looked, the
Reflect verdict on the agent's *own* previous turn (which otherwise lands
about five minutes after the turn ends, so without the ledger the agent
learns its own grade a turn late or never), self-modifications the adaptive
layer applied, canary regressions, platform restarts. It is delta-based and
silent when nothing changed — a quiet system renders nothing, and the block
is the empty string when the setting is off. Canary sessions never see it
(platform state leaking into a synthetic measurement turn would contaminate
it); worker sessions stay lean by design; Dream and RLM views take no turns.

The `agent_state` tool (`core/extensions/session_tools/__init__.py`) is the
on-demand companion for everything the ledger doesn't push automatically:
one call answers what used to take several separate lookups — work in
flight (sessions, background jobs, RLM runs), this session's recent Reflect
verdicts, recent notifications, adaptive-layer counts, recent canary
gate-fails, cron health, memory-store size, open Telos alarms.
`data/workspace/SYSTEM-MAP.md` (`core/context/system_map.py`, regenerated at
every boot) goes deeper still: the real schema of the tables the agent is
likely to query, the data-directory layout, and the live FastAPI route
inventory, so "where do I look" costs a read instead of a guess.

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
