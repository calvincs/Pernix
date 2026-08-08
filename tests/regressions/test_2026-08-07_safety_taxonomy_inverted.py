"""Regression: the tool safety taxonomy inverted actual blast radius.

Shipped defects (architecture review 2026-08-07, appendix E). All of these
were live at 3ef1e6c:

1. Only four tools were `dangerous` (delete_skill, delete_workflow,
   search_web, browse_web), while `create_tool` — which writes model-authored
   Python into the SERVER'S OWN source tree and imports it in-process — was
   `safe`, `add_gate` (shell that re-runs unattended every turn) was
   `caution`, and `add_skill_script` (writes a file load_skill then tells the
   agent to `bash`) was `safe`.
2. `create_skill(approved=...)` / `add_skill_script(approved=...)` were
   model-supplied booleans. Nothing correlated them with a user response, so
   the model could self-authorize on the first call.
3. `shell_env_mode` defaulted to "passthrough": every bash child inherited a
   copy of os.environ, including every provider API key.
4. `add_gate` commands ran with shell=True, agent-chosen cwd, no
   _check_command_security, no setsid/rlimits — a cleaner bypass of shell
   policy than any denylist trick, and persistent across turns.
5. `repl`'s timeout was unclamped despite the schema advertising max 1800,
   leaking one executor thread plus an unkillable child per call.
6. Truncation drill-in pointers were dead: truncate_output wrote to
   data/.tool_output/ and told the model to file_read it, but that directory
   was not an allowed read root, so every pointer resolved to a path that
   never existed.
7. `_DISPATCH_TIMEOUT_GRACE_S` was skipped whenever the caller passed no
   explicit timeout, violating executor.py's own documented invariant that
   the tool's internal timeout must fire first.
8. data/workspace/.venv was writable via the "safe" file_write tool and on
   sys.path for every custom tool — file_write into site-packages planted
   code that later executed in the server process.

The `bash`/`repl` `caution` level is deliberately unchanged and asserted
here, so a later well-meaning promotion has to argue with this file: every
dangerous-gated action is reachable through bash anyway, so the gate is an
intent-surfacing mechanism, not a boundary. See docs/security.md.
"""

import inspect

import pytest

from core.tools.registry import ToolRegistry


def _registry_with(*register_fns) -> ToolRegistry:
    reg = ToolRegistry()
    for fn in register_fns:
        fn(reg)
    return reg


# ---------------------------------------------------------------------------
# 1. Safety levels match blast radius
# ---------------------------------------------------------------------------


def test_toolmaker_write_and_import_tools_are_dangerous():
    from core.extensions.toolmaker import register as toolmaker_register

    reg = _registry_with(toolmaker_register)
    # Both write into core/tools/builtin/ and import into the server process.
    assert reg.get("create_tool").safety_level == "dangerous"
    assert reg.get("update_tool").safety_level == "dangerous"


def test_add_gate_is_dangerous(monkeypatch):
    monkeypatch.setattr("config.settings.gates_enabled", True)
    from core.extensions.evaluation import register as eval_register

    reg = _registry_with(eval_register)
    assert reg.get("add_gate").safety_level == "dangerous"


def test_skill_authoring_tools_are_dangerous():
    from core.extensions.skillmaker import register as skillmaker_register

    reg = _registry_with(skillmaker_register)
    assert reg.get("create_skill").safety_level == "dangerous"
    assert reg.get("add_skill_script").safety_level == "dangerous"


def test_bash_stays_caution_by_design():
    """Not an oversight. bash is the product's core utility and every
    dangerous-gated action is reachable through it, so prompting on each call
    would break the tool without adding a boundary."""
    from core.tools.builtin.core_tools import register as core_register

    reg = _registry_with(core_register)
    assert reg.get("bash").safety_level == "caution"


# ---------------------------------------------------------------------------
# 2. No model-supplied argument can self-authorize
# ---------------------------------------------------------------------------


def test_dangerous_skill_tools_take_no_approved_argument():
    from core.extensions.skillmaker import add_skill_script, create_skill

    for fn in (create_skill, add_skill_script):
        assert "approved" not in inspect.signature(fn).parameters, (
            f"{fn.__name__} still accepts a model-supplied approval argument"
        )


def test_approved_is_absent_from_the_dangerous_tools_schemas():
    from core.extensions.skillmaker import register as skillmaker_register

    reg = _registry_with(skillmaker_register)
    for name in ("create_skill", "add_skill_script"):
        props = (reg.get(name).parameters or {}).get("properties", {})
        assert "approved" not in props, f"{name} still advertises `approved`"


async def test_dangerous_gate_blocks_create_skill_without_server_side_approval(monkeypatch):
    """End-to-end through the executor: no approval state on the session, so
    the call is refused before the tool function ever runs."""
    from core.extensions.skillmaker import register as skillmaker_register
    from core.tools.executor import _execute_single

    monkeypatch.setattr("config.settings.auto_approve_dangerous", False)
    reg = _registry_with(skillmaker_register)
    result = await _execute_single(
        "create_skill",
        {"name": "x", "description": "d" * 20, "instructions": "i" * 40, "approved": True},
        None,
        reg,
    )
    assert result.was_error
    assert "requires explicit user approval" in result.content


# ---------------------------------------------------------------------------
# 3. Shell env default is scrubbed
# ---------------------------------------------------------------------------


def test_shell_env_mode_defaults_to_allowlist():
    from config import Settings

    fresh = Settings()
    assert fresh.shell_env_mode == "allowlist"
    # The allowlist must still carry what the sandbox needs; PATH/HOME are
    # additionally overridden by the bash tool itself.
    assert "PATH" in fresh.shell_env_allowlist
    assert "HOME" in fresh.shell_env_allowlist


def test_bash_child_env_excludes_secrets(monkeypatch, tmp_path):
    """The concrete leak: a secret in the server's environment must not reach
    the child under the default mode."""
    from core.tools.builtin import core_tools

    monkeypatch.setenv("PERNIX_TEST_FAKE_API_KEY", "sk-should-not-leak")
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr("config.settings.shell_env_mode", "allowlist")
    out = core_tools.bash("env")
    assert "sk-should-not-leak" not in out
    assert "PATH=" in out  # the sandbox env is still usable


# ---------------------------------------------------------------------------
# 4. Gate commands are subject to shell policy
# ---------------------------------------------------------------------------


def test_add_gate_refuses_a_denylisted_command(monkeypatch):
    monkeypatch.setattr("config.settings.gates_enabled", True)
    from core.extensions.evaluation import add_gate
    from db import models as db

    sid = db.create_session(title="gate-policy")
    out = add_gate("nasty", "sudo rm -rf /", _context={"session_id": sid})
    assert out.startswith("Error:")
    assert "not registered" in out
    assert db.get_gates(sid) == []


def test_add_gate_refuses_cwd_outside_the_workspace(monkeypatch):
    monkeypatch.setattr("config.settings.gates_enabled", True)
    from core.extensions.evaluation import add_gate
    from db import models as db

    sid = db.create_session(title="gate-cwd")
    out = add_gate("escape", "true", cwd="/", _context={"session_id": sid})
    assert out.startswith("Error:") and "workspace" in out
    assert db.get_gates(sid) == []


def test_gate_cwd_is_workspace_relative_not_process_relative(tmp_path, monkeypatch):
    """A relative cwd resolves against the workspace — the server's own
    process cwd is the source tree, which is exactly what containment is
    protecting."""
    from core.gates import resolve_gate_cwd

    ws = tmp_path / "workspace"
    (ws / "proj").mkdir(parents=True)
    assert resolve_gate_cwd("proj", ws) == (ws / "proj").resolve()
    assert resolve_gate_cwd("", ws) == ws.resolve()
    with pytest.raises(ValueError, match="inside the workspace"):
        resolve_gate_cwd("../..", ws)
    with pytest.raises(ValueError, match="inside the workspace"):
        resolve_gate_cwd("/", ws)


def test_legacy_gate_rows_are_refused_at_run_time_not_crashed(monkeypatch):
    """Rows predating registration-time validation still exist in the DB.
    _run_one refuses them (a gate that cannot run has verified nothing) while
    every other gate in the sweep still runs."""
    monkeypatch.setattr("config.settings.gates_enabled", True)
    from core.gates import run_gates_for_turn
    from core.tools.paths import workspace
    from db import models as db

    workspace().mkdir(parents=True, exist_ok=True)
    sid = db.create_session(title="legacy-gate")
    # Written straight to the table, as a pre-fix add_gate would have.
    db.add_gate(sid, "legacy", "sudo rm -rf /")
    db.add_gate(sid, "escapee", "true", cwd="/")
    db.add_gate(sid, "fine", "true")

    from types import SimpleNamespace

    results = {r.name: r for r in run_gates_for_turn(sid, SimpleNamespace(current_turn_user_msg_id=1), attempt=1)}
    assert not results["legacy"].passed and "security policy" in results["legacy"].error
    assert not results["escapee"].passed and "workspace" in results["escapee"].error
    assert results["fine"].passed  # unaffected by its neighbours


def test_gate_children_get_their_own_process_group(monkeypatch):
    """setsid, like bash: a timeout must be able to kill the whole tree."""
    monkeypatch.setattr("config.settings.gates_enabled", True)
    import os
    from types import SimpleNamespace

    from core.gates import run_gates_for_turn
    from core.tools.paths import workspace
    from db import models as db

    workspace().mkdir(parents=True, exist_ok=True)
    sid = db.create_session(title="gate-pgid")
    db.add_gate(sid, "pgid", 'python3 -c "import os;print(os.getpgid(0))"')

    r = run_gates_for_turn(sid, SimpleNamespace(current_turn_user_msg_id=1), attempt=1)[0]
    assert r.passed, r.output_tail or r.error
    # Without setsid the child would share the server's process group, and a
    # killpg on timeout would either miss the tree or hit the server itself.
    assert int(r.output_tail.strip()) != os.getpgrp()


# ---------------------------------------------------------------------------
# 5. repl timeout is clamped
# ---------------------------------------------------------------------------


def test_repl_clamps_a_runaway_timeout(monkeypatch):
    """repl(timeout=999999) must not set an 11-day cell deadline: the worker
    thread cannot be cancelled and the kernel child is unreachable by the
    executor's post-timeout kill."""
    from core.kernel import KernelError
    from core.tools.builtin import repl_tool

    monkeypatch.setattr("config.settings.session_kernel_enabled", True)
    seen = {}

    class _FakeKernel:
        def execute(self, code, timeout=None, cancel_check=None):
            seen["timeout"] = timeout
            raise KernelError("stopped")

    monkeypatch.setattr(
        "core.kernel.get_kernel_registry",
        lambda: type("R", (), {"get_or_create": lambda self, sid: _FakeKernel()})(),
    )
    repl_tool.repl("1", timeout=999999, _context={"session_id": "s"})
    assert seen["timeout"] == repl_tool._MAX_TIMEOUT_S

    repl_tool.repl("1", timeout=None, _context={"session_id": "s"})
    assert seen["timeout"] == repl_tool._DEFAULT_TIMEOUT_S

    repl_tool.repl("1", timeout=60, _context={"session_id": "s"})
    assert seen["timeout"] == 60.0


def test_repl_schema_ceiling_matches_the_clamp(monkeypatch):
    monkeypatch.setattr("config.settings.session_kernel_enabled", True)
    from core.tools.builtin.repl_tool import _MAX_TIMEOUT_S
    from core.tools.builtin.repl_tool import register as repl_register

    reg = _registry_with(repl_register)
    assert reg.get("repl").max_timeout == int(_MAX_TIMEOUT_S)


# ---------------------------------------------------------------------------
# 6. Truncation drill-in pointers resolve
# ---------------------------------------------------------------------------


def test_truncation_pointer_resolves_to_the_real_artifact(tmp_path, monkeypatch):
    """The exact failure: truncate_output emits a file_read(path=...) pointer
    that safe_read_path must resolve to the file that was actually written."""
    from core.tools import paths, truncation

    monkeypatch.setattr(truncation, "TOOL_OUTPUT_DIR", tmp_path / ".tool_output")
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path / "workspace"))

    preview, meta = truncation.truncate_output("x" * 60_000, "bash")
    assert meta["truncated"] and meta["output_path"]
    assert f'file_read(path="{meta["output_path"]}"' in preview

    resolved = paths.safe_read_path(meta["output_path"])
    assert resolved.exists(), f"drill-in pointer {meta['output_path']} is dead"
    assert resolved.read_text() == "x" * 60_000


def test_tool_output_is_read_only_never_a_write_root(tmp_path, monkeypatch):
    from core.tools import paths, truncation

    monkeypatch.setattr(truncation, "TOOL_OUTPUT_DIR", tmp_path / ".tool_output")
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path / "workspace"))
    assert paths.tool_output_root() in paths.allowed_read_roots()
    assert paths.tool_output_root() not in paths.allowed_write_roots()


# ---------------------------------------------------------------------------
# 7. Dispatch grace is unconditional
# ---------------------------------------------------------------------------


def test_dispatch_grace_applies_without_an_explicit_timeout():
    from core.tools.executor import _DISPATCH_TIMEOUT_GRACE_S, _resolve_timeout
    from core.tools.registry import ToolDef

    def _tool(timeout, max_timeout=0):
        return ToolDef(
            name="t",
            function=lambda: "",
            description="d",
            parameters={"type": "object", "properties": {}},
            timeout=timeout,
            max_timeout=max_timeout,
        )

    # Both shapes: no ceiling declared, and a ceiling with no caller override.
    assert _resolve_timeout(_tool(30), None) == 30 + _DISPATCH_TIMEOUT_GRACE_S
    assert _resolve_timeout(_tool(30, 1800), {}) == 30 + _DISPATCH_TIMEOUT_GRACE_S


# ---------------------------------------------------------------------------
# 8. .venv is not writable through the path tools
# ---------------------------------------------------------------------------


def test_file_write_refuses_the_workspace_venv(tmp_path, monkeypatch):
    """site-packages inside the write root is executable-in-process code:
    ensure_workspace_venv_on_path() puts it on sys.path for custom tools."""
    from core.tools import paths

    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path / "workspace"))
    assert ".venv" in paths.PROTECTED_DIRS
    target = ".venv/lib/python3.12/site-packages/evil.py"
    with pytest.raises(ValueError, match="Protected directory"):
        paths.safe_write_path(target)
    # And not reachable by absolute path either.
    with pytest.raises(ValueError, match="Protected directory"):
        paths.safe_write_path(str(tmp_path / "workspace" / target))
