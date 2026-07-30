"""Pernix — Extension protocol and loader.

Extensions register their tools in the discovery index alongside built-in tools.
They are discoverable via discover_tools() on equal footing.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.tools.registry import ToolRegistry

logger = logging.getLogger("pernix.extensions")

# Extensions that ship with the project
BUNDLED_EXTENSIONS = [
    "core.extensions.web",
    "core.extensions.orchestration",
    "core.extensions.evaluation",
    "core.extensions.scheduling",
    "core.extensions.toolmaker",
    "core.extensions.model_mgmt",
    "core.extensions.session_tools",
    "core.extensions.planning",
    "core.extensions.skillmaker",
    "core.extensions.candor",
    "core.extensions.rlm",
]


@dataclass
class ExtensionInfo:
    """Metadata about a loaded extension."""

    name: str
    module: str
    tools_registered: list[str] = field(default_factory=list)


def load_extensions(registry: ToolRegistry) -> list[ExtensionInfo]:
    """Load all bundled extensions, registering their tools.

    Each extension module must have a register(reg: ToolRegistry) function.
    """
    loaded = []
    for module_path in BUNDLED_EXTENSIONS:
        try:
            mod = importlib.import_module(module_path)
            if not hasattr(mod, "register"):
                logger.warning("Extension %s has no register() function", module_path)
                continue

            # Count tools before
            before = set(t.name for t in registry.all_tools())
            mod.register(registry)
            after = set(t.name for t in registry.all_tools())
            new_tools = sorted(after - before)

            info = ExtensionInfo(
                name=module_path.split(".")[-1],
                module=module_path,
                tools_registered=new_tools,
            )
            loaded.append(info)
            logger.info("Loaded extension '%s': %d tools (%s)", info.name, len(new_tools), ", ".join(new_tools))
        except Exception as e:
            logger.warning("Failed to load extension %s: %s", module_path, e)

    return loaded
