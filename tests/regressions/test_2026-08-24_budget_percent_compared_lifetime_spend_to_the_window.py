"""Resource status divided lifetime spend by the context window (F13, field
case 17683100ecf8 — ARC-AGI-3 attempt).

`_build_resource_status` computed `pct = SUM(token_usage.total) / window`.
The numerator re-counts the re-sent context on every LLM call, so any long
tool loop read "over budget" within a dozen rounds. The field session showed
"1,299% of budget" while its largest prompt filled 36% of the window; the
agent narrated budget panic in ~20 consecutive messages and quit 2-3 rounds
short of a verified win with 84 of 100 tool rounds unused. The percentage
must describe the one number the window constrains — the compiled context —
and lifetime spend must read as an informational count, not a limit.
"""

from core.agent import _build_resource_status
from db import models as db


def _spend(sid: str, total: int, calls: int = 3) -> None:
    per = total // calls
    for _ in range(calls):
        db.add_token_usage(sid, model="m", prompt_tokens=per - 10, completion_tokens=10, total_tokens=per)


def test_percentage_describes_context_not_lifetime_spend(monkeypatch):
    monkeypatch.setattr("config.settings.max_tool_rounds", 100)
    sid = db.create_session()
    _spend(sid, 3_000_000, calls=57)  # field session: 3.06M spent, window 236K

    text = _build_resource_status(sid, None, tool_round=13, context_budget=236_000, context_tokens=84_059)

    # The window-relative number is the context, not the spend.
    assert "36% full" in text
    # Spend appears as a plain count, explicitly not a limit.
    assert "informational only" in text
    assert "over 57 LLM call(s)" in text
    # The old spend-over-window claim is gone in every form.
    assert "of budget" not in text
    assert "1,271%" not in text and "1271%" not in text


def test_rounds_are_named_the_only_binding_limit(monkeypatch):
    monkeypatch.setattr("config.settings.max_tool_rounds", 100)
    sid = db.create_session()
    text = _build_resource_status(sid, None, tool_round=13, context_budget=236_000, context_tokens=50_000)
    assert "Tool rounds remaining: 87/100" in text
    assert "only binding limit" in text


def test_first_round_has_no_context_tokens_yet(monkeypatch):
    """Round 1 builds status before any compile — no percentage is invented."""
    monkeypatch.setattr("config.settings.max_tool_rounds", 100)
    sid = db.create_session()
    _spend(sid, 500_000)
    text = _build_resource_status(sid, None, tool_round=0, context_budget=236_000)
    assert "%" not in text.split("|")[0]  # the context segment carries no made-up percent
    assert "of budget" not in text


def test_round_tier_warnings_unchanged(monkeypatch):
    monkeypatch.setattr("config.settings.max_tool_rounds", 10)
    sid = db.create_session()
    last = _build_resource_status(sid, None, tool_round=9, context_budget=100_000, context_tokens=10_000)
    assert "LAST ROUND" in last
    penult = _build_resource_status(sid, None, tool_round=8, context_budget=100_000, context_tokens=10_000)
    assert "CRITICAL" in penult
