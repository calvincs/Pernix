"""A 30-minute bash call consumed the worker's entire LLM time budget
(field case c93232a0521b).

The per-session LLM budget is wall-clock from the first LLM acquire, reset
only at a new user turn — and a worker's whole life is one turn. While the
search script ran for 1800s the clock ticked, the turn soft-landed as
budget_exhausted the moment the timeout returned, and reflect's retry was
refused for lack of budget: the agent paid 30 minutes for a result it was
never allowed to read. The executor now guarantees a minimum LLM budget
remains after any tool call >= 30s.
"""

from core.tools.executor import _LONG_CALL_CREDIT_THRESHOLD_MS, _credit_long_call


def test_long_call_ensures_minimum_remaining_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "core.llm.client.ensure_session_budget",
        lambda sid, s: calls.append((sid, s)),
    )
    _credit_long_call({"session_id": "sess-1"}, 1_800_000)
    assert calls == [("sess-1", 600.0)]


def test_short_calls_do_not_touch_the_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "core.llm.client.ensure_session_budget",
        lambda sid, s: calls.append((sid, s)),
    )
    _credit_long_call({"session_id": "sess-1"}, _LONG_CALL_CREDIT_THRESHOLD_MS - 1)
    _credit_long_call({}, 1_800_000)  # no session — background caller
    assert calls == []


def test_credit_failure_never_raises(monkeypatch):
    def _boom(sid, s):
        raise RuntimeError("scheduler gone")

    monkeypatch.setattr("core.llm.client.ensure_session_budget", _boom)
    _credit_long_call({"session_id": "sess-1"}, 1_800_000)  # must not raise
