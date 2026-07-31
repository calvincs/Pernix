"""Tests for RLM run visibility: migration v20, live progress plumbing,
the trace/by-session endpoints, view-session lifecycle, and the shared
read-only session policy."""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config import settings
from db import models as db
from db.database import connect_sessions

# =============================================================================
# migration v20 + model helpers
# =============================================================================


def test_v20_ui_session_id_schema():
    with connect_sessions() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(rlm_runs)").fetchall()}
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(rlm_runs)").fetchall()}
    assert "ui_session_id" in cols
    assert "idx_rlm_runs_ui_session" in idx


def _seed_run(run_id="ab12cd34", session_id="sess-1", ui_session_id=None, parent_run_id=None):
    db.create_rlm_run(
        run_id=run_id,
        session_id=session_id,
        task="summarize the corpus",
        source_desc="big.txt",
        root_model="root-m",
        sub_model="sub-m",
        input_chars=1000,
        run_dir=f"rlm/{run_id}",
        parent_run_id=parent_run_id,
        depth=1 if parent_run_id else 0,
        ui_session_id=ui_session_id,
    )


def test_get_rlm_run_and_by_ui_session():
    _seed_run(ui_session_id="view-1")
    assert db.get_rlm_run("ab12cd34")["ui_session_id"] == "view-1"
    assert db.get_rlm_run("missing") is None
    assert db.get_rlm_run_by_ui_session("view-1")["run_id"] == "ab12cd34"
    assert db.get_rlm_run_by_ui_session("nope") is None


def test_update_rlm_run_progress_only_while_running():
    _seed_run()
    db.update_rlm_run_progress("ab12cd34", iterations=3, subcalls=2)
    row = db.get_rlm_run("ab12cd34")
    assert (row["iterations"], row["subcalls"]) == (3, 2)

    db.finish_rlm_run("ab12cd34", status="completed", iterations=5, subcalls=4, answer_preview="done")
    # A straggler progress write from a broker thread must not clobber terminal counters.
    db.update_rlm_run_progress("ab12cd34", iterations=99, subcalls=99)
    row = db.get_rlm_run("ab12cd34")
    assert (row["iterations"], row["subcalls"]) == (5, 4)


def test_list_rlm_run_children():
    _seed_run("aaaa0000")
    _seed_run("bbbb1111", parent_run_id="aaaa0000")
    _seed_run("cccc2222", parent_run_id="aaaa0000")
    kids = db.list_rlm_run_children("aaaa0000")
    assert [k["run_id"] for k in kids] == ["bbbb1111", "cccc2222"]
    assert db.list_rlm_run_children("bbbb1111") == []


def test_fail_orphaned_rlm_runs_parks_view_sessions():
    sid = db.create_session(title="RLM: x", session_type="rlm")
    db.set_session_state(sid, "processing")
    _seed_run(ui_session_id=sid)
    assert db.fail_orphaned_rlm_runs() == 1
    assert db.get_rlm_run("ab12cd34")["status"] == "orphaned"
    assert db.get_session(sid)["state"] == "idle"


# =============================================================================
# engine progress seam + heartbeat
# =============================================================================


def _bare_engine(tmp_path, progress_fn=None):
    from core.extensions.rlm.child_env import StagedContext
    from core.extensions.rlm.engine import RLMEngine
    from core.extensions.rlm.types import RLMCaps

    return RLMEngine(
        run_dir=tmp_path / "run",
        task="t",
        staged=StagedContext(),
        root_chat=lambda m, t: "",
        sub_chat=lambda p, m, t: "",
        caps=RLMCaps(),
        progress_fn=progress_fn,
    )


def test_trace_forwards_to_progress_fn(tmp_path):
    seen = []
    eng = _bare_engine(tmp_path, progress_fn=seen.append)
    eng._trace({"type": "root", "iteration": 0, "response_preview": "hi"})
    assert len(seen) == 1
    assert seen[0]["type"] == "root"
    assert "ts" in seen[0] and seen[0]["depth"] == 0


def test_progress_fn_errors_never_propagate(tmp_path):
    def boom(event):
        raise RuntimeError("progress consumer bug")

    eng = _bare_engine(tmp_path, progress_fn=boom)
    eng._trace({"type": "cell", "iteration": 0})  # must not raise


def test_heartbeat_emits_and_stops(tmp_path, monkeypatch):
    from core.extensions.rlm import engine as engine_mod

    monkeypatch.setattr(engine_mod, "HEARTBEAT_INTERVAL_SECONDS", 0.02)
    beats = []
    got_one = threading.Event()

    def on_progress(event):
        if event.get("type") == "heartbeat":
            beats.append(event)
            got_one.set()

    class FakeBroker:
        def in_flight(self):
            return 2

        def last_activity(self):
            return time.monotonic()

    eng = _bare_engine(tmp_path, progress_fn=on_progress)
    eng._start_heartbeat(FakeBroker(), start=time.monotonic())
    assert got_one.wait(timeout=2.0), "no heartbeat within 2s"
    eng._heartbeat_stop.set()
    beat = beats[0]
    assert beat["in_flight"] == 2
    assert beat["iteration"] == 0
    assert "elapsed" in beat and "quiet_seconds" in beat


def test_heartbeat_absent_without_progress_fn(tmp_path):
    eng = _bare_engine(tmp_path, progress_fn=None)
    eng._start_heartbeat(broker=None, start=time.monotonic())
    assert eng._heartbeat_stop is None


# =============================================================================
# tool glue: progress fan-out + view-session finalization
# =============================================================================


def test_activity_detail_shapes():
    from core.extensions.rlm import _activity_detail

    assert _activity_detail("root", {"response_preview": "Plan:\nmore"}) == "Plan:"
    cell = _activity_detail("cell", {"code": "print(1)\nprint(2)", "duration": 1.5})
    assert cell.startswith("print(1)") and "1.5s" in cell
    assert "final answer" in _activity_detail("cell", {"code": "x", "final": True})
    sub = _activity_detail("subcall", {"model": "m1", "ok": True, "duration": 2})
    assert "m1" in sub and "ok" in sub
    assert "budget" in _activity_detail("notice", {"notice": "budget_exhausted"})
    assert _activity_detail("mystery", {}) is None


def test_make_progress_fn_counters_and_events(monkeypatch):
    import core.extensions.rlm as rlm_mod

    emitted = []
    monkeypatch.setattr(rlm_mod, "_emit_session_event", lambda sid, ev: emitted.append((sid, ev)))
    _seed_run(ui_session_id="view-1")

    fn = rlm_mod._make_progress_fn("ab12cd34", "view-1", "sess-1")
    fn({"type": "root", "iteration": 0, "response_preview": "thinking"})
    fn({"type": "subcall", "model": "m", "ok": True, "duration": 1.0})
    fn({"type": "heartbeat", "iteration": 1, "subcalls": 1, "in_flight": 1, "quiet_seconds": 0, "elapsed": 5})
    fn({"type": "end", "status": "completed"})

    row = db.get_rlm_run("ab12cd34")
    assert (row["iterations"], row["subcalls"]) == (1, 1)

    types = [ev["type"] for _, ev in emitted]
    assert types == ["rlm.activity", "rlm.activity", "rlm.heartbeat"]  # end is not forwarded
    root_ev = emitted[0][1]
    assert root_ev["run_id"] == "ab12cd34"
    assert root_ev["ui_session_id"] == "view-1"
    assert root_ev["kind"] == "root"
    assert root_ev["iterations"] == 1


def test_finalize_run_ui(monkeypatch):
    import core.extensions.rlm as rlm_mod
    from core.extensions.rlm.types import RLMRunResult

    emitted = []
    monkeypatch.setattr(rlm_mod, "_emit_session_event", lambda sid, ev: emitted.append((sid, ev)))
    view_sid = db.create_session(title="RLM: x", session_type="rlm", parent_session_id="parent-1")
    db.set_session_state(view_sid, "processing")

    result = RLMRunResult(answer="a", status="completed", iterations=4, subcalls=2, duration=12.3)
    rlm_mod._finalize_run_ui("parent-1", view_sid, "ab12cd34", result)

    row = db.get_session(view_sid)
    assert row["state"] == "idle"
    assert "completed" in (row["subtitle"] or "")
    assert emitted and emitted[0][1]["type"] == "rlm.done"
    assert emitted[0][1]["status"] == "completed"


def test_record_start_manifest_carries_caps_and_ui_session(tmp_path):
    from core.extensions.rlm import runs
    from core.extensions.rlm.types import RLMCaps

    run_dir = tmp_path / "rlm" / "ff00ff00"
    run_dir.mkdir(parents=True)
    runs.record_start(
        "ff00ff00",
        run_dir,
        "rlm/ff00ff00",
        session_id="sess-1",
        task="t",
        source_desc="s",
        root_model="rm",
        sub_model="sm",
        input_chars=10,
        ui_session_id="view-9",
        caps=RLMCaps(max_iterations=7, max_subcalls=11, timeout_seconds=120.0),
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["ui_session_id"] == "view-9"
    assert manifest["caps"] == {"max_iterations": 7, "max_subcalls": 11, "timeout_seconds": 120.0}
    assert db.get_rlm_run("ff00ff00")["ui_session_id"] == "view-9"


# =============================================================================
# API: trace + by-session + detail
# =============================================================================


def _make_app(*routers):
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    return app


def _make_run_dir(run_id="ab12cd34", trace_lines=None, answer=None):
    run_dir = Path(settings.workspace_dir) / "rlm" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if trace_lines is not None:
        run_dir.joinpath("trace.jsonl").write_bytes(b"".join(trace_lines))
    if answer is not None:
        run_dir.joinpath("answer.txt").write_text(answer)
    return run_dir


async def test_trace_endpoint_pages_by_offset():
    from api.routers import rlm as rlm_router

    ev1 = json.dumps({"ts": 1.0, "depth": 0, "type": "root", "iteration": 0}).encode() + b"\n"
    ev2 = json.dumps({"ts": 2.0, "depth": 0, "type": "cell", "iteration": 0}).encode() + b"\n"
    partial = b'{"ts": 3.0, "type": "sub'  # mid-append, no newline yet
    _seed_run()
    _make_run_dir(trace_lines=[ev1, ev2, partial])

    app = _make_app(rlm_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/rlm/runs/ab12cd34/trace")
        assert resp.status_code == 200
        page = resp.json()
        assert [e["type"] for e in page["events"]] == ["root", "cell"]
        assert page["next_offset"] == len(ev1) + len(ev2)  # partial line not consumed
        assert page["running"] is True

        # Nothing new at the same offset.
        resp = await client.get(f"/api/rlm/runs/ab12cd34/trace?after={page['next_offset']}")
        assert resp.json()["events"] == []

        # Complete the partial line — the next poll picks it up.
        run_dir = Path(settings.workspace_dir) / "rlm" / "ab12cd34"
        with open(run_dir / "trace.jsonl", "ab") as fh:
            fh.write(b'call", "ok": true}\n')
        resp = await client.get(f"/api/rlm/runs/ab12cd34/trace?after={page['next_offset']}")
        events = resp.json()["events"]
        assert [e["type"] for e in events] == ["subcall"]


async def test_trace_endpoint_404_unknown_run():
    from api.routers import rlm as rlm_router

    app = _make_app(rlm_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/rlm/runs/deadbeef/trace")
    assert resp.status_code == 404


async def test_trace_endpoint_missing_file_is_empty_page():
    from api.routers import rlm as rlm_router

    _seed_run()  # no run dir on disk at all
    app = _make_app(rlm_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/rlm/runs/ab12cd34/trace")
    page = resp.json()
    assert page["events"] == [] and page["next_offset"] == 0


async def test_by_session_and_detail_with_answer_and_children():
    from api.routers import rlm as rlm_router

    _seed_run(ui_session_id="view-1")
    _seed_run("bbbb1111", parent_run_id="ab12cd34")
    _make_run_dir(answer="# The Answer\nbody")
    db.finish_rlm_run("ab12cd34", status="completed", iterations=3, subcalls=1, answer_preview="x")

    app = _make_app(rlm_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/rlm/runs/by-session/view-1")
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["run_id"] == "ab12cd34"
        assert detail["answer"].startswith("# The Answer")
        assert [c["run_id"] for c in detail["children"]] == ["bbbb1111"]

        assert (await client.get("/api/rlm/runs/by-session/nope")).status_code == 404

        # Direct detail no longer depends on the run being in the newest 1000.
        resp = await client.get("/api/rlm/runs/ab12cd34")
        assert resp.status_code == 200
        assert resp.json()["answer_path"] == "rlm/ab12cd34/answer.txt"


async def test_running_run_does_not_inline_answer():
    from api.routers import rlm as rlm_router

    _seed_run()
    _make_run_dir(answer="not yet")  # answer.txt exists but the run is still running
    app = _make_app(rlm_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = (await client.get("/api/rlm/runs/ab12cd34")).json()
    assert detail["answer"] is None


# =============================================================================
# read-only policy
# =============================================================================


def test_read_only_reason_covers_special_types():
    from sessions.policy import annotate_read_only, read_only_reason

    assert read_only_reason({"session_type": "snooze"})
    assert read_only_reason({"session_type": "rlm"})
    assert read_only_reason({"session_type": "normal"}) is None
    assert read_only_reason(None) is None

    row = annotate_read_only({"session_type": "rlm"})
    assert row["read_only"] is True and "RLM" in row["read_only_reason"]
    row = annotate_read_only({"session_type": "worker"})
    assert row["read_only"] is False and row["read_only_reason"] is None


@pytest.mark.parametrize("session_type", ["snooze", "rlm"])
async def test_chat_rejects_read_only_sessions(session_type):
    from api.routers import chat as chat_router

    sid = db.create_session(title="ro", session_type=session_type)
    app = _make_app(chat_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/chat", json={"session_id": sid, "message": "hello"})
        assert resp.status_code == 400
        assert "read-only" in resp.json()["detail"]
        resp = await client.post("/api/chat/inject", json={"session_id": sid, "message": "hello"})
        assert resp.status_code == 400


async def test_session_payloads_carry_read_only():
    from api.routers import sessions as sessions_router

    sid = db.create_session(title="RLM: x", session_type="rlm")
    app = _make_app(sessions_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        items = (await client.get("/api/sessions")).json()["items"]
        row = next(s for s in items if s["id"] == sid)
        assert row["read_only"] is True

        detail = (await client.get(f"/api/sessions/{sid}")).json()
        assert detail["read_only"] is True and detail["read_only_reason"]


# =============================================================================
# delete symmetry
# =============================================================================


def test_purge_rlm_artifacts_on_view_session_delete():
    from sessions.manager import SessionManager

    view_sid = db.create_session(title="RLM: x", session_type="rlm", parent_session_id="p1")
    _seed_run(ui_session_id=view_sid)
    db.finish_rlm_run("ab12cd34", status="completed", iterations=1, subcalls=0, answer_preview="")
    run_dir = _make_run_dir(trace_lines=[b"{}\n"])

    SessionManager._purge_rlm_artifacts(None, view_sid)
    assert db.get_rlm_run("ab12cd34") is None
    assert not run_dir.exists()


def test_purge_rlm_artifacts_skips_running_and_cascades_from_parent():
    from sessions.manager import SessionManager

    parent = db.create_session(title="chat")
    running_view = db.create_session(title="RLM: live", session_type="rlm", parent_session_id=parent)
    done_view = db.create_session(title="RLM: done", session_type="rlm", parent_session_id=parent)
    _seed_run("11110000", ui_session_id=running_view)  # still running
    _seed_run("22220000", ui_session_id=done_view)
    db.finish_rlm_run("22220000", status="completed", iterations=1, subcalls=0, answer_preview="")
    live_dir = _make_run_dir("11110000", trace_lines=[b"{}\n"])
    done_dir = _make_run_dir("22220000", trace_lines=[b"{}\n"])

    SessionManager._purge_rlm_artifacts(None, parent)
    assert db.get_rlm_run("11110000") is not None and live_dir.exists()  # running: untouched
    assert db.get_rlm_run("22220000") is None and not done_dir.exists()
