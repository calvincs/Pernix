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
