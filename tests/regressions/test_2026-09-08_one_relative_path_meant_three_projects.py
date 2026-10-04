"""One relative path named three different projects.

Inside a space, with `<ws>/impl/` and `<ws>/spaces/proj/impl/` both real, the
six tools that take a relative path resolved it against three different roots:

    file_read / file_write / bash   ->  the space home
    grep / glob / job_start / gate  ->  the global workspace

So the agent edited `impl/app.py` in the space, tested it with bash in the
space, and then had `grep`, `job_start` and — worst of all — its deterministic
gate report on the *global* copy of the same relative name. reflect treats a
gate as host evidence it cannot overrule, so a turn whose real work was broken
could be certified passing by a check that had never looked at it: the same
pytest command returned "1 failed" in the foreground and "1 passed, exit=0"
from the job and the gate.

The fix is one documented default (paths.workspace_home(): the space home when
the session has one, else the workspace) shared by every tool, with the
containment root kept separate so a soft default never becomes a cross-space
access restriction.
"""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import pytest

from core import gates
from core.tools.registry import ToolRegistry
from db import models as db

MARKERS = {"GLOBAL": "the global workspace copy", "SPACE": "the space copy"}


@pytest.fixture
def space(tmp_path, monkeypatch):
    """A space session with a conflicting global tree of the same names."""
    ws = tmp_path / "workspace"
    home = ws / "spaces" / "proj"
    (ws / "impl").mkdir(parents=True)
    (home / "impl").mkdir(parents=True)
    # A venv that already exists, so build_shell_env never forks `-m venv`.
    (ws / ".venv" / "bin").mkdir(parents=True)
    (ws / ".venv" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    (ws / ".venv" / "bin" / "python").chmod(0o755)

    # Same relative names, different contents and different test outcomes.
    (ws / "impl" / "app.py").write_text("MARKER = 'GLOBAL'\n")
    (home / "impl" / "app.py").write_text("MARKER = 'SPACE'\n")
    (ws / "impl" / "test_app.py").write_text("def test_ok():\n    assert True\n")
    (home / "impl" / "test_app.py").write_text("def test_ok():\n    assert False, 'the real work is broken'\n")
    # Global-only file and directory: the prefer-existing fallback must survive.
    (ws / "docs").mkdir()
    (ws / "docs" / "readme.md").write_text("GLOBAL DOC\n")

    monkeypatch.setattr("config.settings.workspace_dir", str(ws))
    monkeypatch.setattr("config.settings.jobs_enabled", True)
    monkeypatch.setattr("config.settings.gates_enabled", True)
    monkeypatch.setattr("config.settings.shell_security_mode", "permissive")

    sid = db.create_session(title="space session")
    live = _live(home=str(home))
    _install_manager(monkeypatch, live)

    return SimpleNamespace(
        ws=ws.resolve(),
        home=home.resolve(),
        sid=sid,
        live=live,
        reg=_registry(),
        ctx={"session_id": sid, "workspace_home": str(home)},
    )


def _live(home: str | None = None, override: str | None = None):
    """Enough of an AgentSession for the gate sweep and the bash tool."""
    return SimpleNamespace(
        workspace_home=home,
        workspace_override=override,
        session_type="",
        register_process=lambda *_a, **_k: None,
        release_process=lambda *_a, **_k: None,
    )


def _install_manager(monkeypatch, live):
    import sessions.manager as sm

    monkeypatch.setattr(sm, "get_manager", lambda: SimpleNamespace(get=lambda _sid: live))


def _registry() -> ToolRegistry:
    from core.tools.builtin.core_tools import register as core
    from core.tools.builtin.glob_tool import register as glob
    from core.tools.builtin.grep_tool import register as grep
    from core.tools.builtin.jobs_tool import register as jobs

    reg = ToolRegistry()
    for register in (core, grep, glob, jobs):
        register(reg)
    return reg


def _run(space, tool: str, **args) -> str:
    out = space.reg.execute_sync(tool, args, space.ctx)
    return out if isinstance(out, str) else out[0]


def _job_output(space, command: str, timeout: float = 20.0) -> str:
    """Start a job, wait for it to finish, return its whole log."""
    from core.tools.builtin import jobs_tool

    started = _run(space, "job_start", command=command)
    assert started.startswith("Job started: "), started
    job_id = started.split()[2]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if jobs_tool._refresh(db.get_job(job_id))["state"] != "running":
            break
        time.sleep(0.05)
    return started + "\n" + _run(space, "job_tail", job_id=job_id)


def _gate(space, command: str, name: str = "check") -> gates.GateResult:
    db.add_gate(space.sid, name, command, watch_paths=[], cwd="")
    return gates.run_gates(space.sid, {}, attempt=1)[0]


def _which(text: str) -> str:
    """Which tree did this output come from? Fails loudly on a mixed answer."""
    hits = [m for m in MARKERS if m in text]
    assert len(hits) == 1, f"expected exactly one tree in the output, got {hits}: {text[:400]}"
    return hits[0]


# ── the headline: a gate certified work the foreground had failed ────────────


def test_the_gate_now_agrees_with_the_foreground_shell(space):
    """Verbatim from the H07 reproduction. The space's suite fails and the
    global suite of the same relative name passes; bash, the job and the gate
    all ran the identical command and gave three different verdicts."""
    suite = f"cd impl && {sys.executable} -m pytest -q -p no:cacheprovider"

    foreground = _run(space, "bash", command=suite)
    assert "1 failed" in foreground, foreground

    job = _job_output(space, suite)
    assert "1 failed" in job, job

    gate = _gate(space, suite)
    assert gate.passed is False, gate
    assert gates.failing([gate]) == [gate], "a gate that ran the broken tree must block the turn"
    assert gate.cwd == str(space.home), "the gate must report where it ran"


def test_the_turn_end_sweep_takes_the_session_it_was_handed(space, monkeypatch):
    """run_gates_for_turn already holds the live session, so the contract comes
    off it directly instead of through a manager lookup the caller has no
    reason to depend on."""
    from sessions.state import TurnState

    _install_manager(monkeypatch, None)  # the lookup fallback finds nothing
    db.add_gate(space.sid, "which-tree", "cat impl/app.py", watch_paths=[], cwd="")
    session_obj = SimpleNamespace(
        current_turn_user_msg_id=1,
        turn=TurnState(),
        workspace_home=str(space.home),
        workspace_override=None,
    )

    result = gates.run_gates_for_turn(space.sid, session_obj, attempt=1)[0]
    assert _which(result.output_tail) == "SPACE"
    assert result.cwd == str(space.home)


# ── one relative name, one project ───────────────────────────────────────────


def test_every_tool_resolves_one_relative_name_to_the_same_project(space):
    assert _which(_run(space, "file_read", path="impl/app.py")) == "SPACE"
    assert _which(_run(space, "bash", command="cat impl/app.py")) == "SPACE"
    assert _which(_run(space, "grep", pattern="MARKER", path="impl")) == "SPACE"
    assert _which(_job_output(space, "cat impl/app.py")) == "SPACE"
    assert _which(_gate(space, "cat impl/app.py").output_tail) == "SPACE"

    listing = _run(space, "glob", pattern="*.py", path="impl")
    assert "app.py" in listing
    assert str(space.home) in listing, listing


def test_a_write_to_a_relative_name_lands_where_the_searches_look(space):
    _run(space, "file_write", path="impl/app.py", content="MARKER = 'SPACE'\n# edited\n")

    assert (space.ws / "impl" / "app.py").read_text() == "MARKER = 'GLOBAL'\n", "the global copy is untouched"
    assert "# edited" in (space.home / "impl" / "app.py").read_text()
    assert "# edited" in _run(space, "grep", pattern="edited", path="impl")
    assert "# edited" in _job_output(space, "cat impl/app.py")


def test_results_say_which_project_they_hit(space):
    root = str(space.home)
    assert f"[root: {root}]" in _run(space, "grep", pattern="MARKER", path="impl")
    assert f"[root: {root}]" in _run(space, "glob", pattern="*.py", path="impl")
    assert f"[cwd: {root}]" in _run(space, "bash", command="pwd")
    assert f"[cwd: {root}]" in _job_output(space, "pwd")


def test_a_job_still_names_its_root_when_polled_later(space):
    from core.tools.builtin import jobs_tool

    started = _run(space, "job_start", command="pwd")
    job_id = started.split()[2]
    deadline = time.time() + 20.0
    while time.time() < deadline and jobs_tool._refresh(db.get_job(job_id))["state"] == "running":
        time.sleep(0.05)
    assert f"[cwd: {space.home}]" in _run(space, "job_status", job_id=job_id)


# ── the shared toolchain does NOT move with the cwd ──────────────────────────


def test_venv_and_path_stay_on_the_global_workspace(space):
    """The working directory follows the space; the toolchain is shared. A
    naive workspace_home() swap would have pointed PATH at <home>/.venv and
    left a job importing packages bash had just installed."""
    venv_bin = str(space.ws / ".venv" / "bin")
    probe = 'echo "PATH1=${PATH%%:*} HOME=$HOME PWD=$PWD"'

    for where, output in (
        ("bash", _run(space, "bash", command=probe)),
        ("job", _job_output(space, probe)),
        ("gate", _gate(space, probe, name="env-probe").output_tail),
    ):
        assert f"PATH1={venv_bin}" in output, f"{where}: {output}"
        assert f"HOME={space.home}" in output, f"{where}: {output}"
        assert f"PWD={space.home}" in output, f"{where}: {output}"


# ── everything the path fixes before this one established ────────────────────


def test_the_global_only_fallback_is_preserved(space):
    """paths.py:307-360 deliberately falls back to an existing global file or
    parent directory. Sharing a resolver must not quietly delete that."""
    assert "GLOBAL DOC" in _run(space, "file_read", path="docs/readme.md")
    assert "GLOBAL DOC" in _run(space, "grep", pattern="GLOBAL", path="docs")
    assert f"[root: {space.ws}]" in _run(space, "glob", pattern="*.md", path="docs")

    _run(space, "file_write", path="docs/notes.md", content="next to its siblings\n")
    assert (space.ws / "docs" / "notes.md").exists(), "the 7e1be5f parent_exists rule"
    assert not (space.home / "docs").exists()


def test_a_genuinely_new_path_still_defaults_into_the_space_home(space):
    _run(space, "file_write", path="fresh/x.md", content="new work\n")
    assert (space.home / "fresh" / "x.md").exists()
    assert not (space.ws / "fresh").exists()


def test_a_doubled_prefix_still_beats_the_orphan_tree(space):
    """ef1d4c9: the guard runs before any root scan, for searches too."""
    orphan = space.home / "spaces" / "proj" / "impl"
    orphan.mkdir(parents=True)
    (orphan / "app.py").write_text("MARKER = 'ORPHAN'\n")

    assert _which(_run(space, "file_read", path="spaces/proj/impl/app.py")) == "SPACE"
    assert _which(_run(space, "grep", pattern="MARKER", path="spaces/proj/impl")) == "SPACE"
    assert f"[root: {space.home}]" in _run(space, "glob", pattern="*.py", path="spaces/proj/impl")
    assert (orphan / "app.py").read_text() == "MARKER = 'ORPHAN'\n", "the orphan is left alone"


# ── the contract outside a space ─────────────────────────────────────────────


def test_a_global_session_resolves_everything_at_the_workspace_root(space, monkeypatch):
    _install_manager(monkeypatch, _live())
    space.ctx = {"session_id": space.sid}

    assert _which(_run(space, "file_read", path="impl/app.py")) == "GLOBAL"
    assert _which(_run(space, "bash", command="cat impl/app.py")) == "GLOBAL"
    assert _which(_run(space, "grep", pattern="MARKER", path="impl")) == "GLOBAL"
    assert _which(_job_output(space, "cat impl/app.py")) == "GLOBAL"
    assert _which(_gate(space, "cat impl/app.py").output_tail) == "GLOBAL"
    assert f"[root: {space.ws}]" in _run(space, "glob", pattern="*.py", path="impl")


def test_absolute_paths_are_never_rerooted(space):
    """A qualified path means what it says — the space home is not prepended."""
    assert _which(_run(space, "file_read", path=str(space.ws / "impl" / "app.py"))) == "GLOBAL"
    assert _which(_run(space, "grep", pattern="MARKER", path=str(space.ws / "impl"))) == "GLOBAL"
    assert f"[root: {space.ws}]" in _run(space, "glob", pattern="*.py", path=str(space.ws / "impl"))
    assert _which(_run(space, "bash", command=f"cat {space.ws}/impl/app.py")) == "GLOBAL"


def test_a_workspace_override_remains_the_only_root(space, monkeypatch, tmp_path):
    """An override is a sandbox, not a default: it wins over the space home
    everywhere, and nothing resolves outside it."""
    override = tmp_path / "isolated"
    (override / "impl").mkdir(parents=True)
    (override / "impl" / "app.py").write_text("MARKER = 'SPACE'\n")  # the sandbox's own copy
    _install_manager(monkeypatch, _live(home=str(space.home), override=str(override)))
    space.ctx = {
        "session_id": space.sid,
        "workspace_home": str(space.home),
        "workspace_override": str(override),
    }

    assert f"[root: {override.resolve()}]" in _run(space, "grep", pattern="MARKER", path="impl")
    assert f"[root: {override.resolve()}]" in _run(space, "glob", pattern="*.py", path="impl")
    assert f"[cwd: {override.resolve()}]" in _job_output(space, "pwd")
    assert str(override.resolve()) in _gate(space, "pwd", name="pwd-gate").output_tail
    assert "Error" in _run(space, "grep", pattern="MARKER", path=str(space.home / "impl"))


def test_protected_paths_are_still_refused(space):
    """A shared resolver must not hand a search tool a root the file tools
    refuse — or lose the refusals the file tools already had."""
    assert "Protected" in _run(space, "grep", pattern="x", path=".venv")
    assert "Protected" in _run(space, "glob", pattern="*", path="__pycache__")
    assert "Protected" in _run(space, "file_write", path="rules.md", content="nope")


def test_a_search_cannot_escape_the_workspace(space):
    assert "Error" in _run(space, "grep", pattern="x", path="/etc")
    assert "Error" in _run(space, "glob", pattern="*", path="/etc")
    assert "Error" in _run(space, "file_read", path="/etc/passwd")
