"""Agent Mesh build, 2026-09-08: the `rm -rf` workspace and cache exemptions
both began with `tokens[0] != "rm"`, so they were unreachable for any command
that did not start with a bare rm. `cd impl && rm -rf __pycache__` — the most
ordinary shape in Python work — was refused by an error message that listed
__pycache__ as an allowed target.

Both exemptions now evaluate EVERY rm invocation in the command, and every one
of them must qualify.
"""

from __future__ import annotations

from core.tools.builtin.core_tools import (
    _check_command_security,
    _rm_segments,
    _rm_targets_are_safe_caches,
    _split_shell_operators,
)


def _blocked(command: str) -> bool:
    return _check_command_security(command) is not None


# ── the field cases ──────────────────────────────────────────────────────────


def test_cache_cleanup_after_a_cd_is_allowed():
    assert not _blocked("cd impl && rm -rf __pycache__")


def test_the_blocked_cleanup_from_the_agent_mesh_build_is_allowed():
    """Verbatim from session b72959f8407d, refused twice while the agent tried
    to clean up a tree it had just mis-written."""
    cmd = "mv spaces/agent-mesh/STATUS.md STATUS.md 2>/dev/null && rm -rf spaces/agent-mesh; ls -la STATUS.md"
    assert not _blocked(cmd)


# ── nothing was loosened ─────────────────────────────────────────────────────


def test_a_target_outside_the_workspace_still_blocks():
    assert _blocked("rm -rf /etc")


def test_one_bad_segment_blocks_the_whole_command():
    assert _blocked("rm -rf __pycache__ && rm -rf /etc")


def test_bare_glob_still_blocks_even_after_a_cd():
    assert _blocked("cd x && rm -rf *")


def test_parent_traversal_still_blocks():
    assert _blocked("rm -rf ../outside")


def test_sudo_still_blocks():
    assert _blocked("sudo rm -rf /")


# ── the parsing this rests on ────────────────────────────────────────────────


def test_operator_split_respects_quotes():
    assert _split_shell_operators('echo "a;b" && rm -rf .pytest_cache') == [
        'echo "a;b"',
        "rm -rf .pytest_cache",
    ]


def test_semicolon_glued_to_a_token_still_splits():
    """shlex keeps `x;` as one token; the raw-string splitter must not."""
    assert _rm_segments("rm -rf spaces/x; ls -la") == [["rm", "-rf", "spaces/x"]]


def test_redirections_are_not_read_as_targets():
    assert _rm_segments("rm -rf build 2>/dev/null") == [["rm", "-rf", "build"]]


def test_prefix_wrappers_are_peeled():
    assert _rm_segments("env FOO=1 nice -n 5 rm -rf b") == [["rm", "-rf", "b"]]


def test_a_command_with_no_rm_yields_no_segments():
    assert _rm_segments("echo hi") == []
    assert not _rm_targets_are_safe_caches("echo hi")


def test_unparseable_command_is_not_cleared():
    """An unbalanced quote must keep the block, not open it."""
    assert _rm_segments('rm -rf "unclosed') is None
    assert not _rm_targets_are_safe_caches('rm -rf "unclosed')
