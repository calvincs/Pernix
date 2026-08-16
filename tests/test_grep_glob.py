"""Tests for grep_tool.py and glob_tool.py."""

import os

import pytest

from core.tools.builtin.glob_tool import glob_search
from core.tools.builtin.grep_tool import grep

# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def test_grep_match(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "test.py"
    f.write_text("def hello():\n    return 'world'\n")
    result = grep("hello")
    assert "hello" in result


def test_grep_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "test.py"
    f.write_text("nothing here\n")
    result = grep("zzz_nonexistent_zzz")
    assert "No matches" in result


def test_grep_empty_pattern(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    result = grep("")
    assert "Error" in result


def test_grep_path_restriction(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "file.txt").write_text("target_string\n")
    (tmp_path / "root.txt").write_text("target_string\n")
    result = grep("target_string", path="subdir")
    assert "target_string" in result


def test_grep_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    result = grep("pattern", path="/etc")
    assert "Error" in result


def test_grep_include_filter(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    (tmp_path / "a.py").write_text("match_me\n")
    (tmp_path / "b.txt").write_text("match_me\n")
    result = grep("match_me", include="*.py")
    assert "a.py" in result


def test_grep_path_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    result = grep("pattern", path="nonexistent_dir")
    assert "Error" in result or "not found" in result.lower()


# ---------------------------------------------------------------------------
# glob_search
# ---------------------------------------------------------------------------


def test_glob_basic(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("x")
    (tmp_path / "c.txt").write_text("x")
    result = glob_search("*.py")
    assert "a.py" in result
    assert "b.py" in result
    assert "c.txt" not in result


def test_glob_no_results(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    result = glob_search("*.xyz")
    assert "No files found" in result


def test_glob_subdirectory(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "app.py").write_text("x")
    result = glob_search("*.py", path="src")
    assert "app.py" in result


def test_glob_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    result = glob_search("*.py", path="/etc")
    assert "Error" in result


def test_glob_not_a_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    (tmp_path / "file.txt").write_text("x")
    result = glob_search("*.py", path="file.txt")
    assert "Error" in result or "Not a directory" in result


# ---------------------------------------------------------------------------
# root_mismatch_hint — a harness-data path resolves under the workspace root
# and fails with an error that reads as "wrong path" when the root is what is
# wrong. bash with an absolute path is the only whole-filesystem tool.
# ---------------------------------------------------------------------------


def test_glob_harness_data_path_explains_the_root(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    result = glob_search("*.md", path="data/telos")
    assert "Not a directory: data/telos" in result
    assert "resolved against workspace root" in result
    assert "use bash with an absolute path" in result
    assert "telos_status" in result


def test_grep_harness_data_path_explains_the_root(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    result = grep("anything", path="data/memories")
    assert "Error" in result
    assert "resolved against workspace root" in result


def test_glob_workspace_typo_keeps_its_clean_error(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    (tmp_path / "file.txt").write_text("x")
    result = glob_search("*.py", path="file.txt")
    assert result == "Error: Not a directory: file.txt"


def test_root_hint_ignores_real_workspace_paths(tmp_path, monkeypatch):
    from core.tools.paths import root_mismatch_hint

    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "telos").mkdir()
    assert root_mismatch_hint("data/telos") == ""
    assert root_mismatch_hint("notes/todo.md") == ""
    assert root_mismatch_hint("") == ""


def test_root_hint_fires_on_bare_harness_dirs_and_absolute_data_paths(tmp_path, monkeypatch):
    import config
    from core.tools.paths import root_mismatch_hint

    data = tmp_path / "data"
    (data / "telos").mkdir(parents=True)
    (data / "workspace").mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr("config.settings.workspace_dir", str(data / "workspace"))

    assert "resolved against workspace root" in root_mismatch_hint("telos/questions")
    assert "resolved against workspace root" in root_mismatch_hint(str(data / "telos"))
    # data/workspace/... is the workspace under its full name, not harness data.
    assert root_mismatch_hint("data/workspace/notes.md") == ""
    # An absolute path inside the workspace is not a root mistake either.
    assert root_mismatch_hint(str(data / "workspace" / "notes.md")) == ""
