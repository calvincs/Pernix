"""Once the bash denylist saw every command in a line, `rm -rf /tmp/<dir>`
was refused: it used to slip through after a `;`, and only paths inside the
agent workspace are exempt from the rm rule. Exempting /tmp would have given
the agent a second root. Instead the agent got a scratch folder inside the
workspace, `<workspace>/tmp`.

bash, detached jobs, terminals and gates now run with TMPDIR pointing at it,
so `mktemp -d` lands there and `rm -rf tmp/<dir>` (or the absolute path mktemp
printed) passes the existing exemption. The refusal for `/tmp` names the
scratch folder. The prompt says so in one line, and a space session is given
the absolute path, because from a space home a relative tmp/ names the wrong
folder and `$TMPDIR/...` is refused as an rm target. The workspace is a
bind-mounted data dir that survives rebuilds, so a snooze rung deletes
top-level scratch entries that nothing has touched for
workspace_tmp_retention_days (default 3, 0 disables).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from config import settings
from core import retention
from core.tools import paths
from core.tools.builtin.core_tools import _check_command_security


@pytest.fixture
def ws(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "workspace"
    # A stand-in venv python so build_shell_env does not build a real venv.
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("")
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    # pytest's tmp_path is deeper than a deployed workspace; the socket-path
    # guard has its own test below.
    monkeypatch.setattr(paths, "_TMPDIR_MAX_LEN", 4096)
    return root.resolve()


def _age(path: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(path, (t, t), follow_symlinks=False)


# ---------------------------------------------------------------------------
# TMPDIR
# ---------------------------------------------------------------------------


def test_bash_runs_with_tmpdir_in_the_workspace_and_mktemp_lands_there(ws):
    from core.tools.builtin.core_tools import bash

    result = bash('echo "T=$TMPDIR"; mktemp -d', _context={"session_id": "scratch-test"})
    out = result[0] if isinstance(result, tuple) else result
    assert f"T={ws / 'tmp'}" in out
    made = [line for line in out.splitlines() if line.startswith(str(ws / "tmp") + "/")]
    assert made and Path(made[0]).is_dir()


def test_jobs_terminals_and_gates_get_the_same_scratch(ws):
    from core.gates import _gate_env

    assert paths.build_shell_env(ws, ws)["TMPDIR"] == str(ws / "tmp")
    # A space session still shares the global scratch folder.
    home = ws / "spaces" / "pub"
    home.mkdir(parents=True)
    assert paths.build_shell_env(ws, home)["TMPDIR"] == str(ws / "tmp")
    assert _gate_env(ws, home)["TMPDIR"] == str(ws / "tmp")


def test_the_server_process_tmpdir_is_not_changed(ws):
    before = os.environ.get("TMPDIR")
    paths.build_shell_env(ws, ws)
    assert os.environ.get("TMPDIR") == before


def test_helper_follows_a_workspace_override(ws, tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    token = paths.WORKSPACE_OVERRIDE.set(str(sandbox))
    try:
        assert paths.workspace_tmp() == sandbox.resolve() / "tmp"
        assert (sandbox / "tmp").is_dir()
    finally:
        paths.WORKSPACE_OVERRIDE.reset(token)
    assert paths.workspace_tmp() == ws / "tmp"


def test_a_scratch_path_too_long_for_sockets_is_not_exported(ws, monkeypatch):
    monkeypatch.setattr(paths, "_TMPDIR_MAX_LEN", 10)
    assert paths.scratch_tmpdir(ws) is None
    env = paths.build_shell_env(ws, ws)
    assert env.get("TMPDIR") != str(ws / "tmp")


# ---------------------------------------------------------------------------
# rm -rf of scratch
# ---------------------------------------------------------------------------


def test_rm_rf_of_scratch_passes_in_the_recommended_forms(ws):
    assert _check_command_security("rm -rf tmp/build-1") is None
    assert _check_command_security(f"rm -rf {ws}/tmp/tmp.Ab12Cd") is None
    assert _check_command_security("cd tmp && ls; rm -rf tmp/a tmp/b") is None


def test_rm_rf_of_system_tmp_stays_refused_and_names_the_scratch_folder(ws):
    msg = _check_command_security("rm -rf /tmp/x")
    assert msg and msg.startswith("Error")
    assert "tmp/" in msg and "rm -rf tmp/<dir>" in msg
    # `$TMPDIR/...` is not a form the exemption accepts, so it is not advised.
    assert _check_command_security("rm -rf $TMPDIR/x") is not None


def test_a_space_session_is_shown_the_absolute_scratch_path(ws):
    home = ws / "spaces" / "pub"
    home.mkdir(parents=True)
    token = paths.WORKSPACE_HOME.set(str(home))
    try:
        msg = _check_command_security("rm -rf /tmp/x")
        assert f"rm -rf {ws / 'tmp'}/<dir>" in msg
        assert _check_command_security(f"rm -rf {ws}/tmp/x") is None
    finally:
        paths.WORKSPACE_HOME.reset(token)


def test_the_prompt_names_the_scratch_folder():
    from core.context.compiler import BASE_SYSTEM_PROMPT
    from core.context.system_map import _DATA_LAYOUT

    assert "rm -rf tmp/<dir>" in BASE_SYSTEM_PROMPT
    assert "data/workspace/tmp/" in _DATA_LAYOUT


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch(ws) -> Path:
    return paths.workspace_tmp(ws)


def test_sweep_removes_old_entries_and_keeps_fresh_ones(scratch):
    old_dir = scratch / "old"
    (old_dir / "sub").mkdir(parents=True)
    (old_dir / "sub" / "f.txt").write_text("x")
    for p in (old_dir / "sub" / "f.txt", old_dir / "sub", old_dir):
        _age(p, 10)
    old_file = scratch / "old.txt"
    old_file.write_text("x")
    _age(old_file, 10)
    fresh = scratch / "fresh"
    fresh.mkdir()

    assert retention.prune_workspace_tmp(3) == 2
    assert not old_dir.exists() and not old_file.exists()
    assert fresh.is_dir()
    assert scratch.is_dir(), "the folder itself is never removed"


def test_sweep_keeps_a_dir_whose_inner_file_is_fresh(scratch):
    d = scratch / "busy"
    (d / "deep").mkdir(parents=True)
    (d / "deep" / "new.log").write_text("x")
    _age(d / "deep", 10)
    _age(d, 10)
    assert retention.prune_workspace_tmp(3) == 0
    assert (d / "deep" / "new.log").exists()


def test_sweep_never_follows_a_symlink_out_of_the_folder(scratch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "keep.txt"
    precious.write_text("x")
    _age(precious, 10)
    _age(outside, 10)
    link = scratch / "link"
    link.symlink_to(outside)
    _age(link, 10)
    # A stale dir that holds a link out must not be walked through either.
    holder = scratch / "holder"
    holder.mkdir()
    (holder / "out").symlink_to(outside)
    _age(holder / "out", 10)
    _age(holder, 10)

    assert retention.prune_workspace_tmp(3) == 2
    assert not link.exists() and not link.is_symlink()
    assert not holder.exists()
    assert precious.exists() and outside.is_dir()


def test_sweep_leaves_a_symlinked_scratch_folder_alone(ws, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / "v.txt"
    victim.write_text("x")
    _age(victim, 10)
    (ws / "tmp").symlink_to(elsewhere)
    assert retention.prune_workspace_tmp(3) == 0
    assert victim.exists()


def test_sweep_is_a_no_op_at_zero_days(scratch, monkeypatch):
    f = scratch / "old.txt"
    f.write_text("x")
    _age(f, 100)
    assert retention.prune_workspace_tmp(0) == 0
    monkeypatch.setattr(settings, "workspace_tmp_retention_days", 0)
    assert retention.prune_workspace_tmp() == 0
    assert f.exists()


def test_sweep_without_a_scratch_folder_is_quiet(ws):
    assert retention.prune_workspace_tmp(3) == 0
    assert not (ws / "tmp").exists(), "the sweep does not create the folder"


async def test_the_snooze_rung_sweeps_and_then_waits(scratch, monkeypatch):
    from core.snooze import SnoozeRunner

    f = scratch / "old.txt"
    f.write_text("x")
    _age(f, 10)
    runner = SnoozeRunner()
    await runner._sweep_workspace_tmp()
    assert not f.exists()

    g = scratch / "old2.txt"
    g.write_text("x")
    _age(g, 10)
    await runner._sweep_workspace_tmp()
    assert g.exists(), "a second sweep inside 6 hours is skipped"


@pytest.mark.parametrize(
    "command",
    [
        "cd / && rm -rf home",
        "cd /etc; rm -rf ssl",
        "cd /etc; rm -rf .cache",
        "cd .. && rm -rf x",
        "cd ~ ; rm -rf x",
        "cd $SOMEWHERE; rm -rf x",
        "pushd /var; rm -rf log",
        "(cd /; rm -rf home)",
        "cd -; rm -rf x",
    ],
)
def test_a_relative_rm_target_is_not_trusted_after_the_shell_has_left(command):
    """The rm exemptions read a relative target as a path under the
    workspace and never looked at a `cd` earlier in the line, so
    `cd / && rm -rf home` was cleared as a workspace folder."""
    from core.tools.builtin.core_tools import _check_command_security

    assert _check_command_security(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "cd impl && rm -rf __pycache__",
        "cd impl && rm -rf build",
        "cd; rm -rf tmp/a",
        "mkdir -p a && cd a && rm -rf node_modules && ls",
        "cd /etc && ls; cd impl && rm -f x",
    ],
)
def test_a_cd_inside_the_workspace_keeps_the_rm_exemptions(command):
    from core.tools.builtin.core_tools import _check_command_security

    assert _check_command_security(command) is None
