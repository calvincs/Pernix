"""A worker wrote its report where its own file tools put it, and the parent
looked somewhere else.

`get_worker_result` opened `settings.workspace_dir / .worker_<id>_summary.md`
— the global workspace, with no idea that the worker had inherited a space and
that every relative path it wrote resolved into the space home instead.
`_finalize_worker` used the same global root, found nothing, and stamped a
fabricated summary there from the worker's chatty last assistant message
("Done; wrote the report file."). The real deliverable sat on disk, one
directory away, unreadable to the only session that wanted it.

The unscoped fallback was worse: one stale `summary.md` in the global
workspace answered for ANY worker that had no per-worker file, so a parent
could be handed a different session's report as its worker's result.

Every run now opens a durable record naming exactly one report path, derived
from the space the worker actually writes in. The charter states that path,
finalization stamps it, retrieval reads it, and revival versions the previous
artifact instead of deleting it. The shared `summary.md` is adopted only when
its mtime falls inside this worker's own activity window and no sibling
competes for it, and it is labelled when it is.
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path

import pytest

from config import settings
from core.extensions.orchestration import (
    get_worker_result,
    get_worker_transcript,
    resume_worker,
)
from core.extensions.orchestration import report as wreport
from core.tools import paths
from core.tools.builtin.core_tools import file_write
from db import models as db
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _space_worker(mgr, slug="lab", title="W"):
    """A parent in a space and a worker that inherited it — what spawn_worker
    leaves behind, minus the event loop."""
    space = db.create_space(slug.title(), "#123456", slug)
    parent = mgr.create_session(title="P", space_id=space["id"])
    wid = mgr.create_session(
        title=title,
        session_type="worker",
        parent_session_id=parent,
        space_id=space["id"],
    )
    wreport.begin_run(wid, workspace_home=mgr.get(wid).workspace_home, reason="spawn")
    return space, parent, wid


def _write_as_worker(mgr, wid: str, name: str, body: str) -> Path:
    """file_write from inside the worker's own path context."""
    home = mgr.get(wid).workspace_home
    tok = paths.WORKSPACE_HOME.set(home) if home else None
    try:
        file_write(name, body)
    finally:
        if tok is not None:
            paths.WORKSPACE_HOME.reset(tok)
    return Path(home or settings.workspace_dir) / name


REPORT = "# Findings\nThe migration is idempotent.\n\n## Limitations\nOnly the 3.2 branch was sampled.\n"


def _end_a_turn(w, reason=None):
    """Drive a worker through a real turn so it is resumable."""
    from sessions import state_v2 as sv2

    sv2.transition(w, sv2.SessionStateV2.SCOUTING, "prompt-arrived")
    sv2.transition(w, sv2.SessionStateV2.PROCESSING, "scout-done")
    sv2.transition(
        w,
        sv2.SessionStateV2.FINALIZING,
        "loop-complete",
        termination_reason=reason or sv2.TerminationReason.COMPLETE,
    )
    sv2.transition(w, sv2.SessionStateV2.IDLE_READY, "turn-complete")


def test_the_space_workers_report_reaches_its_parent(mgr):
    _space, _parent, wid = _space_worker(mgr)
    db.add_message(wid, "user", "go")
    db.add_message(wid, "assistant", "Done; wrote the report file.")
    written = _write_as_worker(mgr, wid, wreport.report_name(wid), REPORT)

    assert written.exists(), "the worker's own tools put it in the space home"
    assert not (Path(settings.workspace_dir) / wreport.report_name(wid)).exists()

    out = get_worker_result(wid)
    assert "Only the 3.2 branch was sampled." in out
    assert "Done; wrote the report file." not in out


def test_finalize_does_not_fabricate_a_second_report(mgr):
    """The bug's signature: a global stamp built from the last chat line while
    the real report sat in the space home."""
    _space, _parent, wid = _space_worker(mgr)
    db.add_message(wid, "user", "go")
    db.add_message(wid, "assistant", "Done; wrote the report file.")
    _write_as_worker(mgr, wid, wreport.report_name(wid), REPORT)
    w = mgr.get(wid)
    w.termination_reason = "complete"

    asyncio.run(mgr._finalize_worker(w))

    assert not (Path(settings.workspace_dir) / wreport.report_name(wid)).exists()
    assert "Only the 3.2 branch was sampled." in get_worker_result(wid)


def test_finalize_stamps_into_the_run_record_path(mgr):
    """A worker that wrote nothing still gets a stamp — in the space home the
    record names, not the global root."""
    _space, _parent, wid = _space_worker(mgr)
    db.add_message(wid, "user", "go")
    db.add_message(wid, "assistant", "I ran out of time.")
    w = mgr.get(wid)
    w.termination_reason = "complete"

    asyncio.run(mgr._finalize_worker(w))

    stamped = Path(wreport.load_run(wid)["report_path"])
    assert stamped.parent == Path(w.workspace_home)
    assert "I ran out of time." in stamped.read_text()


def test_revival_versions_the_previous_report_instead_of_deleting_it(mgr, monkeypatch):
    _space, parent, wid = _space_worker(mgr)
    db.add_message(wid, "user", "go")
    db.add_message(wid, "assistant", "run A done")
    run_a = _write_as_worker(mgr, wid, wreport.report_name(wid), "# Run A\nfirst deliverable\n")
    _end_a_turn(mgr.get(wid))

    async def _noop_prompt(sid, msg, *a, **k):
        return None

    monkeypatch.setattr(mgr, "prompt", _noop_prompt)

    async def _revive():
        loop = asyncio.get_running_loop()
        ctx = {"session_id": parent, "_loop": loop}
        return await asyncio.to_thread(resume_worker, wid, "", "keep going", ctx)

    out = asyncio.run(_revive())
    assert "revived" in out, out

    assert not run_a.exists(), "the active pointer moved off the run A artifact"
    retired = [Path(r["path"]) for r in wreport.load_run(wid)["retired"]]
    assert retired and retired[0].exists()
    assert "first deliverable" in retired[0].read_text()
    assert wreport.load_run(wid)["seq"] == 2

    _write_as_worker(mgr, wid, wreport.report_name(wid), "# Run B\nsecond deliverable\n")
    assert "second deliverable" in get_worker_result(wid)


def test_two_concurrent_workers_keep_their_own_reports(mgr):
    space = db.create_space("Lab", "#123456", "lab")
    parent = mgr.create_session(title="P", space_id=space["id"])
    ids = []
    for name in ("A", "B"):
        wid = mgr.create_session(
            title=name,
            session_type="worker",
            parent_session_id=parent,
            space_id=space["id"],
        )
        wreport.begin_run(wid, workspace_home=mgr.get(wid).workspace_home, reason="spawn")
        db.add_message(wid, "user", "go")
        db.add_message(wid, "assistant", f"{name} chatter")
        _write_as_worker(mgr, wid, wreport.report_name(wid), f"# Worker {name}\n{name} deliverable\n")
        ids.append(wid)

    a, b = (get_worker_result(i) for i in ids)
    assert "A deliverable" in a and "B deliverable" not in a
    assert "B deliverable" in b and "A deliverable" not in b


def test_a_stale_shared_summary_is_not_served_as_a_workers_result(mgr):
    """The unscoped legacy branch handed one file to every worker."""
    parent = mgr.create_session(title="P")
    ids = []
    for name in ("A", "B"):
        wid = mgr.create_session(title=name, session_type="worker", parent_session_id=parent)
        wreport.begin_run(wid, workspace_home=None, reason="spawn")
        db.add_message(wid, "user", "go")
        db.add_message(wid, "assistant", f"{name} real output")
        ids.append(wid)

    ws = Path(settings.workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "summary.md").write_text("# Unrelated legacy summary from some other session\n")

    for wid, name in zip(ids, ("A", "B")):
        out = get_worker_result(wid)
        assert "Unrelated legacy" not in out
        assert f"{name} real output" in out


def test_a_pre_fix_workers_shared_summary_is_still_reachable_but_labelled(mgr):
    """Compatibility, narrowed: a worker persisted before the per-worker
    convention gets its summary.md back only when the file's mtime falls
    inside its own activity window and no sibling competes for it."""
    parent = mgr.create_session(title="P")
    wid = mgr.create_session(title="old", session_type="worker", parent_session_id=parent)
    db.add_message(wid, "user", "go")
    ws = Path(settings.workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "summary.md").write_text("# Legacy report\nthe pre-fix deliverable\n")
    db.add_message(wid, "assistant", "chatty tail")  # the run closes after the write

    out = get_worker_result(wid)
    assert "the pre-fix deliverable" in out
    assert "LEGACY SUMMARY" in out and "Provenance is inferred" in out


def test_a_capped_preview_hands_back_the_artifact_handle(mgr):
    _space, _parent, wid = _space_worker(mgr)
    db.add_message(wid, "user", "go")
    db.add_message(wid, "assistant", "see the file")
    body = "# Report\n" + ("Q" * 4000) + "\n## Limitations\nSampling was partial.\n"
    path = _write_as_worker(mgr, wid, wreport.report_name(wid), body)

    out = get_worker_result(wid)
    assert "Sampling was partial." not in out, "the cap is still a cap"
    assert str(path) in out, "the preview must name the artifact it clipped"
    assert "file_read" in out
    # The handle is the route to the exact complete report — no reconstruction.
    assert Path(str(path)).read_text() == body


def test_the_final_report_survives_a_thirty_thousand_char_transcript(mgr):
    parent = mgr.create_session(title="P")
    wid = mgr.create_session(title="W", session_type="worker", parent_session_id=parent)
    wreport.begin_run(wid, workspace_home=None, reason="spawn")
    db.add_message(wid, "user", "go")
    for i in range(60):
        db.add_message(wid, "assistant", f"step {i} " + ("x" * 900))
        db.add_message(wid, "tool", f"result {i} " + ("y" * 900))
    final = "# FINAL REPORT\n" + ("z" * 4000) + "\n## Limitations\nCAVEAT_SENTINEL: sources incomplete.\n"
    final_id = db.add_message(wid, "assistant", final)
    db.add_message(wid, "reflect", _json.dumps({"verdict": "pass", "reasoning": "ok"}))

    head = get_worker_transcript(wid)
    assert "CAVEAT_SENTINEL" not in head, "head-first paging still cannot reach the end"
    assert f"#{final_id}" in get_worker_transcript(wid, select="tail")

    tail = get_worker_transcript(wid, select="tail")
    assert "CAVEAT_SENTINEL" in tail
    assert final in tail, "the final report must arrive complete, not reconstructed"

    one = get_worker_transcript(wid, message_id=final_id)
    assert one.count("CAVEAT_SENTINEL") == 1 and final in one


def test_transcript_pagination_walks_the_whole_stream(mgr):
    parent = mgr.create_session(title="P")
    wid = mgr.create_session(title="W", session_type="worker", parent_session_id=parent)
    ids = [db.add_message(wid, "assistant", f"chunk {i}") for i in range(6)]

    first = get_worker_transcript(wid, before_id=ids[3])
    assert "chunk 2" in first and "chunk 3" not in first
    rest = get_worker_transcript(wid, after_id=ids[2])
    assert "chunk 3" in rest and "chunk 2" not in rest


def test_tool_arguments_and_full_result_pointers_survive(mgr):
    parent = mgr.create_session(title="P")
    wid = mgr.create_session(title="W", session_type="worker", parent_session_id=parent)
    db.add_message(
        wid,
        "assistant",
        "calling",
        tool_calls=_json.dumps([{"name": "file_read", "arguments": {"path": "/etc/hosts", "limit": 40}}]),
    )
    big_id = db.add_message(wid, "tool", "R" * 3000)

    out = get_worker_transcript(wid)
    assert "file_read" in out and "/etc/hosts" in out, "arguments must survive"
    assert f"message_id={big_id}" in out, "a clipped result must point at its full row"
    assert len(get_worker_transcript(wid, message_id=big_id)) > 3000
