"""Pernix — Adaptive Layer rendering: prompt blocks + the read-only mirror.

Consumption split (plan 4e): prompt_note/policy render into the compiler's
stable prefix between directives and the skills catalog; routing_hints
render ONLY into the scout's prompt (planning signal, agent prompt stays
lean, I5). Both blocks are omitted entirely when empty so first deploy
shifts no bytes. data/adaptive/ADAPTIVE.md is regenerated on change and
NEVER read back — the DB is the store, the file is a window.

Trial arms (W6): entries with status `trial` render on a deterministic half
of the turns. Both blocks take the same `turn_key` and apply the same coin
(core/adaptive/trial.renders_this_turn), so a turn either sees a trial entry
in BOTH prompts or in neither — a split decision would measure nothing. With
no turn key (a worker build, a script, the mirror) no trial entry renders at
all: the pre-W6 behaviour, and the only honest one when no post-mortem will
be able to say what was in the prompt.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config import settings
from core.adaptive.trial import TRIAL_STATUS, in_arm
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


# (entry_id → (version, evidence ref)) — the audit chain lives in
# adaptive_events, and querying it on every compile would put N queries on
# the hot path. Version-keyed, so the cached string is deterministic for a
# given store state and the rendered bytes stay stable between applies (I8).
_EVIDENCE_CACHE: dict[str, tuple[int, str]] = {}
_EVIDENCE_CACHE_MAX = 512


def _evidence_ref(entry: dict) -> str:
    """First evidence ref recorded on the entry's creating event, truncated.

    Rendered beside the producer so the agent can see not just WHO minted a
    rule it is following but from WHAT — the 2026-08-31 first-person audit's
    §5.1: 'I currently can't tell which producer minted the rule I'm
    following.' Empty when the journal has none (e.g. legacy rows).
    """
    eid, version = entry["id"], int(entry.get("version") or 0)
    cached = _EVIDENCE_CACHE.get(eid)
    if cached and cached[0] == version:
        return cached[1]
    ref = ""
    try:
        from core.adaptive.retire import creating_evidence

        refs = creating_evidence(eid)
        if refs:
            ref = refs[0][:60]
    except Exception:
        ref = ""
    if len(_EVIDENCE_CACHE) >= _EVIDENCE_CACHE_MAX:
        _EVIDENCE_CACHE.clear()
    _EVIDENCE_CACHE[eid] = (version, ref)
    return ref


def select_prompt_entries(session_id: str = "", turn_key: str = "") -> dict:
    """What the compiler prefix will actually contain this turn.

    Returns {"notes", "policies", "dropped", "trial_rendered", "trial_held_out"}.
    Selection is separated from rendering because reflect has to grade against
    the SAME set: its evidence list may only name entries the turn rendered,
    and the post-mortem's treated/control record has to match what the agent
    saw, cap drops included.

    Includes global entries plus this session's scoped ones. Deterministic
    ordering (kind, id) so identical state → identical bytes (I8).
    """
    empty: dict = {"notes": [], "policies": [], "dropped": 0, "trial_rendered": [], "trial_held_out": []}
    if not settings.adaptive_enabled:
        return empty
    try:
        notes = db.adaptive_list_entries(kind="prompt_note", status=db.ADAPTIVE_LIVE_STATUS)
        policies = db.adaptive_list_entries(kind="policy", status=db.ADAPTIVE_LIVE_STATUS)
    except Exception as e:
        logger.warning("Adaptive block build failed: %s", e)
        return empty

    scopes = {"global"}
    if session_id:
        scopes.add(f"session:{session_id}")
    notes = [n for n in notes if n.get("scope") in scopes]
    policies = [p for p in policies if p.get("scope") in scopes]
    in_scope_trials = {e["id"] for e in notes + policies if e.get("status") == TRIAL_STATUS}
    # The coin BEFORE the caps: being held out is the experiment's whole
    # manipulation, so a held-out entry must not go on occupying a slot its
    # absence is supposed to free.
    notes = in_arm(notes, turn_key)
    policies = in_arm(policies, turn_key)
    if not notes and not policies:
        return {**empty, "trial_held_out": sorted(in_scope_trials)}

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

    rendered_trials = {e["id"] for e in notes + policies if e.get("status") == TRIAL_STATUS}
    return {
        "notes": notes,
        "policies": policies,
        "dropped": dropped,
        # Cap-dropped trial entries count as held out, not as treated: they
        # were not in the prompt, whatever the coin said.
        "trial_rendered": sorted(rendered_trials),
        "trial_held_out": sorted(in_scope_trials - rendered_trials),
    }


def build_adaptive_block(session_id: str = "", turn_key: str = "") -> str:
    """prompt_note one-liners + policy entries for the compiler prefix."""
    selected = select_prompt_entries(session_id, turn_key)
    notes, policies, dropped = selected["notes"], selected["policies"], selected["dropped"]
    if not notes and not policies:
        return ""

    # Entries carry their ids so reflect can cite which policies shaped a
    # turn (cited_policies → the adaptive_entry usage signal). A trial entry
    # is deliberately NOT marked as one here: the agent reading a rule that
    # announces it is on probation is not the treatment being measured.
    parts = [_BLOCK_HEADER]
    for n in notes:
        parts.append(f"- [{n['id']}] ({n.get('source', '?')}) {n['title']}: {n['content']}")
    for p in policies:
        ev = _evidence_ref(p)
        provenance = f"{p.get('source', '?')}" + (f" · evidence: {ev}" if ev else "")
        parts.append(f"\n### Policy [{p['id']}] ({provenance}): {p['title']}\n{p['content']}")
    if dropped:
        parts.append(
            f"\n({dropped} lower-priority entr{'y' if dropped == 1 else 'ies'} not rendered — see the Adaptive tab)"
        )
    return "\n".join(parts)


def select_routing_hints(turn_key: str = "") -> dict:
    """The routing hints the scout prompt will carry this turn.

    Returns {"hints", "total", "trial_rendered", "trial_held_out"} — `hints`
    already ranked and truncated exactly as the block renders them, so the
    grade's treated/control record matches the prompt rather than the store.
    """
    empty: dict = {"hints": [], "total": 0, "trial_rendered": [], "trial_held_out": []}
    if not settings.adaptive_enabled:
        return empty
    try:
        hints = [
            h
            for h in db.adaptive_list_entries(kind="routing_hint", status=db.ADAPTIVE_LIVE_STATUS)
            if h.get("scope") == "global"
        ]
    except Exception as e:
        logger.warning("Routing hints build failed: %s", e)
        return empty
    in_scope_trials = {h["id"] for h in hints if h.get("status") == TRIAL_STATUS}
    hints = in_arm(hints, turn_key)
    if not hints:
        return {**empty, "trial_held_out": sorted(in_scope_trials)}
    # The scout block lives in a per-turn user message — no prefix cache to
    # protect — so it CAN rank by live outcomes: best observed success share
    # first (the outcome half of the adaptive_entry signal synthesis writes),
    # then most-used, then recency and id. Laplace smoothing (s+1)/(n+2)
    # keeps unattributed entries at a neutral 0.5 instead of burying them,
    # and stops a single lucky success from outranking a long record.
    if len(hints) > _HINTS_MAX_LINES or sum(len(h["content"]) for h in hints) > _HINTS_CHAR_CAP:
        try:
            sig = {s["subject"]: s for s in db.get_signals_by_subjects([("adaptive_entry", h["id"]) for h in hints])}
        except Exception:
            sig = {}

        def _smoothed_success(h: dict) -> float:
            s = sig.get(h["id"]) or {}
            wins = int(s.get("successes") or 0)
            losses = int(s.get("failures") or 0)
            return (wins + 1.0) / (wins + losses + 2.0)

        # Stacked stable sorts → (smoothed success desc, reinforcements desc,
        # recency desc, id asc).
        hints = sorted(hints, key=lambda h: h["id"])
        hints = sorted(hints, key=lambda h: str((sig.get(h["id"]) or {}).get("last_reinforced_at") or ""), reverse=True)
        hints = sorted(hints, key=lambda h: int((sig.get(h["id"]) or {}).get("reinforcements") or 0), reverse=True)
        hints = sorted(hints, key=_smoothed_success, reverse=True)

    chars = 0
    shown: list[dict] = []
    for h in hints:
        line = f"- [{h['id']}] ({h.get('source', '?')}) {h['title']}: {h['content']}"
        if len(shown) >= _HINTS_MAX_LINES or chars + len(line) > _HINTS_CHAR_CAP:
            break
        shown.append(h)
        chars += len(line)
    rendered_trials = {h["id"] for h in shown if h.get("status") == TRIAL_STATUS}
    return {
        "hints": shown,
        "total": len(hints),
        "trial_rendered": sorted(rendered_trials),
        "trial_held_out": sorted(in_scope_trials - rendered_trials),
    }


def build_routing_hints_block(session_id: str = "", turn_key: str = "") -> str:
    """routing_hint entries for the scout prompt (beside [OPERATIONAL INTEL]).

    `session_id` is only ever used to look up the turn key when the caller has
    not resolved it; routing hints themselves are global-scope by definition.
    """
    if not turn_key and session_id:
        from core.adaptive.trial import turn_key_for_session

        turn_key = turn_key_for_session(session_id)
    selected = select_routing_hints(turn_key)
    hints, total = selected["hints"], selected["total"]
    if not hints:
        return ""

    # Hints carry their ids so scout can echo which ones shaped the plan
    # (used_hints → the adaptive_entry usage signal).
    lines = ["[ADAPTIVE ROUTING HINTS] (learned tool/skill selection guidance; advisory, not binding):"]
    for h in hints:
        lines.append(f"- [{h['id']}] ({h.get('source', '?')}) {h['title']}: {h['content']}")
    if len(hints) < total:
        lines.append(f"(+{total - len(hints)} more hints — use search_adaptive to query the rest)")
    return "\n".join(lines)


def turn_arms(session_id: str = "", turn_key: str = "") -> dict:
    """Which trial entries this turn rendered, and which it held out.

    Both prompt surfaces at once — a trial entry belongs to one arm per TURN,
    not one per block. Only trial entries appear: an active entry renders on
    every turn, so it has no control half and is not an experiment.

    Recomputed at grading time rather than captured during the turn. The coin
    is a pure function of (turn key, entry id), so the ARM cannot drift; only
    the population can (an entry created or retired between the prompt and the
    grade), and that costs one turn of one entry's evidence.
    """
    prompt = select_prompt_entries(session_id, turn_key)
    hints = select_routing_hints(turn_key)
    return {
        "rendered": sorted(set(prompt["trial_rendered"]) | set(hints["trial_rendered"])),
        "held_out": sorted(set(prompt["trial_held_out"]) | set(hints["trial_held_out"])),
    }


def render_mirror() -> None:
    """Regenerate the human-readable mirror. Failure is never fatal.

    Every non-active status is stamped on the heading, so a trial entry reads
    as `### Title [trial]` — the file is where a human looks to see what the
    machine is currently trying out.
    """
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
