"""The terminal tool checked the command it opened against the bash policy and
nothing after that. Opening `bash` is ordinary and admitted; every line then
typed into that shell went straight to the pty, so a command the bash tool
refuses (`sudo`, `dd`, `rm -rf` outside the workspace) could be run by typing
it instead. A third launcher had become the way around the policy of the
first two.

Each line a `send` submits now passes the same denylist scan bash uses. The
unit is the submitted line, not the call, so a word split over two sends or
a multi-line raw paste is still seen. A refused call types nothing at all and
leaves the terminal open. The refused commands below are harmless even if the
guard failed (`dd --version`), and each one would leave a marker file behind,
which is how the tests know nothing ran.
"""

import sys

import pytest

from core.tools.builtin import terminal_tool
from db import models as db

SHELL = "bash --norc --noprofile -i"
REFUSED = "Error: Command blocked by security policy: 'dd' is in the command denylist"
NOT_SENT = "The input was NOT sent: nothing from this call was typed into the terminal."


@pytest.fixture
def term(monkeypatch):
    monkeypatch.setattr("config.settings.shell_security_mode", "permissive")
    sid = db.create_session(title="terminal policy test")
    ctx = {"session_id": sid}

    def call(**kwargs):
        return terminal_tool.terminal(_context=ctx, **kwargs)

    yield call
    terminal_tool.close_all()


def _has_line(out: str, text: str) -> bool:
    """The program printed `text` as a line of its own (not just the echo of what was typed)."""
    return text in out.split("\n")


def _refused_line(marker) -> str:
    return f"dd --version; touch {marker}"


def test_a_denylisted_line_typed_into_a_shell_is_refused_and_never_typed(term, tmp_path):
    marker = tmp_path / "ran"
    term(action="open", command=SHELL, cols=100, rows=12)
    out = term(action="send", input=_refused_line(marker) + "<enter>")
    assert out.startswith(REFUSED) and out.endswith(NOT_SENT), out
    screen = term(action="screen", wait_seconds=1)
    assert "touch" not in screen and "--version" not in screen
    assert "running pid" in screen and not marker.exists()
    # The terminal is still there and still works.
    assert _has_line(term(action="send", input="echo still-$((40 + 2))<enter>"), "still-42")


def test_a_line_split_across_two_sends_is_judged_whole(term, tmp_path):
    marker = tmp_path / "ran"
    term(action="open", command=SHELL, cols=100, rows=12)
    term(action="send", input="d")  # half a word, not submitted: typed, not judged
    out = term(action="send", input=f"d --version; touch {marker}<enter>")
    assert out.startswith(REFUSED) and NOT_SENT in out
    assert not marker.exists()
    assert _has_line(term(action="send", input="<ctrl-c><wait:0.5>echo split-$((40 + 2))<enter>"), "split-42")


def test_newlines_inside_raw_input_submit_lines_too(term, tmp_path):
    marker = tmp_path / "ran"
    term(action="open", command=SHELL, cols=100, rows=12)
    out = term(action="send", input=f"echo first-raw-line\n{_refused_line(marker)}\n", raw=True)
    assert out.startswith(REFUSED) and NOT_SENT in out
    # All or nothing: the allowed first line was not typed either.
    assert "first-raw-line" not in term(action="screen", wait_seconds=1)
    assert not marker.exists()


def test_an_allowed_line_before_a_refused_one_in_the_same_call_is_not_typed(term, tmp_path):
    marker = tmp_path / "ran"
    term(action="open", command=SHELL, cols=100, rows=12)
    out = term(action="send", input=f"echo seen-one<enter>{_refused_line(marker)}<enter>")
    assert out.startswith(REFUSED)
    assert "seen-one" not in term(action="screen", wait_seconds=1)
    assert not marker.exists()


def test_a_refused_call_leaves_the_pending_line_as_it_was(term, tmp_path):
    marker = tmp_path / "ran"
    term(action="open", command=SHELL, cols=100, rows=12)
    term(action="send", input="echo keep-")
    assert term(action="send", input=f"x<enter>{_refused_line(marker)}<enter>").startswith(REFUSED)
    # Had the refused call typed its "x", the shell would print keep-x42.
    assert _has_line(term(action="send", input="$((40 + 2))<enter>"), "keep-42")


def test_text_that_is_typed_but_not_submitted_is_not_judged(term, tmp_path):
    marker = tmp_path / "ran"
    term(action="open", command=SHELL, cols=100, rows=12)
    out = term(action="send", input=_refused_line(marker))
    assert not out.startswith("Error") and "--version" in out  # on the line, not run
    out = term(action="send", input="<ctrl-c><wait:0.5>echo abandoned-$((40 + 2))<enter>")
    assert _has_line(out, "abandoned-42") and not marker.exists()


@pytest.mark.parametrize("mode", ["permissive", "strict"])
def test_ordinary_repl_input_is_not_a_command_word(term, monkeypatch, mode):
    term(action="open", command=f"{sys.executable} -q", cols=80, rows=12, wait_for=r">>> $", wait_seconds=20)
    monkeypatch.setattr("config.settings.shell_security_mode", mode)
    monkeypatch.setattr("config.settings.shell_allowlist", ["echo"])  # strict: print is not on it
    out = term(action="send", input="print(6 * 7)<enter>", wait_for=r">>> $", wait_seconds=20)
    assert _has_line(out, "42"), out


def test_strict_mode_still_scans_typed_lines_with_the_denylist(term, monkeypatch, tmp_path):
    marker = tmp_path / "ran"
    monkeypatch.setattr("config.settings.shell_security_mode", "strict")
    monkeypatch.setattr("config.settings.shell_allowlist", ["bash"])
    term(action="open", command=SHELL, cols=100, rows=12)
    assert term(action="send", input=_refused_line(marker) + "<enter>").startswith(REFUSED)
    assert not marker.exists()


def test_a_mode_where_bash_applies_no_policy_scans_nothing_here_either(term, monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.shell_security_mode", "off")
    term(action="open", command=SHELL, cols=100, rows=12)
    out = term(action="send", input="dd --version<enter>")
    assert not out.startswith("Error") and "coreutils" in out


def test_backspace_edits_the_buffered_line(term, tmp_path):
    marker = tmp_path / "ran"
    term(action="open", command=SHELL, cols=100, rows=12)
    # "dd" with the second d erased is "d", then "f -h": the shell runs df.
    out = term(action="send", input="dd<backspace>f -h .<enter>")
    assert not out.startswith("Error") and "Filesystem" in out
    # An erased stray letter does not hide the word it sat in.
    out = term(action="send", input=f"ddX<backspace> --version; touch {marker}<enter>")
    assert out.startswith(REFUSED) and not marker.exists()


@pytest.mark.parametrize("cancel", ["<ctrl-c>", "<ctrl-u>"])
def test_cancelling_a_line_clears_it(term, cancel):
    term(action="open", command=SHELL, cols=100, rows=12)
    # Without the clear the buffer would read "sudo echo ..." and be refused.
    # The pause is for the shell: a tty discards input queued behind ctrl-c.
    out = term(action="send", input=f"sudo {cancel}<wait:0.5>echo cancel-$((40 + 2))<enter>")
    assert not out.startswith("Error") and _has_line(out, "cancel-42"), out


def test_a_key_the_buffer_cannot_follow_does_not_hide_a_refused_word(term, tmp_path):
    marker = tmp_path / "ran"
    term(action="open", command=SHELL, cols=100, rows=12)
    out = term(action="send", input=f"dd<left><right> --version; touch {marker}<enter>")
    assert out.startswith(REFUSED) and not marker.exists()
    out = term(action="send", input=f"echo harmless <tab>; sudo -n true; touch {marker}<enter>")
    assert out.startswith("Error: Command blocked by security policy: 'sudo'") and not marker.exists()
