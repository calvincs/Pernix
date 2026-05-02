"""Tests for core/snooze_reflect.py — session-origin skill proposal + lesson extraction."""

import json

import pytest

from core.llm.types import ChatResponse, TokenUsage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_with_reflect(
    failure_cause: str = "skill",
    confidence: float = 0.8,
    skill_name: str | None = "test-skill",
    verdict: str = "retry",
    strategy: str = "add a pre-flight check",
    diagnostic: str = "no pre-flight specified",
    what_failed: str = "the actual call",
):
    """Build a session with a load_skill tool call + reflect message."""
    from db import models as db

    sid = db.create_session(title="Test session")
    db.add_message(sid, "user", "Help me with the test-skill workflow.")
    if skill_name:
        # Assistant message with a load_skill tool call
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
        db.add_message(
            sid,
            "assistant",
            "Loading skill.",
            tool_calls=tool_calls,
        )
        db.add_message(sid, "tool", "Loaded skill content here…", tool_call_id="call_1")
    db.add_message(sid, "assistant", "Tried the workflow but it failed.")
    reflect_event = {
        "verdict": verdict,
        "reasoning": "Step 2 failed because the skill's pre-flight check is missing",
        "diagnostic": diagnostic,
        "what_worked": "loading the skill",
        "what_failed": what_failed,
        "strategy": strategy,
        "missing": "",
        "failure_cause": failure_cause,
        "confidence": confidence,
        "latency_ms": 1234,
    }
    db.add_message(sid, "reflect", json.dumps(reflect_event))
    return sid


def _stub_skill_registry(monkeypatch, skill_name: str = "test-skill"):
    """Patch the skill registry so load_instructions returns content."""

    class FakeRegistry:
        def load_instructions(self, name):
            if name == skill_name:
                return "## Usage\nDescribed usage here.\n## Limitations\nKnown limits."
            return None

    monkeypatch.setattr(
        "core.skills.registry.get_skill_registry",
        lambda: FakeRegistry(),
    )


# ---------------------------------------------------------------------------
# _identify_active_skill / _build_tool_summary
# ---------------------------------------------------------------------------


def test_identify_active_skill_finds_load_skill_call():
    from core.snooze_reflect import _identify_active_skill

    sid = _make_session_with_reflect(skill_name="my-skill")
    from db import models as db

    messages = db.get_messages(sid)
    assert _identify_active_skill(messages) == "my-skill"


def test_identify_active_skill_returns_none_without_load_skill():
    from core.snooze_reflect import _identify_active_skill

    sid = _make_session_with_reflect(skill_name=None)
    from db import models as db

    messages = db.get_messages(sid)
    assert _identify_active_skill(messages) is None


def test_build_tool_summary_counts_calls_and_failures():
    """Tool messages whose content begins with 'Error' count as failures."""
    from core.snooze_reflect import _build_tool_summary
    from db import models as db

    sid = db.create_session(title="ToolSummary")
    tool_calls = json.dumps(
        [
            {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
        ]
    )
    db.add_message(sid, "assistant", "Running bash.", tool_calls=tool_calls)
    db.add_message(sid, "tool", "Error: command failed", tool_call_id="c1")
    summary = _build_tool_summary(db.get_messages(sid))
    assert summary["bash"]["calls"] == 1
    assert summary["bash"]["failures"] == 1


# ---------------------------------------------------------------------------
# run_for_session — actionable cause produces proposal + lesson
# ---------------------------------------------------------------------------


async def test_run_for_session_extracts_proposal_and_lesson(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = _make_session_with_reflect(failure_cause="skill", confidence=0.8)
    _stub_skill_registry(monkeypatch)

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                {
                    "proposals": [
                        {
                            "skill_name": "test-skill",
                            "section": "Pre-flight",
                            "problem": "Missing pre-flight check before main step",
                            "proposed_change": "Add: verify config before invoking step 2.",
                            "confidence": 0.85,
                        }
                    ],
                    "lessons": [
                        {
                            "tags": "preflight,test-skill",
                            "weight": "high",
                            "content": "Always run a pre-flight before step 2.",
                            "applies_when": "When using test-skill on uninitialized environments",
                        }
                    ],
                }
            ),
            tool_calls=None,
            usage=TokenUsage(10, 20, 30),
            model="fake-bg-model",
            provider="fake",
            finish_reason="stop",
        )
    ]

    from core.snooze_reflect import run_for_session

    stats = await run_for_session(sid)

    assert stats["proposals_saved"] == 1
    assert stats["lessons_saved"] == 1

    from db import models as db

    proposals = db.list_skill_proposals(skill_name="test-skill")
    assert len(proposals) == 1
    p = proposals[0]
    assert p["source_origin"] == "session"
    assert p["session_id"] == sid
    assert p["workflow_name"] is None
    assert p["confidence"] == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# run_for_session — env failure: lessons only, no proposal
# ---------------------------------------------------------------------------


async def test_run_for_session_env_cause_lesson_only(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = _make_session_with_reflect(failure_cause="env", confidence=0.7)
    _stub_skill_registry(monkeypatch)

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                {
                    "proposals": [
                        {
                            # Even if LLM hallucinates a proposal, env causes are filtered out.
                            "skill_name": "test-skill",
                            "section": "Notes",
                            "problem": "x",
                            "proposed_change": "y",
                            "confidence": 0.9,
                        }
                    ],
                    "lessons": [
                        {
                            "tags": "rate-limit",
                            "weight": "high",
                            "content": "Back off when the upstream rate-limits.",
                            "applies_when": "When upstream rate-limits during burst calls",
                        }
                    ],
                }
            ),
            tool_calls=None,
            usage=TokenUsage(10, 20, 30),
            model="fake-bg-model",
            provider="fake",
            finish_reason="stop",
        )
    ]

    from core.snooze_reflect import run_for_session

    stats = await run_for_session(sid)

    assert stats["proposals_saved"] == 0
    assert stats["lessons_saved"] == 1


# ---------------------------------------------------------------------------
# run_for_session — non-actionable cause: skipped without LLM
# ---------------------------------------------------------------------------


async def test_run_for_session_non_actionable_cause_skipped(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = _make_session_with_reflect(failure_cause="scout", confidence=0.9)
    _stub_skill_registry(monkeypatch)

    from core.snooze_reflect import run_for_session

    stats = await run_for_session(sid)

    assert stats["proposals_saved"] == 0
    assert stats["lessons_saved"] == 0
    assert stats["skipped_reason"].startswith("non_actionable_cause")
    assert mock_llm_client.call_count == 0


# ---------------------------------------------------------------------------
# run_for_session — pass-with-deviation: lesson extracted, no proposal
# ---------------------------------------------------------------------------


async def test_run_for_session_pass_with_deviation_extracts_lesson(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    """verdict=pass + non-empty strategy/diagnostic/what_failed should still
    flow into lesson extraction (failure_cause stays 'none', so proposals
    are skipped — only lessons land)."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = _make_session_with_reflect(
        failure_cause="none",
        confidence=0.9,
        verdict="pass",
        strategy="Abort the re-schedule logic; call get_worker_result first",
        diagnostic="agent skipped scout's plan and ran a new worker instead",
        what_failed="ignored Scout's get_worker_result step",
    )
    _stub_skill_registry(monkeypatch)

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                {
                    "proposals": [],
                    "lessons": [
                        {
                            "tags": "scout-deviation,worker",
                            "weight": "normal",
                            "content": "When Scout names a specific get_worker_result call, "
                            "call it before re-scheduling — re-running the worker "
                            "wastes time and orphans the prior result.",
                            "applies_when": "When Scout's plan starts with retrieving a prior worker output",
                        }
                    ],
                }
            ),
            tool_calls=None,
            usage=TokenUsage(10, 20, 30),
            model="fake-bg-model",
            provider="fake",
            finish_reason="stop",
        )
    ]

    from core.snooze_reflect import run_for_session

    stats = await run_for_session(sid)

    assert stats["skipped_reason"] is None
    assert stats["proposals_saved"] == 0
    assert stats["lessons_saved"] == 1
    assert mock_llm_client.call_count == 1


async def test_run_for_session_pass_no_deviation_skipped(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    """verdict=pass with all retry-shaped fields empty should still skip —
    a clean pass has nothing to extract."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = _make_session_with_reflect(
        failure_cause="none",
        confidence=0.9,
        verdict="pass",
        strategy="",
        diagnostic="",
        what_failed="",
    )
    _stub_skill_registry(monkeypatch)

    from core.snooze_reflect import run_for_session

    stats = await run_for_session(sid)

    assert stats["skipped_reason"].startswith("non_actionable_cause")
    assert mock_llm_client.call_count == 0


async def test_run_for_session_low_confidence_skipped(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = _make_session_with_reflect(failure_cause="skill", confidence=0.4)
    _stub_skill_registry(monkeypatch)

    from core.snooze_reflect import run_for_session

    stats = await run_for_session(sid)

    assert stats["skipped_reason"] == "low_reflect_confidence"
    assert mock_llm_client.call_count == 0


# ---------------------------------------------------------------------------
# run_for_session — proposal below 0.6 dropped, lesson kept
# ---------------------------------------------------------------------------


async def test_proposal_low_confidence_dropped_lesson_kept(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = _make_session_with_reflect(failure_cause="skill", confidence=0.8)
    _stub_skill_registry(monkeypatch)

    mock_llm_client.responses = [
        ChatResponse(
            content=json.dumps(
                {
                    "proposals": [
                        {
                            "skill_name": "test-skill",
                            "section": "Notes",
                            "problem": "uncertain",
                            "proposed_change": "maybe try X",
                            "confidence": 0.5,  # below floor
                        }
                    ],
                    "lessons": [
                        {
                            "tags": "x",
                            "weight": "normal",
                            "content": "Lesson body",
                            "applies_when": "When X happens",
                        }
                    ],
                }
            ),
            tool_calls=None,
            usage=TokenUsage(10, 20, 30),
            model="fake-bg-model",
            provider="fake",
            finish_reason="stop",
        )
    ]

    from core.snooze_reflect import run_for_session

    stats = await run_for_session(sid)

    assert stats["proposals_saved"] == 0
    assert stats["lessons_saved"] == 1


# ---------------------------------------------------------------------------
# run_for_session — LLM raises: no exception escapes
# ---------------------------------------------------------------------------


async def test_run_for_session_llm_error_handled(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    sid = _make_session_with_reflect(failure_cause="skill", confidence=0.8)
    _stub_skill_registry(monkeypatch)

    async def failing_chat(*args, **kwargs):
        raise ConnectionError("LLM down")

    mock_llm_client.chat = failing_chat

    from core.snooze_reflect import run_for_session

    stats = await run_for_session(sid)
    assert stats["proposals_saved"] == 0
    assert stats["lessons_saved"] == 0
    assert (stats["skipped_reason"] or "").startswith("llm_error:")


# ---------------------------------------------------------------------------
# run_for_session — worker sessions skipped
# ---------------------------------------------------------------------------


async def test_run_for_session_worker_skipped(
    mock_llm_client,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    from db import models as db

    sid = db.create_session(title="Worker", session_type="worker")
    # No reflect needed — worker check is the first short-circuit.
    from core.snooze_reflect import run_for_session

    stats = await run_for_session(sid)
    assert stats["skipped_reason"] == "worker_session"
    assert mock_llm_client.call_count == 0


# ---------------------------------------------------------------------------
# Migration v15 — schema additions
# ---------------------------------------------------------------------------


def test_v15_migration_columns_exist():
    """skill_improvement_proposals has the new columns with correct defaults."""
    from db.database import connect_sessions

    with connect_sessions() as conn:
        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(skill_improvement_proposals)").fetchall()}
    for c in ("source_origin", "session_id", "trial_uses", "trial_successes", "last_trial_at"):
        assert c in cols, f"missing column {c}"
    # workflow_name and run_id should now be nullable (notnull=0)
    assert cols["workflow_name"]["notnull"] == 0
    assert cols["run_id"]["notnull"] == 0
    # source_origin defaults to 'workflow'
    assert "workflow" in str(cols["source_origin"]["dflt_value"])
