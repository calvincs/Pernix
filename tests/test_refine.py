"""Tests for core/refine.py — whole-session refine pass."""

import json

import pytest

from core.llm.types import ChatResponse, TokenUsage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_basic_session(
    skill_name: str | None = "test-skill",
    user_msg: str = "Help me with the test-skill workflow.",
    assistant_msg: str = "Done.",
    include_reflect: bool = False,
):
    """Build a session with a user + assistant exchange and (optionally) a
    load_skill tool call. No reflect message by default — refine should
    handle that path."""
    from db import models as db

    sid = db.create_session(title="Refine test session")
    db.add_message(sid, "user", user_msg)
    if skill_name:
        tool_calls = json.dumps(
            [
                {
                    "id": "call_1",
                    "function": {
                        "name": "load_skill",
                        "arguments": json.dumps({"name": skill_name}),
                    },
                }
            ]
        )
        db.add_message(sid, "assistant", "Loading skill.", tool_calls=tool_calls)
        db.add_message(sid, "tool", "Loaded skill content here.", tool_call_id="call_1")
    db.add_message(sid, "assistant", assistant_msg)
    if include_reflect:
        db.add_message(
            sid,
            "reflect",
            json.dumps(
                {
                    "verdict": "pass",
                    "failure_cause": "none",
                    "confidence": 0.9,
                    "reasoning": "fine",
                    "diagnostic": "",
                    "what_worked": "everything",
                    "what_failed": "",
                    "strategy": "",
                }
            ),
        )
    return sid


def _stub_skill_registry(monkeypatch, skill_name: str = "test-skill"):
    class FakeRegistry:
        def load_instructions(self, name):
            if name == skill_name:
                return "## Usage\nDescribed usage here.\n## Common Failures\nKnown limits."
            return None

    monkeypatch.setattr(
        "core.skills.registry.get_skill_registry",
        lambda: FakeRegistry(),
    )


def _llm_response(payload: dict) -> ChatResponse:
    return ChatResponse(
        content=json.dumps(payload),
        tool_calls=None,
        usage=TokenUsage(10, 20, 30),
        model="fake-bg-model",
        provider="fake",
        finish_reason="stop",
    )


# ---------------------------------------------------------------------------
# _parse_refine_output
# ---------------------------------------------------------------------------


def test_parse_refine_output_handles_fences():
    from core.refine import _parse_refine_output

    fenced = "```json\n" + json.dumps({"nothing_actionable": False, "proposals": [], "lessons": []}) + "\n```"
    proposals, lessons, na = _parse_refine_output(fenced)
    assert proposals == []
    assert lessons == []
    assert na is False


def test_parse_refine_output_nothing_actionable_flag():
    from core.refine import _parse_refine_output

    raw = json.dumps({"nothing_actionable": True, "proposals": [], "lessons": []})
    _, _, na = _parse_refine_output(raw)
    assert na is True


def test_parse_refine_output_malformed_returns_empty():
    from core.refine import _parse_refine_output

    proposals, lessons, na = _parse_refine_output("not json at all")
    assert proposals == []
    assert lessons == []
    assert na is False


# ---------------------------------------------------------------------------
# run_for_session — no reflect verdict path (broader gate than snooze_reflect)
# ---------------------------------------------------------------------------


async def test_refine_runs_without_reflect_verdict(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    """Refine should analyze sessions that have no reflect message at all —
    this is the key behavioral difference from snooze_reflect."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = _make_basic_session(include_reflect=False)
    _stub_skill_registry(monkeypatch)

    mock_llm_client.responses = [
        _llm_response(
            {
                "nothing_actionable": False,
                "proposals": [
                    {
                        "skill_name": "test-skill",
                        "section": "Usage",
                        "problem": "Missing concrete example",
                        "proposed_change": "Add: example invocation `test-skill foo`.",
                        "confidence": 0.85,
                    }
                ],
                "lessons": [],
            }
        )
    ]

    from core.refine import run_for_session

    stats = await run_for_session(sid)

    assert stats["skipped_reason"] is None
    assert stats["proposals_saved"] == 1
    assert mock_llm_client.call_count == 1

    from db import models as db

    proposals = db.list_skill_proposals(skill_name="test-skill")
    assert len(proposals) == 1
    assert proposals[0]["source_origin"] == "refine"
    assert proposals[0]["session_id"] == sid


# ---------------------------------------------------------------------------
# run_for_session — nothing_actionable=true persists no data
# ---------------------------------------------------------------------------


async def test_refine_nothing_actionable_persists_no_data(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    """nothing_actionable=true with empty arrays should produce zero
    proposals/lessons but still register as a successful LLM call."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = _make_basic_session(include_reflect=False)
    _stub_skill_registry(monkeypatch)

    mock_llm_client.responses = [_llm_response({"nothing_actionable": True, "proposals": [], "lessons": []})]

    from core.refine import run_for_session

    stats = await run_for_session(sid)

    assert stats["nothing_actionable"] is True
    assert stats["proposals_saved"] == 0
    assert stats["lessons_saved"] == 0
    assert stats["skipped_reason"] is None
    assert mock_llm_client.call_count == 1


# ---------------------------------------------------------------------------
# run_for_session — frustration cue produces both proposal and lesson
# ---------------------------------------------------------------------------


async def test_refine_frustration_signal_extracts_proposal_and_lesson(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    """A session containing a user correction ('stop doing X') should be
    eligible to produce a skill proposal even with no reflect failure."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    from db import models as db

    sid = db.create_session(title="Frustration session")
    db.add_message(sid, "user", "Load the test-skill skill and apply it.")
    tool_calls = json.dumps(
        [
            {
                "id": "c1",
                "function": {
                    "name": "load_skill",
                    "arguments": json.dumps({"name": "test-skill"}),
                },
            }
        ]
    )
    db.add_message(sid, "assistant", "Loading.", tool_calls=tool_calls)
    db.add_message(sid, "tool", "Loaded.", tool_call_id="c1")
    db.add_message(sid, "assistant", "Here is the answer in extremely verbose form...")
    db.add_message(sid, "user", "Stop doing that — don't be so verbose, just give me the answer.")
    db.add_message(sid, "assistant", "Got it. Short answer.")
    _stub_skill_registry(monkeypatch)

    mock_llm_client.responses = [
        _llm_response(
            {
                "nothing_actionable": False,
                "proposals": [
                    {
                        "skill_name": "test-skill",
                        "section": "Usage",
                        "problem": "Output style is too verbose by default for this user.",
                        "proposed_change": "Add note: respond tersely; full prose only on request.",
                        "confidence": 0.8,
                    }
                ],
                "lessons": [
                    {
                        "tags": "verbosity,tone",
                        "weight": "high",
                        "content": "Keep test-skill responses terse unless asked for detail.",
                        "applies_when": "When invoking test-skill for quick answers",
                    }
                ],
            }
        )
    ]

    from core.refine import run_for_session

    stats = await run_for_session(sid)

    assert stats["proposals_saved"] == 1
    assert stats["lessons_saved"] == 1

    proposals = db.list_skill_proposals(skill_name="test-skill")
    assert len(proposals) == 1
    assert proposals[0]["source_origin"] == "refine"


# ---------------------------------------------------------------------------
# run_for_session — gates
# ---------------------------------------------------------------------------


async def test_refine_skips_worker_session(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    from db import models as db

    sid = db.create_session(title="Worker", session_type="worker")
    db.add_message(sid, "user", "Do the thing.")
    db.add_message(sid, "assistant", "Done.")

    from core.refine import run_for_session

    stats = await run_for_session(sid)

    assert stats["skipped_reason"] == "worker_session"
    assert mock_llm_client.call_count == 0


async def test_refine_skips_session_without_assistant_reply(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    from db import models as db

    sid = db.create_session(title="User-only")
    db.add_message(sid, "user", "Hi.")

    from core.refine import run_for_session

    stats = await run_for_session(sid)

    assert stats["skipped_reason"] == "insufficient_exchange"
    assert mock_llm_client.call_count == 0


async def test_refine_drops_low_confidence_proposal(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = _make_basic_session(include_reflect=False)
    _stub_skill_registry(monkeypatch)

    mock_llm_client.responses = [
        _llm_response(
            {
                "nothing_actionable": False,
                "proposals": [
                    {
                        "skill_name": "test-skill",
                        "section": "Usage",
                        "problem": "Vague hunch",
                        "proposed_change": "Try this maybe.",
                        "confidence": 0.3,  # below 0.6 floor
                    }
                ],
                "lessons": [],
            }
        )
    ]

    from core.refine import run_for_session

    stats = await run_for_session(sid)
    assert stats["proposals_saved"] == 0
    assert mock_llm_client.call_count == 1


async def test_refine_drops_proposal_for_unknown_skill(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    """A hallucinated skill_name must not get to write into a different
    skill's review queue — same invariant as snooze_reflect."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = _make_basic_session(include_reflect=False)
    _stub_skill_registry(monkeypatch)

    mock_llm_client.responses = [
        _llm_response(
            {
                "nothing_actionable": False,
                "proposals": [
                    {
                        "skill_name": "some-other-skill",  # not the active one
                        "section": "Usage",
                        "problem": "x",
                        "proposed_change": "y",
                        "confidence": 0.9,
                    }
                ],
                "lessons": [],
            }
        )
    ]

    from core.refine import run_for_session

    stats = await run_for_session(sid)
    assert stats["proposals_saved"] == 0


# ---------------------------------------------------------------------------
# db.get_unrefined_sessions selection
# ---------------------------------------------------------------------------


def test_get_unrefined_sessions_excludes_watermarked():
    from datetime import datetime, timedelta, timezone

    from db import models as db

    sid = db.create_session(title="Watermarked")
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hi back")
    # Back-date updated_at so the idle gate accepts it.
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (past, sid))

    # Before watermark: should appear.
    rows = db.get_unrefined_sessions(min_idle_minutes=10, limit=10)
    assert any(r["id"] == sid for r in rows)

    # After watermark: should disappear.
    db.set_snooze_state(f"refined:{sid}", "2024-01-01T00:00:00+00:00")
    rows_after = db.get_unrefined_sessions(min_idle_minutes=10, limit=10)
    assert not any(r["id"] == sid for r in rows_after)


def test_get_unrefined_sessions_excludes_worker_and_recent():
    from datetime import datetime, timedelta, timezone

    from db import models as db
    from db.database import connect_sessions

    worker_sid = db.create_session(title="Worker", session_type="worker")
    db.add_message(worker_sid, "user", "hi")
    db.add_message(worker_sid, "assistant", "hi")

    recent_sid = db.create_session(title="Recent")
    db.add_message(recent_sid, "user", "hi")
    db.add_message(recent_sid, "assistant", "hi")

    # Back-date both so the per-row idle gate decides on its own.
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (past, worker_sid))

    rows = db.get_unrefined_sessions(min_idle_minutes=10, limit=10)
    ids = {r["id"] for r in rows}
    assert worker_sid not in ids, "worker sessions must be skipped"
    assert recent_sid not in ids, "recent sessions (within idle floor) must be skipped"


def test_get_unrefined_sessions_excludes_no_exchange():
    """A session with only a user message but no assistant reply must not
    be returned — refine has nothing to analyze."""
    from datetime import datetime, timedelta, timezone

    from db import models as db
    from db.database import connect_sessions

    sid = db.create_session(title="No reply")
    db.add_message(sid, "user", "hi, are you there?")
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (past, sid))

    rows = db.get_unrefined_sessions(min_idle_minutes=10, limit=10)
    assert not any(r["id"] == sid for r in rows)


# ---------------------------------------------------------------------------
# db.get_pending_proposal_counts_by_skill
# ---------------------------------------------------------------------------


def test_pending_proposal_counts_by_skill_aggregates():
    from db import models as db

    sid = db.create_session(title="Source")
    for _ in range(2):
        db.add_skill_proposal(
            workflow_name=None,
            run_id=None,
            skill_name="alpha-skill",
            section="Usage",
            problem="p",
            proposed_change="c",
            confidence=0.8,
            source_origin="refine",
            session_id=sid,
        )
    db.add_skill_proposal(
        workflow_name=None,
        run_id=None,
        skill_name="beta-skill",
        section="Usage",
        problem="p",
        proposed_change="c",
        confidence=0.8,
        source_origin="refine",
        session_id=sid,
    )

    counts = db.get_pending_proposal_counts_by_skill()
    assert counts.get("alpha-skill") == 2
    assert counts.get("beta-skill") == 1


def test_pending_proposal_counts_excludes_resolved():
    from db import models as db

    sid = db.create_session(title="Source")
    pid = db.add_skill_proposal(
        workflow_name=None,
        run_id=None,
        skill_name="gamma-skill",
        section="Usage",
        problem="p",
        proposed_change="c",
        confidence=0.8,
        source_origin="refine",
        session_id=sid,
    )
    db.resolve_skill_proposal(pid, "applied")

    counts = db.get_pending_proposal_counts_by_skill()
    assert "gamma-skill" not in counts
