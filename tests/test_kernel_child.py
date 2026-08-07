"""Pernix — ChildREPL plain-scaffold mode + snapshot/restore frames (plan 2a).

Session kernels run the same sandboxed child as RLM but with scaffold="plain":
no sub-LLM stubs (nothing to dial), no answer dict — just a persistent
namespace with the builtins guardrail. Snapshots serialize per top-level name
so one unpicklable object is skipped-and-reported, never fatal.
"""

import threading
import time

import dill
import pytest

from core.extensions.rlm.child_env import ChildBusy, ChildREPL


def _cell(c: ChildREPL, code: str):
    return c.execute_cell(code, deadline=time.monotonic() + 60)


@pytest.fixture
def plain_child(tmp_path):
    c = ChildREPL(tmp_path, scaffold="plain")
    c.start()
    yield c
    c.cleanup()


@pytest.fixture
def rlm_child(tmp_path):
    c = ChildREPL(tmp_path)  # default scaffold="rlm"
    c.start()
    yield c
    c.cleanup()


# ---------------------------------------------------------------------------
# Scaffold modes
# ---------------------------------------------------------------------------


def test_plain_child_has_no_rlm_scaffolding(plain_child):
    r = _cell(plain_child, "print(llm_query)")
    assert "NameError" in r.stderr
    r = _cell(plain_child, "print(answer)")
    assert "NameError" in r.stderr
    r = _cell(plain_child, "print(SHOW_VARS)")
    assert "NameError" in r.stderr


def test_plain_child_persists_namespace_and_guardrail(plain_child):
    _cell(plain_child, "x = 41")
    r = _cell(plain_child, "x += 1\nprint(x)")
    assert "42" in r.stdout
    # Builtins guardrail restored between cells even in plain mode.
    _cell(plain_child, "__builtins__['print'] = None")
    r = _cell(plain_child, "print('alive')")
    assert "alive" in r.stdout


def test_rlm_child_scaffolding_unchanged(rlm_child):
    r = _cell(rlm_child, "print(type(llm_query).__name__, type(answer).__name__)")
    assert "method" in r.stdout and "_AnswerDict" in r.stdout


# ---------------------------------------------------------------------------
# Snapshot / restore
# ---------------------------------------------------------------------------


def test_snapshot_restore_roundtrip(tmp_path, plain_child):
    _cell(plain_child, "x = 42\nwords = ['a', 'b']\nf = lambda n: n * 2\nd = {'k': [1, 2]}")
    snap = tmp_path / "kernel-state.dill"
    reply = plain_child.snapshot(snap)
    assert reply["ok"]
    assert set(reply["stored"]) == {"x", "words", "f", "d"}
    assert reply["skipped"] == {}
    assert reply["bytes"] > 0
    assert snap.exists()

    fresh = ChildREPL(tmp_path / "second", scaffold="plain")
    fresh.start()
    try:
        restore = fresh.restore(snap)
        assert restore["ok"]
        assert set(restore["restored"]) == {"x", "words", "f", "d"}
        r = _cell(fresh, "print(x, words, f(21), d['k'])")
        assert "42 ['a', 'b'] 42 [1, 2]" in r.stdout
    finally:
        fresh.cleanup()


def test_snapshot_skips_unpicklables_and_modules(tmp_path, plain_child):
    # A generator holds a live frame — unpicklable even for dill.
    _cell(plain_child, "import json\ngen = (i for i in range(3))\nok_value = 7")
    reply = plain_child.snapshot(tmp_path / "s.dill")
    assert reply["ok"]
    assert "ok_value" in reply["stored"]
    assert "module" in reply["skipped"].get("json", "")
    assert "TypeError" in reply["skipped"].get("gen", "")
    assert "ok_value" not in reply["skipped"]


def test_snapshot_size_cap_skips_oversized(tmp_path, plain_child):
    _cell(plain_child, "big = 'a' * 500_000\nsmall = 1")
    reply = plain_child.snapshot(tmp_path / "s.dill", max_bytes=10_000)
    assert reply["ok"]
    assert "small" in reply["stored"]
    assert "size cap" in reply["skipped"].get("big", "")


def test_restore_never_clobbers_scaffold(tmp_path, rlm_child):
    # A snapshot file that tries to smuggle a scaffold override in.
    snap = tmp_path / "evil.dill"
    with open(snap, "wb") as f:
        dill.dump({"llm_query": dill.dumps("not-a-function"), "x": dill.dumps(9)}, f)
    reply = rlm_child.restore(snap)
    assert reply["ok"]
    assert reply["restored"] == ["x"]
    r = _cell(rlm_child, "print(callable(llm_query), x)")
    assert "True 9" in r.stdout


def test_snapshot_waits_for_running_cell(tmp_path, plain_child):
    """The round-trip lock serializes drivers: a snapshot issued mid-cell
    lands after the cell and sees its effects."""
    results = {}

    def _run_cell():
        results["cell"] = _cell(plain_child, "import time\ntime.sleep(1.5)\nlate_var = 'set'")

    t = threading.Thread(target=_run_cell)
    t.start()
    time.sleep(0.4)  # cell is mid-sleep and holds the lock
    reply = plain_child.snapshot(tmp_path / "s.dill")
    t.join()
    assert reply["ok"]
    assert "late_var" in reply["stored"]


def test_roundtrip_deadline_starts_after_the_lock_wait(tmp_path, plain_child):
    """Time spent waiting for the round-trip lock must not count against the
    frame's own budget. Before the fix, a frame queued behind a long cell was
    sent with an already-expired deadline and _roundtrip_locked killed a
    perfectly healthy kernel on the very first loop iteration."""
    plain_child._rt_lock.acquire()

    def _hold():
        time.sleep(1.5)
        plain_child._rt_lock.release()

    t = threading.Thread(target=_hold)
    t.start()
    try:
        # A budget far shorter than the lock wait: only correct if the clock
        # restarts once the lock is actually held.
        reply = plain_child._roundtrip(
            {"type": "snapshot", "path": str(tmp_path / "late.dill"), "max_bytes": 1_000_000},
            deadline=time.monotonic() + 1.0,
        )
    finally:
        t.join()
    assert reply["type"] == "snapshot_result"
    assert plain_child.popen.poll() is None  # alive — never killed mid-cell


def test_lock_timeout_reports_busy_and_leaves_the_child_alone(plain_child):
    """Giving up on the lock is a ChildBusy report, not a kill."""
    plain_child._rt_lock.acquire()
    try:
        with pytest.raises(ChildBusy):
            plain_child._acquire_rt("snapshot", timeout=0.2)
        assert plain_child.popen.poll() is None
    finally:
        plain_child._rt_lock.release()
    # And the child still works afterwards.
    assert "ok" in _cell(plain_child, "print('ok')").stdout
