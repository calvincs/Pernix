"""Tests for inter-session queueing via LLM semaphore.

Validates that:
1. Semaphore default timeout is long enough for queued sessions
2. A second session waits (not fails) when the first holds the semaphore
3. session.waiting_llm event is emitted when blocked
4. LLMClient.has_capacity() correctly reflects semaphore state
5. Scout emits a waiting step when blocked on LLM capacity
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.llm.semaphore import FairLLMSemaphore, LLMConcurrencyError, LLMSessionTimeoutError
from core.llm.types import ChatResponse, StreamEvent, StreamEventType, TokenUsage
from sessions.state import AgentSession, SessionState

# ---------------------------------------------------------------------------
# Semaphore timeout tests
# ---------------------------------------------------------------------------


class TestSemaphoreTimeout:
    """Verify the timeout is long enough for inter-session queuing."""

    @pytest.mark.asyncio
    async def test_default_timeout_is_1800(self):
        """Default timeout should be 30 minutes, not 60 seconds.

        The acquire() signature now uses `timeout: float | None = None` and
        resolves None → self._session_timeout; the 1800s default lives on
        the instance (and is overridable for the workflow orchestrator).
        Verify both: the parameter default is the sentinel None, and the
        instance's _session_timeout is 1800."""
        import inspect

        sig = inspect.signature(FairLLMSemaphore.acquire)
        assert sig.parameters["timeout"].default is None
        sem = FairLLMSemaphore(max_concurrent=1)
        assert sem._session_timeout == 1800.0, f"Expected 1800s instance timeout, got {sem._session_timeout}"

    @pytest.mark.asyncio
    async def test_queued_session_waits_and_succeeds(self):
        """A second acquire should wait until the first releases, not fail."""
        sem = FairLLMSemaphore(max_concurrent=1)

        await sem.acquire()
        assert sem.available == 0

        # Second acquire waits, then succeeds after release
        async def delayed_release():
            await asyncio.sleep(0.05)
            sem.release()

        release_task = asyncio.create_task(delayed_release())
        await sem.acquire(timeout=5.0)  # should succeed after ~50ms
        assert sem.available == 0  # we hold it now

        sem.release()
        await release_task

    @pytest.mark.asyncio
    async def test_fifo_ordering(self):
        """Waiters should be served in FIFO order."""
        sem = FairLLMSemaphore(max_concurrent=1)
        order = []

        await sem.acquire()

        async def waiter(name):
            await sem.acquire(timeout=5.0)
            order.append(name)
            sem.release()

        # Start two waiters — should be served in order
        t1 = asyncio.create_task(waiter("first"))
        await asyncio.sleep(0.01)  # ensure t1 queues before t2
        t2 = asyncio.create_task(waiter("second"))
        await asyncio.sleep(0.01)

        assert sem.waiting == 2

        sem.release()  # release initial hold
        await asyncio.gather(t1, t2)
        assert order == ["first", "second"]


class TestSessionSecondsRemaining:
    """Verify session_seconds_remaining returns a budget reflect-retry can act on.

    Regression for session 7b97cf7ef84a: reflect-retry fired ~5 minutes past
    the 1800s LLM session timeout, then burned 220s in scout before failing
    with LLMSessionTimeoutError. The fix: query the budget BEFORE retrying.
    """

    @pytest.mark.asyncio
    async def test_returns_inf_before_first_acquire(self):
        sem = FairLLMSemaphore(max_concurrent=1, session_timeout=1800.0)
        assert sem.session_seconds_remaining("never-acquired") == float("inf")

    @pytest.mark.asyncio
    async def test_returns_remaining_after_acquire(self):
        sem = FairLLMSemaphore(max_concurrent=1, session_timeout=1800.0)
        await sem.acquire(session_id="s1")
        remaining = sem.session_seconds_remaining("s1")
        # Just acquired; nearly the full budget should be available.
        assert 1799.0 < remaining <= 1800.0
        sem.release()

    @pytest.mark.asyncio
    async def test_returns_zero_when_budget_exceeded(self, monkeypatch):
        sem = FairLLMSemaphore(max_concurrent=1, session_timeout=1.0)
        await sem.acquire(session_id="s1")
        sem.release()
        # Pretend an hour has passed.
        import time as _t

        original = _t.monotonic()
        monkeypatch.setattr(_t, "monotonic", lambda: original + 3600.0)
        # Patch the module the semaphore uses
        from core.llm import semaphore as _sem_mod

        monkeypatch.setattr(_sem_mod, "time", _t)
        assert sem.session_seconds_remaining("s1") == 0.0


class TestExtendSessionBudget:
    """The session-time wall-clock cap is right for normal turns but wrong
    for orchestrator sessions whose duration is dominated by waiting on
    workers (run_workflow). extend_session_budget grows the cap so reflect
    and post-flow rounds still have budget left.
    """

    @pytest.mark.asyncio
    async def test_extension_grows_remaining_budget(self):
        sem = FairLLMSemaphore(max_concurrent=1, session_timeout=1800.0)
        await sem.acquire(session_id="orch")
        baseline = sem.session_seconds_remaining("orch")
        new_cap = sem.extend_session_budget("orch", 1800.0 * 4)
        assert new_cap == 1800.0 + 1800.0 * 4
        extended = sem.session_seconds_remaining("orch")
        # Within a few ms of baseline + 4×1800.
        assert extended - baseline > 1800.0 * 4 - 1.0
        sem.release()

    @pytest.mark.asyncio
    async def test_extension_is_idempotent_and_never_shrinks(self):
        sem = FairLLMSemaphore(max_concurrent=1, session_timeout=1800.0)
        sem.extend_session_budget("orch", 5000.0)
        # Smaller extension must NOT shrink the granted budget.
        result = sem.extend_session_budget("orch", 100.0)
        assert result == 1800.0 + 5000.0, "extension must not shrink — once granted, the budget stays granted"

    @pytest.mark.asyncio
    async def test_extension_applies_even_before_first_acquire(self):
        sem = FairLLMSemaphore(max_concurrent=1, session_timeout=1800.0)
        sem.extend_session_budget("orch", 1800.0 * 3)
        # No acquire yet — remaining is inf because clock hasn't started.
        assert sem.session_seconds_remaining("orch") == float("inf")
        # First acquire honours the extended cap.
        await sem.acquire(session_id="orch")
        remaining = sem.session_seconds_remaining("orch")
        assert remaining > 1800.0 * 3, f"extended cap not honoured at first acquire: {remaining}"

    @pytest.mark.asyncio
    async def test_extension_blocks_acquire_only_past_extended_cap(self, monkeypatch):
        sem = FairLLMSemaphore(max_concurrent=1, session_timeout=10.0)
        sem.extend_session_budget("orch", 100.0)  # effective: 110s
        await sem.acquire(session_id="orch")
        sem.release()

        import time as _t

        from core.llm import semaphore as _sem_mod

        original = _t.monotonic()

        # +50s elapsed: well past base 10s but inside extended 110s — still allowed.
        monkeypatch.setattr(_sem_mod, "time", type("T", (), {"monotonic": staticmethod(lambda: original + 50.0)}))
        await sem.acquire(session_id="orch")
        sem.release()

        # +200s elapsed: past extended 110s — must reject.
        monkeypatch.setattr(_sem_mod, "time", type("T", (), {"monotonic": staticmethod(lambda: original + 200.0)}))
        with pytest.raises(LLMSessionTimeoutError):
            await sem.acquire(session_id="orch")

    @pytest.mark.asyncio
    async def test_purge_clears_extension(self):
        sem = FairLLMSemaphore(max_concurrent=1, session_timeout=1800.0)
        sem.extend_session_budget("orch", 5000.0)
        sem.purge_session("orch")
        # After purge, a fresh acquire sees the base timeout, not the extension.
        await sem.acquire(session_id="orch")
        # Budget back to base 1800s (within ~1s for monotonic drift).
        assert sem.session_seconds_remaining("orch") <= 1800.0


class TestResetSessionBudget:
    """Regression for session 14af4333f6d8 (2026-04-28): an interactive
    chat session got "LLM time budget exhausted (>1800s) — turn aborted
    before scout" after a long conversation. The cause: _session_first_active
    is set on the session's FIRST ever acquire and never reset, so
    session_seconds_remaining keeps decrementing across all turns. After
    1800s of wall-clock time (mostly user thinking, not LLM work), the
    session is locked out forever. Fix: reset_session_budget() clears the
    tracking so each new user turn gets a fresh window.
    """

    @pytest.mark.asyncio
    async def test_reset_clears_first_active(self):
        sem = FairLLMSemaphore(max_concurrent=1, session_timeout=1800.0)
        await sem.acquire(session_id="chat")
        sem.release()
        assert sem._session_first_active.get("chat") is not None
        sem.reset_session_budget("chat")
        assert sem._session_first_active.get("chat") is None

    @pytest.mark.asyncio
    async def test_reset_clears_extension_override(self):
        """A workflow run on this session may have installed an override.
        Reset clears it so the next user turn starts at base timeout."""
        sem = FairLLMSemaphore(max_concurrent=1, session_timeout=1800.0)
        sem.extend_session_budget("chat", 5000.0)
        assert sem._session_timeout_override.get("chat") is not None
        sem.reset_session_budget("chat")
        assert sem._session_timeout_override.get("chat") is None

    @pytest.mark.asyncio
    async def test_acquire_after_near_exhaustion_succeeds_after_reset(self, monkeypatch):
        """The whole point: a session at 0 remaining budget can acquire
        again after reset, with a fresh full window."""
        sem = FairLLMSemaphore(max_concurrent=1, session_timeout=10.0)
        await sem.acquire(session_id="chat")
        sem.release()

        import time as _t

        from core.llm import semaphore as _sem_mod

        t0 = _t.monotonic()

        # +20s elapsed: budget exhausted, acquire should reject.
        monkeypatch.setattr(_sem_mod, "time", type("T", (), {"monotonic": staticmethod(lambda: t0 + 20.0)}))
        with pytest.raises(LLMSessionTimeoutError):
            await sem.acquire(session_id="chat")

        # Reset, then acquire — clock starts over from "now".
        sem.reset_session_budget("chat")
        await sem.acquire(session_id="chat")
        # Fresh budget: full session_timeout (10s) is available again.
        remaining = sem.session_seconds_remaining("chat")
        assert remaining > 9.0, f"reset should restore full budget; got {remaining}s remaining"

    def test_reset_empty_session_id_is_noop(self):
        sem = FairLLMSemaphore(max_concurrent=1, session_timeout=1800.0)
        # Should not raise, should not affect any session.
        sem.reset_session_budget("")
        sem.reset_session_budget("nonexistent")  # also fine — it's a pop()


# ---------------------------------------------------------------------------
# LLMClient.has_capacity() tests
# ---------------------------------------------------------------------------


class TestHasCapacity:
    """Test the capacity check helper used for waiting event emission."""

    @pytest.mark.asyncio
    async def test_has_capacity_true_when_available(self):
        from core.llm.client import LLMClient
        from core.llm.router import ProviderRouter

        with patch.object(ProviderRouter, "__init__", lambda self: None):
            client = LLMClient()

        client.router = MagicMock()
        sem = FairLLMSemaphore(max_concurrent=2)
        client.router.get_provider.return_value = "ollama"
        client.router.get_semaphore.return_value = sem

        assert client.has_capacity("test-model") is True

    @pytest.mark.asyncio
    async def test_has_capacity_false_when_full(self):
        from core.llm.client import LLMClient
        from core.llm.router import ProviderRouter

        with patch.object(ProviderRouter, "__init__", lambda self: None):
            client = LLMClient()

        client.router = MagicMock()
        sem = FairLLMSemaphore(max_concurrent=1)
        await sem.acquire()
        client.router.get_provider.return_value = "ollama"
        client.router.get_semaphore.return_value = sem

        assert client.has_capacity("test-model") is False
        sem.release()


# ---------------------------------------------------------------------------
# Agent waiting_llm event emission
# ---------------------------------------------------------------------------


class TestAgentWaitingEvent:
    """Test that session.waiting_llm is emitted when LLM capacity is full."""

    def _make_fake_client(self, has_capacity_val):
        from tests.conftest import FakeLLMClient

        fake = FakeLLMClient()
        fake.has_capacity = MagicMock(return_value=has_capacity_val)
        # Agent calls client.router.registry.resolve_model_id
        fake.router = MagicMock()
        fake.router.registry.resolve_model_id.side_effect = lambda m: m
        fake.resolve_provider = MagicMock(return_value="fake")
        return fake

    @pytest.mark.asyncio
    async def test_waiting_event_emitted_when_no_capacity(self, session_factory, mock_scout):
        """When has_capacity returns False, session.waiting_llm event is emitted."""
        from core.agent import run_agent

        sid = session_factory(title="waiting test")
        session = AgentSession(session_id=sid)
        session.transition_to(SessionState.SCOUTING)
        session.transition_to(SessionState.PROCESSING)
        session.last_scout_report = mock_scout

        events = []
        original_emit = session.emit_event

        def capture_emit(event):
            events.append(event)
            original_emit(event)

        session.emit_event = capture_emit
        fake_client = self._make_fake_client(has_capacity_val=False)

        with patch("core.agent.get_llm_client", return_value=fake_client), patch("core.agent.get_registry") as mock_reg:
            mock_reg.return_value.enabled_tools.return_value = []
            mock_reg.return_value.get_schemas.return_value = []
            await run_agent(sid, "test message", session)

        waiting_events = [e for e in events if e.get("type") == "session.waiting_llm"]
        assert len(waiting_events) >= 1, "Expected session.waiting_llm event when no capacity"

    @pytest.mark.asyncio
    async def test_no_waiting_event_when_capacity_available(self, session_factory, mock_scout):
        """When has_capacity returns True, no waiting event is emitted."""
        from core.agent import run_agent

        sid = session_factory(title="no-wait test")
        session = AgentSession(session_id=sid)
        session.transition_to(SessionState.SCOUTING)
        session.transition_to(SessionState.PROCESSING)
        session.last_scout_report = mock_scout

        events = []
        original_emit = session.emit_event

        def capture_emit(event):
            events.append(event)
            original_emit(event)

        session.emit_event = capture_emit
        fake_client = self._make_fake_client(has_capacity_val=True)

        with patch("core.agent.get_llm_client", return_value=fake_client), patch("core.agent.get_registry") as mock_reg:
            mock_reg.return_value.enabled_tools.return_value = []
            mock_reg.return_value.get_schemas.return_value = []
            await run_agent(sid, "test message", session)

        waiting_events = [e for e in events if e.get("type") == "session.waiting_llm"]
        assert len(waiting_events) == 0, "No waiting event expected when capacity is available"


# ---------------------------------------------------------------------------
# Scout waiting step
# ---------------------------------------------------------------------------


class TestScoutWaiting:
    """Test that scout emits a waiting step when LLM capacity is full."""

    @pytest.mark.asyncio
    async def test_scout_emits_waiting_step(self):
        """Scout should emit step='waiting' when no LLM capacity."""
        from core.scout.report import SessionBrief
        from core.scout.runner import _run_scout_llm

        steps = []

        def capture_emit(event):
            if event.get("type") == "scout.step":
                steps.append(event)

        brief = SessionBrief(session_id="test", is_fresh=True)

        from tests.conftest import FakeLLMClient

        fake = FakeLLMClient()
        fake.has_capacity = MagicMock(return_value=False)

        with (
            patch("core.llm.client.get_llm_client", return_value=fake),
            patch("core.scout.runner.settings") as mock_settings,
        ):
            mock_settings.scout_model = ""
            mock_settings.background_model = ""
            mock_settings.llm_model = "test-model"
            mock_settings.workspace_dir = "/tmp/nonexistent"

            await _run_scout_llm("test query", brief, emit=capture_emit)

        waiting_steps = [s for s in steps if s.get("step") == "waiting"]
        assert len(waiting_steps) == 1, f"Expected 1 waiting step, got {len(waiting_steps)}"

    @pytest.mark.asyncio
    async def test_scout_no_waiting_step_when_capacity(self):
        """Scout should NOT emit waiting step when capacity is available."""
        from core.scout.report import SessionBrief
        from core.scout.runner import _run_scout_llm

        steps = []

        def capture_emit(event):
            if event.get("type") == "scout.step":
                steps.append(event)

        brief = SessionBrief(session_id="test", is_fresh=True)

        from tests.conftest import FakeLLMClient

        fake = FakeLLMClient()
        fake.has_capacity = MagicMock(return_value=True)

        with (
            patch("core.llm.client.get_llm_client", return_value=fake),
            patch("core.scout.runner.settings") as mock_settings,
        ):
            mock_settings.scout_model = ""
            mock_settings.background_model = ""
            mock_settings.llm_model = "test-model"
            mock_settings.workspace_dir = "/tmp/nonexistent"

            await _run_scout_llm("test query", brief, emit=capture_emit)

        waiting_steps = [s for s in steps if s.get("step") == "waiting"]
        assert len(waiting_steps) == 0, f"No waiting step expected, got {waiting_steps}"


# ---------------------------------------------------------------------------
# End-to-end: two sessions competing for one LLM slot
# ---------------------------------------------------------------------------


class TestConcurrentSessions:
    """Integration test: two sessions sharing a single LLM slot."""

    @pytest.mark.asyncio
    async def test_second_session_completes_after_first(self):
        """Session B should wait for session A's LLM call, then succeed."""
        sem = FairLLMSemaphore(max_concurrent=1)

        results = []
        gate = asyncio.Event()

        async def session_a():
            await sem.acquire()
            try:
                gate.set()  # signal that A holds the slot
                await asyncio.sleep(0.1)  # simulate LLM work
                results.append("A")
            finally:
                sem.release()

        async def session_b():
            await gate.wait()  # wait until A holds the slot
            await sem.acquire(timeout=5.0)  # should wait ~100ms, not fail
            try:
                results.append("B")
            finally:
                sem.release()

        await asyncio.gather(
            asyncio.create_task(session_a()),
            asyncio.create_task(session_b()),
        )

        assert results == ["A", "B"], f"Expected A then B, got {results}"
        assert sem.available == 1
