# data/ — Runtime State

All agent runtime data lives here. Wiped on `--rebuild` except `settings.json`,
`skills/`, `certs/`, and `agent/` (see [agent/README.md](agent/README.md)).

```
data/
  workspace/           # Unified file space — all agent files go here
    {project}/         #   Project folders (e.g. "my-gallery/app.html")
    dreams/            #   Dream reports (DREAM-<date>.md), written during idle introspection
    rlm/{run_id}/      #   RLM run residue (trace, staged context) — purged by snooze retention
    .venv/             #   Python venv for bash tool (auto-created)
    .cache/            #   pip cache and temp files
  agent/               # Agent identity & behavior — SOUL.md, RULES.md, SESSIONS.md
  skills/              # Skill packages — NOT wiped on rebuild
    {skill-name}/      #   Each skill has SKILL.md + optional scripts/, references/
    .disabled.json     #   Disabled skill names
  memories/            # Memory store (FTS5 index + markdown files)
  candor/              # Candor operational-memory store (when candor_enabled)
  certs/               # TLS certs for network mode — NOT wiped on rebuild
  sessions.db          # SQLite: sessions, messages, token usage
  settings.json        # User settings (persists across rebuilds)
  tools.json           # Custom tools created by the agent (toolmaker)
  logs/                # Application logs
```

## Key concepts

**Workspace** is the single place for all files the agent creates or works with.
The `bash` tool runs with CWD = `workspace/`. File tools (`file_read`, `file_write`,
`file_edit`) take paths relative to `workspace/` (e.g. `"myproject/app.html"`).
Use `glob("**/*.html")` to find files by pattern.

**Skills** are semi-protected domain expertise packages. The agent can read them freely
but writes require user approval via `ask_user`.
