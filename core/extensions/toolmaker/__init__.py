"""Pernix — Toolmaker extension: agent self-tooling."""

from __future__ import annotations

import importlib
import logging
import re
import subprocess
import sys
from pathlib import Path

from config import settings

logger = logging.getLogger("pernix.ext.toolmaker")

from core.tools.paths import ensure_workspace_venv_on_path

CUSTOM_TOOLS_DIR = Path("core/tools/builtin")

PROHIBITED_PATTERNS = [
    "os.system",
    "__import__('os')",
    "subprocess.Popen",
    "open('/etc",
    "open('/dev",
    "socket.socket",
    "eval(",
    "exec(",
    "--break-system-packages",
    "/usr/bin/pip",
    "/usr/local/bin/pip",
]


def _parse_requirements(raw: str) -> list[str]:
    """Parse a requirements string (newline- or comma-separated) into a clean list."""
    pkgs = []
    for line in re.split(r"[\n,]", raw):
        line = line.strip()
        if line and not line.startswith("#"):
            pkgs.append(line)
    return pkgs


def _write_requirements_file(name: str, pkgs: list[str]) -> None:
    req_file = CUSTOM_TOOLS_DIR / f"custom_{name}.requirements.txt"
    lines = [
        f"# Requirements for custom tool: {name}",
        f"# Reinstall: pip install -r custom_{name}.requirements.txt",
        "",
    ] + pkgs
    req_file.write_text("\n".join(lines) + "\n")


def create_tool(
    name: str, description: str, code: str, tags: str = "", requirements: str = "", _context: dict | None = None
) -> str:
    """Create a custom tool. The code must define a function and a register(reg) function."""
    # Validate name
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        return "Error: Tool name must be lowercase alphanumeric with underscores"

    if len(name) > 50:
        return "Error: Tool name too long (max 50 chars)"

    # Check prohibited patterns
    for pattern in PROHIBITED_PATTERNS:
        if pattern in code:
            return f"Error: Prohibited pattern '{pattern}' in code"

    # Validate syntax
    try:
        compile(code, f"custom_{name}.py", "exec")
    except SyntaxError as e:
        return f"Error: Syntax error in code: {e}"

    # Check for register function
    if "def register(" not in code:
        return "Error: Code must define a register(reg) function"

    # Write module
    filepath = CUSTOM_TOOLS_DIR / f"custom_{name}.py"
    filepath.write_text(code)

    # Hot-load
    try:
        from core.tools.registry import get_registry

        registry = get_registry()
        ensure_workspace_venv_on_path()
        mod = importlib.import_module(f"core.tools.builtin.custom_{name}")
        importlib.reload(mod)

        if not hasattr(mod, "register") or not callable(mod.register):
            filepath.unlink(missing_ok=True)
            return (
                "Error: Tool code must define a callable 'register(reg)' function. "
                f"Found a non-callable 'register' attribute ({type(getattr(mod, 'register', None)).__name__}) "
                "— ensure 'register' is a function, not reassigned to another value."
            )

        mod.register(registry)
        registry.rebuild_index()

        if not registry.exists(name):
            filepath.unlink(missing_ok=True)
            return f"Error: Tool '{name}' not found after registration"

        # Mark as custom and cap timeout
        tool = registry.get(name)
        if tool:
            tool.source = "custom"
            if tool.timeout > 60:
                tool.timeout = 60

        # Write requirements.txt and install packages
        req_summary = ""
        pkgs = _parse_requirements(requirements)
        if pkgs:
            _write_requirements_file(name, pkgs)
            installed = sum(1 for p in pkgs if not install_package(p).startswith("Error"))
            req_summary = f" Packages installed: {installed}/{len(pkgs)}."

        return f"Tool '{name}' created and available for discovery.{req_summary}"
    except Exception as e:
        filepath.unlink(missing_ok=True)
        return (
            f"Error loading tool '{name}': {type(e).__name__}: {e}. "
            f"Your register(reg) must call: "
            f"reg.register(name='tool_name', func=my_func, description='...', "
            f"parameters={{'type':'object','properties':{{...}},'required':[...]}})"
        )


def update_tool(name: str, code: str, requirements: str = "", _context: dict | None = None) -> str:
    """Update an existing custom tool. Keeps backup of previous version."""
    filepath = CUSTOM_TOOLS_DIR / f"custom_{name}.py"
    if not filepath.exists():
        return f"Error: Custom tool '{name}' not found"

    # Check prohibited patterns
    for pattern in PROHIBITED_PATTERNS:
        if pattern in code:
            return f"Error: Prohibited pattern '{pattern}' in code"

    # Backup
    import shutil

    version = 1
    while (CUSTOM_TOOLS_DIR / f"custom_{name}.v{version}.bak").exists():
        version += 1
    shutil.copy2(filepath, CUSTOM_TOOLS_DIR / f"custom_{name}.v{version}.bak")

    # Write new version
    filepath.write_text(code)

    # Reload
    try:
        from core.tools.registry import get_registry

        registry = get_registry()
        ensure_workspace_venv_on_path()
        mod = importlib.import_module(f"core.tools.builtin.custom_{name}")
        importlib.reload(mod)

        if not hasattr(mod, "register") or not callable(mod.register):
            return (
                "Error: Updated tool code must define a callable 'register(reg)' function. "
                f"Found a non-callable 'register' attribute ({type(getattr(mod, 'register', None)).__name__})."
            )

        mod.register(registry)
        registry.rebuild_index()

        # Re-mark as custom
        tool = registry.get(name)
        if tool:
            tool.source = "custom"

        # Update requirements if provided; leave existing file untouched if not
        if requirements:
            pkgs = _parse_requirements(requirements)
            if pkgs:
                _write_requirements_file(name, pkgs)

        return f"Tool '{name}' updated (backup: v{version})"
    except Exception as e:
        return (
            f"Error reloading tool '{name}': {type(e).__name__}: {e}. "
            f"Your register(reg) must call: "
            f"reg.register(name='tool_name', func=my_func, description='...', "
            f"parameters={{'type':'object','properties':{{...}},'required':[...]}})"
        )


def list_custom_tools(_context: dict | None = None) -> str:
    """List all agent-created custom tools."""
    custom_files = sorted(CUSTOM_TOOLS_DIR.glob("custom_*.py"))
    if not custom_files:
        return "No custom tools created."
    lines = []
    for f in custom_files:
        name = f.stem.replace("custom_", "")
        size = f.stat().st_size
        req_file = CUSTOM_TOOLS_DIR / f"custom_{name}.requirements.txt"
        req_str = ""
        if req_file.exists():
            pkgs = _parse_requirements(req_file.read_text())
            if pkgs:
                req_str = f" [requires: {', '.join(pkgs)}]"
        lines.append(f"- {name} ({size} bytes){req_str}")
    return "\n".join(lines)


def restore_tool_packages(name: str, _context: dict | None = None) -> str:
    """Reinstall a custom tool's declared packages into the workspace venv.

    Use this after the workspace venv is rebuilt or corrupted to restore
    all packages the tool declared when it was created.
    """
    req_file = CUSTOM_TOOLS_DIR / f"custom_{name}.requirements.txt"
    if not req_file.exists():
        return f"No requirements file for '{name}'. Pass requirements= when calling create_tool."
    pkgs = _parse_requirements(req_file.read_text())
    if not pkgs:
        return f"requirements.txt for '{name}' is empty — nothing to restore."
    results = [install_package(p) for p in pkgs]
    ok = sum(1 for r in results if not r.startswith("Error"))
    return f"Restored {ok}/{len(pkgs)} packages for '{name}': {', '.join(pkgs)}"


def install_package(package: str, _context: dict | None = None) -> str:
    """Install a Python package via pip in the workspace virtual environment."""
    # Basic validation
    if not re.match(r"^[a-zA-Z0-9._-]+([=<>!]+[a-zA-Z0-9._-]+)?$", package):
        return f"Error: Invalid package name: {package}"

    # Block flag injection via package name
    if "--" in package:
        return f"Error: Invalid package name (flags not allowed): {package}"

    # Always use workspace venv python, never system python
    workspace_venv_python = Path(settings.workspace_dir).resolve() / ".venv" / "bin" / "python"

    # Auto-create workspace venv if missing
    if not workspace_venv_python.exists():
        venv_dir = Path(settings.workspace_dir).resolve() / ".venv"
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception as e:
            return f"Error: Failed to create workspace venv: {e}"
        if not workspace_venv_python.exists():
            return "Error: Failed to create workspace venv"

    try:
        result = subprocess.run(
            [str(workspace_venv_python), "-m", "pip", "install", package],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            return f"Installed: {package}\n{output[-200:]}"
        return f"Error installing {package}:\n{output[-500:]}"
    except subprocess.TimeoutExpired:
        return "Error: pip install timed out after 120s"


def register(reg) -> None:
    common = {"category": "toolmaker", "source": "extension"}
    tags = ["tool", "create", "custom", "make", "code", "extend", "plugin"]

    reg.register(
        name="create_tool",
        func=create_tool,
        description=(
            "Create a custom tool (source='custom', uses workspace venv). "
            "Code must define a function and register(reg) that calls: "
            "reg.register(name='tool_name', func=my_func, description='...', "
            "parameters={'type':'object','properties':{...},'required':[...]}). "
            "Pass requirements='pkg1\\npkg2' to record and install dependencies — "
            "use restore_tool_packages to reinstall after a workspace venv rebuild."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tool name (lowercase, alphanumeric)"},
                "description": {"type": "string", "description": "What the tool does"},
                "code": {"type": "string", "description": "Python code defining the tool and register(reg)"},
                "tags": {"type": "string", "description": "Comma-separated discovery tags"},
                "requirements": {
                    "type": "string",
                    "description": (
                        "Newline- or comma-separated pip packages this tool needs "
                        "(e.g. 'pychromecast==14.0.10\\nroku'). Saved to requirements.txt "
                        "and installed into workspace venv automatically."
                    ),
                },
            },
            "required": ["name", "description", "code"],
        },
        tags=tags + ["new", "build"],
        timeout=60,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="update_tool",
        func=update_tool,
        description="Update an existing custom tool. Previous version backed up. Pass requirements= to update the tool's requirements.txt.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "code": {"type": "string"},
                "requirements": {
                    "type": "string",
                    "description": "Updated pip requirements (leave empty to keep existing).",
                },
            },
            "required": ["name", "code"],
        },
        tags=tags + ["update", "edit", "modify"],
        timeout=60,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="list_custom_tools",
        func=list_custom_tools,
        description="List all agent-created custom tools, including their declared requirements.",
        parameters={"type": "object", "properties": {}},
        tags=tags + ["list", "show"],
        timeout=15,
        parallel_safe=True,
        **common,
    )
    reg.register(
        name="restore_tool_packages",
        func=restore_tool_packages,
        description=(
            "Reinstall a custom tool's declared packages into the workspace venv "
            "(data/workspace/.venv). Use after the workspace venv is rebuilt or corrupted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Custom tool name"},
            },
            "required": ["name"],
        },
        tags=["tool", "restore", "repair", "requirements", "venv", "recovery", "dependencies"],
        timeout=180,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
    reg.register(
        name="install_package",
        func=install_package,
        description=(
            "Install a Python package into the workspace venv (data/workspace/.venv). "
            "Available to custom tools (source='custom'). "
            "Core built-in tools use the project venv instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "Package name (e.g. 'requests' or 'pandas==2.0')"}
            },
            "required": ["package"],
        },
        tags=["pip", "install", "package", "dependency", "library"],
        timeout=120,
        parallel_safe=False,
        safety_level="safe",
        **common,
    )
