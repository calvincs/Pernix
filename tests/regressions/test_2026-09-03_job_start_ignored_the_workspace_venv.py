"""A background job ran on a different toolchain than bash did.

Field case, session 3dc5a307d751 (2026-09-03): the agent pip-installed sympy
into the workspace venv, confirmed the import from the bash tool, then handed
the long solve to `job_start(...)` — where `python3 script.py` died with
ImportError. `job_start` built its child environment from a bare
`os.environ.copy()`, so the job inherited the *server's* PATH: no
`<workspace>/.venv/bin`, no VIRTUAL_ENV, and none of the shell_env_mode
filtering bash applies.

Both launchers now build their environment with `paths.build_shell_env()`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.tools import paths
from core.tools.builtin import jobs_tool
from db import models as db


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A workspace whose venv already exists, so nothing forks `-m venv`."""
    ws = tmp_path / "workspace"
    (ws / ".venv" / "bin").mkdir(parents=True)
    (ws / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    (ws / ".venv" / "bin" / "python").chmod(0o755)
    monkeypatch.setattr("config.settings.workspace_dir", str(ws))
    return ws.resolve()


def _start_job_capturing_env(monkeypatch) -> dict:
    captured: dict = {}

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            captured["argv"] = args[0] if args else None
            self.pid = os.getpid()

    monkeypatch.setattr(jobs_tool.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr("config.settings.jobs_enabled", True)
    sid = db.create_session(title="job env regression")
    result = jobs_tool.job_start("python3 -c 'import sympy'", _context={"session_id": sid})
    assert result.startswith("Job started: "), result
    return captured


def test_job_start_puts_the_workspace_venv_first_on_path(workspace, monkeypatch):
    captured = _start_job_capturing_env(monkeypatch)

    env = captured["env"]
    assert env["PATH"].split(":")[0] == str(workspace / ".venv" / "bin")
    assert env["VIRTUAL_ENV"] == str(workspace / ".venv")
    assert env["PYTHONUNBUFFERED"] == "1"
    # HOME follows the job's cwd rather than the server account's home.
    assert env["HOME"] == captured["cwd"] == str(workspace)


def test_job_start_honors_the_shell_env_mode_filter(workspace, monkeypatch):
    """The old copy handed the server's whole environment to the child —
    the exact leak shell_env_mode exists to close for bash."""
    monkeypatch.setattr("config.settings.shell_env_mode", "allowlist")
    monkeypatch.setattr("config.settings.shell_env_allowlist", ["PATH", "HOME"])
    monkeypatch.setenv("PERNIX_SECRET_TOKEN", "must-not-leak")

    env = _start_job_capturing_env(monkeypatch)["env"]

    assert "PERNIX_SECRET_TOKEN" not in env
    assert env["PATH"].split(":")[0] == str(workspace / ".venv" / "bin")


def test_bash_and_jobs_share_one_env_builder(workspace, monkeypatch):
    """Regression guard against the two paths drifting apart again."""
    from core.tools.builtin import core_tools

    assert core_tools._build_shell_env is paths.build_shell_env

    captured: dict = {}
    real_popen = core_tools.subprocess.Popen

    class _ProbePopen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            captured["env"] = kwargs.get("env")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("config.settings.shell_security_mode", "permissive")
    monkeypatch.setattr(core_tools.subprocess, "Popen", _ProbePopen)
    core_tools.bash("echo hi")

    job_env = _start_job_capturing_env(monkeypatch)["env"]
    assert captured["env"]["PATH"] == job_env["PATH"]
    assert captured["env"]["VIRTUAL_ENV"] == job_env["VIRTUAL_ENV"]


def test_build_shell_env_creates_a_missing_venv(tmp_path, monkeypatch):
    """The venv-ensure moved with the env build; a fresh workspace must still
    come up with a usable interpreter directory."""
    ws = tmp_path / "fresh"
    ws.mkdir()
    monkeypatch.setattr("config.settings.workspace_dir", str(ws))
    calls: list[list[str]] = []

    def _fake_run(argv, **_kwargs):
        calls.append(argv)
        (ws / ".venv" / "bin").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("subprocess.run", _fake_run)
    env = paths.build_shell_env()

    assert calls and calls[0][1:3] == ["-m", "venv"]
    assert env["VIRTUAL_ENV"] == str(Path(ws).resolve() / ".venv")
