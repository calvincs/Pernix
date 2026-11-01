"""Grading must not walk the whole workspace."""

import os
import time

from core.reflect import _newest_workspace_files


def _touch(path, age):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    when = time.time() - age
    os.utime(path, (when, when))


def test_returns_newest_first_and_skips_hidden(tmp_path):
    tmp_path = tmp_path / "ws"
    _touch(tmp_path / "old.txt", 500)
    _touch(tmp_path / "sub" / "new.txt", 10)
    _touch(tmp_path / ".venv" / "newest.py", 1)
    _touch(tmp_path / "sub" / ".hidden", 1)
    assert _newest_workspace_files(tmp_path) == ["sub/new.txt", "old.txt"]


def test_a_huge_old_folder_cannot_hide_a_new_file_or_exhaust_the_scan(tmp_path):
    tmp_path = tmp_path / "ws"
    for i in range(50):
        _touch(tmp_path / "bulk" / f"{i}.txt", 9000)
    os.utime(tmp_path / "bulk", (time.time() - 9000, time.time() - 9000))
    _touch(tmp_path / "fresh" / "made_this_turn.md", 1)
    assert _newest_workspace_files(tmp_path, limit=1, budget=10) == ["fresh/made_this_turn.md"]


def test_a_missing_folder_is_not_an_error(tmp_path):
    assert _newest_workspace_files(tmp_path / "gone") == []
