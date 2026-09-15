"""Pernix — Plain-language explanations for adaptive proposals.

The Learning tab showed each pending proposal as the row it came from: a
producer name, a rationale written by one LLM for another, a payload of
`create policy` edits and an evidence list of `pm:` and `hypothesis:` ids.
Every word of it was true and none of it told the person holding the
Approve button what would change, why the system wanted it, or what would
happen if they walked away. On the live box the owner said as much: "I as
the user have no idea what's happening or being described here."

This module turns one proposal row into three sentences a person can act on:

  what   — who proposed it and the concrete change, in words
  why    — the evidence, in words (what was seen, where, how well recorded)
  fate   — what happens if nobody touches it, and when

`fate_kind` is the machine-readable side of `fate`: "auto" (the veto clock
takes it), "needs_you" (it never applies by itself), "held" (the clock
refuses it — unfounded evidence). The panel colours on that; the text is
for reading.

Deterministic and template-based on purpose: it runs on every listing, must
never cost an LLM call, and must say the same thing twice. The Chat button
is where the conversation happens; this is the label on the door.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from config import settings
from db import models as db

logger = logging.getLogger("pernix.adaptive")

AUTO = "auto"
NEEDS_YOU = "needs_you"
HELD = "held"

# Who the producer is, for a reader who has never opened core/.
PRODUCER_LABELS: dict[str, str] = {
    "dream": "Dream (the overnight review that looks back over past sessions)",
    "refine": "Refine (the after-session grader that reviews each finished session)",
    "canary_propose": "Refine, via the self-test suite (the after-session grader turning a failure into a test)",
    "agent": "the agent itself, during a live session",
    "telos": "Telos (the objectives tracker)",
    "candor": "Candor (the fact tracker)",
    "user": "you",
}

KIND_LABELS: dict[str, str] = {
    "policy": "a rule the agent must follow in every session",
    "prompt_note": "a short note added to the agent's standing instructions",
    "routing_hint": "a hint about which tool or skill to reach for",
}

_ACTION_VERBS: dict[str, str] = {
    "create": "add",
    "add": "add",
    "update": "change",
    "delete": "remove",
    "retire": "remove",
}

_REF_LABELS: dict[str, tuple[str, str]] = {
    "pm": ("graded turn", "graded turns"),
    "candor": ("tracked fact", "tracked facts"),
    "signal": ("scout signal", "scout signals"),
    "feedback": ("thumbs-up/down you gave", "thumbs-up/down you gave"),
    "hypothesis": ("Dream hypothesis", "Dream hypotheses"),
}

_NOT_ADMITTED_RE = re.compile(r"not auto-admitted:\s*(.+?)\)?\s*$", re.DOTALL)
_MAX_QUOTE = 240


def _payload(prop: dict):
    try:
        return json.loads(prop.get("payload_json") or "[]")
    except (TypeError, ValueError):
        return []


def _evidence(prop: dict) -> list[str]:
    try:
        data = json.loads(prop.get("evidence_json") or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _quote(s: str, limit: int = _MAX_QUOTE) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def producer_label(producer: str | None) -> str:
    p = (producer or "").strip()
    return PRODUCER_LABELS.get(p, f"the {p} pass" if p else "the adaptive engine")


def _is_delete_only(payload) -> bool:
    return (
        isinstance(payload, list)
        and bool(payload)
        and all(isinstance(e, dict) and _ACTION_VERBS.get(str(e.get("action") or "")) == "remove" for e in payload)
    )


# ---------------------------------------------------------------------------
# what
# ---------------------------------------------------------------------------


def _describe_edit(e: dict) -> str:
    action = str(e.get("action") or "")
    verb = _ACTION_VERBS.get(action, action or "change")
    kind = str(e.get("kind") or "")
    kind_words = KIND_LABELS.get(kind, kind or "an entry")
    scope = str(e.get("scope") or "")
    where = "" if scope in ("", "global") else f" (only in {scope})"
    entry_id = str(e.get("entry_id") or "")
    title = str(e.get("title") or entry_id or "?")

    if action == "memory_correction":
        files = ", ".join(str(f) for f in (e.get("files") or []) if f) or "a memory file"
        return f"correct {files}: {_quote(e.get('content') or e.get('reason') or '')}"

    if verb == "remove":
        current = db.adaptive_get_entry(entry_id) if entry_id else None
        reason = ""
        for ev in e.get("evidence") or []:
            if isinstance(ev, str) and ev.startswith("retired:"):
                reason = ev.split(":", 1)[1].strip()
        body = f' — it currently says "{_quote(current.get("content") or "", 160)}"' if current else ""
        why = f" because {reason}" if reason else ""
        return f"remove {kind_words} called '{title}'{where}{body}{why}"

    content = _quote(e.get("content") or "")
    said = f': "{content}"' if content else ""
    return f"{verb} {kind_words} called '{title}'{where}{said}"


def _what_canary(prop: dict, spec: dict) -> str:
    name = spec.get("name") or "?"
    prompt = _quote(spec.get("prompt") or "", 220)
    gates = [str(g.get("name") or "?") for g in (spec.get("gates") or []) if isinstance(g, dict)]
    checks = ", ".join(gates) if gates else "its result"
    return (
        f"{producer_label(prop.get('producer'))} wants to add a new self-test called '{name}'. "
        f"A self-test replays a situation the agent once got wrong and checks a future agent gets it right. "
        f'This one gives the agent the task: "{prompt}" and then checks: {checks}. '
        "Approving writes the test into the suite and runs it once to vet it; it does not change how the agent behaves."
    )


def what(prop: dict) -> str:
    payload = _payload(prop)
    who = producer_label(prop.get("producer"))
    if isinstance(payload, dict) and payload.get("canary"):
        return _what_canary(prop, payload.get("canary") or {})
    if isinstance(payload, list) and payload:
        edits = [e for e in payload if isinstance(e, dict)]
        parts = [_describe_edit(e) for e in edits[:3]]
        more = f", and {len(edits) - 3} more change(s)" if len(edits) > 3 else ""
        lead = "wants to " if len(parts) == 1 else "wants to make these changes: "
        joined = parts[0] if len(parts) == 1 else "; ".join(parts)
        effect = (
            "Approving removes it from the agent's instructions right away."
            if _is_delete_only(payload)
            else "Approving puts this into the agent's instructions right away; you can roll it back from the Learning tab."
        )
        return f"{who} {lead}{joined}{more}. {effect}"
    return (
        f"{who} raised something to acknowledge: {_quote(prop.get('rationale') or '', 200) or 'no details recorded'}. "
        "Approving records that you saw it; nothing changes."
    )


# ---------------------------------------------------------------------------
# why
# ---------------------------------------------------------------------------


def _refs_in_words(evidence: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(receipt phrases, session ids, free-text lines)."""
    counts: dict[str, int] = {}
    sessions: list[str] = []
    free: list[str] = []
    for line in evidence:
        head, sep, tail = line.partition(":")
        head = head.strip().lower()
        if sep and head in _REF_LABELS:
            counts[head] = counts.get(head, 0) + 1
        elif sep and head == "session":
            sessions.append(tail.strip())
        elif sep and head in ("dream_hypothesis", "memory", "agent"):
            continue  # bookkeeping refs the reader gains nothing from
        elif sep and head == "retired":
            free.append(f"the rule's original evidence no longer holds ({tail.strip()})")
        else:
            free.append(line)
    phrases = []
    for kind, n in counts.items():
        one, many = _REF_LABELS[kind]
        phrases.append(f"{n} {one if n == 1 else many}")
    return phrases, sessions, free


def why(prop: dict) -> str:
    evidence = _evidence(prop)
    phrases, sessions, free = _refs_in_words(evidence)
    rationale = str(prop.get("rationale") or "")
    # Canary rationales carry a machine suffix "(proposed by refine; approving
    # writes ...; not auto-admitted: ...)" that `fate` already renders.
    rationale = rationale.split(" (proposed by ", 1)[0]
    rationale = re.sub(r"^\[new canary '[^']*'\]\s*", "", rationale)
    if rationale.startswith("dream adaptive-entry retirement"):
        rationale = ""  # the "retired:" evidence line says the same thing, better
    rationale = _quote(rationale, 220)
    bits: list[str] = []
    if free:
        bits.append("What it saw: " + " ".join(_quote(f, 260) for f in free[:2]))
    if sessions:
        shown = ", ".join(s[:12] for s in sessions[:2])
        bits.append(f"It came out of session {shown}.")
    if phrases:
        bits.append(f"It is backed by {', '.join(phrases)} the system recorded.")
    elif not free:
        bits.append("It cites nothing the system recorded — only its own reasoning.")
    if rationale and not any(rationale[:40] in b for b in bits):
        bits.append(f"In its own words: {rationale}")
    return " ".join(bits) if bits else "No evidence was recorded with this proposal."


# ---------------------------------------------------------------------------
# fate
# ---------------------------------------------------------------------------


def _not_admitted_reason(prop: dict) -> str:
    m = _NOT_ADMITTED_RE.search(str(prop.get("rationale") or ""))
    return m.group(1).strip().rstrip(")") if m else ""


def _window_close(prop: dict) -> datetime | None:
    window = settings.adaptive_auto_approve_after_hours
    if window <= 0 or not prop.get("created_at"):
        return None
    try:
        created = datetime.fromisoformat(str(prop["created_at"]))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created + timedelta(hours=window)


def _human_delta(until: datetime, now: datetime) -> str:
    secs = int((until - now).total_seconds())
    if secs <= 0:
        return "now"
    if secs < 3600:
        return f"in {max(1, secs // 60)} min"
    if secs < 48 * 3600:
        return f"in {secs // 3600} h"
    return f"in {secs // 86400} days"


def fate(prop: dict, now: datetime | None = None) -> tuple[str, str]:
    """(fate_kind, sentence). Mirrors auto_approve_stale_proposals exactly:
    canary → never; unfounded non-delete → held; else the clock."""
    now = now or datetime.now(timezone.utc)
    status = prop.get("status") or "pending"
    if status != "pending":
        return NEEDS_YOU, f"Already {status.replace('_', ' ')}."

    payload = _payload(prop)
    if isinstance(payload, dict) and payload.get("canary"):
        reason = _not_admitted_reason(prop)
        because = f" It was not admitted on its own because: {reason}." if reason else ""
        return NEEDS_YOU, (
            "Waits for you. New self-tests never apply on their own; nothing happens until you approve or reject."
            + because
        )

    if not settings.adaptive_enabled:
        return NEEDS_YOU, "Waits for you. The adaptive layer is switched off, so nothing applies on its own."
    close = _window_close(prop)
    if close is None:
        return NEEDS_YOU, "Waits for you. Auto-apply is switched off (adaptive_auto_approve_after_hours = 0)."

    if not _is_delete_only(payload):
        try:
            from core.adaptive.receipts import UNFOUNDED, grade_evidence_json

            if grade_evidence_json(prop.get("evidence_json")) == UNFOUNDED:
                return HELD, (
                    "Held for you. Its evidence points to nothing the system recorded, so the clock will not apply it. "
                    "It stays here until you approve or reject it."
                )
        except Exception as e:  # pragma: no cover — decoration never fails a listing
            logger.debug("explain: receipts grade failed for %s: %s", prop.get("id"), e)

    when = close.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if close <= now:
        return AUTO, (
            f"Applies on its own at the next quiet moment (its {settings.adaptive_auto_approve_after_hours}-hour veto window closed {when}) "
            "unless you reject it first."
        )
    return AUTO, f"Applies on its own {_human_delta(close, now)} ({when}) unless you reject it first."


# ---------------------------------------------------------------------------
# the whole card
# ---------------------------------------------------------------------------


def explain_proposal(prop: dict, now: datetime | None = None) -> dict:
    """Never raises: a proposal the explainer cannot read still gets a card."""
    out = {
        "what": "",
        "why": "",
        "fate": "",
        "fate_kind": NEEDS_YOU,
        "producer_label": producer_label(prop.get("producer")),
    }
    try:
        out["what"] = what(prop)
    except Exception as e:
        logger.debug("explain: what() failed for %s: %s", prop.get("id"), e)
        out["what"] = f"{out['producer_label']} proposed a change this panel could not describe ({type(e).__name__})."
    try:
        out["why"] = why(prop)
    except Exception as e:
        logger.debug("explain: why() failed for %s: %s", prop.get("id"), e)
        out["why"] = "The evidence could not be read."
    try:
        out["fate_kind"], out["fate"] = fate(prop, now=now)
    except Exception as e:
        logger.debug("explain: fate() failed for %s: %s", prop.get("id"), e)
        out["fate"] = "Waits for you."
    return out


def explain_as_text(prop: dict) -> str:
    """The same three parts as one block — what the agent reads in a session."""
    ex = explain_proposal(prop)
    return f"WHAT: {ex['what']}\nWHY: {ex['why']}\nIF YOU DO NOTHING: {ex['fate']}"
