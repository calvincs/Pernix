"""Tests for scout agent pattern."""

from core.scout.report import ScoutReport, SessionBrief
from core.scout.runner import (
    _build_fallback_report,
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
    assert "[IDENTITY]" in section
    assert "[RULES]" in section
    assert "[RELEVANT MEMORY]" in section
    assert "[APPROACH]" in section


def test_bypass_logic():
    assert should_bypass_scout("ok", turn_count=5) is True
    assert should_bypass_scout("/help", turn_count=0) is True
    assert should_bypass_scout("## Evaluation Feedback", turn_count=0) is True
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
