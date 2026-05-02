"""Item #4: search_post_mortems scout tool."""

import json

from core.scout.report import SessionBrief
from core.scout.runner import _exec_scout_tool, _format_post_mortem_hits
from db import models as db


def _seed_pm(session_id: str, verdict: str, failure_cause: str, attempt: int = 1, payload: dict | None = None) -> str:
    return db.add_post_mortem(
        session_id=session_id,
        attempt=attempt,
        verdict=verdict,
        failure_cause=failure_cause,
        confidence=0.8,
        reflect_model="test",
        reflect_latency_ms=10,
        scout_viability="verified",
        execution_mode="inline",
        payload_json=json.dumps(payload or {}),
    )


def test_helper_filters_by_failure_cause():
    sid = db.create_session(title="pm-search A")
    _seed_pm(sid, verdict="retry", failure_cause="skill")
    _seed_pm(sid, verdict="retry", failure_cause="tool")
    _seed_pm(sid, verdict="pass", failure_cause="none")

    hits = db.search_post_mortems_for_scout(failure_cause="skill")
    assert all(h["failure_cause"] == "skill" for h in hits)
    assert len(hits) >= 1


def test_helper_filters_by_subject_substring():
    sid = db.create_session(title="pm-search B")
    _seed_pm(
        sid,
        verdict="retry",
        failure_cause="skill",
        payload={"recommended_skills": ["needle-skill"], "reasoning": "failed"},
    )
    _seed_pm(
        sid, verdict="retry", failure_cause="skill", payload={"recommended_skills": ["other-skill"], "reasoning": "ok"}
    )

    hits = db.search_post_mortems_for_scout(subject="needle-skill")
    assert len(hits) >= 1
    assert all("needle-skill" in h["payload_json"] for h in hits)


def test_helper_no_filters_returns_recent():
    hits = db.search_post_mortems_for_scout()
    assert isinstance(hits, list)
    # Newest-first ordering.
    for prev, nxt in zip(hits, hits[1:]):
        assert prev["created_at"] >= nxt["created_at"]


def test_helper_limit_capped_at_10():
    hits = db.search_post_mortems_for_scout(limit=50)
    assert len(hits) <= 10


def test_handler_formats_results_and_hides_payload():
    sid = db.create_session(title="pm-search C")
    _seed_pm(
        sid,
        verdict="retry",
        failure_cause="skill",
        payload={"reasoning": "top-secret internal details here that should not leak"},
    )

    brief = SessionBrief(session_id=sid)
    output = _exec_scout_tool(
        "search_post_mortems",
        {"failure_cause": "skill", "limit": 5},
        brief,
    )
    # Does not leak raw payload_json markers (JSON opens with `{`).
    assert '"reasoning"' not in output
    # Structured fields show up.
    assert "verdict=retry" in output
    assert "cause=skill" in output


def test_format_helper_handles_empty():
    assert "No matching" in _format_post_mortem_hits([])


def test_format_helper_truncates_reasoning():
    long = "x" * 1000
    hit = {
        "session_id": "abc12345" + "x",
        "verdict": "retry",
        "failure_cause": "skill",
        "attempt": 1,
        "created_at": "2025-01-01T00:00:00+00:00",
        "payload_json": json.dumps({"reasoning": long}),
    }
    out = _format_post_mortem_hits([hit])
    # Total length cap per line ~300.
    assert len(out.splitlines()[0]) <= 300
