"""A user message preempted AWAITING_WORKERS and the resumed turn spawned a
duplicate worker (field case ae952f40e3d1).

Preemption is by design — the user outranks the wait. But the resumed turn's
context said nothing about the worker still running, so the agent read
check_workers ("0/1 done ... processing | idle 152s"), concluded the worker
had been "suspended while I handled the user's message", and spawned
"R11L full-game finish v2" — two workers grinding the same task. The volatile
tail now carries a [WORKERS YOU ARE WATCHING] block naming each outstanding
worker with an explicit do-not-respawn instruction.
"""

import json

from core.context.compiler import _build_volatile_tail, _build_watched_workers_block
from db import models as db


def test_block_names_watched_workers_and_forbids_respawn():
    parent = db.create_session(title="ARC parent")
    worker = db.create_session(title="R11L full-game finish", session_type="worker")
    db.update_session(parent, watched_worker_ids=json.dumps([worker]))

    block = _build_watched_workers_block(parent)
    assert "[WORKERS YOU ARE WATCHING]" in block
    assert worker in block
    assert "R11L full-game finish" in block
    assert "Do NOT spawn" in block


def test_no_block_when_not_watching():
    sid = db.create_session(title="plain")
    assert _build_watched_workers_block(sid) == ""


def test_volatile_tail_carries_the_block():
    tail = _build_volatile_tail("", workers_block="[WORKERS YOU ARE WATCHING]\n- w1")
    assert "[WORKERS YOU ARE WATCHING]" in tail


def test_builder_never_raises_on_garbage():
    sid = db.create_session(title="odd")
    db.update_session(sid, watched_worker_ids="not-json{")
    assert _build_watched_workers_block(sid) == ""
