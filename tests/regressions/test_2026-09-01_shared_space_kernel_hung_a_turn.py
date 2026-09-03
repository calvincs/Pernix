"""A busy shared space kernel parked a repl call for 15 minutes.

Space sessions share one kernel, so a second session's repl call waits for
the round-trip lock. That wait defaulted to RT_LOCK_TIMEOUT (900s) and the
cell budget was granted ON TOP — far past the executor's own dispatch
timeout, which had already told the agent the tool timed out. The kernel
is deliberately unregistered, so the post-timeout subprocess kill cannot
reach it either: the cell eventually ran with nobody left to receive its
result, and a retrying model ran it twice.

The wait is now capped at the caller's own budget, so a busy kernel fails
fast with the existing "shared space kernel busy" message.
"""

import inspect
import threading
import time

import pytest

from core.extensions.rlm.child_env import RT_LOCK_TIMEOUT, ChildBusy


class _FakeChild:
    """Just the locking half of the child env."""

    def __init__(self):
        self._rt_lock = threading.Lock()

    _acquire_rt = None  # bound below from the real implementation


def test_the_repl_tool_passes_its_own_budget_as_the_lock_wait():
    from core.tools.builtin import repl_tool

    src = inspect.getsource(repl_tool.repl)
    assert "lock_timeout=effective_timeout" in src, "waiting longer than the executor does is never useful"


def test_execute_cell_accepts_a_lock_timeout():
    from core.extensions.rlm.child_env import ChildREPL

    sig = inspect.signature(ChildREPL.execute_cell)
    assert "lock_timeout" in sig.parameters
    src = inspect.getsource(ChildREPL.execute_cell)
    assert "lock_timeout or RT_LOCK_TIMEOUT" in src, "the old default must remain for RLM's own child"


def test_the_kernel_forwards_it():
    from core.kernel import SessionKernel

    assert "lock_timeout" in inspect.signature(SessionKernel.execute).parameters
    assert "lock_timeout=lock_timeout" in inspect.getsource(SessionKernel.execute)


def test_a_short_wait_gives_up_quickly_instead_of_parking():
    from core.extensions.rlm.child_env import ChildREPL

    env = object.__new__(ChildREPL)
    env._rt_lock = threading.Lock()
    env._rt_lock.acquire()  # a sibling session is mid-cell
    try:
        started = time.monotonic()
        with pytest.raises(ChildBusy):
            ChildREPL._acquire_rt(env, "cell execution", timeout=0.2)
        elapsed = time.monotonic() - started
        assert elapsed < 5, f"waited {elapsed:.1f}s"
        assert RT_LOCK_TIMEOUT >= 900, "the default is still the long one for RLM's per-run child"
    finally:
        env._rt_lock.release()
