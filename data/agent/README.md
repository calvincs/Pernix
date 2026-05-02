# data/agent/ — Agent Identity & Behavior

This directory holds all agent configuration. Files here are read at every turn (via
the scout) and injected into the agent's context. They survive `--fresh` restarts —
only the embedded birthdate in `SOUL.md` resets. To restore any file to its default
content, run: `git checkout data/agent/`

---

## Files

### `SOUL.md` *(optional, recommended)*

Agent identity, personality, values, and communication style. Injected as `[IDENTITY]`.

- **First line** is a machine-maintained birthdate comment — do not edit it manually:
  ```
  <!-- @birthdate: 2026-04-21T04:27:55.986737+00:00 -->
  ```
  The startup process writes this on first launch and resets it after `--fresh`.
  Everything below is yours to customize freely.
- **How it's used**: Scout reads up to 4,000 chars and extracts the most relevant
  ~300-token excerpt for the agent's `[IDENTITY]` section. Full content used in fallback.
- **Birthdate**: Parsed every turn and shown as `Agent birthdate: YYYY-MM-DD HH:MM:SS UTC`
  in the temporal context block.

### `RULES.md` *(optional)*

Behavioral rules and operational constraints. Injected as `[RULES]`.

- Define how the agent should behave: what to avoid, tone, tool usage preferences,
  delegation patterns, etc.
- **How it's used**: Same pattern as SOUL.md — up to 4,000 chars, scout extracts
  ~300 tokens for `[RULES]`. Full content in fallback path.

### `AGENTS.md` *(optional)*

Project-level instructions: workflows, conventions, task context. Injected as `[INSTRUCTIONS]`.

- Use this for standing project context the agent should always be aware of:
  how to create workflows, where outputs live, project-specific conventions, etc.
- **How it's used**: Scout reads up to 4,000 chars (first of AGENTS.md or INSTRUCTIONS.md
  found) and passes it as `[INSTRUCTIONS]`. Full content in fallback path.
- Falls back to `INSTRUCTIONS.md` if this file is absent.

### `INSTRUCTIONS.md` *(optional, fallback for AGENTS.md)*

Alternative to AGENTS.md — used if AGENTS.md is not present. Same injection behavior.

---

## How Files Are Injected

```
Scout Phase 1
  ├─ Reads SOUL.md    (up to 4,000 chars)
  ├─ Reads RULES.md   (up to 4,000 chars)
  └─ Reads AGENTS.md  (up to 4,000 chars, falls back to INSTRUCTIONS.md)
     └─ LLM extracts relevant excerpt (~300 tokens each) → ScoutReport fields

Agent Context (every turn)
  ├─ [IDENTITY]      ← from ScoutReport.identity
  ├─ [RULES]         ← from ScoutReport.rules
  ├─ [INSTRUCTIONS]  ← from ScoutReport.instructions
  └─ [TEMPORAL CONTEXT] ← includes "Agent birthdate:" parsed from SOUL.md

Fallback (no scout available)
  ├─ [IDENTITY]  ← full SOUL.md, up to 4,000 chars
  └─ [RULES]     ← full RULES.md, up to 4,000 chars
```

---

## Persistence

All files here survive `--fresh` (agents, sessions, memory, and workspace are wiped;
this directory is not). The birthdate line in SOUL.md is the only exception — it is
stripped on `--fresh` and re-stamped on the next startup.
