"""Tests for core.memory.dedup — per-session recall dedup ledger.

The ledger tracks "{file_name}@{epoch}" keys already surfaced to the model in
a session. partition_seen splits incoming SearchResults into (new, seen_keys)
and records the new keys. The check-and-record must be atomic across threads
so parallel tool calls in the same round (recall + search_web via
asyncio.gather in the executor) don't both classify the same entry as new.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


def _result(file_name: str, epoch: int, score: float = 4.0, content: str = "body"):
    """Build a SearchResult-shaped duck for partition_seen.

    partition_seen only reads .entry.file_name, .entry.epoch, .entry.content,
    so SimpleNamespace stand-ins are sufficient.
    """
    entry = SimpleNamespace(
        file_name=file_name,
        epoch=epoch,
        content=content,
        entry_type="finding",
    )
    return SimpleNamespace(entry=entry, score=score, source="bm25")


class _FakeSession:
    def __init__(self):
        self._seen_memory_keys: set = set()
        self._seen_memory_lock = threading.Lock()


def _install_session(monkeypatch, sid: str, session=None):
    """Wire sessions.manager.get_manager() to return the given fake session for sid.

    Any other sid resolves to None so we exercise the "unknown session"
    pass-through path too.
    """
    sess = session if session is not None else _FakeSession()

    class _Mgr:
        def get(self, s):
            return sess if s == sid else None

    import sessions.manager as mgr_mod

    monkeypatch.setattr(mgr_mod, "get_manager", lambda: _Mgr())
    return sess


def test_first_call_passes_all_through(monkeypatch):
    from core.memory.dedup import partition_seen

    sid = "s1"
    _install_session(monkeypatch, sid)
    results = [_result("notes", 100), _result("research", 200)]

    new, seen, footer = partition_seen(results, sid)
    assert new == results
    assert seen == []
    assert footer == ""


def test_second_call_dedups_overlap(monkeypatch):
    from core.memory.dedup import partition_seen

    sid = "s1"
    _install_session(monkeypatch, sid)
    a = _result("notes", 100)
    b = _result("research", 200)
    c = _result("research", 201)  # different epoch — distinct entry

    partition_seen([a, b], sid)
    new, seen, footer = partition_seen([a, b, c], sid)

    assert [r.entry.file_name for r in new] == ["research"]
    assert new[0].entry.epoch == 201
    assert sorted(seen) == ["notes@100", "research@200"]
    assert "notes@100" in footer
    assert "research@200" in footer
    assert "include_seen=True" in footer


def test_include_seen_bypass_via_recall(monkeypatch, tmp_path):
    """recall(include_seen=True) must return full content even after the
    same entries were surfaced in a prior call this session."""
    from core.tools.builtin.memory_tools import recall

    sid = "s-bypass"
    _install_session(monkeypatch, sid)

    class _Store:
        def search(self, *_a, **_kw):
            return [_result("notes", 100, content="full body 1"), _result("research", 200, content="full body 2")]

    import core.memory.store as store_mod

    monkeypatch.setattr(store_mod, "get_memory_store", lambda: _Store())

    first = recall("q", _context={"session_id": sid})
    assert "full body 1" in first
    assert "full body 2" in first

    second_dedup = recall("q", _context={"session_id": sid})
    # Second call without include_seen → footer only, no bodies.
    assert "full body 1" not in second_dedup
    assert "notes@100" in second_dedup

    second_force = recall("q", include_seen=True, _context={"session_id": sid})
    assert "full body 1" in second_force
    assert "full body 2" in second_force


def test_sessions_are_isolated(monkeypatch):
    """Two sessions maintain independent ledgers."""
    from core.memory.dedup import partition_seen

    sess_a = _FakeSession()
    sess_b = _FakeSession()

    class _Mgr:
        def get(self, s):
            return {"a": sess_a, "b": sess_b}.get(s)

    import sessions.manager as mgr_mod

    monkeypatch.setattr(mgr_mod, "get_manager", lambda: _Mgr())

    r = _result("notes", 100)
    partition_seen([r], "a")
    new_b, seen_b, _footer = partition_seen([r], "b")

    assert len(new_b) == 1, "session b should still see this entry as new"
    assert seen_b == []


def test_empty_session_id_is_passthrough(monkeypatch):
    from core.memory.dedup import partition_seen

    results = [_result("notes", 100)]
    new, seen, footer = partition_seen(results, "")
    assert new == results
    assert seen == []
    assert footer == ""


def test_unknown_session_id_is_passthrough(monkeypatch):
    from core.memory.dedup import partition_seen

    class _Mgr:
        def get(self, _s):
            return None

    import sessions.manager as mgr_mod

    monkeypatch.setattr(mgr_mod, "get_manager", lambda: _Mgr())

    results = [_result("notes", 100)]
    new, seen, footer = partition_seen(results, "ghost")
    assert new == results
    assert seen == []
    assert footer == ""


def test_parallel_threads_race_is_atomic(monkeypatch):
    """The within-turn parallel case (recall + search_web fanned out via
    asyncio.gather + asyncio.to_thread in the executor) must produce each
    key exactly once across both calls.

    Concretely: two threads call partition_seen with the same overlapping
    result set; combined output should classify each entry as "new" in
    exactly one thread and "seen" in the other. Without a lock around the
    check-then-add, both threads could see an empty ledger and both could
    add — leaving the model with two full copies of every entry.
    """
    from core.memory.dedup import partition_seen

    sid = "race"
    _install_session(monkeypatch, sid)

    # 50 overlapping entries — wide enough that an unlocked impl would
    # flake on at least one key on most runs.
    results = [_result(f"f{i}", i) for i in range(50)]
    barrier = threading.Barrier(2)

    def _call():
        barrier.wait()  # release both threads simultaneously
        return partition_seen(list(results), sid)

    with ThreadPoolExecutor(max_workers=2) as ex:
        (new_a, seen_a, _fa), (new_b, seen_b, _fb) = (f.result() for f in [ex.submit(_call), ex.submit(_call)])

    keys_new = {f"{r.entry.file_name}@{r.entry.epoch}" for r in new_a} | {
        f"{r.entry.file_name}@{r.entry.epoch}" for r in new_b
    }
    keys_seen = set(seen_a) | set(seen_b)
    all_keys = {f"f{i}@{i}" for i in range(50)}

    # Every key surfaced new exactly once across both threads.
    assert keys_new == all_keys
    # Every key also appeared seen in the other thread.
    assert keys_seen == all_keys
    # No overlap in "new" classifications between threads — a key is new
    # for exactly one of the two.
    new_a_keys = {f"{r.entry.file_name}@{r.entry.epoch}" for r in new_a}
    new_b_keys = {f"{r.entry.file_name}@{r.entry.epoch}" for r in new_b}
    assert new_a_keys.isdisjoint(new_b_keys)


def test_footer_lists_keys_sorted(monkeypatch):
    from core.memory.dedup import partition_seen

    sid = "s-sort"
    _install_session(monkeypatch, sid)
    rs = [_result("z", 1), _result("a", 2), _result("m", 3)]
    partition_seen(rs, sid)
    _new, _seen, footer = partition_seen(rs, sid)

    # Sorted footer ordering helps human review and stable test assertions.
    positions = [footer.index(k) for k in ["a@2", "m@3", "z@1"]]
    assert positions == sorted(positions)


def test_internal_recall_emits_footer_when_all_seen(monkeypatch):
    """When every memory hit was already surfaced earlier this session,
    format_for_tool_output should still render a MEMORY block — the footer
    — so the model knows the entries exist instead of seeing 'no matching
    entries.'"""
    from core.memory import internal_recall as mod
    from core.memory.internal_recall import format_for_tool_output

    sid = "s-allseen"
    _install_session(monkeypatch, sid)

    import core.memory.store as store_mod

    class _Store:
        def search(self, *_a, **_kw):
            return [_result("notes", 100, score=4.5), _result("research", 200, score=4.2)]

    monkeypatch.setattr(store_mod, "get_memory_store", lambda: _Store())
    import core.scout.search as scout_search_mod

    monkeypatch.setattr(scout_search_mod, "gather_cross_session_data", lambda *_a, **_kw: "")

    first = mod.internal_recall("q", current_session_id=sid)
    assert first.memory_text != ""
    assert first.memory_seen_footer == ""

    second = mod.internal_recall("q", current_session_id=sid)
    assert second.memory_text == ""
    assert "notes@100" in second.memory_seen_footer
    # memory_strong derives from raw search scores, not from what survived
    # dedup — a strong-but-seen entry must still nudge the agent.
    assert second.memory_strong is True

    out = format_for_tool_output(second)
    assert "MEMORY:" in out
    assert "no matching entries" not in out
    assert "notes@100" in out


def test_recall_full_dedup_returns_footer_only(monkeypatch):
    """If every result is a repeat, recall returns just the footer (no body)."""
    from core.tools.builtin.memory_tools import recall

    sid = "s-full"
    _install_session(monkeypatch, sid)

    class _Store:
        def search(self, *_a, **_kw):
            return [_result("notes", 100, content="body A"), _result("research", 200, content="body B")]

    import core.memory.store as store_mod

    monkeypatch.setattr(store_mod, "get_memory_store", lambda: _Store())

    recall("q", _context={"session_id": sid})  # prime
    out = recall("q", _context={"session_id": sid})

    assert "body A" not in out
    assert "body B" not in out
    assert "notes@100" in out
    assert "research@200" in out


@pytest.mark.parametrize("file_name,epoch", [("", 100), ("notes", None)])
def test_unkeyable_result_falls_through_as_new(monkeypatch, file_name, epoch):
    """A result missing file_name or epoch can't be keyed — must pass through
    as a "new" result so the model still sees the body. Defensive: shouldn't
    happen in practice, but we don't want to silently drop entries."""
    from core.memory.dedup import partition_seen

    sid = "s-unkeyable"
    _install_session(monkeypatch, sid)
    r = _result(file_name or "x", epoch if epoch is not None else 1)
    r.entry.file_name = file_name
    r.entry.epoch = epoch

    new, seen, footer = partition_seen([r], sid)
    assert new == [r]
    assert seen == []
    assert footer == ""
