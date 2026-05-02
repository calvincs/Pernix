"""Phase 2c: post-mortem artifacts are written on every reflect invocation."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.reflect import Deliverable, ReflectResult, _write_post_mortem, reflect_on_session
from core.scout.report import DeliverableSpec, ScoutReport
from db import models as db


def _session_with_request() -> str:
    sid = db.create_session(title="PM test")
    db.add_message(sid, "user", "Write report.md with findings.")
    db.add_message(sid, "assistant", "Done — report.md has been written.")
    return sid


def test_add_and_list_post_mortem_roundtrip():
    sid = _session_with_request()
    pm_id = db.add_post_mortem(
        session_id=sid,
        attempt=1,
        verdict="pass",
        failure_cause="none",
        confidence=0.9,
        reflect_model="m",
        reflect_latency_ms=50,
        scout_viability="verified",
        execution_mode="inline",
        payload_json=json.dumps({"x": 1}),
    )
    assert pm_id
    rows = db.list_post_mortems(session_id=sid)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "pass"
    assert rows[0]["failure_cause"] == "none"
    assert rows[0]["scout_viability"] == "verified"
    assert json.loads(rows[0]["payload_json"]) == {"x": 1}


def test_list_post_mortems_filters_by_cause():
    sid1 = _session_with_request()
    sid2 = _session_with_request()
    db.add_post_mortem(sid1, 1, "retry", "scout", 0.8, "m", 10, None, "inline", "{}")
    db.add_post_mortem(sid2, 1, "pass", "none", 0.9, "m", 10, None, "inline", "{}")
    scout_only = db.list_post_mortems(failure_cause="scout")
    assert len(scout_only) == 1
    assert scout_only[0]["session_id"] == sid1


def test_write_post_mortem_direct_sets_artifact_id_on_result():
    sid = _session_with_request()
    result = ReflectResult(
        verdict="retry",
        reasoning="missed file",
        failure_cause="agent",
        confidence=0.7,
        deliverables=[Deliverable(description="Write x.md", status="unmet", evidence_ref="")],
    )
    scout = ScoutReport(
        viability="verified",
        execution_mode="inline",
        recommended_tools=["file_write"],
        deliverables_plan=[DeliverableSpec(description="Write x.md")],
    )
    _write_post_mortem(
        sid,
        1,
        result,
        scout,
        tool_summary={
            "file_write": {"calls": 1, "failures": 1, "total_latency_ms": 5, "errors": ["permission denied"]}
        },
    )
    assert result.artifact_id
    row = db.get_post_mortem(result.artifact_id)
    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["failure_cause"] == "agent"
    assert payload["scout_summary"]["viability"] == "verified"
    assert payload["tool_summary"]["file_write"]["failures"] == 1
    assert payload["deliverables"][0]["status"] == "unmet"


def test_write_post_mortem_survives_db_failure(monkeypatch):
    """Post-mortem writer must never raise back into the reflect path."""

    def boom(*a, **kw):
        raise RuntimeError("db exploded")

    monkeypatch.setattr("db.models.add_post_mortem", boom)
    result = ReflectResult(verdict="pass")
    # Should not raise
    _write_post_mortem("nonexistent_session", 1, result, None, None)
    # artifact_id remains empty since write failed
    assert result.artifact_id == ""


@pytest.mark.asyncio
async def test_reflect_on_session_writes_post_mortem_on_pass():
    sid = _session_with_request()
    # Mock the LLM to return a valid JSON reflect response
    fake_client = MagicMock()
    fake_client.chat = AsyncMock(
        return_value=MagicMock(
            content=json.dumps(
                {
                    "verdict": "pass",
                    "reasoning": "looks good",
                    "failure_cause": "none",
                    "confidence": 0.9,
                    "deliverables": [{"description": "Write report.md", "status": "met", "evidence_ref": "report.md"}],
                }
            )
        )
    )
    with patch("core.llm.client.get_llm_client", return_value=fake_client):
        result = await reflect_on_session(sid, attempt=1)
    assert result.verdict == "pass"
    assert result.artifact_id  # post-mortem was written
    rows = db.list_post_mortems(session_id=sid)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "pass"
    assert rows[0]["failure_cause"] == "none"


@pytest.mark.asyncio
async def test_reflect_on_session_writes_post_mortem_on_parse_failure():
    sid = _session_with_request()
    fake_client = MagicMock()
    fake_client.chat = AsyncMock(return_value=MagicMock(content="not json at all"))
    with patch("core.llm.client.get_llm_client", return_value=fake_client):
        # reflect_max_retries default=2, attempt=1 → retry verdict on parse fail
        result = await reflect_on_session(sid, attempt=1)
    # Post-mortem recorded even for parse failure
    rows = db.list_post_mortems(session_id=sid)
    assert len(rows) == 1
    assert "parse error" in rows[0]["verdict"] or rows[0]["verdict"] in ("retry", "escalate", "pass")
