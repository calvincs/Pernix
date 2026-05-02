"""Phase 2a scout self-validation: _self_check_report and viability flow."""

from unittest.mock import patch

from core.scout.report import DeliverableSpec, ScoutReport
from core.scout.runner import _self_check_report

# ---------------------------------------------------------------------------
# Positive path
# ---------------------------------------------------------------------------


def test_valid_report_returns_no_issues():
    # Register a couple of tools so the registry check passes.
    from core.tools.registry import get_registry

    reg = get_registry()
    real_tools = [t.name for t in reg.enabled_tools()][:3]

    report = ScoutReport(
        recommended_tools=real_tools,
        approach_guidance="1. Read file X.\n2. Grep for pattern Y.\n3. Write summary.\n",
    )
    assert _self_check_report(report) == []


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def test_empty_approach_guidance_flags_issue():
    issues = _self_check_report(ScoutReport(approach_guidance=""))
    assert any("approach_guidance" in i for i in issues)


def test_short_approach_guidance_flags_issue():
    issues = _self_check_report(ScoutReport(approach_guidance="do it"))
    assert any("approach_guidance" in i for i in issues)


def test_hallucinated_tool_flags_issue():
    report = ScoutReport(
        recommended_tools=["totally_fake_tool_name_xyz"],
        approach_guidance="1. Do X.\n2. Do Y with that tool.\n3. Done.\n",
    )
    issues = _self_check_report(report)
    assert any("totally_fake_tool_name_xyz" in i for i in issues)


def test_hallucinated_skill_flags_issue():
    report = ScoutReport(
        recommended_skills=["nonexistent_skill_123"],
        approach_guidance="1. Load skill.\n2. Follow instructions.\n3. Done.\n",
    )
    issues = _self_check_report(report)
    assert any("nonexistent_skill_123" in i for i in issues)


def test_unknown_recommended_model_flags_issue():
    # Patch the module-level known_model_ids set to a non-empty value so the check runs.
    import core.scout.runner as runner

    with patch.object(runner, "_known_model_ids", {"known/model-1", "known/model-2"}):
        issues = _self_check_report(
            ScoutReport(
                recommended_model="not/a-real-model",
                approach_guidance="1. Step.\n2. Step.\n3. Step.\n",
            )
        )
    assert any("not/a-real-model" in i for i in issues)


def test_known_recommended_model_passes():
    import core.scout.runner as runner

    with patch.object(runner, "_known_model_ids", {"known/model-1"}):
        issues = _self_check_report(
            ScoutReport(
                recommended_model="known/model-1",
                approach_guidance="1. Use model.\n2. Finish.\n3. Done.\n",
            )
        )
    assert not any("recommended_model" in i for i in issues)


def test_blank_deliverable_description_flags_issue():
    report = ScoutReport(
        approach_guidance="1. Do X.\n2. Do Y.\n3. Finish.\n",
        deliverables_plan=[
            DeliverableSpec(description="Write file.md"),
            DeliverableSpec(description=""),  # blank
        ],
    )
    issues = _self_check_report(report)
    assert any("empty descriptions" in i for i in issues)


def test_revision_budget_constants():
    """Item #5: scout gets up to 2 revision rounds, SCOUT_MAX_ROUNDS=6.

    Round budget accounts for: 1-3 discovery, 4 submit, 5-6 revisions.
    """
    from core.scout import runner

    assert runner._MAX_REVISIONS == 2
    assert runner.SCOUT_MAX_ROUNDS == 6
    # Must have at least 1 non-revision round in addition to the revisions.
    assert runner.SCOUT_MAX_ROUNDS > runner._MAX_REVISIONS


def test_extract_report_clamps_workers_mode_to_inline():
    """Legacy/deprecated 'workers' value in the submit payload must be
    clamped to 'inline'. The enum no longer includes workers (deferred
    until a real auto-spawn implementation lands)."""
    from core.scout.runner import _extract_report

    report = _extract_report(
        {
            "approach_guidance": "1. Step.\n2. Step.\n3. Step.\n",
            "execution_mode": "workers",
            "deliverables_plan": [
                {"description": "Task A"},
                {"description": "Task B"},
            ],
        }
    )
    assert report.execution_mode == "inline"


# ---------------------------------------------------------------------------
# ScoutReport extract_report handles new fields
# ---------------------------------------------------------------------------


def test_extract_report_parses_deliverables():
    from core.scout.runner import _extract_report

    r = _extract_report(
        {
            "recommended_tools": [],
            "approach_guidance": "steps",
            "deliverables_plan": [
                {"description": "Write docs", "execution_hint": "inline"},
                {"description": "Run tests", "execution_hint": "task"},
                "Plain string deliverable",  # tolerated
            ],
            "execution_mode": "tasks",
        }
    )
    assert len(r.deliverables_plan) == 3
    assert r.deliverables_plan[0].description == "Write docs"
    assert r.deliverables_plan[1].execution_hint == "task"
    assert r.deliverables_plan[2].description == "Plain string deliverable"
    assert r.execution_mode == "tasks"


def test_extract_report_clamps_bad_execution_mode():
    from core.scout.runner import _extract_report

    r = _extract_report(
        {
            "recommended_tools": [],
            "approach_guidance": "steps",
            "execution_mode": "magical",
        }
    )
    assert r.execution_mode == "inline"


def test_extract_report_clamps_bad_execution_hint():
    from core.scout.runner import _extract_report

    r = _extract_report(
        {
            "recommended_tools": [],
            "approach_guidance": "steps",
            "deliverables_plan": [{"description": "X", "execution_hint": "telepathy"}],
        }
    )
    assert r.deliverables_plan[0].execution_hint == "inline"


# ---------------------------------------------------------------------------
# Report prompt rendering
# ---------------------------------------------------------------------------


def test_report_prompt_includes_deliverables_section():
    report = ScoutReport(
        approach_guidance="do stuff",
        deliverables_plan=[DeliverableSpec(description="Write A"), DeliverableSpec(description="Write B")],
        execution_mode="tasks",
    )
    text = report.to_system_prompt_section()
    assert "[DELIVERABLES" in text
    assert "tasks" in text
    assert "Write A" in text
    assert "Write B" in text


def test_report_prompt_includes_viability_notice_when_unverified():
    report = ScoutReport(
        approach_guidance="do stuff",
        viability="unverified",
        viability_notes=["tool 'x' missing", "approach too short"],
    )
    text = report.to_system_prompt_section()
    assert "[SCOUT NOTICE]" in text
    assert "tool 'x' missing" in text


def test_report_prompt_omits_viability_notice_when_verified():
    report = ScoutReport(
        approach_guidance="do stuff",
        viability="verified",
    )
    text = report.to_system_prompt_section()
    assert "[SCOUT NOTICE]" not in text


# ---------------------------------------------------------------------------
# Revision-request format reminds of tool-call requirement (#6 audit)
# ---------------------------------------------------------------------------


def test_revision_request_explicitly_forbids_prose():
    """When scout's submitted report is rejected (e.g. unknown model id),
    the revision request must explicitly say 'do not reply with prose'.
    Real failure: scout responded "I'll fix the model ID..." in prose,
    which broke parsing and triggered a full scout retry from scratch.
    """
    from core.scout.runner import _format_revision_request

    text = _format_revision_request(["recommended_model 'foo' is not in AVAILABLE MODELS."])
    assert "submit_report" in text
    assert "prose" in text.lower(), "revision request should warn against prose responses"
