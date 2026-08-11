"""Regression: agent tool calls starved every API request, freezing the UI.

Shipped defect (2026-08-11 production incident on box.ventibean.com): tool
dispatch in `core/tools/executor.py` sent every non-`long_poll` tool through
`asyncio.to_thread`, which runs on the event loop's DEFAULT ThreadPoolExecutor
— sized `min(32, cpu_count + 4)`, i.e. 20 threads on the box. Every API route
hops that same pool for its DB reads (`await asyncio.to_thread(db...)` in
`api/routers/*`, ~150 call sites), so the web UI competed with agent work for
20 slots. One `bash` call can hold a slot for BASH_MAX_TIMEOUT (30 minutes) and
`http_get` for ~150s (15s × 10 redirect hops), so a few concurrent tool calls
wedged the whole app. py-spy caught it live: `bash` on `ThreadPoolExecutor-0_19`
and `http_get` on `ThreadPoolExecutor-0_18` while API requests queued behind
them — a UI that looked hung with the event loop idle and CPU at 1.2%.

The module already documented this exact starvation deadlock, but only routed
`long_poll` tools (3 of them) to a dedicated pool. `bash` — which can hold a
thread 60× longer than any of those — was not one of them.

Second defect, same incident: `AgentSession` tracked the running subprocess in
a single `_active_process` slot. Two concurrent bash calls in one session
overwrote each other, so the post-timeout kill in `_kill_tool_subprocess` could
not reach the first process (its thread stayed blocked for the child's full
runtime), and whichever call finished first cleared the slot out from under the
other. The orphaned children reparented to PID 1 and became zombies.

Pinned because both regress silently: the app keeps working under light load
and only wedges under concurrency, which no unit test would otherwise notice.
"""

from __future__ import annotations

import asyncio
import threading

from core.tools import executor as tool_executor
from sessions.state import AgentSession

# --- Pool isolation -------------------------------------------------------


def test_tool_dispatch_never_uses_the_default_executor():
    """A tool must run on the dedicated pool, not asyncio's default one.

    Asserted by thread name: `asyncio.to_thread` workers are named
    `ThreadPoolExecutor-N_M`; ours are `pernix-tool_M`.
    """
    seen: dict[str, str] = {}

    class _Reg:
        def get(self, name):
            return _TOOL

        def is_disabled(self, name):
            return False

        metrics = _Metrics()

        def execute_sync(self, name, arguments, context):
            seen["thread"] = threading.current_thread().name
            return "ok"

    result = asyncio.run(tool_executor._execute_single("probe", {}, None, _Reg()))

    assert result.was_error is False
    assert seen["thread"].startswith("pernix-tool"), (
        f"tool ran on {seen['thread']!r} — the shipped bug ran it on the default "
        "executor (ThreadPoolExecutor-N_M) that every API route needs"
    )
    assert not seen["thread"].startswith("ThreadPoolExecutor-")


def test_long_poll_tools_keep_their_own_pool():
    """long_poll tools must NOT share the ordinary tool pool.

    They block 30-60 minutes waiting on workers that need tool threads to make
    progress; sharing one pool reopens the starvation deadlock.
    """
    seen: dict[str, str] = {}

    class _Reg:
        def get(self, name):
            return _LONG_POLL_TOOL

        def is_disabled(self, name):
            return False

        metrics = _Metrics()

        def execute_sync(self, name, arguments, context):
            seen["thread"] = threading.current_thread().name
            return "ok"

    asyncio.run(tool_executor._execute_single("orchestrate", {}, None, _Reg()))

    assert seen["thread"].startswith("pernix-longpoll")


def test_pools_are_distinct_objects():
    assert tool_executor._get_tool_executor() is not tool_executor._get_long_poll_executor()


# --- Per-call subprocess registration -------------------------------------


def _fake_proc(pid: int):
    class _P:
        def __init__(self):
            self.pid = pid

        def poll(self):
            return None

    return _P()


def test_concurrent_registrations_do_not_overwrite_each_other():
    """The shipped bug: second bash call clobbered the first's entry."""
    s = AgentSession(session_id="s1")
    a, b = _fake_proc(101), _fake_proc(102)

    h_a = s.register_process(a, owner="bash@1")
    h_b = s.register_process(b, owner="bash@2")

    assert h_a != h_b
    assert {p.pid for p in s.all_processes()} == {101, 102}


def test_timeout_kills_only_the_call_that_timed_out():
    """Sibling tool calls still running healthily must survive."""
    s = AgentSession(session_id="s2")
    s.register_process(_fake_proc(201), owner="bash@1")
    s.register_process(_fake_proc(202), owner="bash@2")

    assert [p.pid for p in s.processes_for("bash@1")] == [201]
    assert [p.pid for p in s.processes_for("bash@2")] == [202]


def test_release_is_scoped_and_idempotent():
    """One call finishing must not untrack another's process."""
    s = AgentSession(session_id="s3")
    h_a = s.register_process(_fake_proc(301), owner="bash@1")
    s.register_process(_fake_proc(302), owner="bash@2")

    s.release_process(h_a)
    s.release_process(h_a)  # idempotent

    assert [p.pid for p in s.all_processes()] == [302]


def test_one_owner_may_register_several_children():
    """An RLM run spawns several children; one exiting must not untrack the rest."""
    s = AgentSession(session_id="s4")
    h1 = s.register_process(_fake_proc(401), owner="rlm@1")
    s.register_process(_fake_proc(402), owner="rlm@1")

    s.release_process(h1)

    assert [p.pid for p in s.processes_for("rlm@1")] == [402]


def test_dispatch_timeout_kills_this_calls_process(monkeypatch):
    """End-to-end: a timing-out bash call kills its own child, not its sibling."""
    from sessions import manager as sess_manager

    s = AgentSession(session_id="s5")
    monkeypatch.setattr(sess_manager, "get_manager", lambda: _FakeManager(s))

    killed: list[int] = []
    monkeypatch.setattr(
        "core.tools.builtin.core_tools._kill_process_tree",
        lambda p: killed.append(p.pid),
    )

    s.register_process(_fake_proc(501), owner="bash@mine")
    s.register_process(_fake_proc(502), owner="bash@sibling")

    tool_executor._kill_tool_subprocess({"session_id": "s5"}, "bash@mine")

    assert killed == [501], "the sibling call's process must not be killed"


# --- fixtures -------------------------------------------------------------


class _Metric:
    def record_success(self, *a):
        pass

    def record_failure(self, *a):
        pass

    def record_timeout(self, *a):
        pass


class _Metrics:
    def __getitem__(self, _name):
        return _Metric()


class _ToolDef:
    def __init__(self, long_poll=False):
        self.timeout = 5
        self.max_timeout = 0
        self.long_poll = long_poll
        self.denied_session_types = set()
        self.safety_level = "safe"
        self.source = "builtin"
        self.parallel_safe = False


_TOOL = _ToolDef(long_poll=False)
_LONG_POLL_TOOL = _ToolDef(long_poll=True)


class _FakeManager:
    def __init__(self, session):
        self._s = session

    def get(self, sid):
        return self._s if sid == self._s.session_id else None
