"""Pernix — Producer contract (plan 4d): the shared adaptive_edits shape.

Refine and snooze_reflect append ADAPTIVE_EDITS_PROMPT to their system
prompt (same chat call, same parse pass) only while the layer is enabled;
queue_producer_edits() normalizes and queues whatever came back. Dream and
Candor construct edits programmatically and call the same entry point.
"""

from __future__ import annotations

import logging

from config import settings

logger = logging.getLogger("pernix.adaptive")

ADAPTIVE_EDITS_PROMPT = """
ADDITIONALLY output an "adaptive_edits" array in the same JSON object (empty
array when nothing qualifies). These are edits to the machine-curated
adaptive layer — durable POLICY, distinct from memory (facts) and skills
(instructions):

  "adaptive_edits": [
    {
      "action": "create|update|delete",
      "kind": "prompt_note|routing_hint|policy|worker_spec",
      "scope": "global",
      "title": "short stable title (becomes the entry id)",
      "content": "the note/hint/rule text (prompt_note <= 400 chars)",
      "evidence": ["session or post-mortem refs backing this"],
      "baseline_version": null
    }
  ]

Mappings: user corrections about HOW to behave -> "prompt_note";
technique / tool-selection patterns -> "routing_hint"; sequencing or
control-flow rules -> "policy" (always human-reviewed before applying);
a recurring delegated-task shape worth templating -> "worker_spec"
(always human-reviewed; content is YAML with keys `instructions`,
optional `model`, optional `gates: [{name, command, watch_paths}]`).
For update/delete include "entry_id" and the "baseline_version" you
observed. Emit at most 2 edits and only for durable, cross-session signal —
a one-off fix belongs in lessons, not here. If an edit would contradict the
user's RULES.md, still emit it but add "conflicts_with_rules": true so it
routes to human review with the conflict flagged."""


def queue_producer_edits(edits: list, producer: str, session_id: str = "", rationale: str = "") -> dict:
    """Normalize LLM-emitted edits and queue them. Never raises."""
    empty = {"batch_id": None, "queued": 0, "proposal_id": None, "proposal_ids": [], "gated": 0, "rejected": []}
    if not settings.adaptive_enabled or not edits:
        return empty
    try:
        from core.adaptive.engine import queue_edits

        cleaned = []
        for e in edits:
            if not isinstance(e, dict):
                continue
            e = dict(e)
            ev = e.get("evidence")
            e["evidence"] = [str(r) for r in ev] if isinstance(ev, list) else []
            # The producer pass itself is always admissible evidence — stamp
            # the session so an edit is never refused for a bare list when
            # the model forgot to echo refs.
            if session_id and f"session:{session_id}" not in e["evidence"]:
                e["evidence"].append(f"session:{session_id}")
            cleaned.append(e)
        result = queue_edits(cleaned, producer, rationale=rationale)
        if result["rejected"]:
            for r in result["rejected"]:
                logger.info("adaptive edit rejected (%s): %s", producer, r["reason"])
        return result
    except Exception as e:
        logger.warning("queue_producer_edits failed (%s): %s", producer, e)
        return empty
