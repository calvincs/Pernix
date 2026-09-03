"""Regression: a tool that wrote its own message row killed the whole session.

Field case a78c6be3fe55 (procedural GIF work). The agent called `view_image`
on a render it had just made. `view_image` persists its synthetic `[view_image]`
user note *while it is still running*, so the note gets a message id BETWEEN
the assistant row that made the call and the tool result row that answered it:

    52138 assistant  tool_calls=[view_image]
    52139 user       [view_image] ... (injected, stamped to the turn root)
    52140 tool       "Image queued: ..."

`repair_unanswered_tool_calls` counted answers by adjacency — it walked forward
from the assistant row and stopped at the first non-`tool` row. The note stopped
it immediately, so a call that HAD returned was declared unanswered and got an
"aborted" stub appended. That stub was a bare dict with no `id`, and the
compiler's conversion loop did `msg["id"]`, so the turn died with
`KeyError: 'id'` before its first LLM call.

The damage outlived the turn. Row order is in the database, so every later
message in that session recompiled the same history and died the same way, in
milliseconds, forever — the session could not be continued and its Context tab
returned 500. Two fixes, both needed: answers are matched by tool_call_id
across the whole list instead of by adjacency, and the compiler reads `id`
with `.get` because history is no longer purely DB rows by that point.
"""

from __future__ import annotations

import json

from core.context.compaction import ABORTED_CALL_STUB, repair_unanswered_tool_calls
from core.context.compiler import compile_context


def _assistant(mid: int, *call_ids: str) -> dict:
    return {
        "id": mid,
        "role": "assistant",
        "content": "",
        "tool_calls": json.dumps([{"id": c, "name": "view_image", "arguments": "{}"} for c in call_ids]),
    }


def _stubs(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m.get("content") == ABORTED_CALL_STUB]


# ---------------------------------------------------------------------------
# The repair pass itself
# ---------------------------------------------------------------------------


def test_answer_separated_by_a_mid_round_row_is_not_stubbed():
    """The exact a78c6be3fe55 shape: the answer is present, just not adjacent."""
    history = [
        _assistant(52138, "tc-1"),
        {"id": 52139, "role": "user", "content": "[view_image] ...", "metadata": json.dumps({"injected": True})},
        {"id": 52140, "role": "tool", "content": "Image queued", "tool_call_id": "tc-1"},
    ]

    out = repair_unanswered_tool_calls(history)

    assert _stubs(out) == []
    assert [m["id"] for m in out] == [52138, 52139, 52140]


def test_every_repaired_row_carries_an_id():
    """A stub with no `id` was what actually crashed the compiler."""
    history = [_assistant(700, "tc-dead")]

    out = repair_unanswered_tool_calls(history)

    assert all("id" in m for m in out), [m for m in out if "id" not in m]
    assert _stubs(out)[0]["id"] == 700  # grouped with the round it belongs to


def test_a_genuinely_unanswered_call_still_gets_its_stub():
    """The original bug this pass exists for must stay fixed: a round that dies
    mid-flight leaves an assistant tool_calls row that providers reject."""
    history = [
        {"id": 1, "role": "user", "content": "go"},
        _assistant(2, "tc-ok", "tc-lost"),
        {"id": 3, "role": "tool", "content": "done", "tool_call_id": "tc-ok"},
    ]

    out = repair_unanswered_tool_calls(history)

    stubs = _stubs(out)
    assert [s["tool_call_id"] for s in stubs] == ["tc-lost"]
    # Recorded results keep their place ahead of the stub.
    assert [m.get("tool_call_id") for m in out if m["role"] == "tool"] == ["tc-ok", "tc-lost"]


def test_partial_answers_around_a_mid_round_row():
    """One call answered late, one never answered — stub only the second."""
    history = [
        _assistant(10, "tc-a", "tc-b"),
        {"id": 11, "role": "user", "content": "[view_image] ...", "metadata": json.dumps({"injected": True})},
        {"id": 12, "role": "tool", "content": "answer a", "tool_call_id": "tc-a"},
    ]

    out = repair_unanswered_tool_calls(history)

    assert [s["tool_call_id"] for s in _stubs(out)] == ["tc-b"]


# ---------------------------------------------------------------------------
# End to end through the compiler — the crash the user actually hit
# ---------------------------------------------------------------------------


def test_compile_survives_a_view_image_round():
    from db import models as db

    sid = db.create_session(title="ViewImageRound")
    root = db.add_message(sid, "user", "ROOT render me a gif")
    db.add_message(
        sid,
        "assistant",
        "",
        tool_calls=json.dumps([{"id": "tc-vi", "name": "view_image", "arguments": '{"path": "out.gif"}'}]),
        metadata=json.dumps({"parent_user_msg_id": root}),
    )
    db.add_message(
        sid,
        "user",
        "[view_image] Harness-injected on the agent's own request.\n[attached: /tmp/out.gif]",
        metadata=json.dumps({"injected": True, "parent_user_msg_id": root}),
    )
    db.add_message(
        sid,
        "tool",
        "Image queued: /tmp/out.gif (12051 bytes).",
        tool_call_id="tc-vi",
        metadata=json.dumps({"parent_user_msg_id": root}),
    )

    payload = compile_context(sid, turn_user_msg_id=root)  # used to raise KeyError: 'id'

    texts = [m["content"] for m in payload.messages if isinstance(m.get("content"), str)]
    assert not any(ABORTED_CALL_STUB in t for t in texts)
    assert any("[view_image]" in t for t in texts)
    assert sum(1 for m in payload.messages if m.get("tool_call_id") == "tc-vi") == 1


def test_compile_tolerates_a_synthetic_row_without_an_id(monkeypatch):
    """Belt and braces: even if some future splice forgets the id, the turn
    must compile. A missing key used to be a hard session kill."""
    from db import models as db

    sid = db.create_session(title="IdlessSplice")
    root = db.add_message(sid, "user", "ROOT hello")
    db.add_message(sid, "assistant", "REPLY", metadata=json.dumps({"parent_user_msg_id": root}))

    real = repair_unanswered_tool_calls

    def _splice_idless(messages):
        return real(messages) + [{"role": "tool", "tool_call_id": "tc-x", "content": "no id here"}]

    monkeypatch.setattr("core.context.compaction.repair_unanswered_tool_calls", _splice_idless)

    payload = compile_context(sid, turn_user_msg_id=root)

    assert any(m.get("content") == "no id here" for m in payload.messages)
