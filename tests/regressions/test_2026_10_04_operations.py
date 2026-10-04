"""Operational failures observed on the box; all fixtures are synthetic."""

import asyncio
import gzip
import json
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from db import models as db


def idle_runner(monkeypatch):
    from core.snooze import SnoozeRunner

    monkeypatch.setattr("config.settings.snooze_enabled", True)
    monkeypatch.setattr("sessions.manager.get_manager", lambda: SimpleNamespace(has_active_work=lambda: False))
    monkeypatch.setattr("core.events.get_event_bus", MagicMock())
    runner = SnoozeRunner()
    monkeypatch.setattr(runner, "_is_idle", lambda **kw: True)
    return runner


@pytest.mark.parametrize("shutdown", [False, True])
async def test_cancel_stops_thread_and_new_cycle_cannot_revive_it(monkeypatch, shutdown):
    from core.pools import run_background

    runner = idle_runner(monkeypatch)
    started, inspect_old, stopped = threading.Event(), threading.Event(), threading.Event()
    observed = []

    def work():
        started.set()
        while not runner._is_cancelled():
            time.sleep(0.002)
        inspect_old.wait(2)
        observed.append(runner._is_cancelled())
        stopped.set()

    async def first():
        await run_background(work)

    monkeypatch.setattr(runner, "_do_cycle", first)
    monkeypatch.setattr(runner, "cycle_backstop_seconds", lambda: 0.05 if not shutdown else 5)
    task = asyncio.create_task(runner.run_cycle(force=True))
    assert await asyncio.to_thread(started.wait, 2)
    if shutdown:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task == "backstop"

    async def second():
        assert not runner._is_cancelled()
        inspect_old.set()
        assert await asyncio.to_thread(stopped.wait, 2)

    monkeypatch.setattr(runner, "_do_cycle", second)
    assert await runner.run_cycle(force=True) == "ran"
    assert observed == [True]
    assert runner.get_stats()["last_successful_cycle"]


async def test_slow_rung_does_not_starve_later_work(monkeypatch):
    from core.snooze import RUNG_TIMEOUTS

    runner = idle_runner(monkeypatch)
    monkeypatch.setitem(RUNG_TIMEOUTS, "consolidate_files", 0.01)
    completed = []

    async def cycle():
        await runner._rung("consolidate_files", asyncio.sleep(10))
        await runner._rung("later", asyncio.sleep(0))
        completed.append(True)

    monkeypatch.setattr(runner, "_do_cycle", cycle)
    assert await runner.run_cycle(force=True) == "partial"
    assert completed == [True]
    saved = json.loads(db.get_snooze_state("snooze_health"))
    assert saved["degraded"]
    assert saved["rung_failures"] == {"consolidate_files": "timeout"}
    assert "later" in saved["rung_durations_ms"]


def test_cluster_batches_resume_and_bound_fingerprints(monkeypatch):
    from core.memory import consolidate as c

    seen = []

    def score(a, b, *args):
        assert len(a.content_fingerprints) <= 24
        seen.append((a.name, b.name))
        return 0

    monkeypatch.setattr(c, "score_pair", score)
    sigs = [c.FileSignature(n, n, set(), set(), 1000, content_fingerprints=["x"] * 1000) for n in "abcd"]
    cursor = {}
    for _ in range(10):
        _, cursor, done = c.scan_cluster_batch(sigs, cursor, lambda: False, pair_limit=2)
        if done:
            break
    assert done
    assert seen == [("a", "b"), ("a", "c"), ("a", "d"), ("b", "c"), ("b", "d"), ("c", "d")]


def test_cluster_worker_checks_cancel_inside_fingerprint_loop():
    from core.memory.consolidate import FileSignature, score_pair

    a = FileSignature("a", "a", {"a"}, {"x"}, 10, content_fingerprints=["same"] * 20)
    assert score_pair(a, a, 0.55, cancel_check=lambda: True) == 0


async def test_failed_splits_back_off_and_other_files_progress(monkeypatch):
    from core.memory import sweeps
    from core.memory.format import MemoryEntry

    files = [
        SimpleNamespace(name="pernix.large", entry_count=150, updated_at="v1"),
        SimpleNamespace(name="pernix.other", entry_count=90, updated_at="v1"),
    ]
    store = MagicMock()
    store.list_files.return_value = files
    store.read_file.return_value = "synthetic"
    entries = [SimpleNamespace(epoch=i, entry_type="note", content=f"entry {i}") for i in range(150)]
    monkeypatch.setattr("core.memory.format.parse_entries_from_markdown", lambda *args: entries)
    chat = AsyncMock(return_value=SimpleNamespace(content="", finish_reason="length"))
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: SimpleNamespace(chat=chat))
    assert await sweeps.split_file(store, lambda: False) == (True, 0)
    assert chat.await_count == 2
    assert "These 50 memory" in chat.call_args_list[0].kwargs["messages"][0]["content"]
    assert "These 25 memory" in chat.call_args_list[1].kwargs["messages"][0]["content"]
    chat.reset_mock()
    assert await sweeps.split_file(store, lambda: False) == (True, 0)
    assert "pernix.other" in chat.call_args_list[0].kwargs["messages"][0]["content"]
    chat.reset_mock()
    assert await sweeps.split_file(store, lambda: False) == (False, 0)
    chat.assert_not_called()
    files[0].updated_at = "v2"
    assert await sweeps.split_file(store, lambda: False) == (True, 0)
    assert chat.await_count == 2
    store.move_entries.assert_not_called()


def provenance():
    from core.reflect import tool_provenance

    return tool_provenance(
        [
            {
                "role": "assistant",
                "tool_calls": [{"id": "old", "name": "http_get", "arguments": '{"url":"/239726/DGRO"}'}],
            },
            {"role": "tool", "tool_call_id": "old", "content": "iShares Core S&P 500 ETF | IVV"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "new", "name": "http_get", "arguments": '{"url":"/264623/DGRO"}'}],
            },
            {"role": "tool", "tool_call_id": "new", "content": "iShares Core Dividend Growth ETF | DGRO"},
        ]
    )


@pytest.mark.parametrize(
    "citations",
    [
        [],
        [{"subject": "264623", "tool_call_id": [], "expected_tool_call_id": {}}],
        [
            {
                "subject": "264623",
                "tool_call_id": "new",
                "quote": "iShares Core Dividend Growth ETF | DGRO",
                "expected_tool_call_id": "old",
                "expected_quote": "iShares Core S&P 500 ETF | IVV",
            }
        ],
    ],
)
def test_recovered_fetch_cannot_produce_false_correction(citations, monkeypatch):
    from core.reflect import ReflectResult, guard_factual_correction
    from sessions.hooks import _deferred_verdict_notification

    result = ReflectResult(
        verdict="retry",
        confidence=0.9,
        failure_kind="factual",
        failure_evidence=citations,
        reasoning="Wrong ETF: 264623 is IVV",
        strategy="Do not reuse 264623",
        what_failed="wrong ETF",
    )
    guard_factual_correction(result, provenance())
    assert result.verdict == "pass"
    assert result.verification == "unknown"
    assert result.correction_rejected
    assert result.strategy == result.what_failed == ""
    notify = MagicMock()
    monkeypatch.setattr("sessions.hooks._broadcast_reflect_notification", notify)
    _deferred_verdict_notification("synthetic", result)
    notify.assert_not_called()


def test_attributed_later_verification_can_support_a_correction():
    from core.reflect import ReflectResult, guard_factual_correction, tool_provenance

    msgs = []
    for call, value in [("one", "Widget 42 voltage is 10V"), ("two", "Widget 42 voltage is 20V")]:
        msgs += [
            {"role": "assistant", "tool_calls": [{"id": call, "name": "http_get", "arguments": "widget42"}]},
            {"role": "tool", "tool_call_id": call, "content": value},
        ]
    result = ReflectResult(
        verdict="retry",
        failure_kind="factual",
        failure_evidence=[
            {
                "subject": "widget42",
                "tool_call_id": "one",
                "quote": "Widget 42 voltage is 10V",
                "expected_tool_call_id": "two",
                "expected_quote": "Widget 42 voltage is 20V",
            }
        ],
    )
    guard_factual_correction(result, tool_provenance(msgs))
    assert result.verdict == "retry"


@pytest.mark.parametrize("ungradable", [ValueError("bad JSON"), SimpleNamespace(correction_rejected="missing source")])
async def test_holdout_counts_ungradable_cases(monkeypatch, ungradable):
    from core import reflect_holdout as h

    monkeypatch.setattr("config.settings.llm_model", "synthetic")
    fixtures = [
        {
            "id": str(i),
            "user_request": "x",
            "final_response": "x",
            "expected_verdict": "pass",
            "expected_failure_cause": "none",
        }
        for i in range(9)
    ]
    monkeypatch.setattr(h, "load_fixtures", lambda _: fixtures)
    results = [SimpleNamespace(verdict="pass", failure_cause="none")] * 7 + [
        SimpleNamespace(verdict="retry", failure_cause="agent"),
        ungradable,
    ]
    monkeypatch.setattr(h, "_grade_evidence", AsyncMock(side_effect=results))
    report = await h.run_holdout()
    assert (report["total"], report["attempted"], report["graded"], report["ungradable"]) == (9, 9, 8, 1)
    assert report["accuracy"] == 0.875
    assert report["success_rate"] == 0.7778
    assert report["completion_rate"] == 0.8889


@pytest.mark.parametrize("timestamp", [None, "2026-09-10T12:34:56Z"])
def test_job_reconciliation_uses_real_completion_time(tmp_path, timestamp):
    from core.tools.builtin.jobs_tool import _elapsed, reconcile_running_jobs

    sid = db.create_session(title="synthetic")
    jobdir = tmp_path / "job"
    jobdir.mkdir()
    (jobdir / "exit_code").write_text("0")
    if timestamp:
        (jobdir / "finished_at").write_text(timestamp)
    db.create_job(
        job_id="j",
        session_id=sid,
        name="test",
        command="true",
        pid=99999999,
        deadline_s=60,
        log_path=str(jobdir / "output.log"),
    )
    assert reconcile_running_jobs() == 1
    row = db.get_job("j")
    assert row["state"] == "done"
    assert row["finished_at"] == ("2026-09-10T12:34:56+00:00" if timestamp else None)
    if timestamp is None:
        assert _elapsed(row) == "unknown"


def test_job_reconcile_does_not_overwrite_a_concurrent_kill(tmp_path, monkeypatch):
    from core.tools.builtin import jobs_tool

    sid = db.create_session(title="synthetic")
    (tmp_path / "exit_code").write_text("0")
    db.create_job(
        job_id="j",
        session_id=sid,
        name="test",
        command="true",
        pid=99999999,
        deadline_s=60,
        log_path=str(tmp_path / "output.log"),
    )
    old = db.get_job("j")
    db.update_job("j", state="killed")
    assert jobs_tool._refresh(old)["state"] == "killed"


def test_daily_logs_compress_and_separate_access_preserving_legacy(tmp_path):
    from core.logging_setup import daily_handler

    legacy = tmp_path / "pernix.log.1"
    legacy.write_text("old history")
    handler = daily_handler(tmp_path / "pernix.log", access=False)
    assert handler.backupCount == 35
    handler.handle(logging.LogRecord("pernix.snooze", logging.WARNING, "", 1, "maintenance failed", (), None))
    handler.handle(logging.LogRecord("uvicorn.access", logging.INFO, "", 1, "poll", (), None))
    handler.doRollover()
    handler.close()
    archives = list(tmp_path.glob("*.gz"))
    assert len(archives) == 1
    content = gzip.decompress(archives[0].read_bytes()).decode()
    assert "maintenance failed" in content
    assert "poll" not in content
    assert legacy.read_text() == "old history"


def test_trust_panel_shows_coverage_and_whole_suite_success(tmp_path):
    from tests.js_harness import NODE, run_js

    if not NODE:
        pytest.skip("node is required")
    result = run_js(
        r"""
import { fns, mod, makeContext, run, report } from './sandbox.mjs';
const {ctx} = makeContext({section: x=>x, stat: (label,value,note)=>({label,value,note}),
    num: x=>Number(x||0), pct: x=>x==null?null:Math.round(x*100)+'%', relTime: x=>x});
run(ctx, fns(['graderSection','plural'], mod('components/modals/trust.js')));
ctx.grader = {holdout:{n:8,total:9,graded:8,correct:7,ungradable:1,accuracy:.875,success_rate:.7778,completion_rate:.8889}};
report({rows:run(ctx, 'graderSection(grader)')});
""",
        tmp_path,
    )
    by_label = {row["label"]: row for row in result["rows"] if isinstance(row, dict)}
    assert by_label["Whole-suite success"]["value"] == "78%"
    assert "1 ungradable" in by_label["Whole-suite success"]["note"]
    assert by_label["Accuracy on graded cases"]["value"] == "88%"
    assert by_label["Grading completion"]["value"] == "89%"


def test_job_reconciliation_pages_past_live_jobs(tmp_path, monkeypatch):
    from core.tools.builtin import jobs_tool

    sid = db.create_session(title="synthetic")
    for job in ("a", "b", "c"):
        directory = tmp_path / job
        directory.mkdir()
        db.create_job(
            job_id=job,
            session_id=sid,
            name=job,
            command="true",
            pid=1,
            deadline_s=60,
            log_path=str(directory / "output.log"),
        )
    (tmp_path / "c" / "exit_code").write_text("0")
    monkeypatch.setattr(jobs_tool, "_pid_alive", lambda _: True)
    assert jobs_tool.reconcile_running_jobs(limit=2) == 0
    assert jobs_tool.reconcile_running_jobs(limit=2) == 1
    assert db.get_job("c")["state"] == "done"


def test_empty_legacy_sidecar_is_not_premature_failure(tmp_path):
    from core.tools.builtin import jobs_tool

    sid = db.create_session(title="synthetic")
    (tmp_path / "exit_code").write_text("")
    db.create_job(
        job_id="empty",
        session_id=sid,
        name="test",
        command="true",
        pid=1,
        deadline_s=60,
        log_path=str(tmp_path / "output.log"),
    )
    assert jobs_tool._refresh(db.get_job("empty"))["state"] == "running"


def test_daily_retention_preserves_35_archives_and_legacy_logs(tmp_path):
    from datetime import date, timedelta

    from core.logging_setup import daily_handler

    handler = daily_handler(tmp_path / "pernix.log", access=False)
    archives = []
    for days in range(36):
        archive = tmp_path / ("pernix.log." + str(date(2026, 8, 1) + timedelta(days=days)) + ".gz")
        archive.write_bytes(b"archive")
        archives.append(str(archive))
    (tmp_path / "pernix.log.1").write_text("legacy")
    assert handler.getFilesToDelete() == archives[:1]
    handler.close()
