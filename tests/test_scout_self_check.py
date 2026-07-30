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


# ---------------------------------------------------------------------------
# Disabled-recommendation handling — separate from hallucination
# ---------------------------------------------------------------------------


def test_disabled_tool_flags_issue_separate_from_hallucinated(monkeypatch, tmp_path):
    """Disabled tool recommendation flagged with a 'disabled' issue, not 'hallucinated'."""
    from core.tools.registry import ToolRegistry

    monkeypatch.setattr("core.tools.registry.TOOLS_CONFIG_PATH", tmp_path / "tools.json")
    reg = ToolRegistry()
    reg.register(name="t_off", func=lambda: "ok", description="t", parameters={"type": "object", "properties": {}})
    reg.disable("t_off")
    monkeypatch.setattr("core.tools.registry._registry", reg)

    report = ScoutReport(
        recommended_tools=["t_off"],
        approach_guidance="1. Do X.\n2. Do Y.\n3. Done.\n",
    )
    issues = _self_check_report(report)
    # Must flag as disabled — not as a hallucination
    assert any("disabled" in i.lower() and "t_off" in i for i in issues)
    assert not any("do not exist" in i.lower() and "t_off" in i for i in issues)


def test_disabled_skill_flags_issue_separate_from_hallucinated(tmp_path, monkeypatch):
    from core.skills.registry import SkillRegistry

    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    d = tmp_path / "ondisk-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: ondisk-skill\ndescription: x\ntags: t\n---\n# body\n")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    reg.disable("ondisk-skill")
    monkeypatch.setattr("core.skills.registry._skill_registry", reg)

    report = ScoutReport(
        recommended_skills=["ondisk-skill"],
        approach_guidance="1. Use the skill.\n2. Done.\n3. Wrap.\n",
    )
    issues = _self_check_report(report)
    assert any("disabled" in i.lower() and "ondisk-skill" in i for i in issues)
    assert not any("do not exist" in i.lower() for i in issues)


def test_validate_report_strips_disabled_tools(monkeypatch, tmp_path):
    """_validate_report removes disabled tools from recommendations."""
    from core.scout.runner import _validate_report
    from core.tools.registry import ToolRegistry

    monkeypatch.setattr("core.tools.registry.TOOLS_CONFIG_PATH", tmp_path / "tools.json")
    reg = ToolRegistry()
    reg.register(
        name="custom_strip_me", func=lambda: "", description="x", parameters={"type": "object", "properties": {}}
    )
    reg.disable("custom_strip_me")
    monkeypatch.setattr("core.tools.registry._registry", reg)

    report = ScoutReport(
        recommended_tools=["custom_strip_me"],
        approach_guidance="1.\n2.\n3.\n",
    )
    out = _validate_report(report)
    assert "custom_strip_me" not in out.recommended_tools


def test_validate_report_strips_disabled_skills(tmp_path, monkeypatch):
    """A disabled skill in recommended_skills must be stripped, AND any
    pre-set auto-injection (injected_skill_name / injected_skill) must be
    cleared — otherwise a stale L2 body for the disabled skill leaks into
    the agent's system prompt. The pre-set values here are load-bearing:
    if we leave them at the dataclass defaults of "", the assertions are
    trivially true and never exercise the clearing path.
    """
    from core.scout.runner import _validate_report
    from core.skills.registry import SkillRegistry

    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    d = tmp_path / "off-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: off-skill\ndescription: x\ntags: t\n---\n# body\n")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    reg.disable("off-skill")
    monkeypatch.setattr("core.skills.registry._skill_registry", reg)

    report = ScoutReport(
        recommended_skills=["off-skill"],
        injected_skill_name="off-skill",  # pre-set: must be cleared
        injected_skill="# stale body that must not leak\n",  # pre-set: must be cleared
        approach_guidance="1.\n2.\n3.\n",
    )
    out = _validate_report(report)
    assert "off-skill" not in out.recommended_skills
    # And the pre-set injection must be cleared (not just left as default).
    assert out.injected_skill_name == "", "stale auto-injection name must be cleared"
    assert out.injected_skill == "", "stale auto-injection body must be cleared"


def test_validate_report_clears_stale_injected_when_no_recommended_skills(tmp_path, monkeypatch):
    """Symmetric: report had no recommended_skills at all, but an auto-inject
    block was already populated. Clearing must happen even when nothing was
    stripped — it's purely about not letting stale state through.
    """
    from core.scout.report import ScoutReport
    from core.scout.runner import _validate_report

    report = ScoutReport(
        recommended_skills=[],
        injected_skill_name="ghost-skill",
        injected_skill="# old body that should be cleared\nstuff\n",
        approach_guidance="1.\n2.\n3.\n",
    )
    out = _validate_report(report)
    assert out.injected_skill_name == ""
    assert out.injected_skill == ""


# ---------------------------------------------------------------------------
# Scout baseline build — discover() must filter disabled before the
# AVAILABLE TOOLS / AVAILABLE SKILLS section is built (lines ~1227 / 1243
# in core/scout/runner.py). Auto-fixed by the registry-side filter; locked
# in here so a future regression in registry.discover() can't silently
# put disabled items back into scout's prompt.
# ---------------------------------------------------------------------------


def test_scout_baseline_skill_discovery_omits_disabled(tmp_path, monkeypatch):
    from core.skills.registry import SkillRegistry

    skills_dir = tmp_path
    for n, desc in (("aboard", "deploy release ship"), ("bboard", "deploy release ship")):
        d = skills_dir / n
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {n}\ndescription: {desc}\ntags: deploy\n---\n# x\n")

    fresh = SkillRegistry()
    fresh.scan(skills_dir)
    fresh.disable("aboard")
    monkeypatch.setattr("core.skills.registry._skill_registry", fresh)

    discovered = fresh.discover("deploy release", limit=10)
    names = {s.name for s in discovered}
    assert "bboard" in names
    assert "aboard" not in names, "scout baseline must not surface disabled skill in AVAILABLE SKILLS"


def test_scout_baseline_tool_discovery_omits_disabled(tmp_path, monkeypatch):
    """Symmetric for tools: ToolRegistry.discover() must filter disabled.
    A disabled tool's name must NOT appear in scout's AVAILABLE TOOLS section.
    """
    from core.tools.registry import ToolRegistry

    monkeypatch.setattr("core.tools.registry.TOOLS_CONFIG_PATH", tmp_path / "tools.json")
    reg = ToolRegistry()
    reg.register(
        name="search_one",
        func=lambda: "",
        description="search the web",
        parameters={},
        tags=["search", "web"],
    )
    reg.register(
        name="search_two",
        func=lambda: "",
        description="search the web",
        parameters={},
        tags=["search", "web"],
    )
    reg.rebuild_index()
    reg.disable("search_one")
    monkeypatch.setattr("core.tools.registry._registry", reg)

    discovered = reg.discover("search the web", limit=10)
    names = {t.name for t in discovered}
    assert "search_two" in names
    assert "search_one" not in names


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


# ---------------------------------------------------------------------------
# Revision economics: only spend a scout round on what scout alone can fix
# ---------------------------------------------------------------------------


def test_hallucinated_tool_is_not_a_blocking_issue():
    """_validate_report strips unknown tool names, so demanding a revision for
    one trades a usable report for a round scout may not survive.

    Real failure (session bcaec717d1da): scout listed "node --check" as a tool,
    the self-check rejected an otherwise-complete report, and the forced
    resubmit landed on the tools-disabled final round and produced nothing.
    """
    from core.scout.runner import _sanitizable_issues, _unfixable_issues

    report = ScoutReport(
        recommended_tools=["totally_fake_tool_name_xyz"],
        approach_guidance="1. Read the file.\n2. Grep for the symbol.\n3. Write the summary.\n",
    )
    assert _unfixable_issues(report) == []
    assert any("totally_fake_tool_name_xyz" in i for i in _sanitizable_issues(report))


def test_empty_approach_guidance_is_blocking():
    """Nothing downstream can write a plan scout didn't — this one is worth a round."""
    from core.scout.runner import _unfixable_issues

    assert any("approach_guidance" in i for i in _unfixable_issues(ScoutReport(approach_guidance="")))


def test_blank_deliverable_description_is_blocking():
    from core.scout.runner import _unfixable_issues

    report = ScoutReport(
        approach_guidance="1. Do X.\n2. Do Y.\n3. Finish up.\n",
        deliverables_plan=[DeliverableSpec(description="")],
    )
    assert any("empty descriptions" in i for i in _unfixable_issues(report))


def test_self_check_still_reports_both_classes():
    """The flat self-check keeps its contract — it feeds viability notes."""
    report = ScoutReport(approach_guidance="", recommended_tools=["totally_fake_tool_name_xyz"])
    issues = _self_check_report(report)
    assert any("approach_guidance" in i for i in issues)
    assert any("totally_fake_tool_name_xyz" in i for i in issues)


# ---------------------------------------------------------------------------
# Last round must still be able to deliver
# ---------------------------------------------------------------------------


def test_last_round_still_offers_submit_report():
    """The penultimate round orders scout to submit, and a revision request
    lands on the final round by construction. Removing every tool there made
    that instruction impossible to follow — 17 revisions, 0 second submits.
    """
    from core.scout.runner import _SCOUT_SUBMIT_ONLY

    names = [t["function"]["name"] for t in _SCOUT_SUBMIT_ONLY]
    assert names == ["submit_report"]


def test_revision_on_penultimate_round_can_still_be_honored():
    """Guard the arithmetic: a revision granted at round N must leave a round
    that can call submit_report.
    """
    from core.scout.runner import _SCOUT_SUBMIT_ONLY, SCOUT_MAX_ROUNDS

    last_round_with_revision_slot = SCOUT_MAX_ROUNDS - 2  # rounds_remaining == 1
    next_round = last_round_with_revision_slot + 1
    assert next_round == SCOUT_MAX_ROUNDS - 1, "revision lands on the final round"
    assert _SCOUT_SUBMIT_ONLY, "and that round must still offer submit_report"
