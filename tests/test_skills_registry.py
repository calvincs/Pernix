"""Tests for core/skills/registry.py and parser.py."""

from pathlib import Path

import pytest

from core.skills.parser import SkillParseError, parse_skill_md
from core.skills.registry import (
    SkillIndex,
    SkillRegistry,
    _expand_synonyms,
    _tokenize,
)

# ---------------------------------------------------------------------------
# parser: parse_skill_md
# ---------------------------------------------------------------------------


def _make_skill_dir(tmp_path, name, frontmatter_yaml, body="# Instructions\nDo things."):
    """Helper to create a skill directory with SKILL.md."""
    d = tmp_path / name
    d.mkdir()
    skill_md = d / "SKILL.md"
    skill_md.write_text(f"---\n{frontmatter_yaml}\n---\n{body}")
    return d


def test_parse_skill_md_basic(tmp_path):
    _make_skill_dir(tmp_path, "my-skill", "name: my-skill\ndescription: A test skill\ntags: code,test")
    fm, body = parse_skill_md(tmp_path / "my-skill" / "SKILL.md")
    assert fm["name"] == "my-skill"
    assert fm["description"] == "A test skill"
    assert "code" in fm["tags"]
    assert "test" in fm["tags"]
    assert "Instructions" in body


def test_parse_skill_md_missing_frontmatter(tmp_path):
    d = tmp_path / "bad-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("No frontmatter here")
    with pytest.raises(SkillParseError):
        parse_skill_md(d / "SKILL.md")


def test_parse_skill_md_missing_name(tmp_path):
    _make_skill_dir(tmp_path, "no-name", "description: A skill without a name")
    with pytest.raises(SkillParseError):
        parse_skill_md(tmp_path / "no-name" / "SKILL.md")


def test_parse_skill_md_missing_description(tmp_path):
    _make_skill_dir(tmp_path, "no-desc", "name: no-desc")
    with pytest.raises(SkillParseError):
        parse_skill_md(tmp_path / "no-desc" / "SKILL.md")


def test_parse_skill_md_tags_as_list(tmp_path):
    _make_skill_dir(tmp_path, "list-tags", "name: list-tags\ndescription: test\ntags:\n  - alpha\n  - beta")
    fm, _ = parse_skill_md(tmp_path / "list-tags" / "SKILL.md")
    assert fm["tags"] == ["alpha", "beta"]


def test_parse_skill_md_default_version(tmp_path):
    _make_skill_dir(tmp_path, "no-ver", "name: no-ver\ndescription: test")
    fm, _ = parse_skill_md(tmp_path / "no-ver" / "SKILL.md")
    assert fm["version"] == "1.0"


# ---------------------------------------------------------------------------
# _tokenize and _expand_synonyms
# ---------------------------------------------------------------------------


def test_tokenize_basic():
    tokens = _tokenize("hello world test")
    assert "hello" in tokens
    assert "world" in tokens
    assert "test" in tokens


def test_tokenize_filters_short():
    tokens = _tokenize("a ab abc abcd")
    assert "a" not in tokens
    assert "ab" not in tokens
    assert "abc" in tokens


def test_expand_synonyms():
    tokens = {"search"}
    expanded = _expand_synonyms(tokens)
    assert "search" in expanded
    assert "find" in expanded
    assert "lookup" in expanded


# ---------------------------------------------------------------------------
# SkillIndex
# ---------------------------------------------------------------------------


def test_skill_index_search(tmp_path):
    reg = SkillRegistry()
    _make_skill_dir(tmp_path, "web-search", "name: web-search\ndescription: Search the web\ntags: web,search")
    _make_skill_dir(tmp_path, "code-review", "name: code-review\ndescription: Review code quality\ntags: code,review")
    reg.scan(tmp_path)

    results = reg.discover("search the internet")
    assert len(results) >= 1
    assert results[0].name == "web-search"


def test_skill_index_no_match(tmp_path):
    reg = SkillRegistry()
    _make_skill_dir(tmp_path, "web-search", "name: web-search\ndescription: Search the web\ntags: web")
    reg.scan(tmp_path)

    results = reg.discover("zzz_completely_unrelated_zzz")
    assert len(results) == 0


# ---------------------------------------------------------------------------
# SkillRegistry
# ---------------------------------------------------------------------------


def test_registry_scan(tmp_path):
    _make_skill_dir(tmp_path, "skill-a", "name: skill-a\ndescription: Skill A")
    _make_skill_dir(tmp_path, "skill-b", "name: skill-b\ndescription: Skill B")
    reg = SkillRegistry()
    count = reg.scan(tmp_path)
    assert count == 2
    assert reg.exists("skill-a")
    assert reg.exists("skill-b")


def test_registry_scan_empty_dir(tmp_path):
    reg = SkillRegistry()
    count = reg.scan(tmp_path)
    assert count == 0


def test_registry_scan_nonexistent(tmp_path):
    reg = SkillRegistry()
    count = reg.scan(tmp_path / "nope")
    assert count == 0


def test_registry_get(tmp_path):
    _make_skill_dir(tmp_path, "my-skill", "name: my-skill\ndescription: test")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    skill = reg.get("my-skill")
    assert skill is not None
    assert skill.name == "my-skill"
    assert reg.get("nonexistent") is None


def test_registry_all_skills(tmp_path):
    _make_skill_dir(tmp_path, "a", "name: a\ndescription: A")
    _make_skill_dir(tmp_path, "b", "name: b\ndescription: B")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    all_skills = reg.all_skills()
    assert len(all_skills) == 2


def test_registry_rescan(tmp_path):
    _make_skill_dir(tmp_path, "old", "name: old\ndescription: Old")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    assert reg.exists("old")

    # Add a new skill and rescan
    _make_skill_dir(tmp_path, "new", "name: new\ndescription: New")
    reg.rescan(tmp_path)
    assert reg.exists("new")


def test_registry_load_instructions(tmp_path):
    body = "# Step 1\nDo this.\n# Step 2\nDo that."
    _make_skill_dir(tmp_path, "instruct", "name: instruct\ndescription: test", body=body)
    reg = SkillRegistry()
    reg.scan(tmp_path)
    instructions = reg.load_instructions("instruct")
    assert "Step 1" in instructions
    assert "Step 2" in instructions


def test_registry_load_instructions_missing():
    reg = SkillRegistry()
    assert reg.load_instructions("nonexistent") is None


def test_registry_list_resources(tmp_path):
    d = _make_skill_dir(tmp_path, "res-skill", "name: res-skill\ndescription: test")
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / "run.sh").write_text("#!/bin/bash")
    refs = d / "references"
    refs.mkdir()
    (refs / "guide.md").write_text("# Guide")

    reg = SkillRegistry()
    reg.scan(tmp_path)
    resources = reg.list_resources("res-skill")
    assert "scripts" in resources
    assert "run.sh" in resources["scripts"]
    assert "references" in resources
    assert "guide.md" in resources["references"]


def test_registry_list_resources_missing():
    reg = SkillRegistry()
    assert reg.list_resources("nonexistent") == {}


def test_registry_read_resource(tmp_path):
    d = _make_skill_dir(tmp_path, "read-res", "name: read-res\ndescription: test")
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / "check.sh").write_text("echo ok")

    reg = SkillRegistry()
    reg.scan(tmp_path)
    content = reg.read_resource("read-res", "scripts/check.sh")
    assert content == "echo ok"


def test_registry_read_resource_traversal(tmp_path):
    _make_skill_dir(tmp_path, "trav", "name: trav\ndescription: test")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    assert reg.read_resource("trav", "../../../etc/passwd") is None
    assert reg.read_resource("trav", "/etc/passwd") is None


def test_registry_skips_hidden_dirs(tmp_path):
    _make_skill_dir(tmp_path, ".hidden", "name: .hidden\ndescription: test")
    _make_skill_dir(tmp_path, "_private", "name: _private\ndescription: test")
    _make_skill_dir(tmp_path, "visible", "name: visible\ndescription: test")
    reg = SkillRegistry()
    count = reg.scan(tmp_path)
    assert count == 1
    assert reg.exists("visible")


# ---------------------------------------------------------------------------
# Skill pre-flight validation
# ---------------------------------------------------------------------------


def test_validate_valid_skill(tmp_path):
    """Skill with a valid Python script passes validation."""
    d = _make_skill_dir(tmp_path, "good", "name: good\ndescription: Valid skill")
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text("print('ok')\n")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    assert reg.is_valid("good")


def test_validate_empty_script(tmp_path):
    """Skill with an empty script file fails validation."""
    d = _make_skill_dir(tmp_path, "empty-script", "name: empty-script\ndescription: Empty script")
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text("")  # empty
    reg = SkillRegistry()
    reg.scan(tmp_path)
    assert not reg.is_valid("empty-script")


def test_validate_syntax_error_script(tmp_path):
    """Skill with a syntax-error Python script fails validation."""
    d = _make_skill_dir(tmp_path, "bad-syntax", "name: bad-syntax\ndescription: Broken script")
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / "bad.py").write_text("def f(\n")  # syntax error
    reg = SkillRegistry()
    reg.scan(tmp_path)
    assert not reg.is_valid("bad-syntax")


def test_validate_no_scripts_dir_is_valid(tmp_path):
    """Skill without a scripts/ directory is considered valid (docs-only)."""
    _make_skill_dir(tmp_path, "docs-only", "name: docs-only\ndescription: Just instructions")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    assert reg.is_valid("docs-only")


def test_validate_invalid_tracked_in_set(tmp_path):
    """Invalid skills are tracked in the _invalid set."""
    d = _make_skill_dir(tmp_path, "tracked", "name: tracked\ndescription: Tracked invalid")
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / "empty.py").write_text("")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    assert "tracked" in reg._invalid
