# Project Instructions

## Creating Workflows

When a user asks to create or save a reusable multi-step pipeline, follow this protocol exactly:

1. **Call `get_workflow_schema()`** — returns the required YAML format with a concrete example.
   Workflows are YAML files, not prose documents. The `steps:` field is a YAML list
   in the frontmatter — NOT markdown sections in the body.

2. **Draft the WORKFLOW.md content** — use the schema as a template. Required frontmatter:
   `name`, `description`, `steps` (list). Each step needs: `id`, `type` (skill|instruction),
   `description`, `output_file`, `depends_on` (list, empty `[]` for first steps).

3. **Call `validate_workflow_content(content)`** — pre-check before writing to catch
   schema errors early (missing fields, invalid step IDs, cycles, missing skill refs).

4. **Call `create_workflow(name, content)`** — this is the ONLY correct way to create a
   workflow. It validates, writes to `data/workflows/{name}/WORKFLOW.md`, and registers
   the workflow. **Never use `file_write` for workflows** — it puts the file in the
   wrong location (workspace) and the registry will not find it.

5. **Call `validate_workflow(name)`** — confirms the workflow is registered and valid.
   If it returns "not found", the file is likely in the wrong location.

6. **Run with `run_workflow(name, inputs)`** — executes all steps in order, respecting
   dependencies. Steps with no shared dependencies run in parallel.

### Workflow step types

- `type: instruction` — worker follows free-form instructions; no skill required
- `type: skill` — worker loads the named skill and follows its instructions;
  add per-step `instructions:` to augment the skill for this specific context

### Key rules

- Workflows live in `data/workflows/`, never in `data/workspace/`
- `output_file` is a bare filename (no slashes) — the executor places it in the run directory
- `depends_on: []` means "run in wave 1 (parallel with other steps that have no deps)"
- Use `discover_workflows()` to check existing workflows before creating a duplicate

## Running Existing Workflows

- `discover_workflows()` — list installed workflows
- `run_workflow(name, inputs)` — execute; workers are spawned automatically per wave
- `schedule_workflow(name, cron_expr)` — schedule on a recurring cron schedule

## Checking Prior Work

Before creating a new workflow for a task, check if one already exists:
- `discover_workflows("keyword")` — search by name/tag/description
- The workspace may have processed outputs in subdirectories (e.g. `summaries/`, `linkedin_posts/`)
