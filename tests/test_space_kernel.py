"""Shared space kernels (v33): key scheme, lifecycle decoupling from member
sessions, mid-turn widening, pinned cwd. No child processes are spawned —
everything here exercises registry bookkeeping."""

from __future__ import annotations

import pytest

from core import spaces as spaces_lib
from core.kernel import KernelRegistry, SessionKernel
from db import models as db


@pytest.fixture(autouse=True)
def _fresh_space_cache(tmp_path, monkeypatch):
    # Keep kernel state dirs inside the test sandbox.
    monkeypatch.setattr("core.kernel.KERNEL_STATE_ROOT", tmp_path / "kernels")
    spaces_lib.invalidate_space_cache()
    yield
    spaces_lib.invalidate_space_cache()


def test_get_or_create_keys_and_shares(tmp_path):
    reg = KernelRegistry()
    a = reg.get_or_create("space-abc123", cwd=str(tmp_path / "home"))
    b = reg.get_or_create("space-abc123")
    assert a is b, "two sessions resolving the same space key share one kernel"
    assert a.cwd == str(tmp_path / "home")  # first creation pins the cwd
    solo = reg.get_or_create("plainsession")
    assert solo is not a
    assert solo.cwd is None


def test_member_session_shutdown_does_not_touch_space_kernel():
    reg = KernelRegistry()
    shared = reg.get_or_create("space-abc123")
    # Deleting/removing a MEMBER session shuts down by the member's id —
    # which is not the space key, so the shared kernel must survive.
    reg.shutdown_session("member-session-id", snapshot=False, purge_state=True)
    assert reg._kernels.get("space-abc123") is shared


def test_space_key_shutdown_removes_it():
    reg = KernelRegistry()
    reg.get_or_create("space-abc123")
    reg.shutdown_session("space-abc123", snapshot=False, purge_state=True)
    assert "space-abc123" not in reg._kernels


def test_mid_turn_widens_to_any_space_member():
    sp = db.create_space("Lab", "#123456", "lab")
    sid = db.create_session(title="member", space_id=sp["id"])
    key = f"space-{sp['id']}"

    assert KernelRegistry._session_mid_turn(key) is False
    db.update_session(sid, state_v2="processing")
    assert KernelRegistry._session_mid_turn(key) is True
    # The single-session check still works for plain keys.
    assert KernelRegistry._session_mid_turn(sid) is True
    db.update_session(sid, state_v2="idle_ready")
    assert KernelRegistry._session_mid_turn(key) is False


def test_state_dir_follows_key(tmp_path, monkeypatch):
    monkeypatch.setattr("core.kernel.KERNEL_STATE_ROOT", tmp_path / "kernels")
    k = SessionKernel("space-xyz", cwd=str(tmp_path))
    assert k.state_dir == (tmp_path / "kernels" / "space-xyz").resolve()


def test_busy_error_names_the_space(monkeypatch):
    """The ChildBusy path mentions the shared space so the agent doesn't
    read a sibling session's cell as a hang."""
    from core.extensions.rlm.child_env import ChildBusy
    from core.kernel import KernelError

    k = SessionKernel("space-xyz")

    class _BusyRepl:
        popen = type("P", (), {"poll": staticmethod(lambda: None)})()

        def execute_cell(self, *a, **kw):
            raise ChildBusy("round-trip lock held")

    k._repl = _BusyRepl()
    monkeypatch.setattr(k, "ensure_started", lambda: None)
    with pytest.raises(KernelError, match="shared space kernel busy"):
        k.execute("1+1", timeout=1)
