# data/agent/ — Agent Identity & Behavior

This directory holds all agent configuration. The context compiler reads these files
on every turn and injects them **whole and verbatim** into the system prompt's fixed
prefix — the "agent directives" block. They survive `--rebuild` restarts — only the
embedded birthdate in `SOUL.md` resets. To restore any file to its default content,
run: `git checkout data/agent/`

---

## Files

### `SOUL.md` *(optional, recommended)*

Agent identity, personality, values, and communication style. Injected as `[IDENTITY]`.

- **First line** is a machine-maintained birthdate comment — do not edit it manually:
  ```
  <!-- @birthdate: 2026-04-21T04:27:55.986737+00:00 -->
  ```
  The startup process writes this on first launch and resets it after `--rebuild`.
  Everything below is yours to customize freely.
- **Birthdate**: Parsed every turn and shown as `Agent birthdate: YYYY-MM-DD HH:MM:SS UTC`
  in the temporal context block.

### `RULES.md` *(optional)*

Behavioral rules and operational constraints. Injected as `[RULES]`.

- Define how the agent should behave: what to avoid, tone, tool usage preferences,
  delegation patterns, etc.
- The whole file reaches the model — write rules once, precisely, and they hold.

### `SESSIONS.md` *(optional)*

Session-specific context: naming conventions, output conventions, standing facts. Injected as `[INSTRUCTIONS]`.

- Use this for things specific to *this installation* that the agent should always know.
  Do not duplicate tool protocols here — skill creation instructions are already built into the system.
- The compiler prepends a framing note: a blank or unset field here means "not pinned
  in config," never "fact unknown" — so placeholder lines don't override memory.
- Falls back to `INSTRUCTIONS.md` if this file is absent.

### `INSTRUCTIONS.md` *(optional, fallback for SESSIONS.md)*

Alternative to SESSIONS.md — used if SESSIONS.md is not present. Same injection behavior.

---

## How Files Are Injected

```
Context compiler (every turn, fixed prefix)
  ├─ [IDENTITY]      ← SOUL.md, whole file
  ├─ [RULES]         ← RULES.md, whole file
  ├─ [INSTRUCTIONS]  ← SESSIONS.md (or INSTRUCTIONS.md), whole file
  └─ [TEMPORAL CONTEXT] ← includes "Agent birthdate:" parsed from SOUL.md
```

There is no excerpting or scout-side summarizing — what you write is what the model
reads, every turn. Because the block sits in the fixed prefix and is byte-stable
across turns, it also extends prompt-prefix caching.

**The 32K guard**: each file is capped at 32,000 chars as an accident brake (a pasted
log dump, a runaway generator) — truncation is logged loudly, never silent. If your
directives genuinely need that scale, compress them at write time; the per-turn
context is the wrong place to absorb it.

---

## Persistence

All files here survive `--rebuild` (sessions, memory, and workspace are wiped;
this directory is not — alongside `settings.json`, `.env`, `data/skills/`, and
`data/certs/`). The birthdate line in SOUL.md is the only exception — it is
stripped on `--rebuild` and re-stamped on the next startup.

---

## Per-space overrides (`spaces/<slug>/`)

A [space](../../docs/guides/spaces.md) may override any of the three files by
placing its own copy at `data/agent/spaces/<slug>/SOUL.md`, `RULES.md`, or
`SESSIONS.md`. Resolution is **per file**: a space that defines only RULES.md
gets the default SOUL.md and SESSIONS.md. The files here are plain markdown —
hand-edit them or use the space's editor in the UI (gear icon on the space).
Deleting an override file reverts that space to the default. The birthdate is
always read from the default SOUL.md — a space override never changes it.
