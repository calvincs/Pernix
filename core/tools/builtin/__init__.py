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

    Leftover agent-authored modules (custom_*.py) from the retired toolmaker
    are skipped, never imported: loaded from here they would register as
    builtins, forced into every schema with a "safe" safety level.
    """
    import core.tools.builtin as pkg

    loaded = 0
    for _importer, modname, _ispkg in pkgutil.iter_modules(pkg.__path__):
        if modname.startswith("_") or modname == "sandbox":
            continue
        if modname.startswith("custom_"):
            logger.warning("legacy custom tool %s ignored", modname)
            continue
        try:
            mod = importlib.import_module(f"core.tools.builtin.{modname}")
            if hasattr(mod, "register"):
                mod.register(registry)
                loaded += 1
                logger.debug("Loaded tool module: %s", modname)
        except Exception as e:
            logger.warning("Failed to load tool module %s: %s", modname, e)

    logger.info("Loaded %d built-in tool modules", loaded)
