"""The second compaction of any long turn was refused on arithmetic alone.

`_CompactionController.stalled()` compared the size at the *next* trigger
with the size recorded *before* the last compaction. Both triggers sit at
fixed thresholds, so the second is always at or above the first; a turn
whose proactive compaction worked at 85k tokens was refused at the 108k
critical trigger and died with compaction_failed at attempt 1 of 3.
"""

from types import SimpleNamespace

import pytest

from core.agent import _CompactionController
from db import models as db
from sessions.state import AgentSession


@pytest.fixture
def controller(monkeypatch):
    async def _compact(session_id, messages, **kwargs):
        return True

    monkeypatch.setattr("core.agent.compact_with_llm", _compact)
    sid = db.create_session(title="long turn")
    session = AgentSession(session_id=sid)
    session.emit_event = lambda e: None
    return _CompactionController(session, sid)


def _payload(tokens: int):
    return SimpleNamespace(token_count=tokens, messages=[], history_budget=50_000)


async def test_a_compaction_that_shrank_does_not_block_the_next_trigger(controller):
    await controller.run(_payload(85_000), transition_reason="compact-proactive")
    controller.observe(60_000)  # the compile right after: it shrank
    assert not controller.stalled
    controller.observe(108_000)  # 30 rounds later, at the critical trigger
    assert not controller.stalled, "the old check compared 108k against 85k and refused"
    assert not controller.exhausted


async def test_a_noop_compaction_is_still_caught(controller):
    await controller.run(_payload(85_000), transition_reason="compact-critical")
    controller.observe(84_000)  # did nothing
    assert controller.stalled


async def test_stalled_resets_on_the_next_attempt(controller):
    await controller.run(_payload(85_000), transition_reason="compact-critical")
    controller.observe(84_000)
    assert controller.stalled
    await controller.run(_payload(84_000), transition_reason="compact-critical")
    controller.observe(40_000)
    assert not controller.stalled
    assert controller.attempts == 2
