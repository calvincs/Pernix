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
    Skips modules starting with _ or custom_.
    """
    import core.tools.builtin as pkg

    loaded = 0
    for _importer, modname, _ispkg in pkgutil.iter_modules(pkg.__path__):
        if modname.startswith("_") or modname == "sandbox":
            continue
        try:
            mod = importlib.import_module(f"core.tools.builtin.{modname}")
            if hasattr(mod, "register"):
                mod.register(registry)
                loaded += 1
                logger.debug("Loaded tool module: %s", modname)
        except Exception as e:
            if modname.startswith("custom_"):
                logger.error("Custom tool failed to load: %s: %s", modname, e)
            else:
                logger.warning("Failed to load tool module %s: %s", modname, e)

    logger.info("Loaded %d built-in tool modules", loaded)
