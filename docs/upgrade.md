# Upgrade Guide

Pernix tries hard to upgrade transparently — `git pull && pip install -r requirements.txt && python run.py` is the happy path, and DB migrations run automatically on startup. This page covers the cases where you need to do something extra.

For the full list of changes by date, see [changelog.md](changelog.md). For DB schema details, see `db/database.py:MIGRATIONS`.

---

## The standard upgrade path

```bash
cd /path/to/pernix
git pull
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

That's it for most upgrades. The DB migrates forward automatically. Settings, memory, sessions, skills, and certs are preserved.

**Take a backup first** — see below. It takes a second and it is the only thing standing between a bad upgrade and losing everything the agent has accumulated.

---

## Backups

### Do not `cp` the database

`data/sessions.db` runs in **WAL mode**. The newest committed transactions live in `data/sessions.db-wal` until a checkpoint folds them back into the main file, which happens on its own schedule (`wal_autocheckpoint=1000`, roughly every 4 MB of writes, plus an hourly checkpoint from the maintenance heartbeat). So while the server is running:

- `cp data/sessions.db backup.db` gives you a snapshot that is **missing every write since the last checkpoint**. It will open cleanly and look fine. That is the dangerous part.
- `cp -r data data.backup` is no better: it copies `sessions.db`, `-wal` and `-shm` at three different instants, and the checkpointer can rewrite all three in between. The result can be torn — a `-wal` that no longer matches its database.

Earlier versions of this page recommended `cp -r data data.backup`. That advice was wrong for a running server, and is corrected here.

Copying the whole `data/` tree **is** fine when Pernix is stopped. If the server is up, use the backup script.

### The backup script

```bash
python scripts/backup.py           # snapshot + rotate
python scripts/backup.py --keep 30 # retain more generations
python scripts/backup.py --json    # machine-readable, for your own cron
```

Snapshots land in `data/backups/`:

```
data/backups/sessions-20260807-031500.db     # the database
data/backups/memories-20260807-031500/       # the markdown corpus
```

The database snapshot is taken with SQLite's `VACUUM INTO`, which runs inside a read transaction and writes a fresh, fully-checkpointed database file. It is consistent by construction and safe to take while the server is serving traffic. The result is an ordinary SQLite file: open it with `sqlite3`, copy it anywhere, restore it by putting it back.

Pernix also runs this automatically. `maintenance.py`'s 24-hour tier calls the same function, **first** in that tier — before the memory health check and the retention prunes, so the snapshot is the one that can undo a bad sweep. Retention is `backup_keep_count` (Settings, default **7**, clamped to 0–90). Setting it to `0` disables the scheduled backup; the CLI still works.

### What is and isn't in a backup

| Path | Backed up | Why |
|---|---|---|
| `data/sessions.db` | ✅ | Sessions, messages, goals, cron, adaptive/canary state. Irreplaceable. |
| `data/memories/**/*.md` | ✅ | The memory corpus. Markdown is the source of truth. |
| `data/memories/_index.db` | ❌ | FTS5 + vector index, derived from the markdown. The memory store's health check rebuilds it. |
| `data/workspace/` | ❌ | Agent scratch space, reproducible, and potentially huge (it can hold a venv). |
| `data/settings.json`, `.env`, `data/certs/` | ❌ | Configuration, not accumulated state — and the files holding your auth token and API keys. A rotating plaintext copy of your secrets next to the database is a liability. Back these up deliberately, wherever you keep secrets. |

`data/skills/` and `data/agent/` are also not included: they are hand-authored files you should be keeping in version control alongside your other configuration.

### Restoring

With the server **stopped**:

```bash
# 1. Move the live database aside rather than deleting it.
mv data/sessions.db data/sessions.db.broken
rm -f data/sessions.db-wal data/sessions.db-shm   # they belong to the old DB

# 2. Put the snapshot in place.
cp data/backups/sessions-20260807-031500.db data/sessions.db

# 3. Restore the matching memory corpus (same timestamp — they are a pair).
rm -rf data/memories && mkdir -p data/memories
cp -r data/backups/memories-20260807-031500/. data/memories/

# 4. Start. The FTS index rebuilds itself; migrations run if the snapshot is older.
python run.py
```

Deleting the stale `-wal`/`-shm` files in step 1 matters: leaving them next to a *different* database is how a restore turns into corruption.

Restoring a snapshot taken by an **older** Pernix is fine — migrations run forward on startup. Restoring one taken by a **newer** Pernix into older code is not; see the downgrade note below.

---

## DB migrations

The schema is at v29. Migrations run sequentially at startup based on the version stored in the `schema_meta` table (`key='schema_version'` — not the SQLite `user_version` pragma). Each migration is forward-only — there's no automatic downgrade.

If you ever need to downgrade Pernix to an older version (and therefore an older schema), the safe path is:

1. Take a snapshot: `python scripts/backup.py`.
2. Check out the older Pernix code.
3. **Use a fresh `data/` directory** with the older code, or restore from a backup taken at that version. Don't try to run an older Pernix against a newer DB.

Running newer Pernix against an older DB is fine — that's just a normal upgrade.

---

## Breaking changes worth knowing about

These are the upgrade points where something the user might have set up needs attention. Each is dated.

### 2026-08-26 — v3.0.0

This is the release where everything since v2.9.0 lands under one tag — including every dated entry below down to 2026-07. Migrations **v27–v29** ship with it and run automatically: v27 adds the `jobs` table, v28 makes answered questions an audit trail (`answer`/`answered_at` columns; rows are kept instead of deleted), v29 adds `rlm_runs.surfaced_at` (history backfilled). Nothing to do for any of them.

Two behavior changes are **on by default** and worth knowing:

- **Background jobs** (`jobs_enabled = true`): the agent gains `job_start` / `job_status` / `job_tail` / `job_kill` for detached long-running compute. Set `jobs_enabled = false` if you don't want the tools registered (restart required).
- **Round-cap auto-continuation** (`round_cap_auto_continue = 1`): a turn that exhausts `max_tool_rounds` while healthy (tools ran, no errors, no stuck spiral) gets one fresh round budget with a transcript notice. Set to `0` to restore the hard stop.

Smaller notes: `view_image` is a new safe tool (vision models only); the settings ceilings for `max_tool_rounds` and `rlm_max_iterations` rose to 1000 (defaults unchanged); a provider that 403s on an exhausted quota is excluded from failover for `provider_quota_cooldown_s` (default 600s).

### 2026-08-12 — The workflow engine was removed

**What's gone.** `run_workflow` and `cancel_workflow`, the `get_workflow_schema` / `create_workflow` / `discover_workflows` / `delete_workflow` / `validate_workflow` tools, the whole `/api/workflows` route family, the Workflows tab in the Explorer, the `workflows_dir` setting, and `WORKFLOW.md` parsing. Seven agent-facing tools in total — the boot line drops from 90 registered to 83 (builtin 35 → 30, extensions 55 → 53).

**Why.** It was never used. Not "rarely" — never: across the full message history of the reference deployment, from first boot to removal, `run_workflow` was called zero times, `create_workflow` zero times, `discover_workflows` zero times, and the `workflow_runs` table never held a single row. Six workflows sat parsed and registered at every boot for two months and not one ever ran. That is not a discoverability problem you fix with better prompting — the tool was in the agent's list the entire time.

The structural reason is that a workflow is a step graph you have to declare *before* the work starts, which is the one assumption an LLM agent lets you drop. Every capability built after it — goals, gates, heartbeats, worker specs — moved the other way, toward deciding the next step from what the last step actually returned. A declared graph cannot do that; when a step surprised it, its only options were retry, skip, or halt. Meanwhile it cost ~2,000 lines of dedicated code, a 1,400-line test file, and workflow-shaped conditionals threaded through `reflect.py`, `scout/runner.py`, `context/compiler.py`, `snooze.py` and `retention.py` — files you touch for unrelated reasons and had to reason around every time.

**How to do the same thing.** The capability is not gone, only the declarative wrapper around it:

| You want | Do this |
|---|---|
| A reusable multi-step procedure | Write it as a **skill** (`create_skill`) whose instructions list the steps in order. This is the durable, shareable artifact — same role `WORKFLOW.md` played, but the agent can deviate when a step surprises it. |
| Steps that must not pollute the main context | `spawn_worker(task, ...)` per step. Each worker gets its own context and its own scout, exactly as workflow steps did. |
| Steps that can run at the same time | Spawn them together and collect with `await_workers`. That is precisely what a workflow "wave" was. |
| Data passed between steps | Have each step write its output to the workspace and give the next step the path — the same `output_file` discipline, without the manifest. |
| A step on a stronger model | `spawn_worker(model=...)`, or a worker spec. This replaces per-step `model:`. |
| Hard pass/fail between steps | **Gates** — deterministic shell checks reflect cannot overrule. Stricter than the old per-step reflect verdict. |
| Run the whole thing on a schedule | `schedule_job` with a prompt that names the skill and its steps. Cron jobs that previously called `run_workflow` need their prompt rewritten this way. |
| A long autonomous run with a budget | **Goals** — persistent objectives with continuation and token budgets. |

**What you need to do on upgrade.** Three things, all small:

1. **Rewrite any cron job whose prompt calls `run_workflow`.** It will now fail as an unknown tool. Point the prompt at a skill instead. Check with: `grep -l run_workflow data/cron_jobs.json`.
2. **Update any skill that references the workflow tools by name.** Same failure mode.
3. **Re-point saved API calls** from `/api/workflows/proposals*` to `/api/skills/proposals*` (see below).

**Your data is untouched.** `data/workflows/` is left exactly as it is — nothing reads it any more, so you can keep the `WORKFLOW.md` files as reference while you convert them into skills, or delete the directory. Delete it whenever you like; nothing depends on it.

**No migration ran.** The `workflow_runs` table and the `workflow_name` / `run_id` columns on `skill_improvement_proposals` are still there — migrations are forward-only, so dropping them would be the one genuinely irreversible part of this change. They are simply never written now (new proposals store `NULL`), and historical rows stay readable.

**One feature moved rather than died.** Skill-improvement proposals — reflect and refine noticing that a skill under-performed, and offering a `SKILL.md` edit for you to review — were served from the workflows router and applied by `core/workflows/apply.py`, but had nothing to do with workflows beyond sharing a module. They now live where they belong:

| Old | New |
|---|---|
| `GET /api/workflows/proposals` | `GET /api/skills/proposals` |
| `POST /api/workflows/proposals/{id}/approve` | `POST /api/skills/proposals/{id}/approve` |
| `POST /api/workflows/proposals/{id}/reject` | `POST /api/skills/proposals/{id}/reject` |
| `POST /api/workflows/proposals/{id}/apply` | `POST /api/skills/proposals/{id}/apply` |

The Explorer's Skills tab already points at the new paths; only external callers need updating.

### 2026-08-07 — Scheduled backups; one-active-goal index (migration v26)

**Backups now happen on their own.** The 24-hour maintenance tier takes a `VACUUM INTO` snapshot plus a copy of the memory corpus into `data/backups/`, keeping `backup_keep_count` generations (default 7). Nothing to do on upgrade; set `backup_keep_count = 0` to turn it off. `python scripts/backup.py` does the same on demand. See [Backups](#backups) above — in particular, stop using `cp` on a running server.

**Migration v26 makes "one active goal per session" real.** v23 documented the invariant but shipped a non-unique index, and the accessor's check could be raced. v26 adds a partial unique index on `session_goals(session_id)` over the live statuses. If your database somehow accumulated duplicate active goals for one session, the migration retires the older ones to `status='error'` (the highest id per session — the one `get_active_goal` was already returning — is kept) before creating the index. Nothing to do; goal budgets that were split across duplicate rows will now read from one goal.

### 2026-08-07 — Three model roles; `schedule_workflow` removed

**Model roles collapsed from six to three.** `scout_model`, `reflect_model`, `critical_model`, `rlm_root_model` and `rlm_sub_model` no longer exist. Everything is now one of:

| Role | Setting | Covers |
|---|---|---|
| Primary | `llm_model` | agent turns + compaction, reflect, eval, RLM root |
| Background | `background_model` | scout, titles, distill, snooze, dream, telos, RLM sub-calls |
| Backup | `fallback_model` | any Primary or Background call that fails |

**What to do:** if you had a distinct `scout_model`, copy it to `background_model` before or after upgrading. Everything else folds into `llm_model` and needs no action. Stale keys left in `data/settings.json` are ignored rather than erroring, so an un-migrated file still boots — it just runs scout on your Primary model until you set `background_model`. Nothing in the UI references the removed keys any more.

Backup failover also got more useful: it now allows a **different model on the same provider**. An Ollama-primary / Ollama-backup configuration previously had no failover at all, because failover required crossing providers.

**`schedule_workflow` was deleted.** The tool was a thin scheduling wrapper whose metadata never persisted. Schedule a workflow the same way you schedule anything else: `schedule_job` with a prompt that runs the workflow. Existing cron jobs are unaffected — only the tool is gone. If a skill of yours calls `schedule_workflow` by name, update it.

Two smaller defaults changed: `max_tool_rounds` is now **50** (was 10 — low enough that ordinary long tasks tripped the ceiling and had to be papered over by goal continuations), and view pruning is now budget-gated rather than unconditional (`view_prune_pressure`, `view_prune_keep_recent`, `view_prune_min_chars`). Neither needs action.

### 2026-08 — Autonomy, canary suite, adaptive layer (migrations v21–v25)

Nothing to do on upgrade: migrations v21–v25 run automatically at startup — v21 adds `cron_runs.fire_time` (claim-before-deliver scheduling), v22 the `gates` table, v23 `session_goals` plus `token_usage.goal_id`, v24 `canary_runs`, and v25 the `adaptive_*` tables. Every new feature is behind a flag that defaults **off** (`gates_enabled`, `goals_enabled`, `heartbeats_enabled`, `session_kernel_enabled`, `canary_enabled`, `adaptive_enabled`; semantic retrieval activates only when `embedding_model` is set), so behavior is unchanged until you opt in. The new OpenAI-compatible provider likewise stays invisible until `OPENAI_API_KEY` is set.

If you plan to enable the self-improvement pair, follow the burn-in order: **`canary_enabled` first** and let nightly sweeps build a baseline for at least a week, **then** `adaptive_enabled` (initially with auto-apply off). See [internals/canary-and-adaptive.md](internals/canary-and-adaptive.md#burn-in--the-recommended-order).

### 2026-07 — RLM (recursive processing) add-on, off by default

Nothing to do on upgrade: migration v18 adds the `rlm_runs` table automatically (v17, from the same period, adds session pinning). If you want the feature, enabling `rlm_enabled` in Settings → General requires a **restart** — the `rlm_process` tool registers at startup only. RLM has no model settings of its own: the root runs on your Primary model and sub-calls on Background. Runs leave residue in `data/workspace/rlm/<run_id>/`; snooze purges it after `rlm_run_retention_days` (default 30), and it's safe to delete by hand.

### 2026-05-05 — `data/agent/AGENTS.md` renamed to `SESSIONS.md`

If you customized your agent identity files and have an `AGENTS.md`, the file is now expected at `SESSIONS.md`. Rename it:

```bash
mv data/agent/AGENTS.md data/agent/SESSIONS.md
```

The system falls back to `INSTRUCTIONS.md` if `SESSIONS.md` is absent, so an old install with neither file just gets the empty default. Existing `AGENTS.md` files are not auto-renamed.

### 2026-05 — `search_web` now requires `TAVILY_API_KEY`

If you were relying on the DuckDuckGo fallback (which existed in earlier versions), it's gone. Set `TAVILY_API_KEY=tvly-...` in `.env` to keep web search working. Tavily has a free tier — sign up at [tavily.com](https://tavily.com).

Without the key, `search_web` returns a setup hint instead of failing silently or degrading.

### 2026-04-20 — Session state machine v2

The 5-state legacy enum (`IDLE | SCOUTING | PROCESSING | ERROR | DELETED`) was replaced with a 10-state machine. Existing sessions migrate to the new states automatically (migration v13 adds `session_state_log`; migration v16 persists `state_v2` and `watched_worker_ids`).

If you have **external integrations that read session state** via the REST API, the `state` field returned by `GET /api/sessions/{id}/status` now uses the new 10-value enum. A `compat_status` field is provided for legacy 3-value compat (kept for the CLI's benefit).

If your integration only reads via SSE events, no change — `session.state_changed` events flow as before with new state values.

### 2026-04-13 — Network mode introduced

If you ran older Pernix with `--host 0.0.0.0` to expose it to your LAN, that's no longer enough. Pernix now requires `network_enabled = true` in Settings (which forces HTTPS + Bearer token + SSRF lockdown). Plain HTTP-on-LAN is no longer supported.

Migration steps:
1. Restart with `network_enabled = true` in Settings.
2. Pernix auto-generates a self-signed cert and a Bearer token on first start.
3. Update your existing clients to use the new token (printed in the logs the first time, or visible at `GET /api/settings/auth-token` from localhost).

For a smoother mobile experience, replace the self-signed cert with mkcert — see [deployment/mkcert.md](deployment/mkcert.md).

### 2026-04 — `auto_approve_dangerous` is now CLI-only

Earlier versions allowed `auto_approve_dangerous = true` to be set via Settings or `.env`. It's now **only** settable via the `--dangerous` command-line flag at startup. This prevents a remote API client (or a prompt injection) from silently elevating its own privileges.

If you previously set `auto_approve_dangerous = true` in `data/settings.json`, the value is ignored. Use `python run.py --dangerous` if you genuinely want to bypass the gate.

For unattended cron jobs, no flag needed — cron sessions auto-skip the gate via `_is_unattended_session()`.

### 2026-04-03 — Initial v2 spec

If you were running a very early pre-release version of Pernix, there's no automatic upgrade path from that era. The data layer changed enough that a clean `git clone` of the current code with a fresh `data/` directory is the supported path. You can copy individual `data/skills/` and `data/memories/` files across if they're useful.

---

## After-upgrade housekeeping

After any upgrade, a couple of things are worth checking:

- **Did the server start cleanly?** Watch the startup logs — migration failures or schema mismatches show up there.
- **Is your model still accessible?** `GET /api/health/detailed` (localhost-only) shows provider connectivity.
- **Did your skills survive?** `data/skills/` is preserved across `--rebuild` and across normal upgrades, so they should be fine. If a skill stops loading, check the YAML frontmatter — syntax errors silently skip the skill (the error is logged).
- **Are your custom tools still present?** Custom tool files (`core/tools/builtin/custom_*.py`) are preserved. If a tool depended on a Python package that's no longer in `data/workspace/.venv/` (e.g. after `--rebuild` wiped the workspace venv), run the agent's `restore_tool_packages` to reinstall.

---

## When something goes wrong

- **Schema mismatch errors at startup** — usually means you upgraded but didn't run the new code yet, or you copied an older `sessions.db` over a newer one. Check `git log -1` confirming you're on the version you expect.
- **"Module not found" Python errors** — you forgot to `pip install -r requirements.txt` after `git pull`.
- **Settings UI shows fields you didn't have before** — that's normal; new releases add settings. Defaults are sensible.
- **Sessions all stuck in old state names** — restart again. The state-machine v2 migration runs at startup and reconciles stuck states.

If something genuinely broke and you can't proceed, the nuclear option:

```bash
# Back up first! (see Backups above — do NOT cp a live WAL database)
python scripts/backup.py

# Wipe runtime state, keep settings + .env + skills + agent identity + certs
python run.py --rebuild
```

`--rebuild` requires typing `yes` to confirm. It deletes sessions, memory, workspace, and logs but preserves everything you configured.
