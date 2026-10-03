"""install_package lives in its own small `packages` extension (2026-10 prune).

It used to ship inside the toolmaker extension; the toolmaker is gone but the
tool stays, because worker kinds and the skills health check name it.
"""

from __future__ import annotations

from core.extensions import BUNDLED_EXTENSIONS, load_extensions
from core.tools.registry import ToolRegistry


def test_packages_extension_is_bundled():
    assert "core.extensions.packages" in BUNDLED_EXTENSIONS


def test_install_package_registers_as_an_extension_tool():
    reg = ToolRegistry()
    infos = load_extensions(reg)
    tool = reg.get("install_package")
    assert tool is not None
    assert tool.source == "extension"
    assert tool.category == "packages"
    assert "workspace venv" in tool.description
    assert any(i.name == "packages" and "install_package" in i.tools_registered for i in infos)


def test_install_package_rejects_flag_injection():
    from core.extensions.packages import install_package

    assert install_package("--index-url=http://evil").startswith("Error")
    assert install_package("requests; rm -rf /").startswith("Error")
