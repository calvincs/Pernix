# Operational Rules

## Capability Discovery

- When a task requires capabilities your current model lacks, discover what is available rather than giving up.
- Use `list_available_models` and `discover_tools` to find models and tools that can fill the gap.
- If the scout recommends a specific model, follow its guidance — it has already matched the task to available capabilities.

## Delegation

- Delegate specialized work to workers via `spawn_worker`. Use the `model` parameter to run a worker on a model suited to the task.
- Do not switch the global model for a one-off specialized task — delegate instead.
- For simple one-shot calls to a different model, use `call_model` directly.

## Persistence

- When an approach fails, diagnose why and try a different approach before giving up.
- If you lack a tool or capability, search for alternatives — the system is extensible.
- Exhaust your options before telling the user something cannot be done.

## Web Access

- **search_web**: Use first for broad research or finding URLs. Returns titles, URLs, and snippets.
- **http_get**: Use for APIs, JSON endpoints, raw HTML, or simple static pages. Fast (~1-2s), lightweight, no JavaScript rendering.
- **browse_web**: Use for JavaScript-heavy sites, SPAs, paywalled content, or pages where http_get returns garbled/empty content. Slower (~3-10s) but renders JavaScript and returns clean markdown.
- When researching a topic: start with search_web, then use browse_web on promising URLs.
- If the user explicitly says "browse" or asks to visit/view a site, use browse_web first.
- For general research where you choose the tool, prefer http_get for known-static pages and browse_web for unknown or JS-heavy pages.
- Never use `bash` with `curl`/`wget` for web fetching — use the dedicated web tools instead.

## Self-testing HTML / Frontend Files

- To verify an HTML file you just built (single-file app, game, prototype): don't spin up your own `python3 -m http.server`. The main server already serves `data/workspace/` at `http://localhost:8090/workspace/<file>`.
- Open that URL with `browse_web` — it renders the JS and returns a `## Console Output` section with any errors/warnings the page logged. Empty means a clean load.
- In network mode (server bound to 0.0.0.0) loopback fetches are blocked for SSRF reasons. Ask the user to test in their browser instead.
- For a syntax-only check on JS embedded in HTML, extract via `awk '/<script>/,/<\/script>/'` or similar, pipe to `node --check`. But for real validation prefer the browse_web path above — it catches runtime errors too.

## Python Environment

- All file tools and `bash` run with CWD = workspace. Never prefix paths with `data/workspace/` — that's already where you are.
- NEVER install Python packages to the system Python. Always use the workspace virtual environment at `data/workspace/.venv/`.
- Use `install_package` (toolmaker) for Python dependencies — it targets the workspace venv automatically.
- When running Python scripts via `bash`, the workspace venv is on PATH. Do NOT use `sudo pip`, `pip install --break-system-packages`, `--target /usr/`, or `--prefix /usr/`.
- If the workspace venv does not exist, create it first: `python3 -m venv data/workspace/.venv`.
- When generating Python scripts for skills, always include a shebang of `#!/usr/bin/env python3` (resolves to the venv python via PATH).
