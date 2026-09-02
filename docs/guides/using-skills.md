# Using skills

A **skill** is a capability pack — a markdown file plus optional scripts that teaches the agent a specific procedure. Skills live in `data/skills/` and load on-demand: the agent discovers what skills exist on every turn, but only loads the full instructions for the ones it needs.

This page is about **using** skills you've installed or authored. To author your own, see [../authoring/writing-skills.md](../authoring/writing-skills.md).

---

## What goes in `data/skills/`

Pernix does not ship with skills preinstalled — `data/skills/` is user-owned content. Typical skills people build or install cover things like:

- Drafting posts in a specific voice or format
- Transcribing YouTube videos via Whisper
- Cleaning up and organizing the workspace
- Reviewing code quality or doing a security-hardening pass
- Crawling a site with a specific scraping tool

Each one is a directory containing a `SKILL.md` file and optional `scripts/`, `resources/`, and `references/` subfolders.

---

## How skills get used

You don't have to do anything explicit. On every turn, scout reads the metadata of every available skill (just the YAML frontmatter — name, description, tags) and decides whether one matches the user's request. If a top-scoring skill matches, scout injects it into the system prompt automatically.

You can also invoke a skill explicitly:

> *"Use the linkedin-post-formatter skill to write me a post about ..."*

There's no slash command for skills — asking in plain language, as above, is the explicit route; otherwise scout surfaces matching skills automatically.

Once invoked, the agent calls `load_skill(name)` to pull the full instruction body (level 2). Scripts in the skill's `scripts/` directory run via `bash` — there are no dynamic Python imports.

---

## Progressive disclosure: L1, L2, L3

Skills load in three layers, all controlled by Pernix:

- **L1 — metadata** is always loaded into context. This is the YAML frontmatter: name, description, tags, version. Cheap, always available.
- **L2 — instructions** are loaded only when the skill is selected. The body of `SKILL.md`, including step-by-step procedure, examples, and references.
- **L3 — scripts** run on demand. The agent calls `bash` to execute a script from the skill's `scripts/` directory. The script can be anything — Python, shell, even a curl call.

This is the "pull model" in action: a dozen installed skills loaded as L1 metadata costs almost nothing in tokens. Only the 1–2 skills relevant to the current turn pay the L2 cost.

---

## Discovering what's available

In a chat:

> *"What skills do you have available?"*

The agent uses `discover_skills` and lists them. Or look in `data/skills/` directly — every subdirectory with a `SKILL.md` is a skill.

In the REST API:

```bash
GET /api/skills
```

Returns the same list with metadata.

---

## Installing a skill someone else wrote

Skills are filesystem packages. Adding one is just dropping a directory into `data/skills/`:

```bash
cd data/skills/
git clone https://example.com/some-skill-repo.git some-skill-name/
```

The next turn picks it up. There's no install command — discovery is just a directory scan.

To uninstall: delete the directory, or use the `×` on the skill's row in Explorer → Capabilities → Skills (`DELETE /api/skills/{name}`).

---

## Per-session skill control

You can disable a particular skill from being auto-injected for a session by including a hint:

> *"Don't use the <skill-name> skill this session."*

This isn't a hard switch — there's no `disabled_skills` setting yet — but the scout respects in-conversation guidance.

For permanent disabling, move the skill out of `data/skills/` (e.g., to `data/skills.disabled/`).

---

## When skills aren't enough

If you find yourself wishing for a skill but the task is small or one-off, you have two options:

- **Just teach the agent in-conversation.** Tell it the procedure once, ask it to remember it. The relevant lesson lands in long-term memory and Snooze may eventually distill it.
- **Author a skill** if it's a procedure you'd want to reuse across sessions or share with others. See [../authoring/writing-skills.md](../authoring/writing-skills.md) for the full format.

Skills are especially valuable for **multi-step procedures with specific tools or APIs** — calling a particular service, formatting output a particular way, walking through a checklist that's hard to fit into a single prompt.
