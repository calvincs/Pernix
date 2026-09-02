# Contributing

Pernix is a personal tool released as-is, but contributions are welcome — bug fixes, new skills, doc improvements, new extensions, performance work. This page explains the practical mechanics: how to run the test suite, how the linters are configured, and what's expected in a PR.

If you're here to author **skills** (no Python required), you don't need any of this — skip to [../authoring/writing-skills.md](../authoring/writing-skills.md).

---

## Set up a development environment

The basic steps are the same as [installation.md](../installation.md), with one extra step:

```bash
git clone <repository-url>
cd pernix
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # adds black, ruff, flake8, pytest plugins
playwright install chromium           # only needed if touching browse_web
```

`requirements-dev.txt` is the dev-only delta — it layers on top of the runtime deps in `requirements.txt`.

---

## Run the test suite

Pernix ships with a comprehensive pytest suite. The fastest way to know your change is sound:

```bash
./check.sh
```

This is the canonical local CI. It runs:

1. `black --check` — code formatting
2. `ruff check` — lint (selects E, F, W, I rule families)
3. `flake8` — additional style checks
4. `pytest` — full test suite, requires ≥63% coverage to pass

The `--fix` flag applies black and ruff auto-fixes before running checks:

```bash
./check.sh --fix
```

### pytest invocations

Tests live in `tests/`. `pyproject.toml` configures `asyncio_mode = auto`, `-v --tb=short`, and `-n auto` (xdist parallelism).

```bash
pytest                                              # full suite, parallel
pytest -n 0                                         # disable parallelism (debug serial)
pytest tests/test_agent_logic.py                    # single file
pytest tests/test_agent_logic.py::test_function     # single test
pytest -m "not slow"                                # skip slow-marked tests
pytest --cov                                        # with coverage report
```

Two pytest markers are defined: `slow` and `integration`. Most tests run in seconds; the `slow` markers are skipped in fast iterations.

### Coverage

`./check.sh` enforces ≥63%. Coverage **omits** `tests/`, the virtualenvs (`.venv/`, `venv/`), `data/`, `static/`, `docs/`, `run.py`, and `core/certs.py` (paths that are either themselves tests, runtime data, or thin entry/glue layers). `core/extensions/*` is deliberately **not** omitted — it holds some of the largest, most concurrent code in the repo, and the gate must see it.

If your change drops coverage below 63%, write tests until it's back over.

---

## Code style

Linting/formatting config is in `pyproject.toml` and `.flake8`:

- **black** — line length 120 (not the default 88).
- **ruff** — selects rule families `E` (pycodestyle errors), `F` (pyflakes), `W` (warnings), `I` (isort).
- **flake8** — additional checks (config in `.flake8`).

Run `./check.sh --fix` before committing to apply formatting auto-fixes.

---

## Branches and pull requests

There's no formal branch convention — feature branches off `main` are fine. Keep PRs reasonably scoped (one logical change per PR). Squash-merge is preferred when the working history is messy.

When opening a PR:

- **Title:** short imperative ("fix race in worker spawner", not "Fixed a race"). Under ~70 characters.
- **Description:** what changed and why.
- **Tests:** if your change is non-trivial, add tests. If the change is hard to test, explain why.
- **Run `./check.sh` locally first.** PRs that can't pass the local CI won't pass review.

For UI changes, please test the affected feature in a browser — type checking and unit tests don't catch interaction regressions.


---

## Filing issues

Bugs, design questions, feature ideas — open an issue. A good bug report has:

- **What you expected** vs **what happened**
- **The minimum repro** (a session prompt, settings, model)
- **Versions** (Pernix commit, Python, Ollama or OpenRouter model used)
- **Logs** if relevant — `data/logs/` has the structured agent log

For larger design questions ("should we add X?"), an issue with the design write-up is the right place to start. Don't open a PR for a significant new feature without a discussion first; it's frustrating for everyone if a week of work hits a "we don't want this in the tree" reply at review.

---

## AI-assisted commits

If you used Claude Code (or any AI coding assistant) to help write the change, include the appropriate trailer:

```
Co-Authored-By: Claude <noreply@anthropic.com>
```

This is a normal git commit trailer — `git commit` accepts it via `-m` or via heredoc. Don't strip it for vanity reasons; it's useful provenance.

---

## What lives where (orientation)

If you're new to the codebase, this is the rough mental map:

| Concept | Path |
|---|---|
| Session orchestration | `sessions/manager.py`, `sessions/state_v2.py` |
| Scout (planning agent) | `core/scout/runner.py` |
| Main agent loop | `core/agent.py` |
| Reflect (quality gate) | `core/reflect.py` |
| Compaction | `core/context/compaction.py`, `core/context/compiler.py` |
| LLM routing | `core/llm/router.py`, `core/llm/registry.py` |
| Builtin tools | `core/tools/builtin/` |
| Extension tools (gated) | `core/extensions/*` |
| RLM engine (recursive processing) | `core/extensions/rlm/` (`engine.py` loop, `child_runner.py` sandbox, `broker.py` sub-calls) |
| Memory store | `core/memory/store.py` |
| Snooze (idle housekeeping) | `core/snooze.py` |
| REST + SSE | `api/app.py`, `api/routers/*.py` |
| Frontend | `static/` (vanilla JS PWA) |
| Frontend device tiers | `static/css/compact.css`, `static/css/touch.css`, `static/js/mobile.js` ([../internals/web-client.md](../internals/web-client.md)) |
| DB schema + migrations | `db/database.py` (`MIGRATIONS` list) |
| Settings | `config.py` |

A more conceptual tour lives in [../architecture.md](../architecture.md); the formal state-machine spec with file:line citations is [../internals/state-machine.md](../internals/state-machine.md).
