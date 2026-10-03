"""Pernix — Packages extension: pip installs into the workspace venv.

The workspace venv (data/workspace/.venv) is the one bash, the Python REPL and
skill scripts run in, so a package installed here is importable from all
three. The project venv the server itself runs in is never touched.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from config import settings


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
    reg.register(
        name="install_package",
        func=install_package,
        description=(
            "Install a Python package into the workspace venv (data/workspace/.venv), "
            "importable from bash, repl and skill scripts. "
            "The server's own project venv is never touched."
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
        category="packages",
        source="extension",
    )
