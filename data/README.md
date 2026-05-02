# data/ — Runtime State

All agent runtime data lives here. Wiped on `--fresh` except `skills/` and `settings.json`.

```
data/
  workspace/           # Unified file space — all agent files go here
    {project}/         #   Project folders (e.g. "my-gallery/app.html")
    .venv/             #   Python venv for bash tool (auto-created)
    .cache/            #   pip cache and temp files
  skills/              # Skill packages — NOT wiped on fresh start
    {skill-name}/      #   Each skill has SKILL.md + optional scripts/, references/
    .disabled.json     #   Disabled skill names
  memories/            # Memory store (FTS5 index + markdown files)
  sessions.db          # SQLite: sessions, messages, token usage
  settings.json        # User settings (persists across fresh starts)
  logs/                # Application logs
  RULES.md             # Agent behavioral rules
  SOUL.md              # Agent identity/personality (optional)
  birthdate.txt        # Agent creation timestamp
```

## Key concepts

**Workspace** is the single place for all files the agent creates or works with.
The `bash` tool runs with CWD = `workspace/`. File tools (`file_read`, `file_write`,
`file_edit`) take paths relative to `workspace/` (e.g. `"myproject/app.html"`).
Use `glob("**/*.html")` to find files by pattern.

**Skills** are semi-protected domain expertise packages. The agent can read them freely
but writes require user approval via `ask_user`.
