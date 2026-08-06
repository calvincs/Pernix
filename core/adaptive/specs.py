"""Pernix — worker_spec consumption (plan 4b table, follow-on work).

A worker_spec is an adaptive entry (kind="worker_spec", always high-risk →
human-approved) whose content is a YAML template:

    instructions: |
      You review pull requests. Check style, tests, and edge cases.
    model: qwen3:32b          # optional — spawn_worker's model= overrides
    gates:                    # optional — attached to the worker session
      - name: tests
        command: python -m pytest -q
        watch_paths: [src/]

Plain-prose content (no YAML mapping) is tolerated: the whole content
becomes `instructions`. Consumption: spawn_worker(spec="<entry-id>") loads
the template; the compiler renders a [WORKER SPECS] catalog line per active
spec so the agent knows what exists.
"""

from __future__ import annotations

import logging

import yaml

from db import models as db

logger = logging.getLogger("pernix.adaptive")


def parse_worker_spec(entry: dict) -> dict:
    """Entry row → {instructions, model, gates}. Never raises."""
    content = (entry.get("content") or "").strip()
    data: dict = {}
    try:
        loaded = yaml.safe_load(content)
        if isinstance(loaded, dict):
            data = loaded
    except yaml.YAMLError:
        pass
    if not data:
        return {"instructions": content, "model": "", "gates": []}

    gates = []
    for g in data.get("gates") or []:
        if isinstance(g, dict) and g.get("name") and g.get("command"):
            wp = g.get("watch_paths") or []
            if isinstance(wp, str):
                wp = [wp]
            gates.append({"name": str(g["name"]), "command": str(g["command"]), "watch_paths": [str(p) for p in wp]})
    return {
        "instructions": str(data.get("instructions") or "").strip(),
        "model": str(data.get("model") or "").strip(),
        "gates": gates,
    }


def load_worker_spec(name: str) -> dict | None:
    """Load an ACTIVE worker_spec by entry id. None when absent/wrong kind."""
    entry = db.adaptive_get_entry((name or "").strip())
    if not entry or entry.get("kind") != "worker_spec" or entry.get("status") != "active":
        return None
    spec = parse_worker_spec(entry)
    spec["entry_id"] = entry["id"]
    spec["title"] = entry.get("title", "")
    return spec


def build_worker_specs_block() -> str:
    """[WORKER SPECS] catalog for the compiler prefix. Empty when none.

    Deterministic ordering (adaptive_list_entries orders by kind, id) —
    identical store state renders identical bytes (I8). The caller
    suppresses this for worker sessions: workers can't spawn workers, and
    the extra bytes would fork their prefix from the parent's.
    """
    from config import settings

    if not settings.adaptive_enabled:
        return ""
    try:
        specs = [s for s in db.adaptive_list_entries(kind="worker_spec") if s.get("scope") == "global"]
    except Exception as e:
        logger.warning("Worker specs block build failed: %s", e)
        return ""
    if not specs:
        return ""
    lines = ["[WORKER SPECS] Reusable worker templates — pass spec=<id> to spawn_worker:"]
    for s in specs:
        parsed = parse_worker_spec(s)
        extras = []
        if parsed["model"]:
            extras.append(f"model={parsed['model']}")
        if parsed["gates"]:
            extras.append(f"{len(parsed['gates'])} gate(s)")
        suffix = f" ({', '.join(extras)})" if extras else ""
        first_line = (parsed["instructions"].splitlines() or [""])[0][:120]
        lines.append(f"- {s['id']}: {s['title']}{suffix} — {first_line}")
    return "\n".join(lines)
