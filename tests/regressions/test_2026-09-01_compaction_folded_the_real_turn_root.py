"""Compaction picked an injected mid-turn user message as the turn root.

`_active_turn_root_index` walks back from the last non-user row, so with
an `/api/chat/inject` message between two tool rounds the boundary
clamped to the injected row, and the real ask plus its earlier rounds
were folded into the summary. The agent then resumed a live turn with a
summary and no verbatim request. The loop now passes its known turn root
and the compactor clamps to that row.
"""

from core.context.compaction import _clamp_boundary_to_live_turn

CONVO = [
    {"id": 90, "role": "user", "content": "the real ask"},
    {"id": 100, "role": "assistant", "content": "", "tool_calls": "[]"},
    {"id": 101, "role": "user", "content": "injected mid-turn note"},
    {"id": 102, "role": "tool", "tool_call_id": "a", "content": "r"},
    {"id": 103, "role": "assistant", "content": "", "tool_calls": "[]"},
    {"id": 104, "role": "tool", "tool_call_id": "b", "content": "r"},
]


def test_known_turn_root_wins_over_the_positional_guess():
    # The positional guess lands on the injected row (index 2)...
    assert _clamp_boundary_to_live_turn(CONVO, boundary_idx=6, turn_user_msg_id=None) == 2
    # ...but the loop knows its root is id 90 (index 0).
    assert _clamp_boundary_to_live_turn(CONVO, boundary_idx=6, turn_user_msg_id=90) == 0


def test_a_boundary_already_below_the_root_is_left_alone():
    assert _clamp_boundary_to_live_turn(CONVO, boundary_idx=0, turn_user_msg_id=90) == 0


def test_unknown_root_id_falls_back_to_the_positional_guess():
    assert _clamp_boundary_to_live_turn(CONVO, boundary_idx=6, turn_user_msg_id=12345) == 2
