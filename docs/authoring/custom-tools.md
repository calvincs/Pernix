# Authoring custom tools

Pernix's **toolmaker** extension lets the agent author new Python tools at runtime — no Pernix source changes, no server restart. Tools live as Python files in `data/tools/`. Each tool gets a name, a JSON-schema parameter spec, and an implementation function. They register into the same registry as builtin tools.

This is the lowest-friction way to add capability when a skill alone isn't enough — typically because you need real Python (network calls with retries, state management, structured parsing) or a third-party Python library.

For markdown-only capability extension, write a [skill](writing-skills.md) instead.

---

## When to write a tool vs a skill

| Scenario | Use |
|---|---|
| Multi-step procedure the agent should follow, no new code needed | **Skill** |
| Need to call a Python library not exposed via existing tools | **Tool** |
| Need to maintain state between calls within a session | **Tool** |
| Need fine control over input/output schema | **Tool** |
| The "tool" is really just "tell the agent to do X then Y" | **Skill** |

A skill can call a custom tool — the two compose well. Common pattern: tool exposes the API, skill teaches the agent how to use it correctly.

---

## Authoring via the toolmaker extension

The agent can do this end-to-end via the `toolmaker` extension's tools:

| Tool | What it does |
|---|---|
| `create_tool` | Author a new tool: name, parameter schema, Python implementation |
| `update_tool` | Modify an existing custom tool's schema or implementation |
| `list_custom_tools` | List tools the agent has authored (vs builtin) |
| `install_package` | Install a pip package into `data/workspace/.venv/` for tool use |
| `restore_tool_packages` | Reinstall packages after a venv wipe |

In a chat:

> *"Author a tool that wraps the OpenWeatherMap API. Take a city name and return today's high, low, and conditions. The API key is in OPENWEATHER_API_KEY in .env. Call it `weather_today`."*

The agent calls `create_tool` with the right schema and Python body. After the call, the tool is registered immediately into the active session's schema. No restart.

You can also create tools by hand — drop a Python file into `data/tools/` matching the pattern other custom tools use.

---

## Where custom tools live

```
data/tools/
├── weather_today.py
├── parse_invoice.py
└── ...
```

Each file defines one tool. Pernix discovers them on startup and on every `create_tool` call.

Custom tools are **excluded from formatters and git** (per the project's `.gitignore` rules) — they're treated as user data rather than source. If you want to share a tool, copy the `.py` file out manually or check it into a separate repo.

---

## Custom-tool venv

Tools that need pip packages install into `data/workspace/.venv/` (a separate venv from the project venv at `.venv/`). The `install_package` tool takes care of this:

```python
install_package(name="openweathermap-py")
```

Packages installed here persist across server restarts. After a `--rebuild` (which wipes the workspace, including this venv), use `restore_tool_packages` to reinstall everything your custom tools depend on — the package list is tracked in `data/tools/_packages.json`.

> **Don't install custom-tool packages into the project venv.** The project venv at `.venv/` is for Pernix itself; mixing in third-party libraries the tools need would clutter dependency management and risk version conflicts.

---

## Tool safety levels

When the agent authors a tool, it picks a safety level. Three options:

| Level | Means |
|---|---|
| `safe` | Read-only or low-risk operations. Default for new tools. |
| `caution` | Possibly impactful operations (e.g., writes a file with potentially large side effects). Logged with extra detail. |
| `dangerous` | Requires the `ask_user` + `approve_dangerous_tool` gate before each invocation. Use for destructive or irreversible operations. |

Pick `dangerous` for anything that:
- Sends real money (payment APIs)
- Sends messages to other people (Slack, email, social posting)
- Mutates external state (deleting cloud resources, draining queues)
- Could exfiltrate sensitive data

The agent will respect the gate even on tools you wrote yourself.

---

## Updating a custom tool

```python
update_tool(
    name="weather_today",
    new_implementation=...,  # full Python source
    new_schema=...,          # JSON schema for parameters
)
```

Updates take effect on the next tool call. The previous version is overwritten on disk; if you want history, version-control `data/tools/` separately.

---

## Removing a custom tool

Delete the Python file, or use `delete_tool` if it exists in your version, or — simplest — overwrite it with an empty `def implementation(...): pass` and leave it alone (no calls will route to it).

`delete_tool` may or may not be `dangerous` depending on the version; the agent will follow the gate appropriately.

---

## Debugging a misbehaving tool

- Custom tool errors are logged the same way builtin tool errors are: `data/logs/`.
- Use `list_custom_tools` to confirm the tool is registered.
- The Tools panel in the UI shows every registered tool's schema and any recent calls.
- For a Python traceback, open `data/logs/agent.log` (or wherever your log destination is) and search for the tool name.

If a tool repeatedly fails, prefer fixing it via `update_tool` rather than asking the agent to monkey-patch it inline — the file on disk is the source of truth.
