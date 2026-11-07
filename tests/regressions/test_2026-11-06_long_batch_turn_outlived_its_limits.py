"""A long batch turn stopped at every limit it met, and waited.

A user handed over 23 playlists (319 videos) to work through one at a time.
The agent did good work for 30 minutes, then:
- the 1800 s session clock cut the turn mid-item, although slots are taken
  one call at a time and the clock protected no other session;
- goal_create, the tool that carries a job across turns, was not on the
  fixed tool list, and its rule said "only when the user asked for a goal";
- compaction refused to fold any of the live turn, so its window sat at
  ~140k tokens and each round ran about 2.5x slower than at 58k;
- proactive compaction fired once per turn, so it never tried again.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.context.compaction import _clamp_boundary_to_live_turn


def test_the_session_clock_is_off_by_default():
    from config import Settings

    assert Settings.llm_session_timeout == 0


def test_goal_create_is_on_the_fixed_tool_list():
    from core.tools.native import NATIVE_TOOLS

    assert {"goal_create", "goal_status", "goal_complete"} <= NATIVE_TOOLS


def test_goal_create_accepts_a_long_batch_as_intent():
    import inspect

    from core.tools.builtin import goal_tools

    src = inspect.getsource(goal_tools.register)
    assert "long batch" in src and "ONLY when the user explicitly asked" not in src


# ── compaction may fold the live turn's own rounds ──────────────────────

ROOT = {"id": 10, "role": "user", "content": "work through all 319 videos"}


def _rounds(start: int, n: int) -> list[dict]:
    rows = []
    for i in range(n):
        rows.append({"id": start + 2 * i, "role": "assistant", "content": "", "tool_calls": "[]"})
        rows.append({"id": start + 2 * i + 1, "role": "tool", "tool_call_id": f"t{i}", "content": "r"})
    return rows


def test_boundary_enters_a_long_live_turn():
    convo = [ROOT] + _rounds(20, 10)
    assert _clamp_boundary_to_live_turn(convo, boundary_idx=15, turn_user_msg_id=10) > 1


def test_boundary_never_leaves_a_tool_result_without_its_call():
    convo = [ROOT] + _rounds(20, 10)
    for b in range(1, len(convo)):
        cut = _clamp_boundary_to_live_turn(convo, boundary_idx=b, turn_user_msg_id=10)
        assert cut == len(convo) or convo[cut]["role"] != "tool"


def test_boundary_stops_before_a_user_row_inside_the_turn():
    convo = [ROOT] + _rounds(20, 3) + [{"id": 40, "role": "user", "content": "queued"}] + _rounds(50, 3)
    assert _clamp_boundary_to_live_turn(convo, boundary_idx=len(convo), turn_user_msg_id=10) == 7


def test_manual_compact_without_a_known_root_still_holds_at_the_root():
    convo = [
        {"id": 1, "role": "user", "content": "old"},
        {"id": 2, "role": "assistant", "content": "a"},
        ROOT,
    ] + _rounds(20, 4)
    assert _clamp_boundary_to_live_turn(convo, boundary_idx=len(convo), turn_user_msg_id=None) == 2


def test_compiler_keeps_the_live_root_after_its_rounds_are_folded():
    import json

    from core.context.compiler import compile_context
    from db import models as db

    sid = db.create_session("batch")
    root = db.add_message(sid, "user", "UNIQUE-ASK work through every video")
    last = root
    for i in range(6):
        last = db.add_message(
            sid, "assistant", f"round {i}", tool_calls=json.dumps([{"id": f"c{i}", "name": "bash", "arguments": "{}"}])
        )
        last = db.add_message(sid, "tool", f"out {i}", tool_call_id=f"c{i}")
    db.add_compaction(session_id=sid, summary="summary of rounds 0-3", compacted_up_to=last - 4, original_count=8)
    payload = compile_context(sid, turn_user_msg_id=root)
    text = " ".join(str(m.get("content")) for m in payload.messages)
    assert "UNIQUE-ASK" in text


# ── proactive compaction repeats after a productive run ─────────────────


def _controller():
    from core.agent import _CompactionController

    return _CompactionController(SimpleNamespace(), "s")


def test_proactive_fires_first_then_waits_for_the_gap():
    c = _controller()
    assert c.proactive_due(3)
    c.attempts, c._last_productive, c._last_round = 1, True, 3
    assert not c.proactive_due(3 + c.PROACTIVE_GAP_ROUNDS - 1)
    assert c.proactive_due(3 + c.PROACTIVE_GAP_ROUNDS)
    assert c.proactive_due(0)  # renewed round window


def test_proactive_stops_after_a_stalled_or_failed_run():
    c = _controller()
    c.attempts, c._last_round = 1, 0
    c._last_productive, c._stalled = True, True
    assert not c.proactive_due(50)
    c._last_productive, c._stalled = False, False
    assert not c.proactive_due(50)
    c._refused = True
    assert not c.proactive_due(50)
