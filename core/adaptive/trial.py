"""Pernix — trial arms: every adaptation is an experiment (hardening W6).

An adaptive entry used to go straight from "a producer wrote it" to "in every
prompt, forever", and the only things that could ever take it back out were a
usage counter, a lint, or a human. Nothing in that loop measured whether the
entry made the agent better — the veto window measured whether anyone
objected in time, which is a different question.

With `adaptive_trial_enabled`, a machine-minted policy/prompt_note/routing_hint
lands as `trial` instead of `active`. A trial entry renders on a deterministic
half of the turns: the coin is `sha1(turn_key + entry_id)`, so the same turn
always lands on the same side, the scout prompt and the compiled system prompt
agree without either of them storing anything, and a restart cannot re-roll a
turn that is already being graded. Reflect records which trial entries the
turn actually rendered and which it held out; the sweep compares the two arms
with the same two-proportion test the tripwire uses.

Human-authored entries never enter a trial: the author is the evidence.
"""

from __future__ import annotations

import hashlib
import logging

from config import settings

logger = logging.getLogger("pernix.adaptive")

# The status a trialled entry holds. It is LIVE (it renders, it counts against
# the per-kind cap, it can be updated and retired) — just not on every turn.
TRIAL_STATUS = "trial"

# Only kinds that render into a prompt can have their effect measured this
# way. worker_spec and the memory-correction edits are not prompt text, so
# there is no "held out" half of a turn for them to be absent from.
TRIAL_KINDS = ("policy", "prompt_note", "routing_hint")


def status_for_new_entry(kind: str, producer: str) -> str:
    """`trial` for a machine-minted prompt entry, `active` for everything else.

    The gate is the producer, not the actor: an edit a human approved in the
    review queue is still prose a producer wrote and nobody has measured, and
    approving it is a veto being declined, not authorship. The one path that
    yields `active` under the flag is `create_entry` (source `user`), where a
    person typed the content themselves.
    """
    if not settings.adaptive_trial_enabled:
        return "active"
    if producer == "user" or kind not in TRIAL_KINDS:
        return "active"
    return TRIAL_STATUS


def turn_key(session_id: str, turn_id: int) -> str:
    """The per-turn coin: one string per (session, turn)."""
    if not session_id:
        return ""
    return f"{session_id}:{int(turn_id or 0)}"


def turn_key_for_session(session_id: str) -> str:
    """This session's current turn key, or "" when there is no live turn.

    Read from the live AgentSession (`turn_key`, stamped by the state machine
    at every turn boundary) rather than recomputed from the DB: the compiled
    prompt, the scout prompt and the grade must all use the SAME key, and the
    state log's idea of the current turn moves the moment the next one starts.
    """
    if not session_id:
        return ""
    try:
        from sessions.manager import get_manager

        live = get_manager().get(session_id)
    except Exception as e:  # no manager (scripts, tests), or none for this id
        logger.debug("No live session for turn key %s: %s", session_id[:12], e)
        return ""
    if live is None:
        return ""
    key = str(getattr(live, "turn_key", "") or "")
    if key:
        return key
    # Restored sessions get their _turn_id back before their first transition.
    turn_id = int(getattr(live, "_turn_id", 0) or 0)
    return turn_key(session_id, turn_id) if turn_id else ""


def renders_this_turn(key: str, entry_id: str) -> bool:
    """Is this trial entry in the TREATED half of the given turn?

    sha1 over `turn_key + entry_id`, first 32 bits, even means rendered. Any
    stable hash would do; what matters is that it is a pure function of the
    pair, so every consumer — the two prompt builders and the grade that
    records what they did — computes the same answer without coordination.

    No key (no live turn: a worker prompt build, a script, a test) means no
    trial entry renders at all. Rendering one outside a turn would put it in
    a prompt no post-mortem can attribute, which is an unmeasured effect —
    exactly what the trial exists to stop.
    """
    if not key or not entry_id:
        return False
    digest = hashlib.sha1(f"{key}{entry_id}".encode()).hexdigest()
    return int(digest[:8], 16) % 2 == 0


def in_arm(rows: list[dict], key: str) -> list[dict]:
    """Drop the trial entries this turn's coin held out. Active rows pass."""
    return [r for r in rows if r.get("status") != TRIAL_STATUS or renders_this_turn(key, r.get("id") or "")]
