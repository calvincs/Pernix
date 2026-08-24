"""A 30-minute bash search timed out and returned zero of its progress output
(field case c93232a0521b, R11L solver worker).

bash already appends "[partial output before timeout]" from the capture
files — but Python block-buffers stdout when it isn't a tty, so a
long-running script's prints sat in an unflushed 8KB buffer and the capture
was empty exactly when it mattered. The tool env now sets PYTHONUNBUFFERED=1
so python children flush as they print.
"""

from core.tools.builtin.core_tools import bash


def test_python_child_output_is_unbuffered_and_survives_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr("config.settings.shell_env_mode", "allowlist")
    # A python child that prints, then outlives the timeout. Buffered, the
    # print would be lost; unbuffered, the partial block carries it.
    out = bash("python3 -c \"import time; print('PROGRESS-MARKER-1'); time.sleep(30)\"", timeout=3)
    assert "timed out" in out
    assert "PROGRESS-MARKER-1" in out


def test_env_carries_unbuffered_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    out = bash("echo flag=$PYTHONUNBUFFERED")
    assert "flag=1" in out
