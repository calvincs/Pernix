"""A pure bash loop never tripped the stuck detector (field case c021aa9b0c79).

The agent ran one identical `python - <<EOF ... EOF` transcript-slicing command
16 times in a row (2026-10-14, 15:38-15:41 UTC), got the same 4,514-char output
every time, and no signal fired. The prompt grew from 53k to 80k tokens before
the model broke out on its own.

Cause: since 98207808 every successful bash call bumped the mutation epoch, so
Signal 2 could never match an earlier bash signature. bash is also
idempotent=False, so the cross-round dedup cache never answered it either.
Signal 1 did not help: tool-only rounds have empty content.

Now a bash call bumps the epoch only when its output differs from the last run
of the same command. A rerun after a `sed -i` edit still changes the output,
so edit→rerun iteration is still not a cycle.
"""

from __future__ import annotations

import json

from core.agent import StuckDetector


class _Registry:
    def exists(self, name):
        return True


def _bash(command: str) -> list[dict]:
    return [{"name": "bash", "arguments": json.dumps({"command": command})}]


def _round(stuck: StuckDetector, command: str, output: str) -> float:
    """One agent round as the loop runs it: evaluate, execute, mark_success."""
    score, _ = stuck.evaluate("", _bash(command), {}, _Registry())
    stuck.mark_success(tool_name="bash", args={"command": command}, content=output)
    return score


def test_identical_command_and_output_trips_signal_two():
    stuck = StuckDetector()
    cmd = "python - <<'EOF'\nprint(segments[0:300:5])\nEOF"
    out = "[cwd: data/workspace] [exit: 0]\n[0m0s] Different species of aliens ..."

    assert _round(stuck, cmd, out) == 0.0  # first run: new output, epoch bumps
    assert _round(stuck, cmd, out) == 0.0  # second run: history holds the pre-bump epoch
    assert _round(stuck, cmd, out) >= 0.4  # third run: nothing changed since the second
    assert "tool_cycle" in stuck.behavioral_flags


def test_the_field_loop_reaches_the_stuck_threshold():
    """16 identical rounds must push repeat_count to the handler's limit (3)."""
    stuck = StuckDetector()
    cmd = "python - <<'EOF'\nprint(segments[0:300:5])\nEOF"
    for _ in range(16):
        _round(stuck, cmd, "same output")
    assert stuck.repeat_count >= 3


def test_sed_edit_then_rerun_is_still_not_a_cycle():
    """The 3dc5a307d751 shape, now with real outputs: the rerun's output changed."""
    stuck = StuckDetector()
    _round(stuck, "python3 solve.py", "IndexError: list index out of range")
    _round(stuck, "sed -i 's/n=10/n=200/' solve.py", "")
    score = _round(stuck, "python3 solve.py", "answer: 42")

    assert score == 0.0
    assert "tool_cycle" not in stuck.behavioral_flags


def test_polling_a_changing_output_is_not_a_cycle():
    """`tail log` every round is fine while the log is still moving."""
    stuck = StuckDetector()
    for i in range(6):
        score = _round(stuck, "tail -3 transcribe.log", f"segment {i}")
        assert score == 0.0
    assert "tool_cycle" not in stuck.behavioral_flags


def test_unknown_output_keeps_the_old_behaviour():
    """Callers that pass no content still bump the epoch on every bash success."""
    stuck = StuckDetector()
    stuck.evaluate("", _bash("ls"), {}, _Registry())
    stuck.mark_success(tool_name="bash", args={"command": "ls"})
    stuck.evaluate("", _bash("ls"), {}, _Registry())
    stuck.mark_success(tool_name="bash", args={"command": "ls"})
    score, _ = stuck.evaluate("", _bash("ls"), {}, _Registry())
    assert score == 0.0
