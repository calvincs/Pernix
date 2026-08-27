"""Tests for the forced follow-up nudge (spec Feature 9).

The trigger heuristic is the risk surface: a false positive loops a finished
answer, a false negative just falls back to today's behavior. So the tests
concentrate on _announces_future_work's edges, plus the nudge composer.
"""

from __future__ import annotations

from core.agent import _announces_future_work, _build_followup_nudge
from db import models as db

# ---------------------------------------------------------------------------
# Trigger heuristic
# ---------------------------------------------------------------------------


def test_future_intent_tail_fires():
    assert _announces_future_work("The config looks wrong. Next, I'll update the settings file.")
    assert _announces_future_work("Found the bug. Let me fix the parser now.")
    assert _announces_future_work("I am going to run the tests.")
    assert _announces_future_work("Proceeding to implement the fix.")


def test_completed_answer_does_not_fire():
    assert not _announces_future_work("Done. The fix is in place and all 12 tests pass.")
    assert not _announces_future_work("The answer is 42.")


def test_early_intent_with_completed_tail_does_not_fire():
    text = (
        "I'll start by checking the config.\n\n"
        "...\n\n"
        "All checks completed. The value was 5 and the file is saved."
    )
    assert not _announces_future_work(text)


def test_trailing_question_suppresses():
    assert not _announces_future_work("I'll need the API key. Which environment should I use?")


def test_courtesy_closer_suppresses():
    assert not _announces_future_work("The report is ready. Let me know if you need anything else.")
    assert not _announces_future_work("Setup complete. Feel free to ask if you want changes — I'll be here.")


def test_empty_and_whitespace_do_not_fire():
    assert not _announces_future_work("")
    assert not _announces_future_work("   \n  ")


# ---------------------------------------------------------------------------
# Nudge composer
# ---------------------------------------------------------------------------


def test_nudge_quotes_the_announced_intent():
    sid = db.create_session(title="T")
    nudge = _build_followup_nudge(sid, "Checks done. Next, I'll write the migration.", 1, 1)
    assert nudge.startswith("[forced follow-up 1/1]")
    assert "write the migration" in nudge


def test_nudge_prefers_active_goal(monkeypatch):
    sid = db.create_session(title="T")
    monkeypatch.setattr(
        "db.models.get_active_goal",
        lambda _sid: {"status": "active", "objective": "ship the release notes"},
    )
    nudge = _build_followup_nudge(sid, "Now I'll get started.", 1, 2)
    assert "ship the release notes" in nudge
    assert "[forced follow-up 1/2]" in nudge
