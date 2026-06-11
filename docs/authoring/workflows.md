# Workflows

A **workflow** is a multi-step procedure the agent can execute as a single named unit. Workflows live in `data/workflows/` and are managed via workflow tools the agent has access to. They're useful when:

- A procedure has many steps and you don't want to retype it.
- You want the agent to follow an exact sequence, not improvise.
- Multiple skills compose into a larger flow.

This page covers when to use workflows, how to create them, and how they relate to skills and cron jobs.

---

## Skill vs workflow vs cron job

| Concept | What it is |
|---|---|
| **Skill** | Markdown-defined capability pack. Discovered by scout; loaded on demand. Best for "how to do X." |
| **Workflow** | Named procedure stored in `data/workflows/`. The agent invokes it explicitly. Best for "do X in order, every time." |
| **Cron job** | A schedule + a session prompt. Best for "run this on a recurring basis." |

A cron job often *contains* a prompt that triggers a workflow. A workflow can call skills as part of its steps. The three layer cleanly.

---

## Creating a workflow — by chatting

The workflow extension exposes tools the agent can use to author one:

> *"Create a workflow called `research-and-publish`. Steps:*
>
> *1. Search the web for the topic the user gives.*
> *2. Spawn 2 workers in parallel — one to summarize, one to fact-check.*
> *3. Wait for both, then merge into a 600-word post.*
> *4. Save the result to data/workspace/projects/posts/.*
> *5. Format it as a Social post using the social-post-formatter skill.*
> *6. Show me the final draft."*

The agent calls `create_workflow` with the name, description, and step list. Once saved, you can invoke it later by name:

> *"Run the research-and-publish workflow on 'open-source LLM tooling in 2026'."*

---

## Workflow management tools

| Tool | What |
|---|---|
| `discover_workflows` | List available workflows with metadata |
| `get_workflow_schema` | Get the WORKFLOW.md format reference |
| `create_workflow` | Author a new workflow |
| `validate_workflow` | Check a WORKFLOW.md's frontmatter and step DAG without saving |
| `run_workflow` | Execute a workflow (steps run as workers, in dependency order) |
| `schedule_workflow` / `cancel_workflow` | Put a workflow on a cron schedule / cancel a run |
| `delete_workflow` | Delete a workflow (dangerous — gated) |

The corresponding REST endpoints under `/api/workflows` cover list/read/create/update/delete and run history.

---

## File layout

```
data/workflows/
├── research-and-publish/
│   └── WORKFLOW.md
├── morning-routine/
│   └── WORKFLOW.md
└── ...
```

Each workflow is a directory containing a `WORKFLOW.md` file: YAML frontmatter (`name`, `description`, `tags`, `version`, and a `steps` list) followed by a freeform markdown body of usage notes. Each step has an `id`, a `type` (`skill` or `instruction`), `instructions`, an `output_file`, an optional per-step `model` override, and a `depends_on` list — steps form a DAG, and independent steps execute in parallel waves.

Cron jobs are persisted separately in `data/cron_jobs.json` — see [../guides/scheduling-cron.md](../guides/scheduling-cron.md). Don't conflate them.

---

## When to use a workflow vs just trusting the agent

If your prompt is going well most of the time and the agent figures out the right sequence, you don't need a workflow. You're not paying for the abstraction yet.

Reach for a workflow when:

- **The order matters and the agent sometimes gets it wrong.** Workflows lock the order in.
- **You're going to invoke the same procedure many times.** Saves typing and ensures consistency.
- **Steps need to be deterministic for downstream consumers** (e.g., a cron job whose output must be in a specific format every day).
- **You want one-step invocation.** "Run X" beats a half-page prompt.

---

## Updating a workflow

Edit the `data/workflows/<name>/WORKFLOW.md` file directly (or via `PUT /api/workflows/{name}`), then ask the agent to `validate_workflow` if you want a sanity check. Updates take effect on the next invocation.

If you want a history of what the workflow was, version-control `data/workflows/` (it's just markdown files).

---

## Deleting a workflow

`delete_workflow` is **dangerous** — the agent has to call `ask_user` and `approve_dangerous_tool` before it can delete one, except in unattended cron sessions which bypass the gate.

You can also just delete the workflow's directory under `data/workflows/`.

---

## Limits

- No formal limit on number of workflows.
- Step dependencies must form a valid DAG — cycles are rejected at parse/validate time.
- Workflow steps execute as worker sub-agents coordinated by the invoking session; independent steps run in parallel waves, subject to `max_concurrent_workers`.
