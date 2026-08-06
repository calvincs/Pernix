"""Pernix — Skill health checks + script contracts (adaptation plan 1d).

Scan-time validation now records the concrete reasons (dict, not set), checks
shell syntax and requirements satisfaction, and load_skill surfaces the
reason + fix up front instead of the agent discovering breakage mid-task.
"""

from pathlib import Path

from core.skills.registry import SkillRegistry


def _make_skill(tmp_path: Path, name: str, extra_fm: str = "", body: str = "# Do things") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    fm = f"name: {name}\ndescription: test skill {name}"
    if extra_fm:
        fm += "\n" + extra_fm
    (d / "SKILL.md").write_text(f"---\n{fm}\n---\n{body}")
    return d


# ---------------------------------------------------------------------------
# Registry validation
# ---------------------------------------------------------------------------


def test_broken_python_script_records_reason(tmp_path):
    d = _make_skill(tmp_path, "pybroken")
    (d / "scripts").mkdir()
    (d / "scripts" / "fetch.py").write_text("def broken(:\n    pass\n")

    reg = SkillRegistry()
    reg.scan(tmp_path)
    assert not reg.is_valid("pybroken")
    issues = reg.validation_issues("pybroken")
    assert issues and "fetch.py" in issues[0] and "syntax" in issues[0].lower()
    # Healthy skills report empty issues.
    assert reg.validation_issues("nonexistent") == []


def test_broken_shell_script_records_reason(tmp_path):
    d = _make_skill(tmp_path, "shbroken")
    (d / "scripts").mkdir()
    (d / "scripts" / "run.sh").write_text("if [ true ; then\necho hi\n")

    reg = SkillRegistry()
    reg.scan(tmp_path)
    assert not reg.is_valid("shbroken")
    assert any("run.sh" in i and "shell syntax" in i for i in reg.validation_issues("shbroken"))


def test_requirements_missing_package_flagged(tmp_path, monkeypatch):
    from config import settings

    # Fake workspace venv with exactly one installed dist: foo 1.0.
    site = Path(settings.workspace_dir) / ".venv" / "lib" / "python3.12" / "site-packages"
    (site / "foo-1.0.dist-info").mkdir(parents=True)

    d = _make_skill(tmp_path, "reqskill")
    (d / "requirements.txt").write_text("foo>=1.0\nbar_pkg==2.0  # comment\n")

    reg = SkillRegistry()
    reg.scan(tmp_path)
    issues = reg.validation_issues("reqskill")
    assert len(issues) == 1
    assert "bar_pkg" in issues[0] and "install_package" in issues[0]


def test_requirements_without_venv_flagged_softly(tmp_path):
    d = _make_skill(tmp_path, "novenv")
    (d / "requirements.txt").write_text("somepkg\n")

    reg = SkillRegistry()
    reg.scan(tmp_path)
    issues = reg.validation_issues("novenv")
    assert issues and "venv not created yet" in issues[0]


def test_scripts_meta_parsed_from_frontmatter(tmp_path):
    extra = "scripts:\n  - path: scripts/check.sh\n    purpose: verify a URL\n    usage: bash scripts/check.sh <url>\n  - malformed-entry\n"
    d = _make_skill(tmp_path, "contract", extra_fm=extra)
    (d / "scripts").mkdir()
    (d / "scripts" / "check.sh").write_text("echo ok\n")

    reg = SkillRegistry()
    reg.scan(tmp_path)
    skill = reg.get("contract")
    assert len(skill.scripts_meta) == 1  # malformed entry dropped
    assert skill.scripts_meta[0]["path"] == "scripts/check.sh"
    assert skill.scripts_meta[0]["purpose"] == "verify a URL"


# ---------------------------------------------------------------------------
# load_skill surfacing
# ---------------------------------------------------------------------------


def test_load_skill_surfaces_health_and_contract(tmp_path, monkeypatch):
    extra = (
        "scripts:\n  - path: scripts/fetch.py\n    purpose: fetch a page\n    usage: python scripts/fetch.py <url>\n"
    )
    d = _make_skill(tmp_path, "sick", extra_fm=extra, body="# Steps\n1. Run the script.")
    (d / "scripts").mkdir()
    (d / "scripts" / "fetch.py").write_text("def broken(:\n")

    reg = SkillRegistry()
    reg.scan(tmp_path)
    monkeypatch.setattr("core.skills.registry.get_skill_registry", lambda: reg)

    from core.tools.builtin.skill_tools import load_skill

    out = load_skill("sick")
    assert out.index("[SKILL HEALTH]") == 0  # warning comes first
    assert "fetch.py" in out and "syntax" in out.lower()
    assert "# Steps" in out  # instructions still load
    assert "**Scripts:**" in out
    assert "fetch a page" in out
    assert "python scripts/fetch.py <url>" in out


def test_load_skill_healthy_no_warning(tmp_path, monkeypatch):
    _make_skill(tmp_path, "healthy", body="# Fine")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    monkeypatch.setattr("core.skills.registry.get_skill_registry", lambda: reg)

    from core.tools.builtin.skill_tools import load_skill

    out = load_skill("healthy")
    assert "[SKILL HEALTH]" not in out
    assert "# Fine" in out


# ---------------------------------------------------------------------------
# Skillmaker contract writing
# ---------------------------------------------------------------------------


def test_upsert_script_contract(tmp_path):
    from core.extensions.skillmaker import _upsert_script_contract
    from core.skills.parser import parse_skill_md

    d = _make_skill(tmp_path, "maker")
    _upsert_script_contract(d, "scripts/go.sh", "does the thing", "bash scripts/go.sh")
    fm, body = parse_skill_md(d / "SKILL.md")
    assert fm["scripts"] == [{"path": "scripts/go.sh", "purpose": "does the thing", "usage": "bash scripts/go.sh"}]
    assert "# Do things" in body

    # Update in place, not duplicate.
    _upsert_script_contract(d, "scripts/go.sh", "does it better", "")
    fm, _ = parse_skill_md(d / "SKILL.md")
    assert len(fm["scripts"]) == 1
    assert fm["scripts"][0]["purpose"] == "does it better"
    assert fm["scripts"][0]["usage"] == "bash scripts/go.sh"


# ---------------------------------------------------------------------------
# Snooze requirements install (Activity 2c)
# ---------------------------------------------------------------------------


async def test_snooze_installs_requirements_once(tmp_path, monkeypatch):
    from config import settings
    from core.snooze import get_snooze
    from db import models as db

    # Dummy venv python so the activity doesn't bail early.
    venv_py = Path(settings.workspace_venv_python)
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("#!/bin/sh\n")

    d = _make_skill(tmp_path, "reqinstall")
    (d / "requirements.txt").write_text("leftpad==1.0\n")

    reg = SkillRegistry()
    reg.scan(tmp_path)
    monkeypatch.setattr("core.skills.registry.get_skill_registry", lambda: reg)
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))

    calls = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: calls.append(a) or _Proc())

    snooze = get_snooze()
    # Outside a live cycle the generation counters differ (cancelled=True by
    # design); in production this method only runs inside a cycle.
    monkeypatch.setattr(snooze, "_is_cancelled", lambda: False)
    await snooze._install_skill_requirements()
    assert len(calls) == 1
    assert db.get_snooze_state("skill_reqs_hash:reqinstall")

    # Unchanged hash -> no second install.
    await snooze._install_skill_requirements()
    assert len(calls) == 1

    # Changed requirements -> reinstall.
    (d / "requirements.txt").write_text("leftpad==2.0\n")
    await snooze._install_skill_requirements()
    assert len(calls) == 2


async def test_snooze_install_failure_leaves_no_watermark(tmp_path, monkeypatch):
    from config import settings
    from core.snooze import get_snooze
    from db import models as db

    venv_py = Path(settings.workspace_venv_python)
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("#!/bin/sh\n")

    d = _make_skill(tmp_path, "reqfail")
    (d / "requirements.txt").write_text("nosuchpkg\n")

    reg = SkillRegistry()
    reg.scan(tmp_path)
    monkeypatch.setattr("core.skills.registry.get_skill_registry", lambda: reg)
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "ERROR: no matching distribution"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())

    snooze = get_snooze()
    monkeypatch.setattr(snooze, "_is_cancelled", lambda: False)
    await snooze._install_skill_requirements()
    assert db.get_snooze_state("skill_reqs_hash:reqfail") is None
