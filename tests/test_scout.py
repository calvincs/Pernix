"""Tests for scout agent pattern."""

from core.scout.report import ScoutReport, SessionBrief
from core.scout.runner import (
    _build_fallback_report,
    _is_degenerate_report,
    _parse_scout_response,
    _validate_report,
    should_bypass_scout,
)


def test_session_brief():
    brief = SessionBrief(
        session_id="abc123",
        title="Test Session",
        turn_count=3,
        tools_used_recently=["bash", "file_write"],
    )
    text = brief.to_prompt_text()
    assert "Test Session" in text
    assert "bash" in text


def test_scout_report_to_prompt():
    report = ScoutReport(
        identity="Be helpful",
        rules="Test code",
        memory_context="Found: FTS5 works",
        recommended_tools=["bash", "file_write"],
        approach_guidance="Start with research",
    )
    section = report.to_system_prompt_section()
    # identity/rules render via the compiler's directives block now — never here.
    assert "[IDENTITY]" not in section
    assert "[RULES]" not in section
    assert "[RELEVANT MEMORY]" in section
    assert "[APPROACH]" in section


def test_bypass_logic():
    assert should_bypass_scout("ok", turn_count=5) is True
    assert should_bypass_scout("/help", turn_count=0) is True
    # An eval retry no longer re-prompts with "## Evaluation Feedback" as the
    # message — the feedback rides the retry directive on the scout report, so
    # the bypass branch that matched that prefix was removed with the design.
    assert should_bypass_scout("## Evaluation Feedback", turn_count=0) is False
    assert should_bypass_scout("[Context was reset...", turn_count=0) is True
    assert should_bypass_scout("Build me a todo app", turn_count=0) is False
    assert should_bypass_scout("Search the web for Python docs", turn_count=0) is False


def test_bypass_conversational_followups():
    """Conversational confirmations in active sessions skip scout — the
    prior turn's scout already mapped the task."""
    assert should_bypass_scout("yes please go ahead and do that", turn_count=3) is True
    assert should_bypass_scout("ok sounds good, continue", turn_count=2) is True
    assert should_bypass_scout("thanks, that looks perfect", turn_count=4) is True
    # First turn never gets the conversational bypass
    assert should_bypass_scout("yes please go ahead and do that", turn_count=0) is False
    # A URL means new work even with a conversational opener
    assert should_bypass_scout("ok now fetch https://example.com/data", turn_count=3) is False
    # Non-conversational short tasks still scout
    assert should_bypass_scout("delete all my cron jobs now", turn_count=3) is False


def test_parse_valid_json():
    report = _parse_scout_response('{"recommended_tools": ["bash"], "approach_guidance": "test"}')
    assert "bash" in report.recommended_tools
    assert report.approach_guidance == "test"


def test_parse_json_with_fences():
    text = '```json\n{"recommended_tools": ["file_write"]}\n```'
    report = _parse_scout_response(text)
    assert "file_write" in report.recommended_tools


def test_parse_json_embedded_in_reasoning():
    text = """Thinking about what tools are needed...
After analysis, here is my response:
{"recommended_tools": ["bash", "file_read"], "tool_rationale": "needs shell access"}
That should cover it."""
    report = _parse_scout_response(text)
    assert "bash" in report.recommended_tools


def test_parse_json_with_trailing_comma():
    text = '{"recommended_tools": ["bash", "file_read",], "rules": "test",}'
    report = _parse_scout_response(text)
    assert len(report.recommended_tools) == 2


def test_parse_empty_response():
    report = _parse_scout_response("")
    assert report.recommended_tools == []


def test_validate_strips_hallucinated_tools():
    from core.tools.builtin import load_builtin_tools
    from core.tools.registry import get_registry

    reg = get_registry()
    load_builtin_tools(reg)
    reg.rebuild_index()

    report = ScoutReport(recommended_tools=["bash", "file_read", "NONEXISTENT_TOOL", "fake_tool"])
    validated = _validate_report(report)
    assert "NONEXISTENT_TOOL" not in validated.recommended_tools
    assert "fake_tool" not in validated.recommended_tools
    assert "bash" in validated.recommended_tools
    # Core minimum always present
    assert "discover_tools" in validated.recommended_tools
    assert "recall" in validated.recommended_tools


def test_fallback_report():
    brief = SessionBrief(
        session_id="test",
        tools_used_recently=["bash", "file_read"],
    )
    report = _build_fallback_report("Build something", brief)
    assert report.from_fallback is True
    assert len(report.recommended_tools) >= 6
    # Recently used tools should be included
    assert "bash" in report.recommended_tools


def test_fallback_includes_grep():
    brief = SessionBrief(session_id="test")
    report = _build_fallback_report("Search files for config", brief)
    assert "grep" in report.recommended_tools


def test_fallback_drops_stale_recent_tools():
    """Recently-used names read back from history may no longer be registered."""
    brief = SessionBrief(session_id="test", tools_used_recently=["bash", "tool_that_was_removed"])
    report = _build_fallback_report("Do the thing", brief)
    assert "tool_that_was_removed" not in report.recommended_tools
    assert "bash" in report.recommended_tools


def test_fallback_offers_registered_web_tools():
    """A degraded turn must still be able to reach a page with the built-in
    browser — otherwise the agent concludes it has none and bootstraps its own.
    """
    from core.tools.registry import get_registry

    reg = get_registry()
    reg.register(
        name="browse_web",
        func=lambda url: "",
        description="stub",
        parameters={"type": "object", "properties": {}},
        category="web",
        source="extension",
    )
    reg.rebuild_index()
    try:
        report = _build_fallback_report("Open the page and check it", SessionBrief(session_id="test"))
        assert "browse_web" in report.recommended_tools
    finally:
        reg._tools.pop("browse_web", None)
        reg.rebuild_index()


def test_degenerate_report_detection():
    assert _is_degenerate_report(ScoutReport()) is True
    # from_fallback alone does not make a report degenerate — the deterministic
    # fallback carries real context.
    assert _is_degenerate_report(ScoutReport(approach_guidance="1. Read the file", from_fallback=True)) is False
    # A thin-but-real report is not degenerate either.
    assert _is_degenerate_report(ScoutReport(recommended_tools=["bash"])) is False
    # identity no longer counts as usable scout output — the compiler's
    # directives block delivers it, so scout output never carries it.
    assert _is_degenerate_report(ScoutReport(identity="You are Pernix")) is True


def test_submit_report_schema_dropped_directive_fields():
    """Scout is no longer asked to echo SOUL/RULES/SESSIONS — the compiler
    injects the files whole. 14 fields → 11 for the structured output."""
    from core.scout.runner import _SCOUT_TOOLS

    submit = next(t for t in _SCOUT_TOOLS if t["function"]["name"] == "submit_report")
    props = submit["function"]["parameters"]["properties"]
    for gone in ("identity", "rules", "instructions"):
        assert gone not in props
    assert "approach_guidance" in props and "recommended_tools" in props


def test_extract_report_ignores_echoed_directives():
    """A model that still emits the old fields must not shadow the real files."""
    from core.scout.runner import _extract_report

    report = _extract_report(
        {
            "identity": "I am a re-worded identity",
            "rules": "stale rules",
            "approach_guidance": "1. Do the thing.",
            "recommended_tools": ["bash"],
        }
    )
    assert report.identity == ""
    assert report.rules == ""
    assert report.approach_guidance == "1. Do the thing."


def test_fallback_report_leaves_directives_to_compiler(tmp_path, monkeypatch):
    """Even with the files present, the fallback no longer copies them —
    the compiler delivers them on every turn, fallback or not."""
    monkeypatch.chdir(tmp_path)
    agent_dir = tmp_path / "data" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "SOUL.md").write_text("# Identity\nBe helpful.")
    (agent_dir / "RULES.md").write_text("# Rules\nTest everything.")

    report = _build_fallback_report("Do something", SessionBrief(session_id="test"))
    assert report.identity == ""
    assert report.rules == ""
    assert report.instructions == ""


def test_fallback_report_carries_context_not_a_blank_stub():
    """Regression: an empty ScoutReport strips identity/rules/approach and
    narrows tools to CORE_MINIMUM. The fallback must be strictly richer.
    """
    report = _build_fallback_report("Play the game and debug it", SessionBrief(session_id="test"))
    assert report.approach_guidance.strip()
    assert len(report.recommended_tools) > 0
    assert not _is_degenerate_report(report)


def test_fallback_report_announces_degraded_scout():
    """The agent must be told the tool list is a default, not a curated plan."""
    report = _build_fallback_report("Build something", SessionBrief(session_id="test"))
    section = report.to_system_prompt_section()
    assert "[SCOUT STATUS]" in section
    assert "discover_tools" in section


def test_bypass_fallback_stays_quiet():
    """A deliberately bypassed turn is not a degradation — no warning."""
    report = _build_fallback_report("thanks", SessionBrief(session_id="test"), reason="bypass")
    assert report.from_fallback is True
    assert "[SCOUT STATUS]" not in report.to_system_prompt_section()


def test_extract_report_parses_used_hints():
    from core.scout.runner import _extract_report

    report = _extract_report(
        {
            "recommended_tools": ["bash"],
            "approach_guidance": "do the thing",
            "used_hints": ["[yt-dlp-403-captions-fallback]", "plain-id", 42, ""],
        }
    )
    # Brackets tolerated, non-strings dropped at count time (str() here), empties dropped.
    assert "[yt-dlp-403-captions-fallback]" in report.used_hints or "yt-dlp-403-captions-fallback" in report.used_hints
    assert "" not in report.used_hints


def test_count_hint_usage_sanitizes_and_counts_once(monkeypatch):
    """Citations are checked against LIVE hint ids and counted at the fresh-
    report seam — a fabricated id never lands a row, a real one lands one."""
    from datetime import datetime, timezone

    from core.scout.report import ScoutReport
    from core.scout.runner import _count_hint_usage
    from db import models as db

    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    now = datetime.now(timezone.utc).isoformat()
    db.adaptive_put_entry(
        {
            "id": "real-hint",
            "kind": "routing_hint",
            "scope": "global",
            "title": "real",
            "content": "prefer x",
            "risk": "low",
            "version": 1,
            "status": "active",
            "source": "refine",
            "created_at": now,
            "updated_at": now,
        }
    )
    db.delete_signal("adaptive_entry", "real-hint")
    report = ScoutReport(used_hints=["[real-hint]", "made-up-hint", "real-hint"])
    _count_hint_usage(report)
    assert report.used_hints == ["real-hint"]  # sanitized + deduped
    row = db.get_signal("adaptive_entry", "real-hint")
    assert row is not None and row["reinforcements"] == 1
    assert db.get_signal("adaptive_entry", "made-up-hint") is None
