"""Tests for job spec validation + isolated test-run (spec Feature 7)."""

from __future__ import annotations

import asyncio

import pytest

from core.extensions import scheduling
from db import models as db
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


# ---------------------------------------------------------------------------
# validate_job_spec
# ---------------------------------------------------------------------------


def test_valid_spec_passes():
    v = scheduling.validate_job_spec("0 3 * * *", "Run the nightly summary and write it to report.md")
    assert v["ok"]
    assert not v["errors"]


def test_bad_cron_is_error():
    v = scheduling.validate_job_spec("99 99 * * *", "A perfectly reasonable prompt here")
    assert not v["ok"]
    assert any("cron_expr" in e for e in v["errors"])


def test_trivial_prompt_is_error():
    v = scheduling.validate_job_spec("0 3 * * *", "go")
    assert not v["ok"]
    assert any("prompt" in e for e in v["errors"])


def test_unknown_tool_is_error():
    v = scheduling.validate_job_spec(
        "0 3 * * *",
        "Run the nightly summary please",
        allowed_tools=["file_read", "definitely_not_a_tool"],
    )
    assert not v["ok"]
    assert any("definitely_not_a_tool" in e for e in v["errors"])


def test_unknown_model_is_warning_not_error(monkeypatch):
    class _Reg:
        def resolve_model_id(self, m):
            return m

        def get_model_info(self, m):
            return None

    class _Client:
        class router:
            registry = _Reg()

    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: _Client())
    v = scheduling.validate_job_spec("0 3 * * *", "Run the nightly summary please", model="ghost/model")
    assert v["ok"]
    assert any("ghost/model" in w for w in v["warnings"])


def test_schedule_job_blocks_invalid_spec(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduling, "CRON_PATH", tmp_path / "cron_jobs.json", raising=False)
    out = scheduling.schedule_job("bad-job", "not a cron", "A perfectly reasonable prompt here")
    assert out.startswith("Error: job spec invalid")


# ---------------------------------------------------------------------------
# run_job_test
# ---------------------------------------------------------------------------


def test_run_job_test_unknown_job():
    result = asyncio.run(scheduling.run_job_test("no-such-job"))
    assert not result["ok"]
    assert "not found" in result["error"]


def test_run_job_test_isolated_run(mgr, tmp_path, monkeypatch):
    # Persisted job the test will read.
    monkeypatch.setattr(scheduling, "CRON_PATH", tmp_path / "cron_jobs.json", raising=False)
    scheduling.CRON_PATH.write_text(
        '[{"name": "smoke", "cron_expr": "0 3 * * *", '
        '"prompt": "Write hello.txt with the word hello in it", "model": ""}]'
    )

    prompted: list[tuple[str, str]] = []

    async def fake_prompt(session_id, message, system_prompt="", idempotency_key=None):
        prompted.append((session_id, message))
        db.add_message(session_id, "user", message)
        db.add_message(session_id, "assistant", "Done — hello.txt written.")

    monkeypatch.setattr(mgr, "prompt", fake_prompt)

    async def fake_wait(session, deadline):
        return True

    monkeypatch.setattr("core.canary.runner._wait_for_turn_end", fake_wait)

    result = asyncio.run(scheduling.run_job_test("smoke"))
    assert result["ok"], result
    assert prompted and "hello.txt" in prompted[0][1]
    assert result["validation"]["ok"]
    assert result["answer_preview"].startswith("Done")
    # The dispatch session is a cron-type session with a clear test title.
    row = db.get_session(result["session_id"])
    assert row["session_type"] == "cron"
    assert row["title"].startswith("Job test:")
    # Isolation scaffolding was cleared afterward.
    s = mgr.get(result["session_id"])
    assert s.workspace_override is None
    assert s.tool_allowlist is None
    # No cron_runs row was recorded — a dry run never touches history.
    runs = db.list_cron_runs(job_name="smoke", limit=5)
    assert not runs
