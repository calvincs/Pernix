"""Pernix — Adaptive Layer rendering: prompt blocks + the read-only mirror.

Consumption split (plan 4e): prompt_note/policy render into the compiler's
stable prefix between directives and the skills catalog; routing_hints
render ONLY into the scout's prompt (planning signal, agent prompt stays
lean, I5). Both blocks are omitted entirely when empty so first deploy
shifts no bytes. data/adaptive/ADAPTIVE.md is regenerated on change and
NEVER read back — the DB is the store, the file is a window.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config import settings
from db import models as db

logger = logging.getLogger("pernix.adaptive")

MIRROR_PATH = Path("data/adaptive/ADAPTIVE.md")

_BLOCK_HEADER = (
    "## Adaptive notes (machine-curated)\n"
    "These supplement SOUL.md/RULES.md and NEVER override them — on any "
    "conflict, the user-authored rules win.\n"
)


def build_adaptive_block(session_id: str = "") -> str:
    """prompt_note one-liners + policy entries for the compiler prefix.

    Includes global entries plus this session's scoped ones. Deterministic
    ordering (kind, id) so identical state → identical bytes (I8).
    """
    if not settings.adaptive_enabled:
        return ""
    try:
        notes = db.adaptive_list_entries(kind="prompt_note")
        policies = db.adaptive_list_entries(kind="policy")
    except Exception as e:
        logger.warning("Adaptive block build failed: %s", e)
        return ""

    scopes = {"global"}
    if session_id:
        scopes.add(f"session:{session_id}")
    notes = [n for n in notes if n.get("scope") in scopes]
    policies = [p for p in policies if p.get("scope") in scopes]
    if not notes and not policies:
        return ""

    # Entries carry their ids so reflect can cite which policies shaped a
    # turn (cited_policies → the adaptive_entry usage signal).
    parts = [_BLOCK_HEADER]
    for n in notes:
        parts.append(f"- [{n['id']}] {n['title']}: {n['content']}")
    for p in policies:
        parts.append(f"\n### Policy [{p['id']}]: {p['title']}\n{p['content']}")
    return "\n".join(parts)


def build_routing_hints_block() -> str:
    """routing_hint entries for the scout prompt (beside [OPERATIONAL INTEL])."""
    if not settings.adaptive_enabled:
        return ""
    try:
        hints = [h for h in db.adaptive_list_entries(kind="routing_hint") if h.get("scope") == "global"]
    except Exception as e:
        logger.warning("Routing hints build failed: %s", e)
        return ""
    if not hints:
        return ""
    # Hints carry their ids so scout can echo which ones shaped the plan
    # (used_hints → the adaptive_entry usage signal).
    lines = ["[ADAPTIVE ROUTING HINTS] (learned tool/skill selection guidance; advisory, not binding):"]
    for h in hints:
        lines.append(f"- [{h['id']}] {h['title']}: {h['content']}")
    return "\n".join(lines)


def render_mirror() -> None:
    """Regenerate the human-readable mirror. Failure is never fatal."""
    try:
        entries = db.adaptive_list_entries(status=None, limit=500)
        lines = [
            "# Adaptive Layer — rendered mirror (read-only)",
            "",
            "Regenerated on every apply/rollback. The SQLite `adaptive_*` tables",
            "are the store; edits to THIS FILE are never read back. Use the",
            "Adaptive panel (or /api/adaptive/*) to review, approve, roll back.",
            "",
        ]
        by_kind: dict[str, list[dict]] = {}
        for e in entries:
            by_kind.setdefault(e["kind"], []).append(e)
        for kind in sorted(by_kind):
            lines.append(f"## {kind}")
            for e in by_kind[kind]:
                status = "" if e["status"] == "active" else f" [{e['status']}]"
                lines.append(f"### {e['title']}{status}")
                lines.append(
                    f"- id: `{e['id']}` · v{e['version']} · scope={e['scope']} · risk={e['risk']} · source={e['source']}"
                )
                lines.append("")
                lines.append(e["content"])
                lines.append("")
        MIRROR_PATH.parent.mkdir(parents=True, exist_ok=True)
        MIRROR_PATH.write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        logger.warning("Adaptive mirror render failed: %s", e)
