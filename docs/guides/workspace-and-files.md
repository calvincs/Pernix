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
├── scratch/                  # ad-hoc files
├── dreams/                   # Dream reports (DREAM-<date>.md) — written during idle introspection
├── rlm/{run_id}/             # RLM run residue (trace, staged context) — self-purges
└── .venv/                    # auto-managed venv for custom tools (not for dev use)
```

The agent organizes outputs into subdirectories by project where it can.

> **Don't confuse the workspace venv with the project venv.** `data/workspace/.venv/` is created on demand by the `toolmaker` extension when a custom tool installs Python packages. The dev venv is `.venv/` at the repo root.

---

## Reading and writing — what the agent can and can't do

Four file tools are always loaded:

| Tool | What it does | Restrictions |
|---|---|---|
| `file_read` | Read a file | Workspace, `/tmp`, `data/skills/`, the tool-output spill tree, and the kernel payload tree |
| `file_write` | Write a file (full overwrite) | Workspace and `/tmp` |
| `file_edit` | In-place edit (string replace, regex, fuzzy whole-file merge) | Workspace and `/tmp` |
| `view_image` | Look at an image file — routes it into the model's sight on the next round | Same read roots as `file_read`; needs a vision model to actually see the pixels; size-budgeted |

Size caps prevent runaway tool calls: `file_write` and `file_edit` honor `max_file_write_size` (default 100 MB), `file_edit` additionally caps how much it reads back with `max_edit_read_size` (default 5 MB), `file_read` truncates output at its own limit, and `view_image` is budgeted by `max_inline_attach_bytes`.

**Protected paths** — the agent cannot write or edit any of these, regardless of where they appear:

- `.env`
- `data/sessions.db`
- `data/settings.json`
- `data/agent/SOUL.md`, `RULES.md`, `SESSIONS.md` (it can ask you to edit these via `ask_user`, but it won't edit them directly)
- Anything outside the write roots — `data/workspace/` and `/tmp` (the other readable directories listed above are read-only)

If you ever see the agent claiming it edited a protected file, that's a bug — file an issue.

---

## Retrieving files

Three paths:

- **In the UI:** the Explorer's Files → Workspace tab shows the tree. Click a file to open it; the viewer's toolbar carries **edit**, **open** (in a browser tab) and **download**. Delete is the `×` that appears on a row on hover; on touch each row carries a `⋯` button instead, whose sheet holds Open, Download and Delete (swiping a row left still deletes it). The toolbar at the top of the tab refreshes the listing and uploads into the folder you are currently looking at. There is no right-click menu.
- **Direct filesystem:** `data/workspace/` is just a regular directory — open files in your editor of choice.
- **REST API:**

  ```bash
  GET  /api/workspace                  # list tree (JSON)
  GET  /workspace/{path}               # download a file
  PUT  /workspace/{path}               # write a file
  POST /api/upload                     # upload a file (multipart)
  DELETE /workspace/{path}             # delete a file
  ```

  See [../api.md](../api.md) for full details.

---

## Putting files in for the agent to read

Two ways to give the agent a file:

1. **Drag-drop into the UI** — uploads to the active session's workspace and adds a reference to the next message you send.
2. **Manually drop into `data/workspace/`** — the agent can read it once you mention the path.

The agent will use `file_read` (or, for images/PDFs attached in chat, the model's own multimodal input) to inspect what's there. For an image already on disk — including plots and renders the agent produced itself — `view_image(path)` puts the actual pixels in front of a vision model instead of leaving the agent to reason blind about its own output.

---

## Cleanup and organization

Pernix does not garbage-collect the workspace automatically. Two options keep it tidy:

- **Ask the agent** — "organize my workspace" works as a plain request; it'll move files into project subdirectories and archive old work. If you do this often, capture the procedure as a skill.
- **`python run.py --rebuild`** — wipes the workspace entirely (along with sessions, memory, and logs). Settings, API keys, skills, and certs are preserved.

For per-project cleanup, just `rm -r data/workspace/projects/old-project/`.

One exception to "no garbage collection": `rlm/` run directories self-purge — snooze deletes them after `rlm_run_retention_days` (default 30).

---

## Per-session vs per-project organization

Sessions and projects are independent concepts in Pernix:

- A **session** is a chat thread. Sessions live in `data/sessions.db`.
- A **project** is a workspace subdirectory the agent uses to keep one body of work together (`data/workspace/projects/foo/`). Projects are agent-managed conventions, not enforced.

Multiple sessions can write into the same project directory (e.g., a research session and a follow-up brief might share `projects/research-x/`). Conversely, one session can touch many projects.
