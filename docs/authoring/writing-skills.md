# Skills, Identity Files, and Web Capabilities

## What Are Skills?

Skills are installable capability packs that extend what Pernix can do. Each skill lives in its own directory under `data/skills/` and contains a `SKILL.md` file with metadata and instructions. The agent discovers available skills automatically — it reads their descriptions during the planning phase and decides whether to load the full instructions for a given task.

Skills are how you teach Pernix about custom APIs, services, or domain-specific procedures. You can add, edit, or remove them at any time without restarting the server.

---

## How Skills Are Loaded (L1 / L2 / L3)

Pernix uses a three-level progressive disclosure model to keep the agent's context lean:

| Level | What it is | When it's loaded |
|---|---|---|
| **L1 — Metadata** | `name`, `description`, `tags`, `version` from SKILL.md frontmatter | Always — on every turn |
| **L2 — Instructions** | The markdown body of `SKILL.md` | On demand, when the agent decides this skill is relevant |
| **L3 — Resources** | Files in `scripts/`, `references/`, `assets/` subdirectories | On demand, when the instructions reference them |

This means having dozens of skills installed does not bloat the context window — only the descriptions are always present. The full instructions load only when needed.

---

## The Agent Identity Files

Three special markdown files live in `data/agent/` and shape how Pernix behaves at a fundamental level. They are not skills — they are always loaded into every session's system prompt.

### `data/agent/SOUL.md` — Who Pernix Is

SOUL.md defines the agent's core identity and communication style. The default persona is:

- **Pragmatic** — focuses on what works, not what's theoretically elegant
- **Direct** — concise answers, no filler, no sycophancy
- **Curious** — investigates rather than assumes
- **Careful** — reads actual code before drawing conclusions; proposes verification steps when uncertain

The file is created automatically on first run with a `<!-- @birthdate: ... -->` timestamp header. When you run `python run.py --rebuild`, the birthdate is reset but the rest of the file is preserved.

**You can and should edit SOUL.md** to match how you want Pernix to communicate. Add project context, preferred coding style, domain knowledge, or personality adjustments. Changes take effect on the next session turn.

### `data/agent/RULES.md` — How Pernix Should Act

RULES.md defines operational guidelines — the agent's decision-making discipline. The defaults cover:

**Capability discovery**
Before reporting that something is impossible, use `list_available_models` and `discover_tools` to check whether a capability exists.

**Delegation**
Spawn workers (`spawn_worker`) for specialized tasks that benefit from a different model, parallelism, or isolation. Don't try to do everything in one context window.

**Persistence**
Diagnose failures rather than giving up. Exhaust options before telling the user something can't be done.

**Web tool priority** (explained in detail below)
Escalate through tools in order of speed: `search_web` → `http_get` → `browse_web`. Don't reach for a slow tool when a fast one will do.

**Python environment**
Always use the workspace virtualenv at `data/workspace/.venv/`. Never install packages to the system Python.

**You can and should edit RULES.md** to add project-specific constraints or procedures. For example: "always run tests before committing," "use this company's internal API for X," or "never delete files without asking."

### `data/agent/SESSIONS.md` — Deployment Context

SESSIONS.md holds deployment-specific context the agent should know across sessions — your timezone, a few facts about your environment, anything that's true *for you* but not obvious to a fresh agent. It's also injected into every session's system prompt alongside SOUL.md and RULES.md.

Older notes may reference a file called `AGENTS.md` or `INSTRUCTIONS.md` — those are legacy or fallback names. The current canonical filename is `SESSIONS.md`.

---

## Web Capabilities

Pernix has three web tools that escalate in power and latency. The agent is trained to try them in order.

### `search_web` — Fast Search (~1 second)

Returns a list of results (titles, URLs, snippets) for a query. Best for:

- Finding information, documentation, news
- Getting a list of relevant links to investigate further
- Quick factual lookups

**Provider:** [Tavily](https://tavily.com) — requires `TAVILY_API_KEY` in `.env`. Tavily returns an AI summary plus structured results.

`search_web` is **gated on the Tavily key** as of recent versions — without a key, the tool returns a setup hint rather than degrading silently. There is no longer an automatic free-tier fallback.

### `http_get` — Direct Fetch (~1–2 seconds)

Fetches a URL and returns the raw content (capped at 100KB). Best for:

- REST API responses (JSON)
- Static HTML pages
- Documentation sites without JavaScript requirements
- Downloading structured data files

Does **not** execute JavaScript. If the page requires JS to render content, use `browse_web` instead.

### `browse_web` — Headless Browser (~3–10 seconds)

Requires `browser_enabled = true` and [Playwright](#enabling-playwright) installed.

Opens a full headless Chromium browser, loads the page (executing JavaScript), waits for content to render, and extracts clean markdown via [trafilatura](https://trafilatura.readthedocs.io/). Also captures browser console errors and warnings.

Best for:

- Single-page applications (SPAs) where content is loaded by JavaScript
- Sites that return blank HTML without JS rendering
- Interactive pages where you need to observe rendered state
- Testing your own workspace HTML files: browse `http://localhost:8090/workspace/<file>` to render it and catch console errors

**Note on bot detection:** Some sites block headless browsers. When `browse_web` hits a Cloudflare wall or returns a bot-detection page, the agent will suggest alternative approaches or escalate to a skill-based solution.

#### Enabling Playwright

Playwright and trafilatura are already in `requirements.txt` and installed by the standard `pip install -r requirements.txt`. You only need to download the browser binary once:

```bash
source .venv/bin/activate
playwright install chromium
```

Then enable in Settings: `browser_enabled = true`. Without that setting, `browse_web` is not registered as a tool.

---

## Extending with Crawl4AI

[Crawl4AI](https://github.com/unclecode/crawl4ai) is an open-source web crawling service that uses a full browser environment and is specifically tuned to bypass aggressive bot detection (Cloudflare, JS challenges, etc.). It complements `browse_web` for cases where even Playwright gets blocked.

Crawl4AI is not bundled with Pernix — it is a separate service you self-host. To use it with Pernix:

1. Deploy Crawl4AI on a machine with a browser environment (their [Docker image](https://github.com/unclecode/crawl4ai#docker) is the easiest path)
2. Create a skill in `data/skills/` that instructs the agent how to call your Crawl4AI endpoint
3. The agent will automatically consider this skill when `browse_web` hits a bot-detection wall (Pernix emits a nudge hint in that scenario)

See the skill writing guide below for how to create the skill.

---

## Writing Your Own Skill

Skills are plain markdown files with YAML frontmatter. Creating one takes a few minutes.

### Minimum Skill Structure

```
data/skills/
└── my-skill-name/
    └── SKILL.md
```

**`SKILL.md` template:**

```markdown
---
name: my-skill-name
description: One clear sentence describing what this skill does and when to use it.
tags: [api, search, automation]
version: "1.0"
---

## Overview

Explain what this skill is for and when the agent should use it.

## How to Use

Step-by-step instructions the agent will follow. Be specific about:
- What tool calls to make (API endpoint, method, parameters)
- What authentication is needed and where to find credentials
- What the expected output looks like
- Error handling and fallback behavior

## Examples

Show a concrete example input/output if it helps clarify usage.
```

### Adding Scripts (L3)

For skills that need to run code, create a `scripts/` subdirectory:

```
data/skills/my-skill-name/
├── SKILL.md
└── scripts/
    └── process.py
```

Reference scripts in your SKILL.md instructions. The agent runs them via the `bash` tool from within the workspace directory context.

You can also declare scripts in the frontmatter with a `scripts:` list — each entry a mapping with a `path` (required) plus optional `purpose` and `usage`. Declared entries are rendered into the skill's injected instructions when it loads, so the agent knows the invocation shape without reading the file first:

```yaml
scripts:
  - path: scripts/process.py
    purpose: Normalize the raw export into rows
    usage: python scripts/process.py <input.csv>
```

Entries without a `path` are ignored.

### Tips for Effective Skills

- **Keep `description` short and specific** — this is what the scout reads to decide whether to load the skill. "Fetches weather forecasts from the National Weather Service API for US locations" is better than "weather tool."
- **Be precise in instructions** — the agent follows what you write literally. Specify the exact API format, required fields, authentication method, and response structure.
- **Include error handling guidance** — what should the agent do if the API returns an error, a rate limit, or unexpected data?
- **Version your skills** — increment the version when you make significant changes so you can track what changed.

### Removing a skill

Deleting a skill is a human action, not an agent tool — the agent-side `delete_skill` (and `list_skills`) tool was removed as unused. Use the Explorer → Capabilities → Skills panel, or `DELETE /api/skills/{name}` directly.

### Skill Discovery

Pernix scans `data/skills/` once, at server startup — a skill directory you drop in by hand is **not** picked up automatically on the next agent turn. To register it without a restart, trigger a rescan: open (or refresh) the Skills panel — listing skills via the API rescans the directory — or have the agent call `load_skill` on it by name, which rescans once as a fallback when the name isn't found in the registry. Skills created through the agent's own skill tools register immediately. Syntax errors in a skill's YAML frontmatter will cause that skill to be skipped (the error is logged).

### Giving a skill its own behavioral test (`verify:` block)

A skill can embed a `verify:` block in its frontmatter — a prompt, a non-empty `gates` list (each a `name` + `command`), and optional `files` / `timeout` — as its own regression test:

```yaml
verify:
  prompt: Use this skill to normalize sample-export.csv and report the row count.
  gates:
    - name: rows-normalized
      command: python -m pytest tests/test_skill_normalize.py
  timeout: 300
```

`core/canary/skill_verify.py` watches every `SKILL.md` for changes (a sha256 content watermark checked at idle) and materializes a `verify:` block as a managed canary named `skill--<name>` with `covers: [skill:<name>]`, resyncing it whenever the block or the skill body changes and retiring it when the block is removed. Because verify-gate commands run on the host and `SKILL.md` is machine-editable (by `update_skill`, the API, and self-healing proposal applies), each gate command must pass the same allowlist proof required for canary auto-admission — a gate that doesn't gets a notification and no canary is created, rather than a silently-unsafe one. See [canary-and-adaptive.md](../internals/canary-and-adaptive.md) for how the canary suite runs these.

### How a skill improves itself (refine + self-healing)

When a session works around a skill's shortcoming — the skill fails, the agent finds a fix, the task succeeds — the refine pass can propose folding that fix back into the `SKILL.md`. Refine attributes a session's lessons to a skill by `load_skill` calls, by `skills/<name>/` path references in the transcript, or by skill names in the scout's plan (checked against the registry so a stray path can't misroute a proposal).

Proposals wait in the same queue as other skill edits, but a **veto window** lets low-risk ones apply on their own: a pending `SKILL.md` proposal older than `skill_proposal_auto_apply_after_hours` (default 24; 0 disables) is machine-checked — the target skill exists and is enabled, the change is at most 4,000 characters, confidence is at least 0.6 — and applied with a timestamped backup written to `data/skill_backups/<skill>/`, capped at `skill_proposal_max_auto_applies_per_day` (default 5) applications per day and run only when the box is idle. A human can still review, edit, or roll back from the backup at any time; nothing here is a silent, unreviewable change, just a default of "no news is consent" the same way adaptive's stale-proposal auto-approval works.
