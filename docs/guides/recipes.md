# Recipes

End-to-end examples that compose multiple Pernix features. Each recipe is something you can copy-paste into a chat to set up.

For per-feature deep dives, see the rest of the [guides/](.) folder.

---

## 1. Daily news brief

**What it does:** every weekday at 8 AM, search the web for the day's top stories in three topics you care about, write a 5-minute-read brief to your workspace, and save the original sources for later reference.

**Features used:** scheduling-cron + `search_web` + workspace writes + memory.

### Setup

In a fresh chat:

> *"Set up a daily news brief. Every weekday at 8 AM, do this:*
>
> *1. Search for the top stories today in three topics: AI policy news, semiconductor industry news, and energy markets.*
> *2. For each topic, pick the 2 most consequential stories and summarize each in 3–4 sentences. Note source and date.*
> *3. Save the brief to `data/workspace/projects/daily-brief/YYYY-MM-DD.md`.*
> *4. Save a list of all source URLs you visited to `data/workspace/projects/daily-brief/sources/YYYY-MM-DD.txt` in case I want to dig deeper.*
> *5. Remember any unusually significant story for the next day's recall — if I ask about it tomorrow, you should remember it."*

The agent will ask you to confirm the cron expression and configure the job. Done — it'll fire every weekday morning.

### Why this works as cron

Cron sessions skip the dangerous-tool gate, so `search_web` runs without prompting. The agent has free use of memory, file writes, and the web search.

---

## 2. Parallel research with synthesis

**What it does:** investigate three angles of one question simultaneously using parallel workers (different models for different parts), then synthesize into a single document.

**Features used:** workers + `search_web` + per-worker model selection + memory.

### Prompt

> *"I want to understand the trade-offs between SQLite, DuckDB, and Postgres for an analytics-on-personal-data tool I'm building. Spawn three workers in parallel:*
>
> *— Worker A: investigate SQLite specifically. Use Claude Sonnet — it'll need depth.*
> *— Worker B: investigate DuckDB. Same model.*
> *— Worker C: investigate Postgres. Same model.*
>
> *Each worker should research the database, then write a 500-word evaluation focused on: install footprint, single-user analytics performance, embeddability, ecosystem maturity, and one weakness. Then merge their findings into a single comparison document with a recommendation, save it to `data/workspace/projects/db-research/comparison.md`."*

### What you'll see

Three workers spawn (a Workers card in the parent transcript, and chips in the strip above the composer). Each runs independently, calls `search_web` and `browse_web` as needed, drafts its evaluation. The parent waits with `AWAITING_WORKERS`. As workers complete, the parent reads each result, synthesizes, and writes the final file.

This gets the parallelism benefit (research happens 3× faster) and isolates context (each worker keeps its own scope, so you don't spend tokens on three databases all loaded into one prompt).

---

## 3. Phone-in research assistant

**What it does:** access Pernix from your phone over your home LAN. Ask questions, ask it to search, ask it to summarize a webpage. Get push notifications when it needs your input.

**Features used:** network mode + mkcert + Web Push + browser tools.

### Setup (one-time, on the server machine)

1. **Install mkcert** and generate a trusted cert. See [../deployment/mkcert.md](../deployment/mkcert.md).
2. **Set network mode** in Settings: `network_enabled = true`, `ssl_mode = custom`, point `ssl_cert_path` and `ssl_key_path` at your mkcert files.
3. **Restart**: `python run.py --qr`.
4. **Scan the QR** with your phone. The token gets stored in your phone's browser localStorage.
5. **Subscribe to push notifications** in the UI on your phone (one-tap).

Now Pernix is reachable at `https://<your-server-ip>:8090` from any device on your LAN. The token-from-URL flow keeps you logged in.

### Daily use

Open Pernix from your phone, ask anything. Long-running tasks (browse, large research) can run while you walk away — when the agent calls `ask_user`, you get a push notification. Tap it to answer.

### Don't expose this to the public internet

Network mode is for trusted LANs only. If you need remote access from outside your network, use Tailscale or WireGuard rather than port-forwarding.

---

## 4. Weekly task triage with memory

**What it does:** every Monday morning, review your task list (kept as a memory entry), look up what you actually did last week (mining session history), and propose a refined plan for this week.

**Features used:** scheduling-cron + memory recall + session search + ask_user.

### Setup

> *"Set up a weekly task triage. Every Monday at 9 AM:*
>
> *1. Recall my current task list from memory (it's stored in `tasks.current.md` — create it if missing).*
> *2. Search session history from the past 7 days. Identify what I actually worked on (filter by user prompts that started a session, plus first agent response).*
> *3. For each task in my current list, mark it as: completed, in-progress, deferred, or stale. Use session activity as evidence.*
> *4. Propose a revised task list for this week, prioritized.*
> *5. Save the result to `data/workspace/projects/triage/YYYY-MM-DD.md` with sections: 'Last week', 'This week', 'Carryover', 'Deferred'.*
> *6. Update `tasks.current.md` with the new list — overwrite, don't merge.*
> *7. Don't ask me to confirm anything — just do it. I'll review the output later."*

The cron-session unattended bypass means the agent runs through this without `ask_user` blocking. Output lands in your workspace by the time you sit down for the week.

### Customization

If you want the agent to pause for input on tricky reclassifications, remove the "don't ask me" line — it'll then fire push notifications to your phone instead, and resume after you answer.

---

## 5. Authoring a one-shot skill from a recurring procedure

**What it does:** turn a multi-step prompt you keep typing into a reusable skill.

**Features used:** skillmaker.

### When to do this

If you find yourself prompting the same procedure more than 2–3 times — same tools, same output format — promote it to a skill. Future prompts can be one line: "use the X skill for Y."

### Prompt

> *"Every time I ask for a 'company brief' on a public company, I want you to do these steps in order:*
>
> *1. Search for the most recent 10-K or annual report.*
> *2. Use browse_web to extract the financial summary section.*
> *3. Search news from the past 60 days for material events (M&A, executive changes, regulatory action).*
> *4. Combine into a 600-word brief with these sections: 'Snapshot', 'Recent financials', 'Material recent events', 'Watchlist questions'.*
> *5. Save to `data/workspace/projects/companies/{ticker}.md`.*
>
> *Make this into a reusable skill called `company-brief` so next time I can just say "use the company-brief skill on AAPL". Use the skillmaker extension."*

The agent calls `create_skill` with the procedure as the L2 instruction body. After the next turn, the skill is loaded and discoverable.

For the SKILL.md format, see [../authoring/writing-skills.md](../authoring/writing-skills.md).
