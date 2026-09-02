"""update_skill silently deleted the rest of a skill's frontmatter.

It rebuilt SKILL.md through _build_skill_md, which emits exactly four keys
(name, description, tags, version). Everything else the file carried was
dropped — including `verify:`, whose loss makes the canary maintenance
sweep retire the skill's behavioural canary as "verify block removed" and
purge it 30 days later, and `scripts:`, the contract the skill's own
helper programs are declared under. The tool is safety_level="safe" and
its `approved` flag is model-supplied, so nothing stood in front of it.
"""

import pytest
import yaml

from core.extensions import skillmaker


@pytest.fixture
def skill(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    d = tmp_path / "deploy-check"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\n"
        "name: deploy-check\n"
        "description: verify a deploy\n"
        "tags:\n"
        "- ops\n"
        "version: '1.2'\n"
        "verify:\n"
        "  command: pytest tests/test_deploy.py\n"
        "  expect_exit: 0\n"
        "scripts:\n"
        "  check.sh: runs the smoke test\n"
        "---\n\n"
        "Original instructions.\n",
        encoding="utf-8",
    )
    return d


def _frontmatter(skill_dir):
    raw = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    return yaml.safe_load(raw.split("---")[1])


def test_the_verify_block_survives_an_instructions_edit(skill):
    skillmaker.update_skill("deploy-check", instructions="Better instructions.", approved=True)
    fm = _frontmatter(skill)
    assert fm["verify"]["command"] == "pytest tests/test_deploy.py", "losing this retires the skill's canary"
    assert fm["scripts"] == {"check.sh": "runs the smoke test"}


def test_the_touched_keys_are_still_updated(skill):
    skillmaker.update_skill("deploy-check", description="a better description", tags="ops,ci", approved=True)
    fm = _frontmatter(skill)
    assert fm["description"] == "a better description"
    assert fm["tags"] == ["ops", "ci"]
    assert fm["version"] == "1.2", "untouched keys keep their value"


def test_the_body_is_replaced_and_a_backup_is_left(skill):
    skillmaker.update_skill("deploy-check", instructions="New body.", approved=True)
    assert "New body." in (skill / "SKILL.md").read_text(encoding="utf-8")
    assert (skill / "SKILL.md.bak").exists(), "a bad update must be recoverable"
    assert "Original instructions." in (skill / "SKILL.md.bak").read_text(encoding="utf-8")
