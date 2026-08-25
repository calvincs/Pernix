"""An RLM view session rode the workers endpoint and drew as a teal worker chip
(field case: worker 86959c5f0c97's run, reported by Calvin 2026-08-25).

get_worker_sessions returns ALL children of a session on purpose (the manager
uses it to find rlm children too), but /api/sessions/{id}/workers serialized
every row as a worker. The UI strip then rendered the RLM view session as a
teal worker chip named "RLM: ..." — mismatching the pink RLM legend key — and
never retired it, because a finished view session parks at state 'idle', not
'idle_ready'. The router now keeps only session_type='worker' children; RLM
chips come exclusively from /api/rlm/runs (pink, self-retiring).
"""

import asyncio

from api.routers.sessions import list_workers
from db import models as db


def test_rlm_view_sessions_are_not_listed_as_workers():
    parent = db.create_session(title="ARC parent")
    worker = db.create_session(title="solver", session_type="worker", parent_session_id=parent)
    rlm_view = db.create_session(title="RLM: digest the game source", session_type="rlm", parent_session_id=parent)

    out = asyncio.run(list_workers(parent))
    ids = [w["id"] for w in out["workers"]]

    assert worker in ids
    assert rlm_view not in ids


def test_plain_worker_listing_still_works():
    parent = db.create_session(title="plain parent")
    worker = db.create_session(title="only child", session_type="worker", parent_session_id=parent)
    out = asyncio.run(list_workers(parent))
    assert [w["id"] for w in out["workers"]] == [worker]
    assert out["workers"][0]["title"] == "only child"
