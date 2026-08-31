"""Pernix — SYSTEM-MAP: the agent's machine-generated map of its own machinery.

Agent-ergonomics plan P3 ("self-knowledge is compiled, not re-derived"):
during the 2026-08-31 co-design session the live agent burned ~6 tool rounds
rediscovering its own API endpoint, guessed a wrong table name
(session_messages) and a wrong directory (data/memory) — and the external
pairing agent independently made the same schema mistake. The pointer to
self-inspect existed ([SERVER CONTEXT]); the first hop still cost rounds.

This module writes data/workspace/SYSTEM-MAP.md at boot: key tables with
their real columns, the data-directory layout, the API route inventory
(from the live FastAPI app, so it can't drift), the registry of context
blocks the agent will see, and the store→tool map. Regenerated every boot,
idempotent, never fatal.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("pernix.context")

MAP_FILENAME = "SYSTEM-MAP.md"

# Tables worth mapping — the ones self-inspection queries actually hit.
_MAPPED_TABLES = (
    "sessions",
    "messages",
    "session_state_log",
    "post_mortems",
    "notifications",
    "questions",
    "jobs",
    "gates",
    "session_goals",
    "token_usage",
    "adaptive_entries",
    "adaptive_events",
    "adaptive_batches",
    "adaptive_proposals",
    "canary_runs",
    "rlm_runs",
    "dream_hypotheses",
    "scout_signals",
    "snooze_state",
    "cron_runs",
)

_DATA_LAYOUT = """\
data/sessions.db          all tables below (SQLite; WAL)
data/memories/*.md        long-term memory (markdown = source of truth; _index.db is rebuildable)
data/workspace/           your working directory (served at {base}/workspace/...)
data/workspace/.jobs/     background-job logs + exit sidecars
data/workspace/rlm/       RLM run residue (trace.jsonl, payloads.jsonl, draft.txt)
data/skills/              skills (SKILL.md + scripts/ + references/)
data/agent/               SOUL.md / RULES.md / SESSIONS.md (user-owned — never machine-written)
data/adaptive/ADAPTIVE.md read-only mirror of the adaptive store (never read back)
data/canaries/            canary suite (CANARY.md per task)
data/telos/               telos layer (questions/soup/claims/ledgers, markdown+YAML)
data/candor/              candor operational-memory store
data/kernels/<sid>/       session-kernel snapshots + large-tool-result payloads
data/settings.json        runtime settings
data/cron_jobs.json       scheduled jobs"""

# The context-block registry (agent-ergonomics plan P4): every block the
# compiler can inject, with its source, authority, and freshness — one
# convention to learn instead of ten. Kept here (not in the compiler) so the
# map and the docs cite one list.
CONTEXT_BLOCKS = (
    ("[SERVER CONTEXT]", "compiler", "reference", "static", "base URL, self-inspection pointers, this map"),
    ("[MODEL CAPABILITY]", "compiler", "reference", "per-session", "vision/audio support of the current model"),
    ("SOUL/RULES/SESSIONS", "user-authored files", "binding", "on file edit", "identity + operating rules"),
    (
        "Adaptive notes/policies",
        "adaptive store (machine)",
        "advisory — RULES.md wins",
        "idle applies",
        "learned prompt_notes + policies, with producer + evidence",
    ),
    (
        "[AVAILABLE SKILLS]",
        "skill registry",
        "reference",
        "on skill change",
        "skill catalog (scout loads full instructions)",
    ),
    ("[TEMPORAL CONTEXT]", "compiler", "reference", "static", "how to read timestamps; session-history lookup tools"),
    (
        "scout report",
        "scout (background model)",
        "advisory plan",
        "per turn",
        "memory recall, approach, tool rationale",
    ),
    (
        "[ACTIVE GOAL #N]",
        "goals store",
        "binding budgets",
        "per turn",
        "objective + budgets; only goal_complete finishes it",
    ),
    ("[CURRENT STATE]", "compiler volatile tail", "reference", "per round", "clock, resource status, goal burn"),
    ("[RESOURCE STATUS]", "agent loop", "binding (rounds)", "per round", "context %, spend, rounds remaining"),
    ("[TELOS]", "telos store", "FYI", "60s cache", "open questions, alarms (text), drive baseline"),
    (
        "[WORKERS YOU ARE WATCHING]",
        "sessions table",
        "reference",
        "per round",
        "watched workers still running — do not respawn",
    ),
    (
        "[SINCE YOUR LAST TURN]",
        "turn ledger (composed)",
        "reference — verify before acting",
        "per turn",
        "finished work, last verdict, adaptive changes, canary regressions, platform restarts",
    ),
    (
        "PRIOR ATTEMPT DIGEST",
        "reflect (retry only)",
        "advisory evidence",
        "per retry",
        "what the failed attempt actually did",
    ),
)

_STORE_TOOLS = """\
| Store | Read | Write |
|---|---|---|
| long-term memory | recall, deep_recall | remember (supersede= for one-call repair), update_memory, forget |
| session history | list_recent_sessions, read_session_summary, search_sessions | (automatic) |
| adaptive store | rendered into prompt; /api/adaptive/* | adaptive_note (2/day, linted) |
| candor ledger | predict_reliability, why_reliability, reliability_questions | (automatic capture) |
| telos layer | telos_status, telos_ask | telos_ask (mints questions) |
| skills | load_skill, read_skill_instructions | create_skill / update_skill |
| post-mortems | (scout: search_post_mortems) | (reflect writes them) |
| workspace files | file_read, grep, glob, bash, repl | file_write, bash |"""


def build_system_map(app=None) -> str:
    """The map's markdown. Pure function of live schema + routes."""
    from config import settings

    scheme = "https" if settings.network_enabled else "http"
    base = f"{scheme}://localhost:{settings.port}"

    lines = [
        "# SYSTEM MAP — machine-generated, regenerated at every boot. Do not edit.",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
        "Read this before spelunking /app or guessing a table, path, or route.",
        "",
        "## Data layout",
        "```",
        _DATA_LAYOUT.format(base=base),
        "```",
        "",
        "## Key tables in data/sessions.db (real columns, from PRAGMA)",
        "```",
    ]
    try:
        from db.models import connect_sessions

        with connect_sessions() as conn:
            for table in _MAPPED_TABLES:
                try:
                    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                except Exception:
                    cols = []
                if cols:
                    lines.append(f"{table}({', '.join(cols)})")
    except Exception as e:
        lines.append(f"(schema unavailable: {e})")
    lines += ["```", ""]

    if app is not None:
        lines += [f"## API routes ({base}; full schemas at {base}/openapi.json)", "```"]
        try:
            routes = []
            for r in app.routes:
                path = getattr(r, "path", "")
                methods = sorted(m for m in (getattr(r, "methods", None) or ()) if m not in ("HEAD", "OPTIONS"))
                if path.startswith("/api") and methods:
                    routes.append(f"{','.join(methods):11s} {path}")
            lines += sorted(set(routes))
        except Exception as e:
            lines.append(f"(route inventory unavailable: {e})")
        lines += ["```", ""]

    lines += [
        "## Context blocks you will see (one envelope convention)",
        "",
        "| Block | Source | Authority | Refresh | Content |",
        "|---|---|---|---|---|",
    ]
    for name, source, authority, fresh, content in CONTEXT_BLOCKS:
        lines.append(f"| {name} | {source} | {authority} | {fresh} | {content} |")
    lines += ["", "## Stores and their tools", "", _STORE_TOOLS, ""]
    return "\n".join(lines)


def write_system_map(app=None) -> str:
    """Write the map into the workspace; returns the path ('' on failure)."""
    try:
        from config import settings

        target = Path(settings.workspace_dir) / MAP_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(build_system_map(app), encoding="utf-8")
        logger.info("SYSTEM-MAP written to %s", target)
        return str(target)
    except Exception as e:
        logger.warning("SYSTEM-MAP generation failed (boot continues): %s", e)
        return ""
