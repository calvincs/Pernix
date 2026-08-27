"""Tests for typed worker kinds (spec Feature 4).

Coverage: kind resolution (built-ins + data-root overrides), spawn_worker
integration (unknown-kind refusal, charter injection, allowlist + persistence),
the deterministic kind gate in get_worker_result, retry_worker kind
inheritance, and get_or_create identity restore (the Feature 5 seam).
"""

from __future__ import annotations

import asyncio
import json

import pytest

import core.extensions.orchestration as orch
from core.extensions.orchestration import kinds
from db import models as db
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


def _processing_parent(mgr):
    from sessions import state_v2 as sv2

    parent_id = mgr.create_session(title="Parent")
    mgr.get(parent_id)._state_v2 = sv2.SessionStateV2.PROCESSING
    return parent_id


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_builtin_kinds_resolve():
    for name in ("research", "code", "explore", "debug", "transform"):
        k = kinds.resolve_kind(name)
        assert k is not None, name
        assert k.name == name
        assert k.role_instructions
        # Every kind must permit the summary-file contract.
        assert "file_write" in k.tool_allowlist, f"{name} kind cannot write its summary file"


def test_unknown_kind_resolves_none():
    assert kinds.resolve_kind("nonexistent") is None
    assert kinds.resolve_kind("") is None


def test_data_root_override_merges_over_builtin(tmp_path, monkeypatch):
    kdir = tmp_path / "worker_kinds"
    kdir.mkdir()
    (kdir / "research.json").write_text(json.dumps({"model": "some/custom-model"}))
    monkeypatch.setattr(kinds, "WORKER_KINDS_DIR", kdir)
    k = kinds.resolve_kind("research")
    assert k.model == "some/custom-model"
    # Un-overridden fields keep the built-in values.
    assert "search_web" in k.tool_allowlist


def test_data_root_custom_kind(tmp_path, monkeypatch):
    kdir = tmp_path / "worker_kinds"
    kdir.mkdir()
    (kdir / "scribe.json").write_text(
        json.dumps(
            {
                "description": "writes docs",
                "role_instructions": "You write documentation.",
                "tool_allowlist": ["file_read", "file_write"],
            }
        )
    )
    monkeypatch.setattr(kinds, "WORKER_KINDS_DIR", kdir)
    assert "scribe" in kinds.list_kind_names()
    k = kinds.resolve_kind("scribe")
    assert k.tool_allowlist == frozenset({"file_read", "file_write"})


def test_resolve_kind_model_background(monkeypatch):
    monkeypatch.setattr("config.settings.background_model", "tiny-model:1b")
    k = kinds.WorkerKind(name="x", description="", role_instructions="", model="background")
    assert kinds.resolve_kind_model(k) == "tiny-model:1b"


# ---------------------------------------------------------------------------
# spawn_worker integration
# ---------------------------------------------------------------------------


def test_spawn_unknown_kind_refused_before_session_created(mgr, loop):
    parent_id = _processing_parent(mgr)
    before = len(db.get_worker_sessions(parent_id))
    out = orch.spawn_worker("task", kind="bogus", _context={"session_id": parent_id, "_loop": loop})
    assert out.startswith("Error: Unknown worker kind")
    assert "research" in out  # lists valid kinds
    assert len(db.get_worker_sessions(parent_id)) == before


def test_spawn_with_kind_applies_bundle(mgr, loop):
    parent_id = _processing_parent(mgr)
    out = orch.spawn_worker(
        "map the codebase",
        title="Mapper",
        kind="explore",
        _context={"session_id": parent_id, "_loop": loop},
    )
    assert out.startswith("Worker spawned:")
    assert "[explore]" in out
    wid = out.split()[2]

    row = db.get_session(wid)
    assert row["worker_kind"] == "explore"
    assert "EXPLORE worker" in row["system_prompt"]
    assert "file:line" in row["system_prompt"]  # verification criteria rode along

    w = mgr.get(wid)
    assert w.tool_allowlist is not None
    assert "file_read" in w.tool_allowlist
    assert "bash" not in w.tool_allowlist  # explore is read-only
    # Drain the scheduled prompt task so the loop closes clean.
    loop.run_until_complete(asyncio.sleep(0))


def test_spawn_kind_model_persisted(mgr, loop, monkeypatch):
    parent_id = _processing_parent(mgr)

    # Model validation consults the live LLM registry; make it unavailable so
    # spawn takes its documented "could not validate, proceed" path.
    def _boom():
        raise RuntimeError("no llm in tests")

    monkeypatch.setattr("core.llm.client.get_llm_client", _boom)
    out = orch.spawn_worker(
        "t",
        kind="code",
        model="qwen3:test",
        _context={"session_id": parent_id, "_loop": loop},
    )
    wid = out.split()[2]
    row = db.get_session(wid)
    assert row["model_override"] == "qwen3:test"
    assert mgr.get(wid).model_override == "qwen3:test"
    loop.run_until_complete(asyncio.sleep(0))


def test_retry_worker_inherits_kind(mgr, loop, monkeypatch):
    parent_id = _processing_parent(mgr)
    out = orch.spawn_worker(
        "original task",
        title="R1",
        kind="research",
        _context={"session_id": parent_id, "_loop": loop},
    )
    wid = out.split()[2]
    loop.run_until_complete(asyncio.sleep(0))

    out2 = orch.retry_worker(wid, reason="stalled", _context={"session_id": parent_id, "_loop": loop})
    assert out2.startswith("Worker spawned:")
    assert "[research]" in out2
    wid2 = out2.split()[2]
    assert db.get_session(wid2)["worker_kind"] == "research"
    loop.run_until_complete(asyncio.sleep(0))


# ---------------------------------------------------------------------------
# Deterministic kind gate in get_worker_result
# ---------------------------------------------------------------------------


def test_kind_gate_research_uncited(mgr):
    parent_id = mgr.create_session(title="P")
    wid = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    db.update_session(wid, worker_kind="research")
    db.add_message(wid, "user", "go")
    db.add_message(wid, "assistant", "Everything is fine. Trust me on this one.")
    db.add_message(wid, "reflect", json.dumps({"verdict": "pass", "reasoning": "ok"}))
    out = orch.get_worker_result(wid)
    assert "KIND GATE (research)" in out


def test_kind_gate_research_cited_passes_clean(mgr):
    parent_id = mgr.create_session(title="P")
    wid = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    db.update_session(wid, worker_kind="research")
    db.add_message(wid, "user", "go")
    db.add_message(wid, "assistant", "Per https://example.com/spec the limit is 5.")
    db.add_message(wid, "reflect", json.dumps({"verdict": "pass", "reasoning": "ok"}))
    out = orch.get_worker_result(wid)
    assert "KIND GATE" not in out


def test_kind_gate_untyped_worker_unaffected(mgr):
    parent_id = mgr.create_session(title="P")
    wid = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    db.add_message(wid, "user", "go")
    db.add_message(wid, "assistant", "No citations here at all")
    db.add_message(wid, "reflect", json.dumps({"verdict": "pass", "reasoning": "ok"}))
    assert "KIND GATE" not in orch.get_worker_result(wid)


# ---------------------------------------------------------------------------
# Identity restore on rehydrate (Feature 5 seam)
# ---------------------------------------------------------------------------


def test_get_or_create_restores_worker_identity(mgr):
    parent_id = mgr.create_session(title="P")
    wid = mgr.create_session(title="W", session_type="worker", parent_session_id=parent_id)
    db.update_session(wid, worker_kind="explore", model_override="pinned:7b")
    # Simulate a reap: drop from memory, then rehydrate.
    mgr._sessions.pop(wid)
    w = mgr.get_or_create(wid)
    assert w.model_override == "pinned:7b"
    assert w.tool_allowlist is not None and "file_read" in w.tool_allowlist
    assert "bash" not in w.tool_allowlist


def test_get_or_create_normal_session_untouched(mgr):
    sid = mgr.create_session(title="N")
    db.update_session(sid, model_override="ghost:1b")  # column exists but must be ignored
    mgr._sessions.pop(sid)
    s = mgr.get_or_create(sid)
    assert s.model_override is None
    assert s.tool_allowlist is None
