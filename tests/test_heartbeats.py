"""Pernix — Heartbeats (adaptation plan 3c).

Cron spawns turns; a heartbeat steers one. Steer = a role=system row picked
up at the next round boundary; parked sessions degrade to follow_up; ticks
coalesce; the agent's namespace can never see the user's heartbeat.
"""

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

import core.extensions.scheduling as sched
from db import models as db


@pytest.fixture(autouse=True)
def _hb_env(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.heartbeats_enabled", True)
    monkeypatch.setattr(sched, "CRON_PATH", tmp_path / "cron_jobs.json")
    monkeypatch.setattr(sched, "_save_jobs", lambda: None)
    sched._heartbeat_last_turn.clear()


class _FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, func, trigger=None, id=None, kwargs=None, **_opts):
        self.jobs[id] = SimpleNamespace(id=id, func=func, trigger=trigger, kwargs=kwargs or {})

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def get_jobs(self):
        return list(self.jobs.values())

    def remove_job(self, job_id):
        del self.jobs[job_id]

    def pause_job(self, job_id):
        pass


@pytest.fixture
def fake_sched(monkeypatch):
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    return fake


# ---------------------------------------------------------------------------
# Parsing + namespace separation
# ---------------------------------------------------------------------------


def test_parse_every():
    assert sched._parse_every("30s") == ("interval", 30)
    assert sched._parse_every("5m") == ("interval", 300)
    assert sched._parse_every("2h") == ("interval", 7200)
    assert sched._parse_every("10s") == ("interval", 30)  # floor
    assert sched._parse_every("0 9 * * 1-5") == ("cron", "0 9 * * 1-5")


def test_agent_cannot_see_or_clear_user_heartbeat(fake_sched):
    sid = "sess-ns-separation"
    sched.set_user_heartbeat(sid, "user says: stay on target", every="5m")
    assert sched.get_user_heartbeat(sid)["instruction"].startswith("user says")

    # Agent namespace: list shows nothing, clear can't touch it.
    ctx = {"session_id": sid}
    assert "No agent heartbeats" in sched.list_heartbeats(_context=ctx)
    assert "Error" in sched.clear_heartbeat("user", _context=ctx)
    assert sched.get_user_heartbeat(sid) is not None  # untouched

    # Agent's own works and is invisible to the user accessor.
    out = sched.set_heartbeat("progress", "post a status summary", every="10m", _context=ctx)
    assert "Heartbeat 'progress' set" in out
    assert "progress" in sched.list_heartbeats(_context=ctx)
    assert sched.get_user_heartbeat(sid)["instruction"].startswith("user says")
    assert "cleared" in sched.clear_heartbeat("progress", _context=ctx)


def test_bad_every_rejected(fake_sched):
    out = sched.set_heartbeat("x", "instr", every="whenever", _context={"session_id": "s"})
    assert out.startswith("Error:")


# ---------------------------------------------------------------------------
# Delivery paths
# ---------------------------------------------------------------------------


def _meta(sid, name="pulse", delivery="steer"):
    return {
        "name": f"hb_agent_{sid[:12]}_{name}",
        "kind": "heartbeat",
        "owner": "agent",
        "hb_name": name,
        "heartbeat_session_id": sid,
        "instruction": "check progress and report",
        "every": "5m",
        "delivery": delivery,
    }


def _fake_session(state_name, turn=7):
    return SimpleNamespace(current_turn_user_msg_id=turn, pending_messages=deque(), _state=state_name)


def _patch_state(monkeypatch, manager_session):
    from sessions import state_v2 as sv2

    monkeypatch.setattr(sv2, "_current_state", lambda s: getattr(sv2.SessionStateV2, s._state))
    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: SimpleNamespace(get=lambda sid: manager_session, prompt=None),
    )


async def test_steer_writes_system_row_and_coalesces(monkeypatch):
    sid = db.create_session(title="hb-steer")
    session = _fake_session("PROCESSING")
    _patch_state(monkeypatch, session)

    await sched._execute_heartbeat_job(_meta(sid))
    msgs = db.get_messages(sid)
    hb_rows = [m for m in msgs if m["role"] == "system" and "[heartbeat:pulse]" in m["content"]]
    assert len(hb_rows) == 1

    # Second tick in the same turn coalesces — no second row.
    await sched._execute_heartbeat_job(_meta(sid))
    msgs = db.get_messages(sid)
    assert len([m for m in msgs if "[heartbeat:pulse]" in m.get("content", "")]) == 1

    # New turn -> steers again.
    session.current_turn_user_msg_id = 8
    await sched._execute_heartbeat_job(_meta(sid))
    msgs = db.get_messages(sid)
    assert len([m for m in msgs if "[heartbeat:pulse]" in m.get("content", "")]) == 2


async def test_parked_session_degrades_to_follow_up(monkeypatch):
    sid = db.create_session(title="hb-parked")
    session = _fake_session("AWAITING_WORKERS")
    _patch_state(monkeypatch, session)

    await sched._execute_heartbeat_job(_meta(sid, delivery="steer"))
    assert len(session.pending_messages) == 1
    assert "[heartbeat:pulse]" in session.pending_messages[0].message
    # No steer row was written.
    assert not [m for m in db.get_messages(sid) if "[heartbeat" in m.get("content", "")]

    # Undelivered copy still queued -> coalesce.
    await sched._execute_heartbeat_job(_meta(sid))
    assert len(session.pending_messages) == 1


async def test_idle_session_gets_prompt(monkeypatch):
    sid = db.create_session(title="hb-idle")
    prompts = []

    async def _prompt(s, text):
        prompts.append((s, text))

    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: SimpleNamespace(get=lambda _sid: None, prompt=_prompt),
    )
    await sched._execute_heartbeat_job(_meta(sid))
    assert prompts and "[heartbeat:pulse]" in prompts[0][1]
    # Claim-before-deliver discipline: a completed cron_run row exists.
    runs = db.list_cron_runs(f"hb_agent_{sid[:12]}_pulse")
    assert runs and runs[0]["status"] == "completed"


# ---------------------------------------------------------------------------
# Restart round-trip
# ---------------------------------------------------------------------------


def test_load_jobs_routes_heartbeats(monkeypatch, tmp_path, fake_sched):
    import json as _json

    restored = []
    monkeypatch.setattr(sched, "_add_heartbeat_job_internal", lambda job_id, meta: restored.append((job_id, meta)))
    entries = [
        {
            "name": "hb_user_abc123def456",
            "cron_expr": "",
            "prompt": "",
            "kind": "heartbeat",
            "owner": "user",
            "hb_name": "user",
            "heartbeat_session_id": "abc123def456xyz",
            "instruction": "stay focused",
            "every": "5m",
            "delivery": "steer",
            "paused": False,
        },
        {"name": "normal-job", "cron_expr": "0 9 * * *", "prompt": "daily", "paused": False},
    ]
    (tmp_path / "cron_jobs.json").write_text(_json.dumps(entries))
    monkeypatch.setattr(sched, "CRON_PATH", tmp_path / "cron_jobs.json")

    captured_normal = []
    monkeypatch.setattr(
        sched,
        "_add_job_internal",
        lambda name, expr, prompt, session_id=None, model="", extra_meta=None: captured_normal.append(name),
    )
    monkeypatch.setattr(sched, "_schedule_coalesced_catchup", lambda entries: None)
    sched._load_jobs()

    assert restored and restored[0][0] == "hb_user_abc123def456"
    assert restored[0][1]["instruction"] == "stay focused"
    assert captured_normal == ["normal-job"]  # heartbeat never hit CronTrigger path


@pytest.mark.asyncio
async def test_maintenance_protects_heartbeat_sessions(monkeypatch, tmp_path):
    """A heartbeat's host session must survive the idle reaper. Heartbeat jobs
    park session_id=None and carry the real id under heartbeat_session_id, so
    reading only session_id leaves them unprotected."""
    import json as _json

    import maintenance

    data = tmp_path / "data"
    data.mkdir()
    (data / "cron_jobs.json").write_text(
        _json.dumps(
            [
                {
                    "name": "hb_user_abc",
                    "kind": "heartbeat",
                    "session_id": None,
                    "heartbeat_session_id": "hb-host-session",
                },
                {"name": "normal-job", "cron_expr": "0 9 * * *", "session_id": "cron-session"},
            ]
        )
    )
    monkeypatch.chdir(tmp_path)

    seen: dict = {}

    class _FakeManager:
        def reap_dead_subscribers(self):
            return 0

        def reap_idle_sessions(self, max_idle=1800, protected_ids=None):
            seen["protected"] = set(protected_ids or ())
            return 0

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _FakeManager())

    runner = maintenance.MaintenanceRunner()
    runner._tick_count = 5
    await runner._tick()

    assert seen["protected"] == {"cron-session", "hb-host-session"}
