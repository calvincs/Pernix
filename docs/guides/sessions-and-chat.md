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

- **+ new** in the sidebar header starts a fresh thread. Each session has its own memory recall scope and its own workers.
- **Switch** by clicking another session in the sidebar — your prior session keeps its state in the background.
- **Clear** the current session's messages by typing `/clear` in the composer; it asks first, and the session itself remains in the DB. **Delete** a session with the `×` on its sidebar row, or on touch from the row's `⋯` sheet. Either way a dialog names the session and how many messages go with it, and the delete then sits behind a five-second **Undo** in the toast before it reaches the server.
- **Archive** a session — the box icon in the row's controls, or **Archive** in the `⋯` sheet — when you are done with it but not done *with* it. See [Archiving](#archiving-instead-of-deleting) below.

Programmatically:

| Endpoint | Effect |
|---|---|
| `POST /api/sessions` | Create a new session |
| `PATCH /api/sessions/{id}` | Rename, pin, move to a space, or archive/restore |
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
| **Scheduled** | A scheduled job run (internally: *cron*) — each firing gets its own session. |
| **Worker** | A sub-agent spawned by another session. |
| **Dream** | The idle-time introspection journal (see below). |
| **Large-input** | A live view of one `rlm_process` run (internally: *RLM*), nested under the session that launched it (see below). |
| **Self-check** | A canary run — only present once the canary suite is enabled. |

Click a legend entry to hide or show that type — useful when scheduled runs start to crowd out your own threads. **Hiding a type changes what is fetched, not just what is drawn.** The list holds the 500 most recently updated sessions, and on an instance that runs canaries, workers and scheduled jobs, most of those 500 are machinery: switching a type off used to blank its rows and leave the page that much shorter, with your own older chats still stranded behind **Load older sessions**. Now the request itself leaves the type out, the page refills with what is left, and the count on the legend entry keeps naming the whole population so you can always switch it back on. The filter persists across reloads and applies to the archived group too.

A seventh entry, **Archived**, appears once anything is archived; it is a place rather than a type, and toggling it shows the archived group at the foot of the list. Each entry's tooltip names the internal term the settings and docs use. **Load older sessions** at the end of the list fetches the next page and says how many remain.

**Spaces have the same time buckets.** Below the space's own header, its sessions are grouped into *Pinned / Today / Yesterday / This Week / This Month / Older* just like the list underneath — smaller sub-headers, indented, showing a count only while folded. *Older* starts folded, and *This Month* joins it once a space holds more than 15 sessions; a space whose sessions all land in the same bucket shows no sub-headers at all, because a header that partitions nothing is noise. Past 15 sessions the space shows its most recent 15 and one **Show all N** row at the end, which unfolds the whole space (and becomes **Show fewer**). Every one of those choices is remembered per space by this browser, alongside the collapsed groups and the sidebar's width. The count on the space header always names every session in the space, folded or not.

On a desktop browser you can drag the sidebar's right edge to make it wider or narrower — 200 to 520 pixels, and never more than 45% of the window. Double-click the edge to go back to the default width; with the edge focused, the arrow keys move it 16 pixels at a time and Home and End jump to the narrowest and widest. The width is remembered by this browser, the same way the collapsed state is (the toggle at the top of the chat pane hides the sidebar altogether). Phones and tablets keep their own sidebar sizes. A row's own controls — copy id, pin, rename, move to space, archive, delete — overlay the right end of the row on hover or keyboard focus rather than sitting beside the title, so the extra width all goes to the title. (On touch there is no hover: the row carries one always-visible `⋯` instead.)

Finding things:

- The sidebar **search box** is full-text over all message content — it finds any past conversation, not just titles. It shows the top 20 matching sessions and says so when there are more.
- **Ctrl+K** opens the command palette. It lists every session, and above them the things you would otherwise go hunting for a button for: a new session (in a space or not), **Open Explorer →** any tab, **Settings**, **Clear conversation**, **Toggle theme**. Matching is a plain substring (not fuzzy) over a session's title and first message, or a command's name and aliases, so type a run of characters that actually appears; with an empty box it is still a plain session switcher.
- **Ctrl+F** searches the transcript on screen. A session opens on its most recent 200 messages, so **Load earlier messages** at the top brings older ones into range first.
- **↑** in an empty composer recalls your message history.

(`/help` in any chat lists all of these.)

---

## Archiving instead of deleting

Deleting used to be the only way to get a finished conversation out of the sidebar, which made a year of chats a choice between clutter and losing the transcript. **Archiving is the third answer.** An archived session:

- leaves the session list and its space group,
- keeps **every message** — nothing is summarized, trimmed or dropped,
- stays findable in the sidebar's full-text search, where the hit is marked *archived*,
- opens read-only, with the composer disabled and a **Restore** button above it.

Restoring is one click and takes effect on the page you are already reading — no reload. Neither archiving nor restoring changes a session's `updated_at`, so a restored chat goes back exactly where it was in the list rather than jumping to the top.

**Where the controls are.** On a mouse, the row's hover overlay gains a box icon between *move to space* and *delete*; on touch the row's `⋯` sheet gains **Archive** (or **Restore**) directly above *Delete*. A space header offers **Archive idle sessions…**, which asks first and names the number — that number comes from a dry run, so it is exactly what the confirm then archives.

**Where they go.** The sidebar legend grows an **Archived (N)** entry once anything is archived. Turning it on adds one collapsed **Archived** group at the foot of the list whose rows offer **Restore** in place of *Archive*; the choice is remembered by this browser like the type filters are. The Ctrl+K palette lists live sessions only — search is how you reach an archived one by name.

**Automatically.** Ordinary chats idle for more than `session_archive_idle_days` (default 30) are archived by a snooze sweep. Pinned chats are exempt. Space sessions are **not** exempt — the rule that spares them from every delete sweep is about never losing a transcript, and archiving loses nothing. Set the knob to `0` to turn the sweep off and use the archive purely by hand.

Sessions that have been archived for longer than `session_delete_archived_days` are hard-deleted. That knob is `0` — never — by default, and deliberately: the archive is what *not deleting* means, so putting a horizon on it is you opting back in to losing transcripts. Both knobs live in **Settings → Storage**.

---

## Dream journal sessions

When [Dream introspection](../internals/dream.md) is enabled, each day of dreaming narrates itself into a day-keyed journal session ("Dream Jul 31") — hypotheses raised, verdicts, report writes. These sessions are **read-only**: the composer is disabled and the server rejects messages. They're excluded from search and memory distillation, and pruned after `dream_journal_retention_days` (default 14). Filter them out with the sidebar legend if you'd rather not see them.

---

## RLM run views

When the agent kicks off an [RLM run](../internals/rlm.md) (`rlm_process` over an input too large to read inline), the run appears in three places:

- A **live chip** in the worker strip above the launching session's composer — `RLM · it 7/20 · 6 calls · 4m10s` — pulsing while the run works, alongside any worker chips. Below 900px the strip is one summary line instead, counting the runs. The parent transcript also gets start/finish lines.
- A nested **RLM session** in the sidebar under its parent (same collapsible group as workers), with a pulsing dot while running.
- Clicking either opens the **trace viewer** in place of the chat: the root model's per-iteration reasoning, each REPL cell (collapsible, with code and stdout/stderr), every sub-LLM call with latency, live iteration/sub-call/elapsed progress against the run's caps, and the final answer once the run ends. The view tails the trace live and doubles as the permanent record afterward.

Like dream journals, RLM sessions are **read-only** — there's no transcript to chat in; the conversation lives in the parent session. Deleting one also deletes the run's on-disk trace; runs older than `rlm_run_retention_days` (default 30) are purged together with their view sessions.

---

## The append-only model

Pernix never modifies stored messages. When the conversation gets long enough to threaten the context window, **compaction** kicks in — older messages get replaced in the prompt with a summarized digest, but the originals stay in the database. The UI always shows full history; only the *view* sent to the next LLM call is changed.

This means:

- Scrolling back through a long session always shows the original turns — the transcript opens on the last 200 messages and **Load earlier messages** at the top fetches the page before, saying how many are left.
- A failed compaction can be retried without losing state.
- You can audit what the agent saw at any point via the **State timeline** — the state badge in the status bar opens it.

Three triggers fire compaction:

| Trigger | When | What happens |
|---|---|---|
| **Proactive** | context > 75% full (`compaction_threshold`) | Compact at end-of-turn |
| **Critical** | context > 85% full (`context_critical_threshold`) | Compact mid-turn |
| **Overflow** | provider returns "context too long" | Compact and retry the failed call |

---

## Pausing, resuming, cancelling

You can intervene mid-turn at four granularities:

- **Cancel** — stops the entire turn. Ends in `CANCELLING → IDLE_READY` typically within seconds. The cancelled turn's tool results so far are kept in history, but no further work happens. In the UI the send button turns into a **Stop** button for the length of a turn; `/cancel` in the composer does the same thing. Cancelling is cooperative — the agent finishes the step it is on — so the button greys out and the status bar reads "Stopping…" until the server confirms.
- **Pause the session** — the pause button appears in the status bar while a turn is running and takes effect at the next round boundary; mid-tool-call work is not interrupted. The same button resumes.
- **Pause a worker** — if the agent has spawned workers (sub-agents), you can pause individual ones at the next round boundary. The worker session enters `PAUSE_REQUESTED → PAUSED`; the parent is untouched. Resume later when ready. See [workers.md](workers.md).
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

The override lives in `session.model_override` (not in global Settings). Set it by clicking the model name in the status bar — it opens a menu of every model your providers offer, grouped by provider, as a bottom sheet titled *Model for this session* below 900px. Or via `PATCH /api/sessions/{id}` with `{"model_override": "..."}`.

The other two model roles (Background and Backup) follow the global setting; only Primary is overridable per session.

---

## Backpressure and the message queue

A session is single-threaded — only one turn runs at a time. If you send messages while one is processing, they queue in `pending_messages`. The cap is `max_pending_messages` (default 10).

If the queue fills, further submissions get rejected immediately with a `session.queue_full` SSE event. This is intentional — it prevents a runaway loop of submissions from piling up indefinitely. Adjust the cap if you have a use case that genuinely needs more headroom.

---

## ask_user — when the agent waits for you

Some tools require human input. The most common is `ask_user`: the agent describes a question or a proposed dangerous action; the session enters `AWAITING_USER` and stops consuming LLM time. As soon as you answer (in the UI or via `POST /api/questions/{id}/answer`), a new turn begins from Scout.

The `ask_user` flow is what gates dangerous tools — see [../faq.md](../faq.md#why-does-the-agent-ask-me-to-confirm-things-like-web-searches-or-deleting-a-skill).
