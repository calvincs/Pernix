"""The stuck detector called honest edit→rerun iteration a loop (field case
c93232a0521b, R11L solver worker).

Signal 2 hashes a round's tool calls and flags a repeat if the same hash
appears in the last 10 rounds. But "fix the script, rerun the same command
line" produces an identical bash args hash on purpose — the CHANGE is in the
file, not the command. The worker got four "You are repeating tool calls
(bash)" nudges across an edit→rerun solver workflow with zero failed calls
(and session ae952f40e3d1 got one more). Signal 2 now records a mutation
epoch with each signature: an identical call is a cycle only when no
file_write/file_edit/multiedit/repl succeeded in between.
"""

from core.agent import StuckDetector


class _Registry:
    def exists(self, name):
        return True


def _bash(cmd):
    return [{"name": "bash", "arguments": '{"command": "%s"}' % cmd}]


def test_rerun_after_edit_is_not_a_cycle():
    d = StuckDetector()
    d.evaluate("run it", _bash("python solve.py"), {}, _Registry())
    d.mark_success(tool_name="file_edit", args={"path": "solve.py"})
    score, _ = d.evaluate("run again", _bash("python solve.py"), {}, _Registry())
    assert "tool_cycle" not in d.behavioral_flags
    assert score < 0.4


def test_rerun_after_repl_mutation_is_not_a_cycle():
    d = StuckDetector()
    d.evaluate("run", _bash("python solve.py"), {}, _Registry())
    d.mark_success(tool_name="repl", args={"code": "x=1"})
    d.evaluate("run", _bash("python solve.py"), {}, _Registry())
    assert "tool_cycle" not in d.behavioral_flags


def test_verbatim_rerun_with_no_change_is_still_a_cycle():
    d = StuckDetector()
    d.evaluate("run", _bash("python solve.py"), {}, _Registry())
    # a non-mutating success (recall) must not launder the repeat
    d.mark_success(tool_name="recall", args={})
    score, _ = d.evaluate("run", _bash("python solve.py"), {}, _Registry())
    assert "tool_cycle" in d.behavioral_flags
    assert score >= 0.4
