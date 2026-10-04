"""Distillation re-read the same opening bytes of a session forever.

`_maybe_distill` runs at the END OF EVERY TURN on `db.get_messages(session_id)`
— the whole session, with no gate of any kind. `distill_session` clipped each
message to its first 800 characters (400 for tool results) and sent the
extractor `transcript[:40000]`.

Measured on a 365-message session: 535,292 raw chars became 245,447 after the
per-message clip, of which 40,000 were sent — 7.5% of the session. Past that
size the prefix is FROZEN: two consecutive distillations sent byte-identical
input, so every turn spent a background LLM call re-extracting material it had
already extracted, and no late correction could ever arrive. Long research
that begins with a hypothesis and refutes it stored the hypothesis.

There was no watermark to fix this with: grep for watermark / covered /
last_distilled / distilled_up_to returned nothing, and `snooze_reviewed_at`
gates only the snooze catch-up path.

Now: a durable `sessions.distilled_up_to` (v38), bounded chunks over the new
material only, coverage committed per chunk and only after extraction AND
storage both succeed, head-and-tail clipping so a correction at the end of a
long message survives, and provenance on every stored entry that separates a
tool-verified outcome from an agent assertion.
"""

import json

import pytest

from core.llm.types import ChatResponse, TokenUsage
from core.memory.distill import distill_session
from core.memory.store import get_memory_store


def _resp(content: str) -> ChatResponse:
    return ChatResponse(
        content=content, tool_calls=None, usage=TokenUsage(10, 20, 30), model="t", provider="fake", finish_reason="stop"
    )


def _entry(**over) -> dict:
    base = {
        "type": "note",
        "content": "The ARC rule is a chained path, not a per-object colour map.",
        "file": "pernix.research",
        "tags": "arc",
        "weight": "normal",
    }
    base.update(over)
    return base


def _entries(*entries: dict) -> ChatResponse:
    return _resp(json.dumps(list(entries) or [_entry()]))


def _sent(client) -> str:
    return "\n".join(call["messages"][1]["content"] for call in client.calls)


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.memory_recall", True)
    return get_memory_store()


def _long_research(db, rounds: int = 60) -> tuple[str, dict]:
    """A session that begins with a wrong hypothesis and ends refuting it."""
    sid = db.create_session(title="Long research session")
    ids: dict[str, int] = {}
    db.add_message(sid, "user", "Why is the ARC task failing? Keep the per-object constraint in mind.")
    ids["hypothesis"] = db.add_message(
        sid, "assistant", "HYPOTHESIS-ALPHA: the rule is a per-object colour map; I will encode it that way."
    )
    for i in range(rounds):
        db.add_message(sid, "user", f"ROUND-{i:03d}-Q " + ("detail " * 80))
        if i == 20:
            # A correction at the END of a long message: past the old
            # 800-character head clip, inside the new tail.
            ids["clip"] = db.add_message(
                sid,
                "assistant",
                f"ROUND-{i:03d}-A " + ("preamble " * 300) + " CORRECTION-CLIP: the colour-map encoding never fit.",
            )
        else:
            db.add_message(sid, "assistant", f"ROUND-{i:03d}-A " + ("analysis " * 80))
        db.add_message(sid, "tool", f"ROUND-{i:03d}-T " + ("bytes " * 40), tool_call_id=f"tc{i}")
    db.add_message(sid, "user", "So the colour-map theory was wrong?")
    ids["final"] = db.add_message(
        sid, "assistant", "CORRECTION-FINAL: confirmed — HYPOTHESIS-ALPHA is wrong. The rule is a CHAINED PATH."
    )
    return sid, ids


async def test_a_late_correction_finally_reaches_the_extractor(mock_llm_client, memory):
    """The refutation is past the per-message clip in one case and past the
    40,000-char aggregate cutoff in the other. Both used to be invisible."""
    from db import models as db

    sid, ids = _long_research(db)
    messages = db.get_messages(sid)
    mock_llm_client.responses = [_entries()]

    await distill_session(sid, title="Long research session", messages=messages)
    sent = _sent(mock_llm_client)

    assert "HYPOTHESIS-ALPHA" in sent
    assert "CORRECTION-CLIP" in sent, "a correction at the end of a long message must survive the clip"
    assert "CORRECTION-FINAL" in sent, "the session's conclusion is past the old 40,000-char cutoff"
    # Source ids are in the transcript, so an entry can cite what it rests on.
    assert f"#{ids['final']}" in sent

    # Coverage landed, and a second pass over an UNCHANGED session is a no-op
    # instead of another byte-identical call.
    assert db.get_distill_watermark(sid) == ids["final"]
    before = len(mock_llm_client.calls)
    await distill_session(sid, title="Long research session", messages=messages)
    assert len(mock_llm_client.calls) == before, "nothing new must cost no LLM call"


async def test_a_second_pass_over_a_grown_session_sends_new_material(mock_llm_client, memory):
    """The headline waste: past ~40k the input never changed again."""
    from db import models as db

    sid, ids = _long_research(db)
    mock_llm_client.responses = [_entries()]
    await distill_session(sid, title="Long research session", messages=db.get_messages(sid))
    first = _sent(mock_llm_client)
    mock_llm_client.calls.clear()

    db.add_message(sid, "user", "And what does that mean for the encoder?")
    db.add_message(sid, "assistant", "LATE-FINDING: the encoder needs a path walker, not a lookup table.")

    mock_llm_client.responses = [_entries(_entry(content="The encoder needs a path walker."))]
    await distill_session(sid, title="Long research session", messages=db.get_messages(sid))
    second = _sent(mock_llm_client)

    assert second != first
    assert "LATE-FINDING" in second
    # Already-covered bodies are not re-sent...
    assert "ROUND-005-A" not in second
    # ...but the session's opening request still frames the new material, so
    # this is not a tail-only view that forgets the standing constraint.
    assert "per-object constraint" in second
    assert "distilled previously" in second


async def test_a_failed_extraction_does_not_commit_coverage(mock_llm_client, memory):
    """Coverage is a claim that the material was processed. It is committed
    after extraction AND storage, never before."""
    from db import models as db

    sid, _ids = _long_research(db, rounds=6)
    messages = db.get_messages(sid)

    async def failing_chat(*args, **kwargs):
        raise ConnectionError("extractor down")

    original = mock_llm_client.chat
    mock_llm_client.chat = failing_chat
    await distill_session(sid, title="Long research session", messages=messages)
    assert db.get_distill_watermark(sid) == 0

    # Unparseable output is a failed extraction too, not an empty one.
    mock_llm_client.chat = original
    mock_llm_client.responses = [_resp("I could not produce JSON for this one, sorry.")]
    await distill_session(sid, title="Long research session", messages=messages)
    assert db.get_distill_watermark(sid) == 0

    # And a run that works does commit it.
    mock_llm_client.responses = [_entries()]
    await distill_session(sid, title="Long research session", messages=messages)
    assert db.get_distill_watermark(sid) == max(m["id"] for m in messages)


async def test_assistant_prose_is_not_evidence_that_something_was_verified(mock_llm_client, memory):
    """The completion prose says the suite is green; the tool result says
    three tests failed. A stored claim has to say which of those it rests on."""
    from db import models as db

    sid = db.create_session(title="Suite run")
    db.add_message(sid, "user", "Run the suite and tell me where we stand on the parser port.")
    prose_id = db.add_message(sid, "assistant", "All tests pass now; the parser port is complete and green. " * 6)
    tool_id = db.add_message(sid, "tool", "3 failed, 41 passed — tests/test_parser.py::test_nested_groups " * 6)
    db.add_message(sid, "assistant", "Correction: three parser tests still fail; the port is not complete. " * 4)

    mock_llm_client.responses = [
        _entries(
            {
                "type": "finding",
                "content": "The parser port is complete and the suite is green.",
                "file": "pernix.research",
                "tags": "parser",
                "weight": "high",
                "status": "verified",
                "source_msgs": [prose_id],
            },
            {
                "type": "finding",
                "content": "Three parser tests still fail after the port; test_nested_groups is one of them.",
                "file": "pernix.lessons",
                "tags": "parser",
                "weight": "high",
                "status": "verified",
                "source_msgs": [tool_id],
                "supersedes": "that the parser port was complete and the suite green",
            },
        )
    ]
    await distill_session(sid, title="Suite run", messages=db.get_messages(sid))

    # The claim citing only assistant prose is stored as an assertion, however
    # confidently the extractor labelled it "verified".
    research = memory.read_file("pernix.research") or ""
    assert "agent assertion, no tool result cited" in research
    assert "tool-verified" not in research
    assert f"msgs {prose_id}" in research

    # The claim citing the tool result is, and it carries what it corrects.
    lessons = memory.read_file("pernix.lessons") or ""
    assert "tool-verified" in lessons
    assert f"msgs {tool_id}" in lessons
    assert "[Corrects an earlier claim: that the parser port was complete" in lessons
