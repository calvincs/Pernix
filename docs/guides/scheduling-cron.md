# Scheduling and cron

The **scheduling extension** runs sessions on a cron schedule. Useful for: morning news briefs, daily activity summaries, weekly housekeeping passes, watchdog scripts that check for changes and alert.

Jobs are stored in `data/cron_jobs.json` and persisted across restarts. Each job has a name, a cron expression, and the prompt that should fire when it runs.

---

## Creating a job — by chatting

The natural way:

> *"Every weekday at 8 AM, search for the day's top tech news, summarize the three most relevant items for me, and save the output to my workspace."*

The agent calls `schedule_job` with a generated cron expression. Confirm when it asks; the job's now scheduled.

---

## Creating a job — explicit tool call

If you want to specify the cron expression yourself:

```python
schedule_job(
    name="morning-brief",
    cron_expr="0 8 * * 1-5",
    prompt="Search for top tech news today. Summarize the three most relevant items. Save to data/workspace/projects/daily-brief/.",
)
```

Cron expression is the standard 5-field syntax: `minute hour day month weekday`. Examples:

| Schedule | Meaning |
|---|---|
| `0 8 * * 1-5` | 8:00 UTC, Monday through Friday |
| `*/15 * * * *` | Every 15 minutes |
| `0 0 1 * *` | First of every month at midnight UTC |
| `30 14 * * 0` | Sundays at 14:30 UTC |

---

## What happens when a job fires

1. The scheduler creates a new session at the cron time.
2. `prompt` is sent as the first user message.
3. The session runs through scout → agent loop → reflect → finalize, just like any chat session.
4. Output (text response, files, memory entries) lands as you'd expect.

Cron sessions are marked **unattended**. Two consequences:

- **The dangerous-tool gate is bypassed.** There's no human present to answer `ask_user`, so the gate would deadlock. Cron-spawned workers (parent → worker) inherit this.
- **`ask_user` calls fail fast** rather than blocking forever. The agent has to make a decision without your input.

Both behaviors live in `_is_unattended_session()` in `core/tools/executor.py`.

---

## Managing scheduled jobs

| REST | What |
|---|---|
| `GET /api/jobs` | List all jobs |
| `POST /api/jobs` | Create a new job (same args as `schedule_job`) |
| `POST /api/jobs/{name}/pause` | Pause an enabled job (won't fire until resumed) |
| `POST /api/jobs/{name}/resume` | Resume a paused job |
| `POST /api/jobs/{name}/validate` | Re-check the job's spec (cron, prompt, tools, model) |
| `POST /api/jobs/{name}/test` | Dry-run the job once in an isolated workspace (result → notification) |
| `DELETE /api/jobs/{name}` | Delete the job |

Or via tools the agent can use:

- `list_scheduled_jobs`
- `update_scheduled_job` (change schedule or instructions)
- `set_job_state` (pause/resume)
- `test_job` (isolated dry-run — prove the job works before it fires)
- `remove_scheduled_job`

The UI's jobs panel shows the list with next-run times, status, a spec badge
(`valid` / `invalid` / `unvalidated`), the last test outcome, and a **test**
button per job.

### Validation and test-runs

Creating or editing a job validates the spec first: an unparseable cron
expression, a trivial prompt, or an unknown tool in `allowed_tools` is
rejected at save time with the reasons spelled out — not discovered weeks
later when the job silently fails on schedule. A model that isn't in the
local registry is a warning, not a blocker (remote ids can still resolve at
dispatch).

`test_job` (or the panel's **test** button) then proves the job actually
runs: the prompt fires once in a throwaway temp workspace under the job's own
model and tool allow-list, bounded at 5 minutes. Nothing is recorded in the
run history and the schedule is untouched. The transcript survives as a
session titled `Job test: <name>`, and a job that calls `ask_user` is flagged
— an unattended fire would hang on that question forever.

---

## What can you do in a cron session?

Anything a normal session can — including spawning workers, calling skills, writing files, sending notifications. The unattended bypass means the agent can run search, browse, file deletions, etc. without you confirming each call.

Practical patterns:

- **Daily research brief** — `search_web` + summarization + write to workspace.
- **Watchdog with alert** — `http_get` a status page or RSS feed; if a value crosses a threshold, fire `notify_webhook_url` (set in Settings).
- **Weekly memory consolidation report** — read your memory store, summarize what's been learned this week, save to workspace.
- **Workspace tidy-up** — schedule a recurring "organize the workspace" session (or a skill you've written for it).

For end-to-end recipes, see [recipes.md](recipes.md).

---

## Notifications

If you want to know when a cron job did something:

- **Webhook** — set `notify_webhook_url` in Settings. The agent will POST to it whenever `ask_user` fires (which in unattended mode is rare, but happens for explicit user confirmation requests).
- **Web Push** — if you've subscribed via the UI, push notifications fire on `ask_user`.
- **Workspace files** — the agent can write a file the cron job creates; you find it in the Explorer's Files → Workspace tab next time you check.
- **A follow-up cron job** — schedule a 9 AM "what did the 8 AM job produce?" session.

---

## Limits and gotchas

- **Max 5 concurrent workers** (`max_concurrent_workers`) applies to cron sessions too. If your daily brief tries to spawn 10 parallel research workers, the spawns past the cap fail immediately with an error — there's no queue — until earlier workers complete.
- **`llm_session_timeout`** (default 1800 seconds) caps a cron session's total LLM time. A long-running cron session that would blow the budget gets terminated cleanly.
- **No retry semantics** — if a cron job fails (network error, model down), it just fails for that run. The next run fires on schedule. Build retry into your prompt if you need it.
- **Crash safety** — each run's row is claimed in the DB before the prompt is dispatched, and any ticks missed while the server was down are coalesced into a single catch-up run at startup rather than replayed one by one.
- **Time zone** — cron times are UTC. The scheduler and every trigger are pinned to UTC regardless of the host's `TZ`, so convert your local times when writing expressions. `data/agent/SESSIONS.md` typically records your timezone for the agent's awareness, but the scheduler itself doesn't use it.

---

## Disabling all cron

Either pause each job (`/pause`) or delete `data/cron_jobs.json`. See [../internals/extensions.md](../internals/extensions.md) for how the scheduling extension registers.
