# Workspace and files

The **workspace** is the sandboxed directory the agent reads from and writes to. It lives at `data/workspace/` by default. Anything the agent produces — research notes, downloaded data, generated images, scripts — lands here.

This page covers where files go, what's protected, and how to retrieve outputs.

---

## Layout

```
data/workspace/
├── projects/
│   ├── daily-brief/
│   │   ├── 2026-05-08.md
│   │   └── ...
│   ├── research-x/
│   │   └── ...
│   └── ...
├── scratch/                  # ad-hoc files; cleaned by workspace-organizer skill
└── .venv/                    # auto-managed venv for custom tools (not for dev use)
```

The agent organizes outputs into subdirectories by project where it can. Loose files at the workspace root tend to get tidied by the `workspace-organizer` skill on idle.

> **Don't confuse the workspace venv with the project venv.** `data/workspace/.venv/` is created on demand by the `toolmaker` extension when a custom tool installs Python packages. The dev venv is `.venv/` at the repo root.

---

## Reading and writing — what the agent can and can't do

Three file tools are always loaded:

| Tool | What it does | Restrictions |
|---|---|---|
| `file_read` | Read a file | Workspace, `data/skills/`, `data/workflows/` |
| `file_write` | Write a file (full overwrite) | Workspace only |
| `file_edit` | In-place edit (string replace, regex, fuzzy whole-file merge) | Workspace only |

All three honor `max_file_write_size` (default 100 MB) and `max_edit_read_size` (default 5 MB) to prevent runaway tool calls.

**Protected paths** — the agent cannot write or edit any of these, regardless of where they appear:

- `.env`
- `data/sessions.db`
- `data/settings.json`
- `data/agent/SOUL.md`, `RULES.md`, `SESSIONS.md` (it can ask you to edit these via `ask_user`, but it won't edit them directly)
- Anything outside `data/workspace/` (and the readable directories listed above)

If you ever see the agent claiming it edited a protected file, that's a bug — file an issue.

---

## Retrieving files

Three paths:

- **In the UI:** the file panel shows the workspace tree. Click a file to open it; right-click to download or delete.
- **Direct filesystem:** `data/workspace/` is just a regular directory — open files in your editor of choice.
- **REST API:**

  ```bash
  GET  /api/workspace                  # list tree (JSON)
  GET  /workspace/{path}               # download a file
  POST /api/workspace                  # upload a file
  DELETE /api/workspace?path=<path>    # delete a file
  ```

  See [../api.md](../api.md) for full details.

---

## Putting files in for the agent to read

Two ways to give the agent a file:

1. **Drag-drop into the UI** — uploads to the active session's workspace and adds a reference to the next message you send.
2. **Manually drop into `data/workspace/`** — the agent can read it once you mention the path.

The agent will use `file_read` and (for binary types like PDF or PNG) `summarize_webpage` or its equivalents to inspect what's there.

---

## Cleanup and organization

Pernix does not garbage-collect the workspace automatically. Two options keep it tidy:

- **The `workspace-organizer` skill** — invoke it explicitly ("organize my workspace") and it'll move files into project subdirectories, archive old work, and flag stale data.
- **`python run.py --rebuild`** — wipes the workspace entirely (along with sessions, memory, and logs). Settings, API keys, skills, and certs are preserved.

For per-project cleanup, just `rm -r data/workspace/projects/old-project/`.

---

## Per-session vs per-project organization

Sessions and projects are independent concepts in Pernix:

- A **session** is a chat thread. Sessions live in `data/sessions.db`.
- A **project** is a workspace subdirectory the agent uses to keep one body of work together (`data/workspace/projects/foo/`). Projects are agent-managed conventions, not enforced.

Multiple sessions can write into the same project directory (e.g., a research session and a follow-up brief might share `projects/research-x/`). Conversely, one session can touch many projects.
