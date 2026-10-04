"""Agent Mesh build, 2026-09-08: the last turn of session b72959f8407d ran
pytest twice and got "155 passed" both times. Both results sat in the graded
slice. Reflect returned `retry` anyway, on the grounds that "the TEST
EXECUTION SUMMARY records zero pytest calls", and raised a high-urgency
notification telling the user the build was unverified.

The TOOL EXECUTION SUMMARY lists tool names and counts, so a program run
inside `bash` has no row of its own — the grader read that absence as proof
the program never ran. Commands now get their own evidence section, and the
prompt says absence from the summary is not evidence of absence.
"""

from __future__ import annotations

import json

from core.reflect import _commands_run_section


def _assistant(call_id: str, name: str, args: dict):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": json.dumps([{"id": call_id, "name": name, "arguments": json.dumps(args)}]),
    }


def _tool(call_id: str, content: str):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


# ── the field case ───────────────────────────────────────────────────────────


def test_a_pytest_run_inside_bash_is_visible_to_the_grader():
    msgs = [
        _assistant("c1", "bash", {"command": "cd impl && python -m pytest -q 2>&1 | tail -6"}),
        _tool("c1", "warnings summary\n\n======= 155 passed, 1 warning in 1.19s ======="),
    ]
    section = _commands_run_section(msgs)
    assert "pytest" in section
    assert "155 passed" in section


def test_the_section_is_labelled_as_observed_fact():
    msgs = [_assistant("c1", "bash", {"command": "ls"}), _tool("c1", "a\nb")]
    assert "COMMANDS RUN THIS ATTEMPT" in _commands_run_section(msgs)
    assert "observed facts" in _commands_run_section(msgs)


# ── shape ────────────────────────────────────────────────────────────────────


def test_repl_code_is_captured_too():
    msgs = [
        _assistant("c1", "repl", {"code": "import subprocess; subprocess.run(['pytest'])"}),
        _tool("c1", "155 passed"),
    ]
    assert "subprocess" in _commands_run_section(msgs)


def test_non_shell_tools_are_left_to_the_tool_summary():
    msgs = [_assistant("c1", "file_read", {"path": "TASKS.md"}), _tool("c1", "contents")]
    assert _commands_run_section(msgs) == ""


def test_a_command_with_no_result_still_shows_the_command():
    section = _commands_run_section([_assistant("c1", "bash", {"command": "pytest -q"})])
    assert "pytest -q" in section


def test_no_commands_means_no_section():
    assert _commands_run_section([{"role": "user", "content": "hi"}]) == ""


def test_malformed_tool_calls_do_not_raise():
    assert _commands_run_section([{"role": "assistant", "tool_calls": "not json"}]) == ""


def test_long_output_is_reduced_to_its_last_line():
    """For a test run the summary line IS the result."""
    tail = "\n".join(f"line {i}" for i in range(200)) + "\n155 passed in 1.2s"
    section = _commands_run_section([_assistant("c1", "bash", {"command": "pytest"}), _tool("c1", tail)])
    assert "155 passed in 1.2s" in section
    assert "line 3" not in section


# ── the rule that stops the wrong inference ──────────────────────────────────


def test_the_prompt_forbids_reading_absence_as_proof():
    import core.reflect as reflect

    assert "NEVER evidence that it did not run" in reflect.REFLECT_PROMPT
    assert "COMMANDS RUN THIS ATTEMPT" in reflect.REFLECT_PROMPT
