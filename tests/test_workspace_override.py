"""Pernix — Session-scoped workspace override (adaptation plan 1g).

The override is per-call state: ToolRegistry.execute_sync sets
paths.WORKSPACE_OVERRIDE from the executor-supplied context before invoking
a tool function and always resets it after, so concurrent sessions on the
long-poll executor's reused threads can never see each other's override.
"""

from pathlib import Path

from config import settings
from core.tools import paths
from core.tools.registry import ToolRegistry


def _global_ws() -> Path:
    return Path(settings.workspace_dir).resolve()


# ---------------------------------------------------------------------------
# paths.workspace() semantics
# ---------------------------------------------------------------------------


def test_workspace_default_unchanged():
    """No override -> the global workspace leads the roots. /tmp rides along
    in default mode only (approved 2026-08-25 — bash always wrote there, the
    file-tool jail bought no containment); override mode excludes it, which
    test_safe_write_path_confined_to_override enforces."""
    assert paths.WORKSPACE_OVERRIDE.get() is None
    assert paths.workspace() == _global_ws()
    assert paths.allowed_write_roots() == [_global_ws(), Path("/tmp").resolve()]


def test_workspace_honors_override(tmp_path):
    override = tmp_path / "isolated"
    token = paths.WORKSPACE_OVERRIDE.set(str(override))
    try:
        assert paths.workspace() == override.resolve()
        assert paths.allowed_write_roots() == [override.resolve()]
    finally:
        paths.WORKSPACE_OVERRIDE.reset(token)
    assert paths.workspace() == _global_ws()


def test_safe_write_path_confined_to_override(tmp_path):
    """With an override active, writes resolve inside it and the global
    workspace becomes out-of-bounds."""
    override = tmp_path / "isolated"
    global_ws = _global_ws()
    global_ws.mkdir(parents=True, exist_ok=True)
    token = paths.WORKSPACE_OVERRIDE.set(str(override))
    try:
        resolved = paths.safe_write_path("notes.txt")
        assert resolved == (override.resolve() / "notes.txt")

        try:
            paths.safe_write_path(str(global_ws / "escape.txt"))
        except ValueError:
            pass
        else:  # pragma: no cover - failure branch
            raise AssertionError("global-workspace path accepted under override")
    finally:
        paths.WORKSPACE_OVERRIDE.reset(token)


# ---------------------------------------------------------------------------
# execute_sync plumbing
# ---------------------------------------------------------------------------


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        name="report_workspace",
        func=lambda: str(paths.workspace()),
        description="test tool reporting the active workspace root",
        parameters={"type": "object", "properties": {}},
        category="test",
    )
    return reg


def test_execute_sync_applies_and_resets_override(tmp_path):
    reg = _make_registry()
    override = tmp_path / "isolated"

    out = reg.execute_sync("report_workspace", {}, {"workspace_override": str(override)})
    assert out == str(override.resolve())

    # Reset must have happened even though the call succeeded...
    assert paths.WORKSPACE_OVERRIDE.get() is None
    # ...and a follow-up call without the key sees the global workspace.
    out = reg.execute_sync("report_workspace", {}, {"session_id": "x"})
    assert out == str(_global_ws())


def test_execute_sync_resets_override_on_tool_error(tmp_path):
    reg = ToolRegistry()

    def _boom() -> str:
        raise RuntimeError("boom")

    reg.register(
        name="boom",
        func=_boom,
        description="always raises",
        parameters={"type": "object", "properties": {}},
        category="test",
    )
    out = reg.execute_sync("boom", {}, {"workspace_override": str(tmp_path / "iso")})
    assert out.startswith("Error:")
    assert paths.WORKSPACE_OVERRIDE.get() is None


def test_file_tools_end_to_end_under_override(tmp_path):
    """file_write/file_read confined to the override via the real tools."""
    from core.tools.builtin.core_tools import file_read, file_write

    reg = ToolRegistry()
    reg.register(
        name="file_write",
        func=file_write,
        description="write",
        parameters={"type": "object", "properties": {}},
        category="test",
    )
    reg.register(
        name="file_read",
        func=file_read,
        description="read",
        parameters={"type": "object", "properties": {}},
        category="test",
    )
    override = tmp_path / "isolated"
    ctx = {"workspace_override": str(override)}

    reg.execute_sync("file_write", {"path": "a.txt", "content": "hello"}, ctx)
    assert (override.resolve() / "a.txt").read_text() == "hello"
    assert not (_global_ws() / "a.txt").exists()

    out = reg.execute_sync("file_read", {"path": "a.txt"}, ctx)
    assert "hello" in out

    # Without the override the same relative path is a different file.
    out = reg.execute_sync("file_read", {"path": "a.txt"}, {})
    assert "hello" not in out
