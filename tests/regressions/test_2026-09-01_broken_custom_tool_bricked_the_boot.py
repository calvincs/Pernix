"""Agent-authored tool code could break the next boot, silently.

Custom modules are re-imported on every start. A module that raises at
import was logged and skipped, so the same failure repeated forever and
the tool was simply missing with no signal; one that BLOCKS at import (a
module-level loop or network wait) never returned and the app never
finished starting. Meanwhile update_tool wrote the new source BEFORE
trying to load it, so a syntax error replaced a working tool with a broken
file and the error message talked about register() rather than the tool
that had just been lost.
"""

import pytest

from core.extensions import toolmaker


@pytest.fixture
def tools_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(toolmaker, "CUSTOM_TOOLS_DIR", tmp_path)
    return tmp_path


_GOOD = "def register(reg):\n    pass\n"


def test_create_refuses_code_that_does_not_compile(tools_dir):
    out = toolmaker.create_tool("broken", "desc", "def register(reg:\n")
    assert "Syntax error" in out
    assert not list(tools_dir.glob("custom_*.py")), "nothing may reach disk"


def test_update_refuses_bad_syntax_without_touching_the_working_file(tools_dir):
    path = tools_dir / "custom_greet.py"
    path.write_text(_GOOD)
    out = toolmaker.update_tool("greet", "def register(reg:\n")
    assert "does not compile" in out
    assert path.read_text() == _GOOD, "the working tool must be untouched"


def test_a_failed_reload_restores_the_previous_version(tools_dir, monkeypatch):
    path = tools_dir / "custom_greet.py"
    path.write_text(_GOOD)

    def _boom(*a, **k):
        raise RuntimeError("register blew up")

    monkeypatch.setattr(toolmaker.importlib, "import_module", _boom)
    out = toolmaker.update_tool("greet", "def register(reg):\n    return 1\n")
    assert "Error reloading" in out
    assert "restored" in out.lower()
    assert path.read_text() == _GOOD, "a tool that fails to load must not replace the working one"


def test_a_broken_module_is_quarantined_at_boot(tmp_path, monkeypatch):
    from core.tools.builtin import _quarantine_custom_module

    class _Pkg:
        __path__ = [str(tmp_path)]

    bad = tmp_path / "custom_bad.py"
    bad.write_text("raise RuntimeError('at import time')\n")
    _quarantine_custom_module(_Pkg(), "custom_bad", RuntimeError("at import time"))

    assert not bad.exists(), "the next boot must not re-import it"
    assert (tmp_path / "custom_bad.py.broken").exists(), "and the source is kept for inspection"
