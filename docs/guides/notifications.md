# Notifications

Pernix does a lot on its own — it grades its answers, tunes its own habits, runs a canary suite, dreams over its memory. Almost all of that needs nothing from you, so almost none of it should interrupt you. Every notification belongs to a **category**, and each category has one **tier** that decides how loudly it speaks.

| Tier | What you get | Used for |
|---|---|---|
| **Interrupt** | A number on the bell, a live desktop notification, and a Web Push / webhook to your phone | The agent needs you, or you must be told: a question, a failed job, a turn that stopped and needs a reply, a goal that ran out of budget, money (the fallback model carrying the load) |
| **Bell** | A quiet item in the bell panel and a small dot on the bell — never a number, never a push | Worth a look, not worth a buzz: a canary parked, an embeddings outage, an MCP server that stopped answering, a skill that was rolled back for you |
| **Log** | A line in the **Activity** tab — never a badge | Receipts for self-maintenance that already has its own tab: skill auto-applies, dream corrections, canary upkeep, space suggestions |
| **Drop** | Nothing recorded | Reserved for overrides, and for synthetic canary and worker sessions (see below) |

Questions from the agent (`ask_user`) are separate from this: they live in the question panel, always count toward the badge and always push.

## Where to look

Open the bell. It has two tabs:

- **Needs you** — open questions and open interrupt items first, then the quiet bell items. *Dismiss* removes one; *Clear* removes them all.
- **Activity** — everything, newest first, grouped by day, with filter chips per area. A line that repeated shows `×N`. A line with a link opens the session or the tab it is about. The header says how many lines are new since your last visit — that is the daily summary; there is no digest push.

**Dismiss never deletes.** A dismissed item leaves *Needs you* and stays in *Activity* until the retention window ends (`notification_retention_days`, default 30). Open interrupt items are never pruned.

Some items close themselves when their cause goes away — the embeddings outage when the server answers again, an MCP alert when the connection is back, a parked canary when it is un-parked, a tripwire flag when the batch is cleared. They leave the bell and stay in *Activity* marked resolved.

**One rollup instead of many.** Skill proposals that wait for *your* decision (changes that fail the safety check, or every pending one when auto-apply is off) are summed into a single item — "N skill proposals wait for your decision" — that opens the Skills tab and clears at zero.

## Sessions that are not your conversation

- **Canary and worker sessions** raise no reflect, timeout, stream-error or `notify_user` alert at all. A canary run is synthetic and the Canary tab records its result; a worker reports to the session that spawned it.
- **Cron sessions** fall one step: a stopped turn or a timeout in a scheduled job is a bell item, because a failed *job* already interrupts (`jobs.failed`).
- **`notify_user`** from the agent is quiet while you are in the session. From a cron or background session — or when the agent marks it `high` — it interrupts, at most three times per session per hour; further ones that hour are delivered quietly.

## Changing it

Settings → Integrations → **Notification tiers** has one choice per **area** (Default, Interrupt, Bell, Log only, Off). The default needs no tuning; override an area only if you disagree with it. In `settings.json`:

```json
{ "notify_tier_overrides": { "canary": "bell", "system.mcp_down": "interrupt" } }
```

A key is an area (`canary`) or a full category (`canary.parked`); the category wins when both are set. A key for a retired area (`adaptive`, removed with the adaptive layer in 3.2) is dropped silently when settings are saved. `notify_tiers_enabled: false` is the kill switch: every notice goes back to a bell row with its old urgency and its old channels — no restart needed. `push_urgency_floor` still applies on top of the tiers (set it to `urgent` to silence the phone entirely; questions keep pushing).

## Phone push

Web Push needs a real VAPID contact. Set `vapid_subject` to a `mailto:` address you own or to `https://…` — **not** the default `mailto:admin@localhost`: Apple's push service rejects placeholder subjects, and Pernix logs a warning at start-up when it sees one. A subscription is deleted only when the push service says it is gone (HTTP 404/410). A 401 or 403 means *our* credentials were refused, so the subscription is kept, the status code and reason are logged, and after three refusals in a row one bell item ("Push rejected by …") tells you. `GET /api/health` shows the running counters (`push.push_sent_ok`, `push_rejected`, `push_gone`, `push_failed`).

## Every category

Generated from `core/notices.py`. *Session types* lists where a category's tier differs from its default.

| Category | Tier | Session types / notes |
|---|---|---|
| `agent.notify_user` | bell | canary → drop |
| `agent.notify_user_urgent` | interrupt | canary → drop |
| `external.message` | bell | `POST /api/notify` with normal urgency |
| `external.message_urgent` | interrupt | `POST /api/notify` with `high`/`urgent` |
| `sessions.reflect_attention` | interrupt | cron → bell, snooze → log, canary/worker → drop |
| `sessions.reflect_followup` | bell | the answer was already delivered; snooze → log, canary/worker → drop |
| `sessions.timeout` | interrupt | cron → bell, snooze → log, canary/worker → drop |
| `sessions.goal_budget` | interrupt | cron → bell |
| `sessions.stream_error` | interrupt | snooze → log, canary/worker → drop |
| `jobs.failed` | interrupt | |
| `jobs.uncertain_after_restart` | bell | |
| `jobs.test_passed` / `jobs.test_failed` | log / bell | the Jobs tab already shows a test's result |
| `review.pending` | bell | one coalesced row counting the skill proposals only you can decide; opens the Skills tab, resolves at zero |
| `canary.contaminated`, `canary.probe_retired`, `canary.suite_chronic`, `canary.maintenance`, `canary.auto_admitted`, `canary.stale` | log | the Canary tab is the surface |
| `canary.parked`, `canary.suite_unhealthy` | bell | coalesce; resolved when un-parked / healthy |
| `skills.verify_unsafe`, `skills.auto_rolled_back` | bell | |
| `skills.rolled_back`, `skills.proposals_auto_applied` | log | |
| `dream.corrections_applied`, `dream.queue_stalled`, `dream.promotion_stalled` | log | |
| `spaces.suggested` | log | the suggestion row in the sidebar is the surface |
| `system.fallback_burn` | interrupt | the fallback model is carrying the load |
| `system.embeddings_down`, `system.mcp_down` | bell | coalesce; resolve when the service is back |
| `system.embeddings_switched`, `system.embeddings_recovered` | log | |
| `system.tavily_key`, `system.tavily_limit`, `system.memory_oversized`, `system.push_rejected` | bell | |

## For contributors

Never call `db.add_notification` — call `core.notices.notify(category, title, body, …)` and, when the cause goes away, `core.notices.resolve(category, subject)`. A new kind of notification is a new entry in `CATEGORIES` with one tier; a test fails if a call site names an unregistered category or writes to the table directly. `notify()` never raises and never calls a model, and only the interrupt tier reaches the event bus, so a notice can never cause another notice.
