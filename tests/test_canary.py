"""Pernix — Golden-task canary suite (adaptation plan 3.5).

Isolation is a predicate list, not a vibe: every enumerated exclusion gets
its own assertion here. The runner test exercises the real gate scorer
against a real temp workspace with a faked pipeline.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.canary.parser import CanaryParseError, parse_canary_md, scan_canaries
from db import models as db

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_VALID = """---
name: sample
prompt: do the thing
gates:
  - name: g1
    command: "true"
    watch_paths: [out.txt]
tags: a, b
timeout: 120
files:
  seed.txt: hello
---
body notes
"""


def _write_canary(base: Path, name: str, text: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "CANARY.md"
    p.write_text(text)
    return p


def test_parse_valid(tmp_path):
    p = _write_canary(tmp_path, "sample", _VALID)
    c = parse_canary_md(p)
    assert c.name == "sample"
    assert c.gates == [{"name": "g1", "command": "true", "watch_paths": ["out.txt"]}]
    assert c.tags == ["a", "b"]
    assert c.timeout == 120
    assert c.files == {"seed.txt": "hello"}
    assert c.body == "body notes"
    assert c.flaky is False


def test_parse_requires_gates(tmp_path):
    p = _write_canary(tmp_path, "nogates", "---\nname: nogates\nprompt: x\n---\n")
    with pytest.raises(CanaryParseError, match="gates"):
        parse_canary_md(p)


def test_parse_rejects_traversal_files(tmp_path):
    bad = "---\nname: esc\nprompt: x\ngates:\n  - {name: g, command: 'true'}\nfiles:\n  ../evil.txt: pwn\n---\n"
    p = _write_canary(tmp_path, "esc", bad)
    with pytest.raises(CanaryParseError, match="relative"):
        parse_canary_md(p)


def test_scan_skips_invalid(tmp_path):
    _write_canary(tmp_path, "good", _VALID.replace("name: sample", "name: good"))
    _write_canary(tmp_path, "broken", "not frontmatter at all")
    names = [c.name for c in scan_canaries(tmp_path)]
    assert names == ["good"]


def test_seed_suite_parses():
    defs = scan_canaries(Path("data/canaries"))
    assert len(defs) >= 6
    for d in defs:
        assert d.gates, d.name
        assert d.prompt, d.name


# ---------------------------------------------------------------------------
# Isolation predicates
# ---------------------------------------------------------------------------


def test_fts_excludes_canary_sessions():
    normal = db.create_session(title="n")
    canary = db.create_session(title="c", session_type="canary")
    db.add_message(normal, "user", "the zorbulon frobnicator broke")
    db.add_message(canary, "user", "the zorbulon frobnicator broke")
    hits = db.search_messages_fts("zorbulon frobnicator")
    sids = {h["session_id"] for h in hits}
    assert normal in sids
    assert canary not in sids


def test_sweep_queries_exclude_canary():
    canary = db.create_session(title="c", session_type="canary")
    for i in range(4):
        db.add_message(canary, "user" if i % 2 == 0 else "assistant", "x" * 200)
    db.add_message(canary, "reflect", "verdict")
    db.update_session(canary, state="idle")
    assert canary not in {s["id"] for s in db.get_unreviewed_sessions(min_age_minutes=0)}
    assert canary not in {s["id"] for s in db.get_unproposed_sessions(min_age_minutes=0)}
    assert canary not in {s["id"] for s in db.get_unrefined_sessions(min_idle_minutes=0)}


async def test_candor_early_returns_for_canary(monkeypatch):
    from sessions.hooks import _maybe_candor

    called = []
    monkeypatch.setattr(
        "core.extensions.candor.bridge.get_candor_bridge",
        lambda: called.append(1),
        raising=False,
    )
    # A canary session dict short-circuits before any candor import is used.
    await _maybe_candor("sid", {"session_type": "canary"}, session_obj=SimpleNamespace())
    assert not called


async def test_distill_skipped_for_canary(monkeypatch):
    from sessions import hooks

    monkeypatch.setattr("config.settings.memory_recall", True)
    seen = []
    monkeypatch.setattr(hooks.db, "get_messages", lambda sid: seen.append(sid) or [])
    await hooks._maybe_distill("sid", {"session_type": "canary"})
    assert not seen  # returned before even reading messages


def test_post_mortem_stamped_and_consumers_skip():
    from core.reflect import ReflectResult, _write_post_mortem
    from core.synthesis import attribute

    canary = db.create_session(title="c", session_type="canary")
    r = ReflectResult(verdict="retry", reasoning="x", failure_cause="wrong_approach", confidence=0.9)
    _write_post_mortem(canary, 1, r, None, {})
    pms = db.list_post_mortems(session_id=canary)
    assert pms
    payload = json.loads(pms[0]["payload_json"])
    assert payload["session_type"] == "canary"
    # Synthesis: no attributions from a canary row.
    assert attribute(pms[0]) == []


def test_normal_post_mortem_not_stamped():
    from core.reflect import ReflectResult, _write_post_mortem

    sid = db.create_session(title="n")
    _write_post_mortem(sid, 1, ReflectResult(verdict="pass", reasoning="ok"), None, {})
    payload = json.loads(db.list_post_mortems(session_id=sid)[0]["payload_json"])
    assert "session_type" not in payload


async def test_dream_evidence_skips_canary_pms():
    from core.dream.observe import build_pack

    canary = db.create_session(title="c", session_type="canary")
    normal = db.create_session(title="n")
    db.add_post_mortem(
        canary, 1, "retry", "wrong_approach", 0.9, "m", 1, None, None, json.dumps({"session_type": "canary"})
    )
    db.add_post_mortem(normal, 1, "retry", "wrong_approach", 0.9, "m", 1, None, None, json.dumps({}))
    pack = await build_pack(store=None)
    pm_items = [i for i in pack.items if i.kind == "pm"]
    referenced = {i.ref.get("session_id") for i in pm_items}
    assert normal in referenced
    assert canary not in referenced


# ---------------------------------------------------------------------------
# Tool gating: denied_session_types
# ---------------------------------------------------------------------------


async def test_memory_write_denied_in_canary(monkeypatch):
    from core.tools.executor import _execute_single
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(
        name="remember",
        func=lambda **kw: "saved",
        description="d",
        parameters={"type": "object", "properties": {}},
        denied_session_types={"canary"},
    )
    session = SimpleNamespace(session_type="canary", workspace_override=None)
    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: SimpleNamespace(get=lambda sid: session),
    )
    result = await _execute_single("remember", {}, {"session_id": "s"}, reg)
    assert result.was_error
    assert "canary sessions" in result.content


async def test_worker_denial_semantics_preserved(monkeypatch):
    from core.tools.executor import _execute_single
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(
        name="spawn_worker",
        func=lambda **kw: "spawned",
        description="d",
        parameters={"type": "object", "properties": {}},
        denied_session_types={"worker"},
    )
    worker = SimpleNamespace(session_type="worker", workspace_override=None)
    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: SimpleNamespace(get=lambda sid: worker),
    )
    result = await _execute_single("spawn_worker", {}, {"session_id": "w"}, reg)
    assert result.was_error and "worker sessions" in result.content


def test_real_memory_tools_deny_canary():
    from core.tools.builtin import memory_tools
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    memory_tools.register(reg)
    for name in ("remember", "ingest", "update_memory", "forget"):
        assert "canary" in reg.get(name).denied_session_types, name
    for name in ("recall", "deep_recall"):
        assert "canary" not in reg.get(name).denied_session_types, name


# ---------------------------------------------------------------------------
# Snooze transparency
# ---------------------------------------------------------------------------


def _fake_manager(sessions):
    return SimpleNamespace(_sessions={i: s for i, s in enumerate(sessions)})


def test_is_idle_ignores_canary_sessions(monkeypatch):
    import time as _time

    from core.snooze import SnoozeRunner

    busy_canary = SimpleNamespace(
        session_type="canary",
        has_background_tasks=True,
        last_activity_time=_time.time(),
        _state="PROCESSING",
    )
    from sessions import state_v2 as sv2

    monkeypatch.setattr(sv2, "_current_state", lambda s: getattr(sv2.SessionStateV2, s._state))
    monkeypatch.setattr("sessions.manager.get_manager", lambda: _fake_manager([busy_canary]))
    monkeypatch.setattr("db.models.list_cron_runs", lambda limit=5: [])
    assert SnoozeRunner()._is_idle() is True


def test_has_active_work_ignores_canary(monkeypatch):
    from sessions import state_v2 as sv2
    from sessions.manager import SessionManager

    busy_canary = SimpleNamespace(session_type="canary", has_background_tasks=True, _state="PROCESSING")
    monkeypatch.setattr(sv2, "_current_state", lambda s: getattr(sv2.SessionStateV2, s._state))
    mgr = SessionManager.__new__(SessionManager)
    mgr._sessions = {"c": busy_canary}
    assert mgr.has_active_work() is False
    busy_normal = SimpleNamespace(session_type="normal", has_background_tasks=False, _state="PROCESSING")
    mgr._sessions["n"] = busy_normal
    assert mgr.has_active_work() is True


async def test_canary_prompt_does_not_cancel_snooze(monkeypatch):
    """manager.prompt on a canary session must not fire request_cancel."""
    from sessions.manager import SessionManager

    cancels = []
    fake_snooze = SimpleNamespace(
        request_cancel=lambda: cancels.append("cancel"),
        notify_activity=lambda: cancels.append("activity"),
    )
    monkeypatch.setattr("core.snooze.get_snooze", lambda: fake_snooze)

    mgr = SessionManager.__new__(SessionManager)
    canary = SimpleNamespace(session_type="canary")
    mgr._sessions = {"c1": canary}
    # Stop after the snooze gate: get_or_create raises to end the call.
    mgr.get = lambda sid: mgr._sessions.get(sid)
    mgr.get_or_create = lambda sid: (_ for _ in ()).throw(RuntimeError("stop"))
    with pytest.raises(RuntimeError):
        await SessionManager.prompt(mgr, "c1", "run the canary")
    assert cancels == []

    normal = SimpleNamespace(session_type="normal")
    mgr._sessions["n1"] = normal
    with pytest.raises(RuntimeError):
        await SessionManager.prompt(mgr, "n1", "hi")
    assert cancels == ["cancel", "activity"]


# ---------------------------------------------------------------------------
# canary_runs rows + retention
# ---------------------------------------------------------------------------


def test_canary_run_rows_and_filters():
    rid = db.add_canary_run("t1", "scheduled", "sid1", "[]", True, retries=1, tokens=500, duration_s=12.5)
    db.add_canary_run("t2", "post_batch", "sid2", "[]", False, batch_id="batch-9")
    assert rid > 0
    assert len(db.list_canary_runs()) == 2
    assert [r["task"] for r in db.list_canary_runs(task="t1")] == ["t1"]
    batch = db.list_canary_runs(batch_id="batch-9")
    assert len(batch) == 1 and batch[0]["passed"] == 0


def test_prune_canary_runs():
    db.add_canary_run("old", "scheduled", None, "[]", True)
    import sqlite3

    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute("UPDATE canary_runs SET created_at = '2020-01-01T00:00:00+00:00'")
    db.add_canary_run("new", "scheduled", None, "[]", True)
    assert db.prune_canary_runs(30) == 1
    assert [r["task"] for r in db.list_canary_runs()] == ["new"]


# ---------------------------------------------------------------------------
# Runner (real gates, faked pipeline)
# ---------------------------------------------------------------------------


class _FakeState:
    IDLE = "idle"


def _runner_manager(monkeypatch, solve):
    """Fake manager whose prompt() 'solves' the task in the temp workspace."""
    from sessions import state_v2 as sv2

    sessions = {}

    def create_session(title="", session_type="normal", **kw):
        sid = db.create_session(title=title, session_type=session_type)
        sessions[sid] = SimpleNamespace(
            session_id=sid,
            session_type=session_type,
            workspace_override=None,
            model_override=None,
            reflect_count=1,
            cancel_requested=False,
            _parked=False,
        )
        return sid

    async def prompt(sid, message):
        s = sessions[sid]
        solve(Path(s.workspace_override))
        s._parked = True

    mgr = SimpleNamespace(create_session=create_session, get=lambda sid: sessions.get(sid), prompt=prompt)
    monkeypatch.setattr("sessions.manager.get_manager", lambda: mgr)
    monkeypatch.setattr(
        sv2,
        "_current_state",
        lambda s: sv2.SessionStateV2.IDLE_READY if getattr(s, "_parked", False) else sv2.SessionStateV2.PROCESSING,
    )
    return mgr


async def test_run_canary_end_to_end_pass(monkeypatch, tmp_path):
    from core.canary.parser import CanaryDef
    from core.canary.runner import run_canary

    _runner_manager(monkeypatch, lambda ws: (ws / "hello.txt").write_text("hi\n"))
    c = CanaryDef(
        name="mini",
        prompt="write hello.txt",
        gates=[{"name": "exists", "command": "grep -qx hi hello.txt", "watch_paths": []}],
        timeout=60,
        files={"seed.txt": "s"},
    )
    result = await run_canary(c, trigger="manual")
    assert result.passed is True
    assert result.retries == 1
    assert result.gate_results and result.gate_results[0]["passed"]
    rows = db.list_canary_runs(task="mini")
    assert len(rows) == 1 and rows[0]["passed"] == 1 and rows[0]["trigger"] == "manual"
    # Per-run gates were cleaned up.
    assert db.get_gates(result.session_id) == []


async def test_run_canary_fail_records_gate_detail(monkeypatch):
    from core.canary.parser import CanaryDef
    from core.canary.runner import run_canary

    _runner_manager(monkeypatch, lambda ws: None)  # agent does nothing
    c = CanaryDef(
        name="mini-fail",
        prompt="write hello.txt",
        gates=[{"name": "exists", "command": "test -f hello.txt", "watch_paths": []}],
        timeout=60,
    )
    result = await run_canary(c, trigger="scheduled", batch_id="b-1")
    assert result.passed is False
    rows = db.list_canary_runs(task="mini-fail")
    assert rows[0]["batch_id"] == "b-1"
    gates = json.loads(rows[0]["gate_results_json"])
    assert gates and gates[0]["passed"] is False


async def test_run_canary_missing_name():
    from core.canary.runner import run_canary

    result = await run_canary("no-such-canary", trigger="manual")
    assert result.passed is False and "not found" in result.error


async def test_run_sweep_respects_enabled_flag(monkeypatch):
    from core.canary.runner import run_sweep

    monkeypatch.setattr("config.settings.canary_enabled", False)
    assert await run_sweep() == []


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def test_ensure_canary_schedule(monkeypatch):
    import core.extensions.scheduling as sched

    jobs = {}

    class _S:
        def add_job(self, func, trigger=None, id=None, kwargs=None, **opts):
            jobs[id] = SimpleNamespace(func=func, trigger=trigger, kwargs=kwargs)

    monkeypatch.setattr(sched, "_scheduler", _S())
    monkeypatch.setattr("config.settings.canary_enabled", True)
    sched.ensure_canary_schedule()
    assert "_canary_sweep" in jobs
    assert jobs["_canary_sweep"].kwargs["meta"]["transient"] is True

    jobs.clear()
    monkeypatch.setattr("config.settings.canary_enabled", False)
    sched.ensure_canary_schedule()
    assert jobs == {}


async def test_post_batch_defers_while_active(monkeypatch):
    import core.extensions.scheduling as sched

    jobs = {}

    class _S:
        def add_job(self, func, trigger=None, id=None, kwargs=None, **opts):
            jobs[id] = SimpleNamespace(func=func, trigger=trigger, kwargs=kwargs)

    monkeypatch.setattr(sched, "_scheduler", _S())
    monkeypatch.setattr("config.settings.canary_enabled", True)
    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: SimpleNamespace(has_active_work=lambda: True),
    )

    ran = []

    async def fake_sweep(meta):
        ran.append(meta)

    monkeypatch.setattr(sched, "_execute_canary_sweep_job", fake_sweep)
    await sched._execute_canary_batch_job({"batch_id": "b-7", "attempts": 0})
    assert not ran  # deferred
    assert jobs["_canary_batch_b-7"].kwargs["meta"]["attempts"] == 1

    # Idle now -> runs with post_batch trigger.
    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: SimpleNamespace(has_active_work=lambda: False),
    )
    await sched._execute_canary_batch_job({"batch_id": "b-7", "attempts": 1})
    assert ran and ran[0]["trigger"] == "post_batch"


def test_enqueue_helpers(monkeypatch):
    import core.extensions.scheduling as sched

    jobs = {}

    class _S:
        def add_job(self, func, trigger=None, id=None, kwargs=None, **opts):
            jobs[id] = SimpleNamespace(func=func, trigger=trigger, kwargs=kwargs)

    monkeypatch.setattr(sched, "_scheduler", _S())
    monkeypatch.setattr("config.settings.canary_enabled", True)
    assert sched.enqueue_post_batch_sweep("batch-1")
    assert "_canary_batch_batch-1" in jobs
    assert sched.enqueue_manual_canary("file-create")
    assert jobs["_canary_manual_file-create"].kwargs["meta"]["names"] == ["file-create"]

    monkeypatch.setattr("config.settings.canary_enabled", False)
    assert sched.enqueue_post_batch_sweep("batch-2") is False


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def test_canary_tools(monkeypatch):
    from core.tools.builtin.canary_tools import canary_run, canary_status

    queued = []
    monkeypatch.setattr(
        "core.extensions.scheduling.enqueue_manual_canary",
        lambda name: queued.append(name) or True,
    )
    assert "Error" in canary_run("definitely-not-a-canary")
    out = canary_run("file-create")
    assert "queued" in out and queued == ["file-create"]

    db.add_canary_run("file-create", "manual", None, json.dumps([{"name": "g", "passed": True}]), True)
    status = canary_status()
    assert "file-create" in status and "PASS" in status


def test_canary_tools_registration_gated(monkeypatch):
    from core.tools.builtin import canary_tools
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    monkeypatch.setattr("config.settings.canary_enabled", False)
    canary_tools.register(reg)
    assert reg.get("canary_run") is None

    monkeypatch.setattr("config.settings.canary_enabled", True)
    canary_tools.register(reg)
    assert reg.get("canary_run") is not None
    assert reg.get("canary_run").denied_session_types == {"canary", "worker"}
