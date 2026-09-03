"""An aborted tool round left an assistant tool_calls row with no results.

The assistant row is persisted before the round executes; a cancel during
a parallel batch, the executor backstop, or a DB error while recording
results escaped without writing tool rows, and the compiler only ever
dropped tool-side orphans. OpenAI-format providers then rejected the
transcript on every later turn until compaction folded the row.

Two layers now hold: the compiler stubs unanswered calls in the view it
builds, and the agent loop writes stub rows before it unwinds.
"""

import asyncio
import json

from core.agent import _stub_unanswered_calls
from core.context.compaction import ABORTED_CALL_STUB, exclude_orphans, repair_unanswered_tool_calls


def test_view_stubs_an_assistant_with_no_results():
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": json.dumps([{"id": "a"}, {"id": "b"}])},
        {"role": "user", "content": "next turn"},
    ]
    out = exclude_orphans(history)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool", "user"]
    assert [m["tool_call_id"] for m in out if m["role"] == "tool"] == ["a", "b"]
    assert all(m["content"] == ABORTED_CALL_STUB for m in out if m["role"] == "tool")


def test_view_stubs_only_the_missing_call_after_the_recorded_ones():
    history = [
        {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}, {"id": "c"}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "user", "content": "hi"},
    ]
    out = repair_unanswered_tool_calls(history)
    tools = [(m["tool_call_id"], m["content"]) for m in out if m["role"] == "tool"]
    assert tools == [("a", "ok"), ("b", ABORTED_CALL_STUB), ("c", ABORTED_CALL_STUB)]
    assert out[-1]["role"] == "user"


def test_view_leaves_a_complete_round_alone():
    history = [
        {"role": "assistant", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    assert repair_unanswered_tool_calls(history) == history


async def test_loop_persists_stub_rows_before_unwinding():
    saved = []

    async def _save(role, content, **kw):
        saved.append((role, content, kw.get("tool_call_id")))
        return 1

    calls = [{"tc": {"id": "x", "name": "bash"}}, {"tc": {"id": "y", "name": "file_read"}}]
    await _stub_unanswered_calls(calls, save_turn_msg=_save, exc=asyncio.CancelledError())
    assert [(r, i) for r, _, i in saved] == [("tool", "x"), ("tool", "y")]
    assert all("cancelled" in c for _, c, _ in saved)


async def test_loop_stub_failure_never_masks_the_unwind():
    async def _save(role, content, **kw):
        raise RuntimeError("db down")

    await _stub_unanswered_calls([{"tc": {"id": "x", "name": "bash"}}], save_turn_msg=_save, exc=TimeoutError())
