"""A long first turn was killed by a compactor that had correctly declined.

`_clamp_boundary_to_live_turn` holds the boundary at the live turn's root, so
on a first turn there is nothing behind it: `to_summarize` is empty and
`compact_with_llm` returns False without calling a summarizer at all. That
clamp is deliberate and load-bearing (2026-08-07, 2026-09-01). The bug was
what the loop did with the answer: `compaction.run()` returning False went
straight to `termination_reason = "compaction_failed"`, so a refusal and a
failure ended the turn identically.

They are not the same. The compiler had already fitted history into its own
budget and pinned an id-addressable trim notice — the request was sendable.
Utilization was over the critical threshold because of the OUTPUT
RESERVATION, not because history overflowed: `derive_max_output` returns the
provider's `max_completion_tokens` cap, and on a 192k window any cap under
~26.8k puts a full first turn over 0.85. Measured on a 60-round first turn:
max_output 32,000 -> 0.773 (safe), 16,384 -> 0.906, 8,192 -> 0.936 with
nothing trimmed at all. Pernix's own default of 32,000 measures 0.842, about
one percent under the trip.

Also pinned here, three smaller evidence leaks on the path that does work:
the trim notice was unbounded even though it is pinned and never re-trimmed,
a dropped plain assistant row was rendered at 80 characters where a dropped
user row gets 500, and the view-pruning stub named no message id at all, so a
pruned tool result was the one dropped thing in the window with no way back.
"""

import json

import pytest

from core.agent import _CompactionController, run_agent
from core.context.compaction import NOTHING_TO_SUMMARIZE, apply_view_pruning
from core.context.compiler import _NOTICE_MAX_CHARS, _build_trim_notice
from core.llm.types import StreamEvent, StreamEventType, TokenUsage
from sessions import state_v2 as sv2
from sessions.state import AgentSession


def _stream_answer() -> list[StreamEvent]:
    return [
        StreamEvent(type=StreamEventType.TOKEN, content="the audit is done"),
        StreamEvent(type=StreamEventType.USAGE, usage=TokenUsage(prompt_tokens=10, completion_tokens=5)),
        StreamEvent(type=StreamEventType.DONE),
    ]


def _long_first_turn(db, sid: str, rounds: int = 40) -> int:
    """One user root and nothing but this turn's own work after it."""
    root = db.add_message(sid, "user", "Port the parser and get the suite green.")
    for i in range(rounds):
        db.add_message(
            sid,
            "assistant",
            f"round-{i:02d} MILESTONE: " + ("reasoning about the failure " * 40),
            tool_calls=json.dumps([{"id": f"tc{i}", "name": "bash", "arguments": '{"command": "pytest -q"}'}]),
        )
        db.add_message(sid, "tool", f"log-{i:02d}\n" + ("FAILED tests/test_x.py::case\n" * 120), tool_call_id=f"tc{i}")
    return root


@pytest.fixture
def fake_stream(monkeypatch):
    from core.tools.registry import ToolRegistry
    from tests.conftest import FakeLLMClient

    fake = FakeLLMClient(stream_events=[_stream_answer()])
    monkeypatch.setattr("core.agent.get_llm_client", lambda: fake)
    monkeypatch.setattr("core.llm.client._client", fake)
    monkeypatch.setattr("core.agent.get_registry", lambda: ToolRegistry())
    return fake


async def test_a_long_first_turn_survives_on_the_trim_path(fake_stream, monkeypatch):
    """The turn that used to die with compaction_failed now answers."""
    from db import models as db

    # A small provider completion cap is the whole trigger: it leaves history
    # more room, so nothing is wrong, and pushes utilization over 0.85.
    monkeypatch.setattr("core.agent.derive_max_output", lambda model: 500)

    sid = db.create_session(title="Long first turn")
    root = _long_first_turn(db, sid)
    session = AgentSession(session_id=sid)
    session.current_turn_user_msg_id = root
    session.context_budget_override = 20_000

    await run_agent(sid, "Port the parser and get the suite green.", session, pre_saved=True)

    # Measured at this fixture: 20,000-token budget, 500-token output
    # reservation, 58 rows trimmed, utilization 0.891 — over the 0.85
    # critical threshold with history already inside its own budget.
    assert session.termination_reason != "compaction_failed"
    assert session.termination_reason == "complete"

    # It survived VIA TRIM, and the notice it survived on is addressable.
    sent = fake_stream.calls[-1]["messages"]
    notice = next(
        m["content"] for m in sent if isinstance(m.get("content"), str) and m["content"].startswith("[Context trim")
    )
    assert "session_read(msg_id)" in notice
    ids = {m["id"] for m in db.get_messages(sid)}
    assert any(str(i) in notice for i in ids), "the notice must name the rows it dropped"

    # The root ask is pinned and still verbatim in the request.
    assert any("Port the parser" in str(m.get("content", "")) for m in sent)


async def test_a_refusal_is_not_an_attempt_and_does_not_park_the_session(monkeypatch):
    """Nothing to summarize is not a failed compaction: no attempt is spent,
    the session returns to PROCESSING, and cannot_help latches."""
    from db import models as db

    async def _refuses(session_id, messages, **kwargs):
        outcome = kwargs.get("outcome")
        if outcome is not None:
            outcome.reason = NOTHING_TO_SUMMARIZE
        return False

    monkeypatch.setattr("core.agent.compact_with_llm", _refuses)
    sid = db.create_session(title="first turn")
    session = AgentSession(session_id=sid)
    session.emit_event = lambda e: None
    sv2._set_state(session, sv2.SessionStateV2.PROCESSING)

    controller = _CompactionController(session, sid)
    payload = type("P", (), {"token_count": 19_000, "messages": [], "history_budget": 15_000})()

    assert await controller.run(payload, transition_reason="compact-critical") is False
    assert controller.cannot_help is True
    assert controller.attempts == 0, "a refusal must not burn the attempt budget"
    assert not controller.exhausted
    assert sv2._current_state(session) == sv2.SessionStateV2.PROCESSING
    # Announced once per turn, not once per round.
    assert controller.announce_floor() is True
    assert controller.announce_floor() is False


async def test_a_compaction_that_really_failed_still_ends_the_turn(monkeypatch):
    """The fall-through is for refusals only. A compactor that tried and lost
    keeps its old meaning, or a genuinely full context runs forever."""
    from db import models as db

    async def _fails(session_id, messages, **kwargs):
        return False  # no outcome written: the cautious default is "failed"

    monkeypatch.setattr("core.agent.compact_with_llm", _fails)
    sid = db.create_session(title="really failed")
    session = AgentSession(session_id=sid)
    session.emit_event = lambda e: None
    sv2._set_state(session, sv2.SessionStateV2.PROCESSING)

    controller = _CompactionController(session, sid)
    payload = type("P", (), {"token_count": 19_000, "messages": [], "history_budget": 15_000})()

    assert await controller.run(payload, transition_reason="compact-critical") is False
    assert controller.cannot_help is False
    assert controller.attempts == 1


def test_the_trim_notice_cannot_become_the_next_context_consumer():
    """The notice is pinned, so its tokens are permanent for the turn. Three
    hundred dropped groups must not answer a context problem with three
    hundred lines — and the collapsed ones stay reachable by id."""
    groups = [
        {
            "kind": "assistant_group",
            "msgs": [
                {"_db_id": 1000 + i, "role": "assistant", "_tool_names": ["bash"], "content_len": 5000},
                {"_db_id": 2000 + i, "role": "tool", "content_len": 40_000},
            ],
        }
        for i in range(300)
    ]
    groups.insert(
        0,
        {
            "kind": "user",
            "msgs": [{"_db_id": 7, "role": "user", "content_full": "the original ask", "content_len": 16}],
        },
    )

    notice = _build_trim_notice(groups)
    assert len(notice) <= _NOTICE_MAX_CHARS
    assert len(notice.splitlines()) < 60
    # The user's own words survive, and the collapsed remainder is addressable.
    assert "the original ask" in notice
    assert "further dropped item(s)" in notice
    assert "session_read" in notice


def test_a_dropped_assistant_row_is_quoted_like_a_dropped_user_row():
    """`_snapshot` captured 500 characters; the renderer showed 80."""
    body = "MILESTONE: schema v3 rejected, fallback is v2 because " + ("of the migration cost " * 30)
    groups = [
        {
            "kind": "other",
            "msgs": [
                {
                    "_db_id": 42,
                    "role": "assistant",
                    "content_len": len(body),
                    "content_preview": body[:500],
                }
            ],
        }
    ]
    notice = _build_trim_notice(groups)
    assert body[:400] in notice
    assert "msg 42" in notice


def test_a_view_pruned_tool_result_names_its_message_id():
    """The stub said how many characters used to be here and nothing about
    where they went."""
    messages = [{"id": 100 + i, "role": "user", "content": f"msg {i}"} for i in range(10)]
    messages.insert(0, {"id": 55, "role": "tool", "content": "x" * 5_000})

    stub = apply_view_pruning(messages, keep_recent=2, min_chars=10)[0]["content"]
    assert "msg 55" in stub
    assert "session_read(55)" in stub
    assert "5000 chars" in stub
