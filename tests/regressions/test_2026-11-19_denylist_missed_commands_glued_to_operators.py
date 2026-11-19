"""The bash denylist found command words with shlex and treated `;` `&&` `||`
`|` as separators only when they stood alone as tokens. `ls; reboot` was
refused, but `ls;reboot`, `ls&&reboot`, `ls & reboot`, a newline, a subshell,
a `{ ...; }` group, `$(...)`, backticks, `if ...; then reboot; fi`, `eval`,
`command`, `builtin`, and quoted or escaped command words (`'reboot'`,
`u''mount`, `r\\eboot`) all hid the next command. The same scan guards bash,
job_start, gate commands and every line typed into a terminal, so each shape
was a way past all four.

The scan now walks the raw string the way the shell does: it cuts at every
operator with or without spaces, opens subshells, groups and substitutions
(also inside double quotes, never inside single quotes), steps over reserved
words, assignments, redirections and wrappers, and reads `eval ARGS` and
`sh -c SCRIPT` as scripts of their own. Arguments and quoted text stay data:
`echo reboot`, `grep -r mount .` and a commit message that mentions reboot are
allowed as before, and so are the data lines of a heredoc. The `rm -rf`
exemptions use the same scan, so an rm hidden in a subshell is checked too.
"""

from __future__ import annotations

import pytest

from core.tools.builtin.core_tools import (
    _check_command_security,
    _extract_command_words,
    _rm_segments,
)

MUST_REFUSE = [
    "ls; sudo reboot",
    "ls;reboot",
    "ls; reboot",
    "ls ;reboot",
    "ls;sudo ls",
    "ls&&reboot",
    "ls|reboot",
    "ls & reboot",
    "ls &reboot",
    "ls\nreboot",
    "(reboot)",
    "{ reboot; }",
    "echo $(reboot)",
    "echo `reboot`",
    "if true; then reboot; fi",
    "while true; do umount /x; done",
    "a=1;reboot",
    "! reboot",
    "ls; 'reboot'",
    "ls; r\\eboot",
    "eval reboot",
    "ls;env reboot",
    "ls >f;umount /x",
    "ls;dd of=x",
    "ls |& umount x",
    "command umount /x",
    "builtin eval umount /x",
    'ls;"umount" /x',
    "ls;u''mount /x",
    # Already refused before the fix; must stay refused.
    "ls;/sbin/reboot",
    "time umount /x",
    "exec umount /x",
    "ls && sudo reboot",
    'sh -c "reboot"',
    # Neighbouring shapes the same walk covers.
    'echo "$(reboot)"',
    "echo `echo \\`reboot\\``",
    "cat <(reboot)",
    "diff >(umount x) y",
    ">f reboot",
    "2>&1 reboot",
    "a=1 reboot",
    "bash -lc 'reboot'",
    "f() { reboot; }",
    "function f { reboot; }",
    "case x in a) reboot;; esac",
    "until false; do dd if=x; done",
    "eval 'ls; reboot'",
    "timeout 5 umount /x",
    "xargs -n 1 reboot",
    "a=($(reboot))",
    "echo ${x:-$(reboot)}",
    "cat <<EOF\n$(reboot)\nEOF",
    "ls ;\\\nreboot",
    # An unbalanced quote must not hide what follows it.
    'ls "; reboot',
    # Nesting deep enough to exhaust the parser reports every word.
    '"$(' * 3000 + "reboot",
]

MUST_ALLOW = [
    "echo reboot",
    "grep -r mount .",
    'git commit -m "fix mount; reboot later"',
    "echo 'a; reboot'",
    "echo '$(reboot)'",
    """python -c "print('dd')" """,
    "cat mount.txt",
    "ls | grep dd",
    "man mount",
    "which reboot",
    "command -v reboot",
    "ls # then reboot; dd",
    "ls 2>&1 | grep dd",
    "ls &>/dev/null; echo ok",
    "ls >|f",
    "a=(reboot dd)",
    "for x in reboot dd; do echo $x; done",
    "case $x in start) echo;; esac",
    "find . \\( -name dd \\)",
    "git log --format='%h; dd'",
    "cat <<'EOF'\nreboot\numount /x\nEOF\necho done",
    "cat <<EOF\nreboot now\nEOF",
    "cat <<-EOF\n\treboot\n\tEOF",
    "cd impl && rm -rf __pycache__",
    "rm -rf .pytest_cache 2>/dev/null; ls",
]


@pytest.mark.parametrize("command", MUST_REFUSE)
def test_a_hidden_command_is_refused(command):
    assert _check_command_security(command) is not None, _extract_command_words(command)


@pytest.mark.parametrize("command", MUST_ALLOW)
def test_arguments_and_quoted_text_stay_allowed(command):
    assert _check_command_security(command) is None, _extract_command_words(command)


def test_arguments_are_not_command_words():
    assert _extract_command_words('git commit -m "a; reboot" && ls -la') == ["git", "ls"]


def test_an_rm_in_a_subshell_is_checked_by_the_exemptions():
    """The cache exemption cleared a command when its visible rm qualified,
    even if another rm sat inside a subshell it could not see."""
    assert _rm_segments("rm -rf __pycache__; (rm -rf /etc)") == [
        ["rm", "-rf", "__pycache__"],
        ["rm", "-rf", "/etc"],
    ]
    assert _check_command_security("rm -rf __pycache__; (rm -rf /etc)") is not None
    assert _check_command_security("rm -rf __pycache__ && echo $(rm -rf /etc)") is not None


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf ~",
        "(rm -rf ~)",
        "rm -rf ~/projects",
        "rm -rf -- ~",
        "rm -fR ~",
        "rm -rf {a,/etc}",
        "rm -rf ~/.cache",
        "rm --recursive --force /etc",
    ],
)
def test_rm_targets_the_shell_expands_are_not_read_as_workspace_names(command):
    """`~` and `{a,b}` are expanded by the shell before rm runs. Read as
    literal names they looked like paths inside the workspace, so the
    workspace exemption cleared `rm -rf ~`."""
    assert _check_command_security(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf __pycache__; reboot",
        "rm -rf __pycache__ && sudo ls",
        "rm -rf __pycache__; ls > /etc/hosts",
        "coproc reboot",
    ],
)
def test_an_exempt_rm_does_not_clear_the_rest_of_the_command(command):
    """An exempt `rm -rf` ended the whole check with "allowed", so nothing
    written after it was looked at."""
    assert _check_command_security(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "rm -f a.tmp; grep -r needle .",
        'rm -f "$OUT/a.txt" "$OUT/b.txt"\nsort -rn counts.txt',
        "rm --force a.tmp",
        "rm -rf __pycache__; rm -f /tmp/a.log",
        "rm -rf __pycache__; ls",
    ],
)
def test_rm_flags_are_judged_per_invocation(command):
    """The flags of the whole command were pooled, so a plain `rm -f` read as
    `rm -rf` whenever another word in the script had an `r` in a flag. With
    every rm now visible to the scan, that would refuse routine cleanup."""
    assert _check_command_security(command) is None
