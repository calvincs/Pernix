"""Item #2: build_session_brief honors per-session context_budget."""

from config import settings
from core.scout.runner import build_session_brief
from db import models as db


def _seed_session_with_messages(title: str, msg_count: int = 5) -> str:
    sid = db.create_session(title=title)
    # Insert messages with explicit token_count to make utilization math predictable.
    for i in range(msg_count):
        db.add_message(sid, "user", content=f"msg {i}" * 10, token_count=1000)
    return sid


def test_brief_uses_explicit_budget_when_provided():
    sid = _seed_session_with_messages("Budget Test A")
    # total_tokens = 5_000
    small = build_session_brief(sid, context_budget=10_000)
    large = build_session_brief(sid, context_budget=100_000)
    assert small.context_utilization == 0.5
    assert large.context_utilization == 0.05
    assert small.context_utilization != large.context_utilization


def test_brief_falls_through_to_settings_default_when_arg_missing():
    sid = _seed_session_with_messages("Budget Test B")
    default = build_session_brief(sid)
    expected = 5_000 / max(settings.context_budget, 1)
    assert abs(default.context_utilization - min(expected, 1.0)) < 1e-9


def test_brief_utilization_clamped_at_one():
    sid = _seed_session_with_messages("Budget Test C", msg_count=10)
    brief = build_session_brief(sid, context_budget=1_000)
    assert brief.context_utilization == 1.0
