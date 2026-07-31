"""Session capability policy — the single predicate behind every read-only gate.

The server (chat endpoints) and the client (composer state) must agree on
which sessions accept messages. This used to live as independent
session_type == "snooze" string comparisons in chat.py and app.js — two
copies of the same rule that could drift. Now chat.py enforces the predicate
and the session payloads carry the verdict (read_only + read_only_reason),
so the UI renders policy instead of re-deriving it.
"""

_READ_ONLY_REASONS = {
    # Dream journals: Pernix narrates them during snooze; user messages would
    # interleave into the narration and be destroyed by journal retention.
    "snooze": "This is a dream journal (read-only). Start a chat session to discuss its contents.",
    # RLM view sessions: message-less sidebar anchors for a run's trace
    # viewer. There is no transcript to chat in — the conversation lives in
    # the parent session that launched the run.
    "rlm": "This is an RLM run view (read-only). Chat in the parent session — this page just watches the run.",
}


def read_only_reason(session_row: dict | None) -> str | None:
    """Why this session rejects new messages, or None if it accepts them."""
    return _READ_ONLY_REASONS.get(((session_row or {}).get("session_type")) or "")


def annotate_read_only(session_row: dict) -> dict:
    """Stamp read_only / read_only_reason onto an API session payload (in place)."""
    reason = read_only_reason(session_row)
    session_row["read_only"] = reason is not None
    session_row["read_only_reason"] = reason
    return session_row
