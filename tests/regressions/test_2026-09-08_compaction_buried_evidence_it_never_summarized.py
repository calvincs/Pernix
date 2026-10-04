"""Compaction retired evidence the summarizer was never shown.

`_serialize_messages` took 2,000 characters per message, stopped near 60,000
characters, read `content` and nothing else, and returned a string. The
boundary was then set from the *slice*, not from that string: `compacted_up_to
= to_summarize[-1]["id"]`. On a realistic slice — 59 rows, 279,892 chars —
58,349 chars reached the summarizer and 221,543 (79.2%) did not, yet the
marker advanced over all 59. In default production config roughly 70% of every
steady-state slice was retired un-summarized, and `compile_context` filters on
`id > compacted_up_to`, so those rows left the window permanently with no
notice and no pointers.

Three ways a fact could be behind the boundary and absent from the summary:
past the per-message clip, past the aggregate cutoff, and stored only in an
assistant row's `tool_calls` — which the serializer never read at all, so a
whole tool round arrived as the twelve characters "[assistant] " and the
command it ran existed nowhere in the input.

The ratio gate had the same denominator bug: it divided by the full slice
rather than the input actually supplied. Measured 0.0010 against an honest
0.0046 — 4.8x lenient, so a bloated summary of a small prefix could never trip
the 0.35 threshold.

Now: complete message/tool groups, chunked across as many calls as the slice
needs, each chunk gated against its own input, and the boundary walks the
slice and stops at the first row no accepted chunk covered.
"""

import json

import pytest

from core.context.compaction import _chunk_slice, compact_with_llm
from core.context.tokens import get_estimator
from core.llm.types import ChatResponse, TokenUsage

MARKER_ROLES = {"compaction", "scout", "notice", "reflect", "model_divider", "eval"}


def _summary(text: str = "") -> ChatResponse:
    return ChatResponse(
        content=text
        or (
            '```json\n{"goal": "long research", "progress": ["p"]}\n```\n'
            "Summary prose long enough to clear the twenty-token floor this "
            "compaction quality gate applies to every chunk it accepts."
        ),
        tool_calls=None,
        usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        model="test",
        provider="fake",
        finish_reason="stop",
    )


def _filler(row: int, chars: int) -> str:
    """A body that opens with a unique per-row token, so a test can ask
    whether THAT row reached the summarizer."""
    head = f"ROW-{row:03d}-MARK "
    return head + ("analysis text " * (chars // 14))[: max(chars - len(head), 0)]


def _build_slice(db, sid: int) -> dict:
    """40 old rounds, then a live turn. Returns the planted ids."""
    ids: dict[str, int] = {}
    for i in range(40):
        role = "user" if i % 2 == 0 else "assistant"
        if i == 4:
            # Past the old 2,000-char per-message clip.
            body = _filler(i, 2_500) + " CORRECTION-BETA-9271 the earlier hypothesis is refuted. " + _filler(i, 1_000)
            ids["beta"] = db.add_message(sid, role, body)
        elif i == 6:
            # An assistant tool round: content is "", everything is in
            # tool_calls. One admitted call, one the gate refused.
            ids["toolcall"] = db.add_message(
                sid,
                "assistant",
                "",
                tool_calls=json.dumps(
                    [
                        {
                            "id": "tc1",
                            "name": "bash",
                            "arguments": json.dumps({"command": "python /srv/audit/PATCH-DELTA-7788.py"}),
                        },
                        {"id": "tc2", "name": "nope", "arguments": "{}", "_rejected": True},
                    ]
                ),
            )
            db.add_message(sid, "tool", f"ROW-{i:03d}-MARK exit 0", tool_call_id="tc1")
            db.add_message(sid, "tool", "Tool 'nope' does not exist", tool_call_id="tc2")
        elif i == 12:
            # Past the per-message clip that still applies: this one must be
            # recorded as clipped rather than counted as covered whole.
            body = _filler(i, 6_000) + " CORRECTION-OMEGA-3140 buried past the body clip. " + _filler(i, 3_000)
            ids["omega"] = db.add_message(sid, role, body)
        elif i == 30:
            # Past the old 60,000-char aggregate cutoff.
            ids["gamma"] = db.add_message(sid, role, "CORRECTION-GAMMA-5512 " + _filler(i, 5_000))
        else:
            db.add_message(sid, role, _filler(i, 5_000))

    ids["live_root"] = db.add_message(sid, "user", "LIVE TURN: keep going with the audit.")
    db.add_message(sid, "assistant", "working on it")
    return ids


def _marker(db, sid: int) -> dict | None:
    markers = [m for m in db.get_messages(sid) if m["role"] == "compaction"]
    return markers[-1] if markers else None


def _prompts(client) -> str:
    return "\n".join(call["messages"][1]["content"] for call in client.calls)


@pytest.fixture
def big_slice(monkeypatch):
    from db import models as db

    monkeypatch.setattr("config.settings.compaction_keep_tokens", 2_000)
    sid = db.create_session(title="Long research turn")
    ids = _build_slice(db, sid)
    stripped = [{"role": m["role"], "content": m["content"]} for m in db.get_messages(sid)]
    return sid, ids, stripped


async def test_the_boundary_stops_where_the_summarizer_stopped(mock_llm_client, big_slice):
    """Every row behind the marker reached a summarizer call, the tool round's
    command reached it too, and the one body that had to be clipped is on the
    record rather than counted as covered whole."""
    from db import models as db

    sid, ids, stripped = big_slice
    mock_llm_client.responses = [_summary()]

    assert await compact_with_llm(sid, stripped) is True
    prompts = _prompts(mock_llm_client)

    marker = _marker(db, sid)
    assert marker is not None
    meta = json.loads(marker["metadata"])
    boundary = meta["compacted_up_to"]

    # THE headline assertion: not one row behind the boundary is absent from
    # the input the summarizer was given.
    rows = [m for m in db.get_messages(sid) if m["role"] not in MARKER_ROLES]
    behind = [m for m in rows if m["id"] <= boundary]
    assert behind, "nothing was compacted"
    for row in behind:
        token = (row["content"] or "")[:14]
        if token.startswith("ROW-"):
            assert token in prompts, f"msg {row['id']} is behind the boundary but was never summarized"

    # The slice needed more than one call — the old code made exactly one and
    # retired everything that did not fit in it.
    assert len(mock_llm_client.calls) >= 2
    coverage = meta["coverage"]

    # The live turn is still untouched (the 2026-08-07 clamp).
    assert boundary < ids["live_root"]

    # Corrections past the old per-message clip and past the old aggregate
    # cutoff both reached the summarizer this time.
    assert "CORRECTION-BETA-9271" in prompts
    assert "CORRECTION-GAMMA-5512" in prompts

    # The command lived only in tool_calls. So did the tool's identity, and
    # the fact that the second call was refused rather than run.
    assert "PATCH-DELTA-7788" in prompts
    assert "bash(" in prompts
    assert "REJECTED" in prompts

    # The one thing still omitted is omitted ON THE RECORD, with a pointer.
    assert "CORRECTION-OMEGA-3140" not in prompts
    assert f"session_read({ids['omega']})" in prompts
    clipped = {c["msg_id"] for c in coverage["clipped"]}
    assert ids["omega"] in clipped
    assert "session_read" in marker["content"], "the summary itself must carry the recovery pointer"

    assert coverage["covered_to"] == boundary
    assert coverage["covered_count"] == len(behind)


async def test_a_failed_middle_chunk_leaves_the_rest_in_the_window(mock_llm_client, big_slice, monkeypatch):
    """A chunk the summarizer never answered must not be behind the marker.
    The chunks before it are real progress and are kept."""
    from db import models as db

    sid, ids, stripped = big_slice
    calls = {"n": 0}
    first_prompt = {}

    async def one_then_fail(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            first_prompt["text"] = kwargs["messages"][1]["content"]
            return _summary()
        raise ConnectionError("summarizer down")

    mock_llm_client.chat = one_then_fail

    assert await compact_with_llm(sid, stripped) is True

    marker = _marker(db, sid)
    meta = json.loads(marker["metadata"])
    boundary = meta["compacted_up_to"]
    assert meta["coverage"]["chunks_used"] == 1

    rows = [m for m in db.get_messages(sid) if m["role"] not in MARKER_ROLES]
    behind = [m for m in rows if m["id"] <= boundary]
    ahead = [m for m in rows if m["id"] > boundary]
    assert behind and ahead, "one chunk landed, the rest must survive"
    for row in behind:
        token = (row["content"] or "")[:14]
        if token.startswith("ROW-"):
            assert token in first_prompt["text"]
    # The rows the failed chunk would have carried are still in the compiler's
    # active window, not summarized away.
    assert any("CORRECTION-GAMMA-5512" in (m["content"] or "") for m in ahead)


async def test_a_failed_first_chunk_writes_no_marker(mock_llm_client, big_slice):
    """Summary failure must never advance the marker."""
    from db import models as db

    sid, _ids, stripped = big_slice

    async def always_fail(*args, **kwargs):
        raise ConnectionError("summarizer down")

    mock_llm_client.chat = always_fail

    assert await compact_with_llm(sid, stripped) is False
    assert _marker(db, sid) is None


async def test_the_ratio_gate_measures_the_input_it_supplied(mock_llm_client, big_slice):
    """A summary that is 40% of the chunk it was given is bloated. Divided by
    the whole slice instead it looks like 10% and sailed through."""
    from db import models as db

    sid, ids, stripped = big_slice
    rows = [m for m in db.get_messages(sid) if m["role"] not in MARKER_ROLES]
    slice_rows = [m for m in rows if m["id"] < ids["live_root"]]
    chunk_one = _chunk_slice(slice_rows)[0]

    est = get_estimator()
    chunk_tokens = est.count(chunk_one.text)
    bloated = "restated detail about the audit run " * int(chunk_tokens * 0.4 / 6)

    # The two readings the old code confused.
    assert est.count(bloated) / chunk_tokens > 0.35
    assert est.count(bloated) / sum(est.count_message(m) for m in slice_rows) < 0.35

    mock_llm_client.responses = [_summary(bloated)]
    assert await compact_with_llm(sid, stripped) is False
    assert _marker(db, sid) is None
