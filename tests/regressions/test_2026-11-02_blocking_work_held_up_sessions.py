"""Work that froze every session, or held one open, on the live box.

Measured 2026-11-02:

- Files search walked 285,690 files on the event loop: 2.29 s per search with
  every session's stream frozen. Delete and upload had the same flaw.
- The embedding sweep scanned and stored rows on the loop, under the store's
  thread lock.
- Distill was awaited in FINALIZING: normal turns had a median of 5.3 s and a
  worst of 286 s, and a follow-up message waited for it.
- Three of 19 restarts logged no application shutdown: docker's 10 s stop
  timeout beat uvicorn's 15 s connection wait, so lifespan shutdown never ran.
- The sidebar polled 436 KB of uncompressed JSON every 10 s from each tab.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException, UploadFile

from api.routers import sessions as sessions_router
from api.routers import workspace as ws
from core.llm import embeddings as emb
from sessions import hooks


def _off_loop_recorder(fn, seen: list):
    """Wrap fn so each call records the thread it ran on."""

    def wrapper(*args, **kwargs):
        seen.append(threading.get_ident())
        return fn(*args, **kwargs)

    return wrapper


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    return tmp_path


async def test_search_runs_off_the_event_loop(workspace, monkeypatch):
    (workspace / "needle.txt").write_text("x")
    seen: list = []
    monkeypatch.setattr(ws, "_search_workspace", _off_loop_recorder(ws._search_workspace, seen))

    result = await ws.list_workspace(path="", q="needle")

    assert [e["name"] for e in result["entries"]] == ["needle.txt"]
    assert seen and seen[0] != threading.get_ident()


async def test_delete_runs_off_the_event_loop_and_keeps_its_errors(workspace, monkeypatch):
    target = workspace / "big"
    target.mkdir()
    (target / "a.txt").write_text("x")
    seen: list = []
    monkeypatch.setattr(ws, "_delete_entry", _off_loop_recorder(ws._delete_entry, seen))

    assert await ws.delete_workspace_entry("big") == {"deleted": True, "path": "big"}
    assert not target.exists()
    assert seen and seen[0] != threading.get_ident()

    with pytest.raises(HTTPException) as missing:
        await ws.delete_workspace_entry("big")
    assert missing.value.status_code == 404


async def test_upload_runs_off_the_event_loop(workspace, monkeypatch):
    seen: list = []
    monkeypatch.setattr(ws, "_write_upload", _off_loop_recorder(ws._write_upload, seen))

    result = await ws.upload_file(UploadFile(io.BytesIO(b"data"), filename="a.txt"), path="sub")

    assert result == {"filename": "a.txt", "path": "sub/a.txt", "size": 4}
    assert (workspace / "sub" / "a.txt").read_bytes() == b"data"
    assert seen and seen[0] != threading.get_ident()


async def test_overlapping_uploads_of_one_name_keep_every_file(workspace):
    """The name check used to be safe only because it never left the loop."""
    results = await asyncio.gather(
        *(ws.upload_file(UploadFile(io.BytesIO(b"%d" % i), filename="same.txt"), path="") for i in range(8))
    )

    assert len({r["filename"] for r in results}) == 8
    assert sorted(p.read_bytes() for p in workspace.glob("same*")) == sorted(b"%d" % i for i in range(8))


async def test_embedding_sweep_touches_the_store_off_the_loop(monkeypatch):
    threads: list = []

    class Store:
        def pending_embeddings(self, limit=256):
            threads.append(threading.get_ident())
            return [{"file_name": "f", "epoch": "1", "content": "c", "content_hash": "h"}]

        def store_embeddings(self, rows):
            threads.append(threading.get_ident())
            return len(rows)

    async def _embed_texts(texts):
        return [[0.1] for _ in texts]

    monkeypatch.setattr("config.settings.embedding_model", "m")
    monkeypatch.setattr(emb, "embed_texts", _embed_texts)

    assert await emb.embed_pending(Store()) == 1
    assert len(threads) == 2 and threading.get_ident() not in threads


class _Refs:
    def __init__(self):
        self.refs = 0
        self.peak = 0

    def add_background_ref(self):
        self.refs += 1
        self.peak = max(self.peak, self.refs)

    def remove_background_ref(self):
        self.refs -= 1


async def _drain_distill():
    while hooks._distill_running:
        await asyncio.sleep(0)


async def test_turn_end_does_not_wait_for_distill(monkeypatch):
    release = asyncio.Event()
    calls: list = []

    async def _slow(session_id, session):
        calls.append(session_id)
        await release.wait()

    monkeypatch.setattr(hooks, "_maybe_distill", _slow)
    monkeypatch.setattr("config.settings.reflect_enabled", False)
    monkeypatch.setattr("config.settings.gates_enabled", False)
    from db import models as db

    sid = db.create_session(title="Named")
    refs = _Refs()

    await asyncio.wait_for(hooks.run_post_task_hooks(sid, session_obj=refs), timeout=2)
    await asyncio.sleep(0)

    assert calls == [sid], "distill should have started"
    assert refs.refs == 1, "the session stays protected while distill runs"

    release.set()
    await _drain_distill()
    assert refs.refs == 0


async def test_one_distill_per_session_and_a_late_turn_gets_one_more_pass(monkeypatch):
    gates = [asyncio.Event(), asyncio.Event()]
    seen: list = []
    live = {"now": 0, "peak": 0}

    async def _step(session_id, session):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        seen.append(session["turn"])
        await gates[len(seen) - 1].wait()
        live["now"] -= 1

    monkeypatch.setattr(hooks, "_maybe_distill", _step)
    refs = _Refs()

    hooks._schedule_distill("s", {"turn": 1}, refs)
    await asyncio.sleep(0)
    hooks._schedule_distill("s", {"turn": 2}, refs)
    hooks._schedule_distill("s", {"turn": 3}, refs)  # the newest request wins

    gates[0].set()
    await asyncio.sleep(0.01)
    gates[1].set()
    await _drain_distill()

    assert seen == [1, 3]
    assert live["peak"] == 1
    assert refs.peak == 1 and refs.refs == 0


async def test_a_failed_distill_releases_the_session(monkeypatch):
    async def _boom(session_id, session):
        raise RuntimeError("model down")

    monkeypatch.setattr(hooks, "_maybe_distill", _boom)
    refs = _Refs()

    hooks._schedule_distill("s", {}, refs)
    await _drain_distill()

    assert refs.refs == 0 and "s" not in hooks._distill_running


def test_docker_waits_longer_than_a_clean_shutdown_takes():
    root = Path(__file__).resolve().parents[2]
    service = yaml.safe_load((root / "docker-compose.yml").read_text())["services"]["pernix"]
    server_wait = 15  # GRACEFUL_SHUTDOWN_S in run.py, both servers
    lifespan_budget = 16  # turn drain 5 + browser 3 + MCP 8, in api/app.py

    run_py = (root / "run.py").read_text()
    assert "GRACEFUL_SHUTDOWN_S = 15" in run_py
    assert "timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S" in run_py  # uvicorn
    assert "workers_kill_timeout = GRACEFUL_SHUTDOWN_S" in run_py  # granian
    assert int(service["stop_grace_period"].rstrip("s")) > server_wait + lifespan_budget


def test_session_list_is_gzipped_for_clients_that_take_it():
    payload = {"items": [{"title": "x" * 40} for _ in range(200)]}

    plain = sessions_router._session_list_response(payload, accepts_gzip=False)
    packed = sessions_router._session_list_response(payload, accepts_gzip=True)

    assert json.loads(plain.body) == payload
    assert "content-encoding" not in plain.headers
    assert packed.headers["content-encoding"] == "gzip"
    assert packed.headers["content-length"] == str(len(packed.body))
    assert packed.headers["vary"] == "Accept-Encoding"
    assert len(packed.body) < len(plain.body) / 4
    assert json.loads(gzip.decompress(packed.body)) == payload


def test_a_small_session_list_is_left_alone():
    response = sessions_router._session_list_response({"items": []}, accepts_gzip=True)

    assert "content-encoding" not in response.headers
    assert response.headers["vary"] == "Accept-Encoding"


async def test_the_route_honours_accept_encoding(monkeypatch):
    request = SimpleNamespace(headers={"accept-encoding": "gzip, br"})
    response = await sessions_router.list_sessions(request, limit=50)

    assert response.status_code == 200
    assert "items" in json.loads(response.body)
