"""Tests for file_edit.py: fuzzy matching cascade, edit operations."""

import os

import pytest

from core.tools.builtin.file_edit import (
    _apply_edit,
    _block_anchor_replace,
    _exact_replace,
    _indentation_flexible_replace,
    _levenshtein,
    _make_diff,
    _similarity,
    _whitespace_normalized_replace,
    file_edit,
    multiedit,
)

# ---------------------------------------------------------------------------
# _levenshtein
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("", "", 0),
        ("abc", "", 3),
        ("", "xyz", 3),
        ("abc", "abc", 0),
        ("kitten", "sitting", 3),
        ("abc", "abd", 1),
    ],
)
def test_levenshtein(a, b, expected):
    assert _levenshtein(a, b) == expected


def test_levenshtein_symmetric():
    assert _levenshtein("abc", "xyz") == _levenshtein("xyz", "abc")


# ---------------------------------------------------------------------------
# _similarity
# ---------------------------------------------------------------------------


def test_similarity_identical():
    assert _similarity("hello", "hello") == 1.0


def test_similarity_empty():
    assert _similarity("", "") == 1.0


def test_similarity_partial():
    s = _similarity("abc", "abd")
    assert 0.0 < s < 1.0


def test_similarity_completely_different():
    s = _similarity("abc", "xyz")
    assert s < 0.5


# ---------------------------------------------------------------------------
# Replacer strategies
# ---------------------------------------------------------------------------


def test_exact_replace_single():
    results = list(_exact_replace("hello world", "world", "planet", False))
    assert results == ["hello planet"]


def test_exact_replace_all():
    results = list(_exact_replace("aXaXa", "X", "Y", True))
    assert results == ["aYaYa"]


def test_exact_replace_no_match():
    results = list(_exact_replace("hello", "xyz", "abc", False))
    assert results == []


def test_whitespace_normalized_single():
    content = "if  (x   == 1):\n    pass"
    old = "if (x == 1):\n    pass"
    new = "if (x == 2):\n    pass"
    results = list(_whitespace_normalized_replace(content, old, new, False))
    assert len(results) == 1
    assert "x == 2" in results[0]


def test_whitespace_normalized_no_match():
    results = list(_whitespace_normalized_replace("abc", "xyz", "new", False))
    assert results == []


def test_indentation_flexible():
    content = "    def foo():\n        return 1"
    old = "def foo():\n    return 1"  # Less indentation
    new = "def foo():\n    return 2"
    results = list(_indentation_flexible_replace(content, old, new, False))
    assert len(results) == 1
    assert "return 2" in results[0]


def test_block_anchor_basic():
    content = "line1\ndef start():\n    x = 1\n    y = 2\n    return x\nline6"
    old = "def start():\n    x = 1\n    y = 2\n    return x"
    new = "def start():\n    x = 10\n    return x"
    results = list(_block_anchor_replace(content, old, new, False))
    assert len(results) == 1
    assert "x = 10" in results[0]


def test_block_anchor_too_short():
    """Block anchor needs at least 3 lines."""
    results = list(_block_anchor_replace("ab", "a\nb", "c\nd", False))
    assert results == []


# ---------------------------------------------------------------------------
# _apply_edit (cascade)
# ---------------------------------------------------------------------------


def test_apply_edit_exact():
    result, strategy = _apply_edit("foo bar baz", "bar", "qux", False)
    assert result == "foo qux baz"
    assert strategy == "exact"


def test_apply_edit_fuzzy_whitespace():
    result, strategy = _apply_edit("foo   bar", "foo bar", "foo baz", False)
    assert result is not None
    assert "baz" in result
    assert strategy in ("exact", "whitespace-normalized")


def test_apply_edit_no_match():
    result, strategy = _apply_edit("hello", "zzzzz_no_match_zzzzz", "new", False)
    assert result is None
    assert strategy is None


# ---------------------------------------------------------------------------
# _make_diff
# ---------------------------------------------------------------------------


def test_make_diff():
    diff = _make_diff("line1\nline2\n", "line1\nline3\n", "test.py")
    assert "---" in diff
    assert "+++" in diff
    assert "-line2" in diff
    assert "+line3" in diff


# ---------------------------------------------------------------------------
# file_edit (integration with filesystem)
# ---------------------------------------------------------------------------


def test_file_edit_basic(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "test.txt"
    f.write_text("hello world\ngoodbye world\n")

    result = file_edit("test.txt", "hello", "hi")
    assert "Edited" in result
    assert "hi world" in f.read_text()


def test_file_edit_identical_strings(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    result = file_edit("test.txt", "same", "same")
    assert "identical" in result.lower()
    assert not result.lower().startswith("error")
    assert "no changes" in result.lower() or "no-op" in result.lower()


def test_file_edit_create_new_file(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    result = file_edit("new_file.txt", "", "new content")
    assert "Created" in result
    assert (tmp_path / "new_file.txt").read_text() == "new content"


def test_file_edit_file_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    result = file_edit("nonexistent.txt", "old", "new")
    assert "Error" in result


def test_file_edit_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "test.txt"
    f.write_text("hello world")

    result = file_edit("test.txt", "zzz_no_match_zzz", "new")
    assert "Error" in result
    assert "not found" in result


def test_file_edit_replace_all(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "test.txt"
    f.write_text("foo foo foo")

    result = file_edit("test.txt", "foo", "bar", replace_all=True)
    assert "Edited" in result
    assert f.read_text() == "bar bar bar"


# ---------------------------------------------------------------------------
# multiedit
# ---------------------------------------------------------------------------


def test_multiedit_basic(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "test.txt"
    f.write_text("aaa bbb ccc")

    result = multiedit(
        "test.txt",
        [
            {"old_string": "aaa", "new_string": "111"},
            {"old_string": "bbb", "new_string": "222"},
        ],
    )
    assert "Applied 2/2" in result
    assert f.read_text() == "111 222 ccc"


def test_multiedit_empty_edits(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    result = multiedit("test.txt", [])
    assert "Error" in result


def test_multiedit_partial_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "test.txt"
    f.write_text("aaa bbb ccc")

    result = multiedit(
        "test.txt",
        [
            {"old_string": "aaa", "new_string": "111"},
            {"old_string": "zzz_no_match", "new_string": "222"},
        ],
    )
    assert "Edit 2 failed" in result


def test_file_edit_shows_resolved_path(tmp_path, monkeypatch):
    """file_edit success message includes the absolute resolved path."""
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "resolved.txt"
    f.write_text("old text\n")
    result = file_edit("resolved.txt", "old text", "new text")
    assert "Edited" in result
    assert str(tmp_path) in result


# ---------------------------------------------------------------------------
# Regression: block-anchor must not silently edit with 0 middle similarity
# ---------------------------------------------------------------------------


def test_block_anchor_rejects_low_similarity():
    """When only one anchor pair exists and the middle is unrelated, reject."""
    content = "def foo():\n    completely_unrelated_body_line\n    return x\n"
    old = "def foo():\n    x = 1\n    y = 2\n    z = 3\n    return x"
    new = "def foo():\n    changed\n    return x"
    results = list(_block_anchor_replace(content, old, new, False))
    assert results == [], "block-anchor should reject below similarity floor"


def test_block_anchor_accepts_high_similarity():
    """Near-identical middle lines still match."""
    content = "def foo():\n    x = 1\n    y = 2\n    return x\nend\n"
    old = "def foo():\n    x = 1\n    y = 2\n    return x"
    new = "def foo():\n    x = 10\n    return x"
    results = list(_block_anchor_replace(content, old, new, False))
    assert len(results) == 1
    assert "x = 10" in results[0]


def test_file_edit_falls_through_without_wrong_edit(tmp_path, monkeypatch):
    """file_edit must error rather than silently edit when fuzzy floor fails."""
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "prog.py"
    f.write_text("def unchanged():\n" "    real_body\n" "    return 0\n" "\n" "def other():\n" "    x = 99\n")
    result = file_edit(
        "prog.py",
        "def unchanged():\n    totally\n    different\n    body\n    return 0",
        "def unchanged():\n    BAD\n    return 0",
    )
    assert "Error" in result
    assert "real_body" in f.read_text(), "file must be untouched on fuzzy-reject"


def test_file_edit_annotates_fuzzy_strategy(tmp_path, monkeypatch):
    """When a fuzzy strategy fires, the success message flags it."""
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "t.txt"
    # Extra spaces in the middle line force whitespace-normalized strategy.
    f.write_text("alpha\nif  (x   ==   1):\n    pass\nomega\n")
    result = file_edit(
        "t.txt",
        "if (x == 1):\n    pass",
        "if (x == 2):\n    pass",
    )
    assert "Edited" in result
    assert "[fuzzy:" in result


# ---------------------------------------------------------------------------
# Regression: binary files must be refused
# ---------------------------------------------------------------------------


def test_file_edit_rejects_binary(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "blob.bin"
    f.write_bytes(b"hello\x00world")
    result = file_edit("blob.bin", "hello", "goodbye")
    assert "Error" in result
    assert "binary" in result.lower()
    assert f.read_bytes() == b"hello\x00world"


def test_multiedit_rejects_binary(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "blob.bin"
    f.write_bytes(b"a\x00b\x00c")
    result = multiedit("blob.bin", [{"old_string": "a", "new_string": "A"}])
    assert "Error" in result
    assert "binary" in result.lower()


# ---------------------------------------------------------------------------
# Regression: atomic file creation + size cap
# ---------------------------------------------------------------------------


def test_file_edit_create_uses_atomic_write(tmp_path, monkeypatch):
    """Creating via empty old_string should not leave a .tmp file behind."""
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    result = file_edit("fresh/sub/new.txt", "", "body")
    assert "Created" in result
    assert (tmp_path / "fresh/sub/new.txt").read_text() == "body"
    leftovers = list((tmp_path / "fresh/sub").glob(".new.txt.*"))
    assert leftovers == [], f"tmp file left behind: {leftovers}"


def test_file_edit_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr("config.settings.max_file_write_size", 100, raising=False)
    f = tmp_path / "t.txt"
    f.write_text("small")
    big = "x" * 500
    result = file_edit("t.txt", "small", big)
    assert "Error" in result
    assert "size cap" in result


# ---------------------------------------------------------------------------
# Regression: lone-CR line endings are preserved
# ---------------------------------------------------------------------------


def test_file_edit_preserves_cr_line_endings(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "cr.txt"
    f.write_bytes(b"line1\rline2\rline3\r")
    result = file_edit("cr.txt", "line2", "LINE2")
    assert "Edited" in result
    assert f.read_bytes() == b"line1\rLINE2\rline3\r"


def test_file_edit_preserves_crlf_line_endings(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"line1\r\nline2\r\nline3\r\n")
    result = file_edit("crlf.txt", "line2", "LINE2")
    assert "Edited" in result
    assert f.read_bytes() == b"line1\r\nLINE2\r\nline3\r\n"


# ---------------------------------------------------------------------------
# Regression: multiedit abort message
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Regression: edit read-size gate
# ---------------------------------------------------------------------------


def test_file_edit_refuses_large_file(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr("config.settings.max_edit_read_size", 1024, raising=False)
    f = tmp_path / "huge.txt"
    f.write_text("y" * 5000)
    result = file_edit("huge.txt", "y", "Y")
    assert "Error" in result
    assert "whole-file edit cap" in result
    # File must be untouched.
    assert f.read_text() == "y" * 5000


def test_multiedit_refuses_large_file(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr("config.settings.max_edit_read_size", 1024, raising=False)
    f = tmp_path / "huge.txt"
    f.write_text("z" * 5000)
    result = multiedit("huge.txt", [{"old_string": "z", "new_string": "Z"}])
    assert "Error" in result
    assert "whole-file edit cap" in result
    assert f.read_text() == "z" * 5000


# ---------------------------------------------------------------------------
# Regression: _levenshtein is iterative and bails on oversized inputs
# ---------------------------------------------------------------------------


def test_levenshtein_oversized_is_bounded():
    """Inputs beyond the safety cap must not recurse or spend O(n*m) time."""
    import time

    a = "x" * 100_000
    b = "y" * 100_000
    start = time.monotonic()
    dist = _levenshtein(a, b)
    elapsed = time.monotonic() - start
    # Should short-circuit, not actually compute the full matrix.
    assert elapsed < 0.1
    # And similarity collapses toward 0.
    sim = _similarity(a, b)
    assert sim <= 0.01


def test_levenshtein_no_recursion_for_long_first_arg():
    """Regression: the old impl swapped args via recursion; ensure iterative."""
    import sys

    # Set a tight recursion limit briefly — the function must still run.
    orig_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(50)
        dist = _levenshtein("abcdef" * 200, "xyz")
        assert dist > 0
    finally:
        sys.setrecursionlimit(orig_limit)


def test_multiedit_abort_message_is_clear(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "t.txt"
    f.write_text("aaa bbb ccc")
    result = multiedit(
        "t.txt",
        [
            {"old_string": "aaa", "new_string": "111"},
            {"old_string": "zzz_nope", "new_string": "222"},
        ],
    )
    assert "no file changes written" in result
    assert f.read_text() == "aaa bbb ccc"
