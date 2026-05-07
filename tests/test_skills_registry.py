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


# ---------------------------------------------------------------------------
# Disabled state — toggle, persistence, filtering
# ---------------------------------------------------------------------------


def _scan_two_skills(tmp_path):
    _make_skill_dir(tmp_path, "alpha", "name: alpha\ndescription: web search helper\ntags: web,search")
    _make_skill_dir(tmp_path, "beta", "name: beta\ndescription: deploy pipeline guide\ntags: deploy")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    return reg


def test_registry_disable_then_is_disabled(tmp_path):
    reg = _scan_two_skills(tmp_path)
    assert not reg.is_disabled("alpha")
    reg.disable("alpha")
    assert reg.is_disabled("alpha")
    assert not reg.is_disabled("beta")


def test_registry_enable_clears_disabled(tmp_path):
    reg = _scan_two_skills(tmp_path)
    reg.disable("alpha")
    reg.enable("alpha")
    assert not reg.is_disabled("alpha")


def test_registry_disable_persists_across_rescan(tmp_path):
    """Toggle survives PUT/DELETE rescan path."""
    reg = _scan_two_skills(tmp_path)
    reg.disable("alpha")
    reg.rescan(tmp_path)
    assert reg.is_disabled("alpha")
    assert not reg.is_disabled("beta")


def test_registry_disabled_persists_to_json_in_legacy_format(tmp_path):
    """File format matches what the API router historically wrote: a sorted JSON array of names."""
    import json as _json

    reg = _scan_two_skills(tmp_path)
    reg.disable("beta")
    reg.disable("alpha")  # add out of order
    saved = _json.loads((tmp_path / ".disabled.json").read_text())
    assert saved == ["alpha", "beta"]  # sorted


def test_registry_discover_excludes_disabled(tmp_path):
    """Default discover() filters disabled out so scout/agent never see them."""
    reg = _scan_two_skills(tmp_path)
    results = reg.discover("web", limit=10)
    assert "alpha" in {r.name for r in results}
    reg.disable("alpha")
    results = reg.discover("web", limit=10)
    assert "alpha" not in {r.name for r in results}


def test_registry_discover_include_disabled_kwarg_returns_all(tmp_path):
    """Explorer UI uses include_disabled=True to render the toggle row."""
    reg = _scan_two_skills(tmp_path)
    reg.disable("alpha")
    results = reg.discover("web", limit=10, include_disabled=True)
    assert "alpha" in {r.name for r in results}


def test_registry_load_instructions_disabled_returns_none(tmp_path):
    reg = _scan_two_skills(tmp_path)
    assert reg.load_instructions("alpha") is not None
    reg.disable("alpha")
    assert reg.load_instructions("alpha") is None
    # Override path for the UI
    assert reg.load_instructions("alpha", include_disabled=True) is not None


def test_registry_list_resources_disabled_returns_empty(tmp_path):
    """Disabled skill's resources hidden from agent paths but visible to UI."""
    d = _make_skill_dir(tmp_path, "with-scripts", "name: with-scripts\ndescription: has scripts")
    scripts = d / "scripts"
    scripts.mkdir()
    (scripts / "run.sh").write_text("#!/bin/sh\necho hi")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    assert reg.list_resources("with-scripts").get("scripts") == ["run.sh"]
    reg.disable("with-scripts")
    assert reg.list_resources("with-scripts") == {}
    assert reg.list_resources("with-scripts", include_disabled=True).get("scripts") == ["run.sh"]


def test_registry_read_resource_disabled_returns_none(tmp_path):
    d = _make_skill_dir(tmp_path, "rsrc", "name: rsrc\ndescription: r")
    refs = d / "references"
    refs.mkdir()
    (refs / "note.md").write_text("hello")
    reg = SkillRegistry()
    reg.scan(tmp_path)
    assert reg.read_resource("rsrc", "references/note.md") == "hello"
    reg.disable("rsrc")
    assert reg.read_resource("rsrc", "references/note.md") is None


def test_registry_enabled_skills_excludes_disabled(tmp_path):
    reg = _scan_two_skills(tmp_path)
    reg.disable("alpha")
    enabled = {s.name for s in reg.enabled_skills()}
    assert enabled == {"beta"}
    # all_skills() unchanged — UI introspection must still see everything
    assert {s.name for s in reg.all_skills()} == {"alpha", "beta"}


def test_registry_exists_unchanged_by_disable(tmp_path):
    """exists() means 'is registered' — disable doesn't remove it."""
    reg = _scan_two_skills(tmp_path)
    reg.disable("alpha")
    assert reg.exists("alpha")  # still registered, just toggled off


def test_skill_index_search_excludes_via_exclude_param():
    idx = SkillIndex()
    idx._entries["a"] = idx._entries.get("a")  # prime; just verify API accepts kwarg
    # Build a minimal entry directly to keep test hermetic
    from core.skills.registry import _SkillIndexEntry, _tokenize

    idx._entries.clear()
    idx._entries["a"] = _SkillIndexEntry(
        name="a",
        description="web search",
        tags=["web"],
        has_scripts=False,
        has_references=False,
        name_tokens=_tokenize("a"),
        desc_tokens=_tokenize("web search"),
        tag_tokens={"web"},
    )
    idx._entries["b"] = _SkillIndexEntry(
        name="b",
        description="web fetcher",
        tags=["web"],
        has_scripts=False,
        has_references=False,
        name_tokens=_tokenize("b"),
        desc_tokens=_tokenize("web fetcher"),
        tag_tokens={"web"},
    )
    names = {r.name for r in idx.search("web")}
    assert names == {"a", "b"}
    names = {r.name for r in idx.search("web", exclude={"a"})}
    assert names == {"b"}


# ---------------------------------------------------------------------------
# Cooccurrence + exclude param: a disabled sibling promoted via cooccurrence
# must NOT leak into results.
# ---------------------------------------------------------------------------


def test_skill_index_search_cooccurrence_respects_exclude_set():
    """If a disabled skill is listed as a cooccurrence sibling of an enabled
    neighbor, ``SkillIndex.search`` must drop it from the promoted results.
    Without this, disabling 'foo' would still surface 'foo' whenever 'bar'
    matches and 'foo' is in bar's cooccurrence list.
    """
    from core.skills.registry import (
        SKILL_COOCCURRENCE,
        SkillIndex,
        _SkillIndexEntry,
        _tokenize,
    )

    idx = SkillIndex()
    idx._entries["primary"] = _SkillIndexEntry(
        name="primary",
        description="orchestrator deploy",
        tags=["deploy"],
        has_scripts=False,
        has_references=False,
        name_tokens=_tokenize("primary"),
        desc_tokens=_tokenize("orchestrator deploy"),
        tag_tokens={"deploy"},
    )
    idx._entries["sibling"] = _SkillIndexEntry(
        name="sibling",
        description="release helper",  # description does NOT match the query
        tags=[],
        has_scripts=False,
        has_references=False,
        name_tokens=_tokenize("sibling"),
        desc_tokens=_tokenize("release helper"),
        tag_tokens=set(),
    )
    # Make sibling promote whenever primary matches.
    SKILL_COOCCURRENCE["primary"] = ["sibling"]
    try:
        names = {r.name for r in idx.search("deploy")}
        assert names == {"primary", "sibling"}, "sibling should normally promote via cooccurrence"
        names = {r.name for r in idx.search("deploy", exclude={"sibling"})}
        assert names == {"primary"}, "disabled sibling must NOT leak through cooccurrence path"
    finally:
        SKILL_COOCCURRENCE.pop("primary", None)


def test_registry_discover_excludes_cooccurring_disabled_sibling(tmp_path):
    """End-to-end variant: disable a real skill, then trigger discover for an
    enabled neighbor whose cooccurrence map mentions the disabled one. The
    disabled sibling must NOT appear in the discover() output.
    """
    from core.skills.registry import SKILL_COOCCURRENCE

    _make_skill_dir(
        tmp_path,
        "primary",
        "name: primary\ndescription: orchestration deploy guide\ntags: deploy",
    )
    _make_skill_dir(
        tmp_path,
        "sibling",
        "name: sibling\ndescription: unrelated text token zonk\ntags: zonk",
    )
    reg = SkillRegistry()
    reg.scan(tmp_path)
    SKILL_COOCCURRENCE["primary"] = ["sibling"]
    try:
        # Sanity: sibling promotes when both enabled.
        names = {r.name for r in reg.discover("deploy", limit=10)}
        assert "sibling" in names
        # Disable the sibling — discover() must drop it from the cooccurrence
        # promotion path.
        reg.disable("sibling")
        names = {r.name for r in reg.discover("deploy", limit=10)}
        assert "primary" in names
        assert "sibling" not in names
    finally:
        SKILL_COOCCURRENCE.pop("primary", None)


# ---------------------------------------------------------------------------
# Persistence edge cases: malformed JSON, stale names, save-before-scan.
# ---------------------------------------------------------------------------


def test_registry_load_disabled_swallows_malformed_json(tmp_path, caplog):
    """A corrupted .disabled.json must NOT block scan() — it should log a
    warning and treat as empty."""
    import logging

    _make_skill_dir(tmp_path, "alpha", "name: alpha\ndescription: a")
    # Pre-write a malformed disabled file BEFORE the first scan.
    (tmp_path / ".disabled.json").write_text("not-valid-json{[", encoding="utf-8")

    reg = SkillRegistry()
    with caplog.at_level(logging.WARNING, logger="pernix.skills.registry"):
        count = reg.scan(tmp_path)

    assert count == 1
    assert reg.exists("alpha")
    # Disabled set ended up empty (corrupt → ignored).
    assert reg._disabled == set()
    assert not reg.is_disabled("alpha")
    # And we logged about it.
    assert any("Failed to read" in r.message and ".disabled.json" in r.message for r in caplog.records)


def test_registry_prunes_stale_disabled_entries_on_scan(tmp_path):
    """A name in .disabled.json that doesn't match any skill on disk is pruned
    on scan — otherwise a future skill of the same name (recreated, restored
    from backup) would silently come back disabled. The pruned set is
    persisted so disk and memory stay consistent.
    """
    import json as _json

    _make_skill_dir(tmp_path, "alpha", "name: alpha\ndescription: a")
    # Pre-disable both a real skill and a ghost name that doesn't exist on disk.
    (tmp_path / ".disabled.json").write_text(_json.dumps(["ghost-skill", "alpha"]), encoding="utf-8")

    reg = SkillRegistry()
    reg.scan(tmp_path)
    # Real skill stays disabled.
    assert reg.is_disabled("alpha")
    # Ghost name is pruned out of the in-memory set.
    assert not reg.is_disabled("ghost-skill")
    # And persisted back to disk so the file stays clean.
    assert _json.loads((tmp_path / ".disabled.json").read_text()) == ["alpha"]
    # enabled_skills() correctly excludes only the real disabled skill.
    assert {s.name for s in reg.enabled_skills()} == set()


def test_registry_disable_before_scan_raises(tmp_path):
    """disable() before scan() raises rather than silently dropping state.

    Without a known skills_dir there is no .disabled.json to write to, and the
    in-memory entry would be wiped on the next scan() (which calls
    _load_disabled). Failing loud beats failing silent.
    """
    import pytest as _pytest

    reg = SkillRegistry()
    assert reg._disabled_path is None
    with _pytest.raises(RuntimeError, match="called before scan"):
        reg.disable("never-scanned")
    with _pytest.raises(RuntimeError, match="called before scan"):
        reg.enable("never-scanned")
    # Nothing was written anywhere
    assert not (tmp_path / ".disabled.json").exists()


def test_registry_legacy_format_round_trip(tmp_path):
    """Lock the contract in BOTH directions: writing via disable()/enable()
    must produce exactly what a fresh registry's _load_disabled() reads
    back. The on-disk format is a sorted JSON array of names (the same shape
    the API router historically wrote)."""
    import json as _json

    _make_skill_dir(tmp_path, "alpha", "name: alpha\ndescription: a")
    _make_skill_dir(tmp_path, "beta", "name: beta\ndescription: b")
    _make_skill_dir(tmp_path, "gamma", "name: gamma\ndescription: c")

    reg = SkillRegistry()
    reg.scan(tmp_path)
    reg.disable("gamma")
    reg.disable("alpha")

    # On-disk shape matches the legacy contract.
    raw = (tmp_path / ".disabled.json").read_text(encoding="utf-8")
    assert _json.loads(raw) == ["alpha", "gamma"]

    # A second registry started from scratch reads the file and reproduces
    # the same disabled set.
    reg2 = SkillRegistry()
    reg2.scan(tmp_path)
    assert reg2.is_disabled("alpha")
    assert reg2.is_disabled("gamma")
    assert not reg2.is_disabled("beta")
    assert {s.name for s in reg2.enabled_skills()} == {"beta"}


def test_registry_concurrent_disable_and_discover(tmp_path):
    """Smoke-test: many threads calling disable/enable + discover concurrently
    must not raise (the registry holds an RLock and the index search is a
    pure read of an immutable map). The assertion is "no exception escapes",
    not a behavioral guarantee about which view a particular thread sees.
    """
    import threading

    for n in ("a", "b", "c", "d", "e"):
        _make_skill_dir(tmp_path, n, f"name: {n}\ndescription: web {n} thing\ntags: web")
    reg = SkillRegistry()
    reg.scan(tmp_path)

    errors: list[Exception] = []
    stop = threading.Event()

    def toggler():
        try:
            i = 0
            while not stop.is_set():
                name = "abcde"[i % 5]
                if i % 2:
                    reg.disable(name)
                else:
                    reg.enable(name)
                i += 1
        except Exception as e:  # pragma: no cover - test will assert
            errors.append(e)

    def reader():
        try:
            while not stop.is_set():
                _ = reg.discover("web", limit=10)
                _ = reg.enabled_skills()
        except Exception as e:  # pragma: no cover - test will assert
            errors.append(e)

    threads = [threading.Thread(target=toggler) for _ in range(2)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    # Brief race window — enough for thousands of cycles.
    import time as _time

    _time.sleep(0.25)
    stop.set()
    for t in threads:
        t.join(timeout=2)
    assert not errors, f"thread error(s): {errors!r}"
