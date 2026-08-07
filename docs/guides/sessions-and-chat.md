# Sessions and chat

A **session** is one persistent conversation thread — your project, your daily brief, your research investigation. It has an ID, a list of messages (append-only), a state, and optional per-session overrides. Sessions live in `data/sessions.db` and survive restarts.

This page covers what happens when you send a message, how to manage sessions, and the controls available for unusual situations.

For the formal state machine and reaper rules, see [../internals/state-machine.md](../internals/state-machine.md). For the conceptual walkthrough, see [../architecture.md](../architecture.md).

---

## A turn end-to-end

When you send a message, it goes through five phases:

1. **Session accepts** — your message lands on the session queue. If a turn is already running, your message queues behind it (up to `max_pending_messages`, default 10).
2. **Scout** plans the approach: searches memory, picks tools and skills, drafts a plan. Runs on the Background role (`background_model`, small/fast). Visible in the timeline as the `SCOUTING` state.
3. **Agent loop** executes the plan, streaming response tokens and tool calls. Runs on `llm_model` (your primary). Visible as `PROCESSING`.
4. **Reflect** checks whether the agent fulfilled your intent. If not, it triggers up to 2 retries (`reflect_max_retries`). Reuses the same `turn_id` with a higher `retry_index`.
5. **Post-hooks** run in the background: auto-titling, distillation into long-term memory, worker cleanup. Visible as `FINALIZING`.

The whole sequence emits SSE events the UI streams in real time. You can also subscribe to the event stream programmatically — see [../api.md](../api.md).

---

## Creating, switching, and clearing sessions

In the UI:

- **New session** in the sidebar starts a fresh thread. Each session has its own memory recall scope and its own workers.
- **Switch** by clicking another session in the sidebar — your prior session keeps its state in the background.
- **Clear / delete** a session from the session menu. Cleared sessions remain in the DB; deleted ones are removed.

Programmatically:

| Endpoint | Effect |
|---|---|
| `POST /api/sessions` | Create a new session |
| `POST /api/chat` | Send a message to a session (returns JSON; events flow over the SSE stream) |
| `GET /api/sessions/{id}` | Fetch session state |
| `DELETE /api/sessions/{id}` | Delete the session |
| `GET /api/sessions/{id}/events` | Subscribe to SSE event stream |

See [../api.md](../api.md) for full details.

---

## The sidebar: session types, filtering, and finding things

Not every session in the sidebar is a chat you started. Each carries a colored dot for its type, and the legend at the bottom of the sidebar shows the counts:

| Type | What it is |
|---|---|
| **Session** | A conversation you started. |
| **Cron** | A scheduled job run — each firing gets its own session. |
| **Worker** | A sub-agent spawned by another session. |
| **Dream** | The idle-time introspection journal (see below). |
| **RLM** | A live view of one `rlm_process` run, nested under the session that launched it (see below). |

Click a legend entry to hide or show that type — useful when cron runs start to crowd out your own threads. The filter persists across reloads.

Finding things:

- The sidebar **search box** is full-text over all message content — it finds any past conversation, not just titles.
- **Ctrl+K** opens a fuzzy-find palette to jump to any session by name.
- **Ctrl+F** searches within the current transcript.
- **↑** in an empty composer recalls your message history.

(`/help` in any chat lists all of these.)

---

## Dream journal sessions

When [Dream introspection](../internals/dream.md) is enabled, each day of dreaming narrates itself into a day-keyed journal session ("Dream Jul 31") — hypotheses raised, verdicts, report writes. These sessions are **read-only**: the composer is disabled and the server rejects messages. They're excluded from search and memory distillation, and pruned after `dream_journal_retention_days` (default 14). Filter them out with the sidebar legend if you'd rather not see them.

---

## RLM run views

When the agent kicks off an [RLM run](../internals/rlm.md) (`rlm_process` over an input too large to read inline), the run appears in three places:

- A **live chip** in the activity strip of the launching session — `RLM · it 7/20 · 6 calls · 4m10s` — pulsing while the run works, alongside any worker chips. The parent transcript also gets start/finish lines.
- A nested **RLM session** in the sidebar under its parent (same collapsible group as workers), with a pulsing dot while running.
- Clicking either opens the **trace viewer** in place of the chat: the root model's per-iteration reasoning, each REPL cell (collapsible, with code and stdout/stderr), every sub-LLM call with latency, live iteration/sub-call/elapsed progress against the run's caps, and the final answer once the run ends. The view tails the trace live and doubles as the permanent record afterward.

Like dream journals, RLM sessions are **read-only** — there's no transcript to chat in; the conversation lives in the parent session. Deleting one also deletes the run's on-disk trace; runs older than `rlm_run_retention_days` (default 30) are purged together with their view sessions.

---

## The append-only model

Pernix never modifies stored messages. When the conversation gets long enough to threaten the context window, **compaction** kicks in — older messages get replaced in the prompt with a summarized digest, but the originals stay in the database. The UI always shows full history; only the *view* sent to the next LLM call is changed.

This means:

- Scrolling back through a long session always shows the original turns.
- A failed compaction can be retried without losing state.
- You can audit what the agent saw at any point via the timeline drawer.

Three triggers fire compaction:

| Trigger | When | What happens |
|---|---|---|
| **Proactive** | context > 75% full (`compaction_threshold`) | Compact at end-of-turn |
| **Critical** | context > 85% full (`context_critical_threshold`) | Compact mid-turn |
| **Overflow** | provider returns "context too long" | Compact and retry the failed call |

---

## Pausing, resuming, cancelling

You can intervene mid-turn at three granularities:

- **Cancel** — stops the entire turn. Ends in `CANCELLING → IDLE_READY` typically within seconds. The cancelled turn's tool results so far are kept in history, but no further work happens.
- **Pause a worker** — if the agent has spawned workers (sub-agents), you can pause individual ones at the next round boundary. The parent session enters `PAUSE_REQUESTED → PAUSED`. Resume later when ready. See [workers.md](workers.md).
- **Don't intervene; just wait** — most "stuck" sessions are reclaimed by the reaper within minutes. See [../faq.md#my-session-is-stuck-in-processing-what-do-i-do](../faq.md#my-session-is-stuck-in-processing-what-do-i-do).

---

## The 3-second rapid-fire merge

If you hit Send twice in quick succession (within 3 seconds), Pernix folds the second message into the running turn rather than starting a new one. This avoids the "I sent two messages and got confused" problem when typing fast.

If you wait longer, the second message queues behind the first turn and runs as its own turn after the first completes.

---

## Per-session model overrides

You can give a single session a different primary model than your global setting. Useful for:

- Running a research session on Claude Sonnet but a quick-script session on Qwen 3.
- Comparing two models on the same prompt — open two sessions with different `model_override`.

The override lives in `session.model_override` (not in global Settings). Set it by clicking the **model badge** in the status bar, via the UI session menu, or via `PATCH /api/sessions/{id}` with `{"model_override": "..."}`.

The other model roles (scout, fallback, background) follow the global setting; only the primary is overridable per session.

---

## Backpressure and the message queue

A session is single-threaded — only one turn runs at a time. If you send messages while one is processing, they queue in `pending_messages`. The cap is `max_pending_messages` (default 10).

If the queue fills, further submissions get rejected immediately with a `session.queue_full` SSE event. This is intentional — it prevents a runaway loop of submissions from piling up indefinitely. Adjust the cap if you have a workflow that genuinely needs more headroom.

---

## ask_user — when the agent waits for you

Some tools require human input. The most common is `ask_user`: the agent describes a question or a proposed dangerous action; the session enters `AWAITING_USER` and stops consuming LLM time. As soon as you answer (in the UI or via `POST /api/questions/{id}/answer`), a new turn begins from Scout.

The `ask_user` flow is what gates dangerous tools — see [../faq.md](../faq.md#why-does-the-agent-ask-me-to-confirm-things-like-web-searches-or-deleting-a-skill).
