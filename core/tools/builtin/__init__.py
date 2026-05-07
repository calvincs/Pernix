"""Pernix — Built-in tool loader. Discovers and registers all tool modules."""

from __future__ import annotations

import importlib
import logging
import pkgutil

from core.tools.registry import ToolRegistry

logger = logging.getLogger("pernix.tools.loader")


def load_builtin_tools(registry: ToolRegistry) -> None:
    """Auto-discover and register all built-in tool modules.

    Scans this package for modules with a register(reg) function.
    Skips modules starting with _.

    Custom tool modules (custom_*.py) are treated specially:
    - The workspace venv is added to sys.path before import so their
      package dependencies (installed via install_package) resolve.
    - Any tools they register are marked source='custom' so the rest of
      the system knows they use the workspace venv, not the project venv.
    """
    import core.tools.builtin as pkg
    from core.tools.paths import ensure_workspace_venv_on_path

    loaded = 0
    for _importer, modname, _ispkg in pkgutil.iter_modules(pkg.__path__):
        if modname.startswith("_") or modname == "sandbox":
            continue
        is_custom = modname.startswith("custom_")
        try:
            if is_custom:
                ensure_workspace_venv_on_path()
            before = set(registry._tools.keys())
            mod = importlib.import_module(f"core.tools.builtin.{modname}")
            if hasattr(mod, "register"):
                mod.register(registry)
                loaded += 1
                logger.debug("Loaded tool module: %s", modname)
                if is_custom:
                    for tname in set(registry._tools.keys()) - before:
                        registry._tools[tname].source = "custom"
        except Exception as e:
            if is_custom:
                logger.error("Custom tool failed to load: %s: %s", modname, e)
            else:
                logger.warning("Failed to load tool module %s: %s", modname, e)

    logger.info("Loaded %d built-in tool modules", loaded)
