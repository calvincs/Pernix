"""Regression: a leftover custom_*.py must not load as a builtin.

The toolmaker (create_tool/update_tool) was retired in the 2026-10 prune.
Deployments may still carry agent-written core/tools/builtin/custom_*.py
files (gitignored, so a pull leaves them in place). Without a guard the
builtin loader would import them like any other module, register their tools
as source="builtin" — forced into every schema with a "safe" safety level —
and run their module-level code in the server process. The loader skips them
with a warning instead.
"""

from __future__ import annotations

import logging
import pkgutil

import core.tools.builtin as builtin_pkg
from core.tools.builtin import load_builtin_tools
from core.tools.registry import ToolRegistry


def test_custom_module_is_skipped_never_imported(monkeypatch, caplog):
    real_iter = pkgutil.iter_modules
    imported: list[str] = []

    def _iter(path):
        yield from real_iter(path)
        yield (None, "custom_leftover", False)

    import importlib

    real_import = importlib.import_module

    def _import(name, *a, **kw):
        imported.append(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtin_pkg.pkgutil, "iter_modules", _iter)
    monkeypatch.setattr(builtin_pkg.importlib, "import_module", _import)

    reg = ToolRegistry()
    with caplog.at_level(logging.WARNING, logger="pernix.tools.loader"):
        load_builtin_tools(reg)

    assert "core.tools.builtin.custom_leftover" not in imported
    assert any("legacy custom tool custom_leftover ignored" in r.getMessage() for r in caplog.records)
    # The real builtins still load.
    assert reg.get("bash") is not None
    assert all(t.source != "custom" for t in reg.all_tools())


def test_toolmaker_tools_are_gone():
    from core.extensions import BUNDLED_EXTENSIONS, load_extensions

    assert "core.extensions.toolmaker" not in BUNDLED_EXTENSIONS
    reg = ToolRegistry()
    load_builtin_tools(reg)
    load_extensions(reg)
    for name in ("create_tool", "update_tool", "list_custom_tools", "restore_tool_packages"):
        assert reg.get(name) is None, name
    assert reg.get("install_package") is not None
