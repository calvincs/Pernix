"""A worker cleared three stages of its task, sent its parent two progress
notes and wrote its files. Its replies carried tool calls and no text, as some
models' replies do. Then its turn was cancelled from the UI.

The parent was told the worker "produced NO OUTPUT" (await_workers), "WARNING:
no output produced" (check_workers) and "finished with no output"
(get_worker_result). It read that as a failure, killed the worker's working
connection, and rebuilt from nothing what the worker had already built.

"Output" meant an assistant message with text. A finished worker with no text
is now described by what it did and how it ended: cancelled is said to be a
stop and not a failure, the tool calls, progress notes and written files are
counted and listed, and the way forward is resume_worker. A worker that did
nothing at all still reads as no output.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.extensions import orchestration as orch
from db import models as db


def _call(tool, **args):
    return {"id": f"c-{tool}", "name": tool, "arguments": json.dumps(args)}


@pytest.fixture
def family(monkeypatch):
    parent = db.create_session(title="parent")
    wid = db.create_session(title="play the game", session_type="worker", parent_session_id=parent)
    # One stand-in serves as the parent and the worker: the functions under
    # test read worker_ids/touch from the first and the ending from the second.
    obj = SimpleNamespace(error="", termination_reason="cancelled", _turn_id=1, worker_ids=[wid], touch=lambda: None)
    manager = SimpleNamespace(get=lambda _: obj, worker_inventory=lambda _parent: [wid])
    monkeypatch.setattr("sessions.manager.get_manager", lambda: manager)
    monkeypatch.setattr(orch, "_worker_is_mid_turn", lambda _: False)
    db.add_message(wid, "user", "clear as many stages as you can")
    return parent, wid, obj


def _work(wid):
    """Tool calls only — no assistant text anywhere, like the real run."""
    db.add_message(wid, "assistant", "", tool_calls=json.dumps([_call("bash", command="python3 probe.py")]))
    db.add_message(
        wid, "assistant", "", tool_calls=json.dumps([_call("file_write", path="game/STATUS.md", content="x")])
    )
    db.add_message(
        wid, "assistant", "", tool_calls=json.dumps([_call("notify_parent", message="Stage 1 verified by the engine.")])
    )
    db.add_message(
        wid,
        "assistant",
        "",
        tool_calls=json.dumps(
            [
                _call("file_write", path="game/levels/02.asm", content="y"),
                # the same tools reached through the use_tool wrapper
                _call("use_tool", name="notify_parent", arguments={"message": "Stage 2 verified; starting stage 3."}),
            ]
        ),
    )


def test_a_cancelled_worker_is_described_by_what_it_did(family):
    _parent, wid, _obj = family
    _work(wid)
    out = orch.get_worker_result(wid)
    assert "was CANCELLED" in out and "it did not fail" in out
    assert "finished with no output" not in out
    assert "5 tool call(s), 2 progress note(s), 2 file(s) written" in out
    assert "Stage 2 verified; starting stage 3." in out
    assert "game/STATUS.md" in out and "game/levels/02.asm" in out
    assert "resume_worker" in out and "spawn_worker" not in out  # continue it, do not start over
    assert "Original task: clear as many stages as you can" in out


def test_a_worker_that_ran_out_of_rounds_is_not_called_empty_either(family):
    _parent, wid, obj = family
    obj.termination_reason = "round_ceiling"
    _work(wid)
    out = orch.get_worker_result(wid)
    assert "hit its round limit before it wrote a final result" in out
    assert "5 tool call(s)" in out and "CANCELLED" not in out


def test_a_cancelled_worker_that_did_nothing_still_says_it_was_stopped(family):
    _parent, wid, _obj = family
    out = orch.get_worker_result(wid)
    assert "was CANCELLED" in out and "0 tool call(s)" in out


def test_a_worker_that_truly_did_nothing_still_reads_as_no_output(family):
    _parent, wid, obj = family
    obj.termination_reason = "complete"
    assert "finished with no output" in orch.get_worker_result(wid)
    assert orch._silent_end(wid)[0] == "empty"


def test_the_status_line_and_the_wait_summary_say_cancelled_not_no_output(family, monkeypatch):
    parent, wid, _obj = family
    _work(wid)
    from sessions import state_v2 as sv2

    monkeypatch.setattr(sv2, "_current_state", lambda _w: sv2.SessionStateV2.IDLE_READY)
    monkeypatch.setattr(orch, "_worker_idle_seconds", lambda _w: 0)
    ctx = {"session_id": parent}
    status = orch.check_workers(_context=ctx)
    assert "CANCELLED before it wrote a result (5 tool call(s), 2 progress note(s), 2 file(s) written)" in status
    assert "no output produced" not in status

    waited = orch.await_workers(worker_ids=[wid], _context=ctx)
    assert "CANCELLED before writing a result" in waited and "NOT a failure" in waited
    assert "NO OUTPUT" not in waited
