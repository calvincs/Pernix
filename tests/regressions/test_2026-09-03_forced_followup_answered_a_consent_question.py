"""The forced follow-up overrode the user on an explicit consent offer.

Field case, session 3dc5a307d751 (2026-09-03). The agent finished a report,
asked whether to spend its remaining rounds on the last open avenue, and
closed with "Say the word and I'll start." — a question handed back to the
user. `_announces_future_work` only suppressed on a reply whose very last
character was "?", and the closing sentence here was a statement, so the
nudge fired and the agent started the work the user had not approved. It
happened twice in the same turn.

Two guards now: a question anywhere in the last three sentences suppresses,
and the courtesy-closer vocabulary covers consent phrasing ("say the word",
"want me to", "your call", "shall I", "give me the go").
"""

from __future__ import annotations

import pytest

from core.agent import _announces_future_work

# The literal tail from the session, lightly trimmed.
SESSION_TAIL = (
    "Want me to actually go do it now — the side-condition solve? That's the "
    "one remaining avenue that could change the answer, and it's mechanical: "
    "expand the two candidate branches and check the sign. I have 500 rounds; "
    "it's the right thing to spend them on. Say the word and I'll start."
)


def test_the_session_tail_is_not_abandoned_work():
    assert _announces_future_work(SESSION_TAIL) is False


@pytest.mark.parametrize(
    "text",
    [
        "The audit is written up. Want me to go fix the two mediums? I'll start on your go.",
        "That's everything I found. Shall I open the PR? I'll wire the tests in too.",
        "Report's done. Your call whether I keep digging — I'll take another pass if you like.",
        "Both options work. Give me the green light and I'll implement the second one.",
        "Analysis complete. Should I widen the search? I'm going to need another hour if so.",
        "Draft attached. Tell me if you want the longer version and I'll write it.",
    ],
)
def test_consent_offers_do_not_fire(text):
    assert _announces_future_work(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "The config looks wrong. Next, I'll update the settings file.",
        "Found the bug. Let me fix the parser now.",
        "Traced it to the compiler. I'm going to rewrite the history filter.",
    ],
)
def test_a_real_promise_still_fires(text):
    assert _announces_future_work(text) is True


def test_a_question_earlier_in_a_long_answer_still_fires():
    """The suppressor is scoped to the closing stretch — a rhetorical question
    in the body of a long answer must not disarm the nudge."""
    text = (
        "Why did the first attempt fail? The cache was stale. "
        "I checked the loader, the parser, and the writer, and all three "
        "agree on the schema now. The remaining work is the migration file. "
        "I'll write it next."
    )
    assert _announces_future_work(text) is True
