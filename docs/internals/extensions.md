# Extensions

Pernix loads its non-core capability through an **extension** layer. Each extension is a Python module under `core/extensions/`. Extensions register tools into the agent's schema conditionally — based on settings, environment, and dependencies — so a fresh install with all defaults exposes a different surface than one with `browser_enabled=true` and a `TAVILY_API_KEY` configured.

This page is the inventory: what extensions exist, what each registers, and how each is gated.

For per-feature usage, see the relevant guide. For tool authoring, see [../authoring/custom-tools.md](../authoring/custom-tools.md).

---

## How extensions register

Each extension exposes a `register()` function called at server startup. `register()` reads settings + environment + available imports and registers zero or more tools into the global tool registry. Tools registered here behave the same as builtin tools — same safety levels, same execution path, same approval gating.

If a setting that gates an extension changes (e.g., turning `browser_enabled`, `candor_enabled`, or `rlm_enabled` on), Pernix needs a restart to pick up the change. The registry is built once per process.

---

## The twelve extensions

### `web`

`core/extensions/web/__init__.py`

| Tool | Safety | Gated on |
|---|---|---|
| `search_web` | dangerous | `web_search_enabled` (default `true`) AND `TAVILY_API_KEY` set — skips registration entirely when flag is off |
| `browse_web` | dangerous | `browser_enabled` (default `true`) AND Playwright/Chromium installed — skips registration entirely when flag is off |
| `http_get` | safe | always; max bytes capped by `max_fetch_size` (default 100 KB) |

In network mode, `http_get` and `browse_web` block RFC-1918 private IPs and loopback to mitigate SSRF.

### `orchestration`

`core/extensions/orchestration/__init__.py`

Always enabled. The worker model lives here.

| Tool | Safety | What |
|---|---|---|
| `spawn_worker` | safe | Create a parallel sub-agent on a chosen model |
| `check_workers` | safe | List workers and their states |
| `get_worker_result` / `get_worker_transcript` | safe | Fetch a finished worker's final response / full transcript |
| `message_worker` | safe | Inject a message into a running worker mid-turn |
| `set_worker_state` | safe | Pause/unpause a worker at the next round boundary |
| `cancel_worker` / `retry_worker` | safe | Stop a worker / respawn it with revised instructions |
| `await_workers` | safe | Block parent until specified workers settle |
| `notify_parent` | safe | Worker-side: push a status note up to the parent session |

See [../guides/workers.md](../guides/workers.md).

### `planning`

`core/extensions/planning/__init__.py`

Lightweight feature-tracker for spec-driven development. Always enabled.

| Tool | Safety | What |
|---|---|---|
| `add_feature` | safe | Add a feature/task to the active plan |
| `mark_feature_passed` | safe | Mark complete |
| `list_features` | safe | Show outstanding plan items |

`plan_review_timeout` (default 120 s) limits how long the planning extension waits for user review before timing out.

### `scheduling`

`core/extensions/scheduling/__init__.py`

Always enabled. Cron job lifecycle.

| Tool | Safety | What |
|---|---|---|
| `schedule_job` | safe | Create a recurring session |
| `update_scheduled_job` | safe | Modify schedule or instructions |
| `set_job_state` | safe | Pause/resume a job |
| `remove_scheduled_job` | safe | Delete a job |
| `list_scheduled_jobs` | safe | List all jobs |

Cron-spawned sessions are flagged unattended in `_is_unattended_session()` (`core/tools/executor.py`) and skip the dangerous-tool gate. Workers spawned from such sessions inherit that. See [../guides/scheduling-cron.md](../guides/scheduling-cron.md).

### `session_tools`

`core/extensions/session_tools/__init__.py`

Always enabled. Lets the agent introspect prior sessions.

| Tool | Safety | What |
|---|---|---|
| `list_recent_sessions` | safe | List most-recent sessions ordered by last activity (newest first) |
| `read_session_summary` | safe | Read the auto-titled summary + key points of a prior session |

Use `list_recent_sessions` for chronological queries ("what did we do today/yesterday?"). Use `search_sessions` (builtin) for topic lookups ("find sessions where we discussed X") — it runs FTS5 keyword search over message content, not timestamp filtering.

### `skillmaker`

`core/extensions/skillmaker/__init__.py`

Always enabled. Skill authoring without leaving the chat.

| Tool | Safety | What |
|---|---|---|
| `create_skill` | dangerous | Author a new SKILL.md from inside a chat |
| `update_skill` | safe | Modify an existing skill |
| `add_skill_script` | dangerous | Drop a script into a skill's `scripts/` |
| `add_skill_reference` | safe | Drop a doc into `references/` |
| `remove_skill_script` / `remove_skill_reference` | safe | Delete a script / reference |

`delete_skill` lives in builtin tools (`core/tools/builtin/skill_tools.py`) and is **dangerous**.

See [../authoring/writing-skills.md](../authoring/writing-skills.md).

### `toolmaker`

`core/extensions/toolmaker/__init__.py`

Always enabled. Custom Python tool authoring.

| Tool | Safety | What |
|---|---|---|
| `create_tool` | dangerous | Author a new tool: name, description, Python body (with a `register(reg)` function) |
| `update_tool` | dangerous | Modify an existing custom tool |
| `list_custom_tools` | safe | List user-authored vs builtin tools |
| `install_package` | caution | pip install into `data/workspace/.venv/` |
| `restore_tool_packages` | caution | Reinstall after a venv wipe |

Custom tools install packages into the workspace venv (`data/workspace/.venv/`), kept separate from the project venv. See [../authoring/custom-tools.md](../authoring/custom-tools.md).

### `evaluation`

`core/extensions/evaluation/__init__.py`

Mostly internal. Auto-evaluates outcomes when `eval_auto = true` (default `false`). Settings:

- `eval_auto` — enable automatic evaluation
- `eval_threshold` (default 0.7) — pass threshold
- `eval_max_retries` (default 2) — eval-driven retries per turn
- `eval_browser_verify` (default `false`) — use a headless browser to verify outcomes (useful for frontend changes)

Most users leave this off. Reflect (the always-on quality gate) covers most of the value.

### `model_mgmt`

`core/extensions/model_mgmt/__init__.py`

Internal. Provides tools the agent uses to introspect and switch models. Not user-facing in the typical sense, though `list_available_models` may surface in chats.

### `candor`

`core/extensions/candor/__init__.py`

| Tool | Safety | Gated on |
|---|---|---|
| `predict_reliability` | safe | `candor_enabled` |
| `why_reliability` | safe | `candor_enabled` |
| `reliability_questions` | safe | `candor_enabled` |

Operational-memory add-on (off by default): calibrated reliability tracking with an auditable evidence ledger. `register()` is a hard off-switch — with `candor_enabled=false` the tools don't exist, so toggling requires a restart; observation capture and the scout intel brief toggle hot. Store at `data/candor/`. Settings: [../configuration.md](../configuration.md#candor-operational-memory-add-on).

### `rlm`

`core/extensions/rlm/__init__.py`

| Tool | Safety | Gated on |
|---|---|---|
| `rlm_process` | caution | `rlm_enabled` |

Recursive long-input processing (off by default): analyzes inputs far beyond the context window in a sandboxed child REPL with brokered, budgeted sub-LLM calls. Same restart-gated registration pattern as candor; the `rlm_*` caps apply hot, and there are no RLM-specific model settings — the root uses Primary, sub-calls use Background. Run residue at `data/workspace/rlm/<run_id>/` (purged by snooze retention); audit rows in the `rlm_runs` table (migration v18). Architecture and security posture: [rlm.md](rlm.md).

### `telos`

`core/extensions/telos/__init__.py`

| Tool | Safety | Gated on |
|---|---|---|
| `telos_status` | safe | `telos_enabled` |
| `telos_ask` | safe | `telos_enabled` |
| `telos_goal_add` | safe | `telos_enabled` |
| `telos_goal_complete` | safe | `telos_enabled` |

The teleological layer's agent surface (off by default): read the drive state, mint Questions, grow the goal DAG, complete completable goals (which runs the Hevel discharge audit). Deliberately absent: trace-ledger writes, root re-expression, alarm clearing — see [telos.md](telos.md). Same restart-gated registration pattern as candor; the engine itself (snooze Activity 16, daily cron, post-task hook) gates hot on `telos_enabled`.

---

## Gating summary table

| Extension | Default state | Settings that gate it |
|---|---|---|
| web — `search_web` | available only with key | `web_search_enabled`, `TAVILY_API_KEY` |
| web — `browse_web` | on (needs browser binary) | `browser_enabled`, Playwright/Chromium |
| web — `http_get` | on | none |
| orchestration | on | none |
| planning | on | none |
| scheduling | on | none |
| session_tools | on | none |
| skillmaker | on | none |
| toolmaker | on | none |
| evaluation | mostly off | `eval_auto` |
| model_mgmt | on | none |
| candor | off | `candor_enabled` (tool registration restart-gated) |
| rlm — `rlm_process` | off | `rlm_enabled` (tool registration restart-gated) |
| telos | off | `telos_enabled` (tool registration restart-gated) |

The total number of registered tools varies by configuration. With a minimal install (no Tavily key, no Chromium binary), the web extension contributes only `http_get`; with a fully-loaded install, it adds `search_web` and `browse_web`.

---

## Adding a new extension

If the existing extensions don't cover what you need, two options:

1. **Custom tool** via `toolmaker` — for one-off tools, no Pernix code change. See [../authoring/custom-tools.md](../authoring/custom-tools.md).
2. **New extension module** — for a coherent group of related tools. Drop a directory under `core/extensions/yourmodule/` with an `__init__.py` exposing `register()`. Pernix discovers it on next start.

The second path is appropriate for serious capability extensions you'd want to maintain or share. Use the existing extensions as patterns — `core/extensions/scheduling/` is a clean example of "tools + persistent state on disk + REST endpoints," and `core/extensions/rlm/` is the reference for a gated, off-by-default add-on with a DB table, workspace run dirs, and snooze retention.
