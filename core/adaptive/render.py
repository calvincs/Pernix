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

# Rendering caps (v3.1): the blocks were uncapped, and at the 24/kind store
# caps the worst case was ~14k tokens in every compiled prompt. Constants,
# not settings — the retirement sweep is what keeps the population healthy;
# these are the hard ceiling, and zero-management means no knob to tend.
_MAX_POLICIES = 12
_POLICY_BLOCK_CHAR_CAP = 12000
_HINTS_MAX_LINES = 12
_HINTS_CHAR_CAP = 1600

# Deterministic priority when a cap bites: the human's entries always
# render, then the producers in descending content-quality order as the
# audit measured it. Ties break on id — stable bytes between applies (I8).
_SOURCE_RANK = {"user": 0, "refine": 1, "candor": 2, "telos": 3, "dream": 4}


def _priority(entry: dict) -> tuple:
    return (_SOURCE_RANK.get(entry.get("source"), 5), entry["id"])


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

    # Cap selection is pure Python over stored fields ((source_rank, id)),
    # then the SELECTED subset renders in the query's (kind, id) order —
    # bytes change only when entries change (idle applies / human action),
    # never mid-turn, so the prefix stays cache-stable (I8).
    dropped = 0
    if len(policies) > _MAX_POLICIES:
        keep_ids = {p["id"] for p in sorted(policies, key=_priority)[:_MAX_POLICIES]}
        dropped += len(policies) - _MAX_POLICIES
        policies = [p for p in policies if p["id"] in keep_ids]
    while policies and sum(len(p["content"]) + len(p["title"]) for p in policies) > _POLICY_BLOCK_CHAR_CAP:
        lowest = max(policies, key=_priority)
        policies = [p for p in policies if p["id"] != lowest["id"]]
        dropped += 1

    # Entries carry their ids so reflect can cite which policies shaped a
    # turn (cited_policies → the adaptive_entry usage signal).
    parts = [_BLOCK_HEADER]
    for n in notes:
        parts.append(f"- [{n['id']}] {n['title']}: {n['content']}")
    for p in policies:
        parts.append(f"\n### Policy [{p['id']}]: {p['title']}\n{p['content']}")
    if dropped:
        parts.append(
            f"\n({dropped} lower-priority entr{'y' if dropped == 1 else 'ies'} not rendered — see the Adaptive tab)"
        )
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
    # The scout block lives in a per-turn user message — no prefix cache to
    # protect — so it CAN rank by live usage: most-used first (the counters
    # scout itself feeds via used_hints), recency and id as tie-breaks.
    if len(hints) > _HINTS_MAX_LINES or sum(len(h["content"]) for h in hints) > _HINTS_CHAR_CAP:
        try:
            sig = {s["subject"]: s for s in db.get_signals_by_subjects([("adaptive_entry", h["id"]) for h in hints])}
        except Exception:
            sig = {}
        # Stacked stable sorts → (reinforcements desc, recency desc, id asc).
        hints = sorted(hints, key=lambda h: h["id"])
        hints = sorted(hints, key=lambda h: str((sig.get(h["id"]) or {}).get("last_reinforced_at") or ""), reverse=True)
        hints = sorted(hints, key=lambda h: int((sig.get(h["id"]) or {}).get("reinforcements") or 0), reverse=True)

    # Hints carry their ids so scout can echo which ones shaped the plan
    # (used_hints → the adaptive_entry usage signal).
    lines = ["[ADAPTIVE ROUTING HINTS] (learned tool/skill selection guidance; advisory, not binding):"]
    total = 0
    shown = 0
    for h in hints:
        line = f"- [{h['id']}] {h['title']}: {h['content']}"
        if shown >= _HINTS_MAX_LINES or total + len(line) > _HINTS_CHAR_CAP:
            break
        lines.append(line)
        total += len(line)
        shown += 1
    if shown < len(hints):
        lines.append(f"(+{len(hints) - shown} more hints — use search_adaptive to query the rest)")
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
