# Pernix Documentation

Pernix is a self-hosted, headless AI agent server. You run it on your own hardware, point it at a local Ollama install or OpenRouter, and it runs as a personal AI workstation: persistent memory, sandboxed workspace, web UI, REST API, scheduled jobs, and parallel workers.

This index is organized by what you're trying to do.

---

## I'm new — get me running

1. [installation.md](installation.md) — full setup, requirements, optional dependencies
2. [quickstart.md](quickstart.md) — five-minute path from zero to first chat
3. [faq.md](faq.md) — common gotchas and "why did it do that" answers

## I want to use Pernix

How Pernix behaves day-to-day. Read the ones that match what you're trying to do.

- [guides/sessions-and-chat.md](guides/sessions-and-chat.md) — sessions, the turn lifecycle, pausing/resuming
- [guides/workspace-and-files.md](guides/workspace-and-files.md) — where outputs land, file safety, retrieval
- [guides/memory-and-recall.md](guides/memory-and-recall.md) — what Pernix remembers across sessions and how to manage it
- [guides/using-skills.md](guides/using-skills.md) — installing and invoking skills
- [guides/workers.md](guides/workers.md) — spawning parallel sub-agents for multi-part work
- [guides/scheduling-cron.md](guides/scheduling-cron.md) — recurring agents on a cron schedule
- [guides/recipes.md](guides/recipes.md) — runnable, copy-pasteable end-to-end examples

## I want to extend Pernix

Authoring new capabilities — no Pernix code changes required.

- [authoring/writing-skills.md](authoring/writing-skills.md) — the SKILL.md schema and how to write your own
- [authoring/custom-tools.md](authoring/custom-tools.md) — author Python tools via the toolmaker extension
- [authoring/workflows.md](authoring/workflows.md) — multi-step workflows the agent can follow

## I'm operating / deploying Pernix

Beyond the localhost-only default. Read these before exposing Pernix to anything.

- [security.md](security.md) — full security model, threat surface, mitigations
- [deployment/network-mode.md](deployment/network-mode.md) — HTTPS, Bearer auth, SSRF lockdown
- [deployment/mkcert.md](deployment/mkcert.md) — trusted TLS for LAN access without browser warnings
- [deployment/llm-providers.md](deployment/llm-providers.md) — Ollama and OpenRouter setup, the four model roles, failover

## I'm building integrations / scripting Pernix

- [api.md](api.md) — full REST API and SSE event reference
- Live API explorer at `http://localhost:8090/docs` once the server is running (Swagger UI from FastAPI)

## I want to understand how Pernix works

- [architecture.md](architecture.md) — guided walkthrough of the agent loop, scout, reflect, snooze
- [configuration.md](configuration.md) — every setting explained, with defaults
- [internals/state-machine.md](internals/state-machine.md) — formal session state machine with file:line citations
- [internals/extensions.md](internals/extensions.md) — the nine extension modules and their gates
- [internals/reflect-and-snooze.md](internals/reflect-and-snooze.md) — quality-gate retry and idle-time consolidation
- [internals/rlm.md](internals/rlm.md) — recursive long-input processing (sandboxed REPL + sub-LLM broker)

## I want to contribute

- [dev/contributing.md](dev/contributing.md) — test, lint, PR conventions
- [changelog.md](changelog.md) — what changed, when
- [upgrade.md](upgrade.md) — DB migrations and breaking changes between releases

---

> Pernix is a power tool. Run it on a dedicated machine or in a VM/container — not on your daily-driver workstation. See [security.md](security.md) before exposing it on a network.
