"""Pernix — canary authoring: structured-spec creation and the gate allowlist.

The suite no longer grows itself (3.2): refine proposes no canaries and
nothing auto-admits one. What stays is the create API's write path and the
advisory gate-command proof.
"""

from pathlib import Path

import pytest

from core.canary.propose import materialize_canary
from db import models as db

_SPEC = {
    "name": "regression-pin",
    "prompt": "Reproduce the fix: create out.txt containing DONE.",
    "gates": [{"name": "out", "command": "grep -qx DONE out.txt", "watch_paths": []}],
    "files": {"seed.txt": "fixture"},
    "rationale": "session X kept mangling file writes",
}


@pytest.fixture(autouse=True)
def _canaries_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.canaries_dir", str(tmp_path / "canaries"))
    monkeypatch.setattr("config.settings.canary_enabled", True)


def test_refine_no_longer_proposes_canaries():
    import json

    from core.refine import REFINE_PROMPT, _parse_refine_output

    raw = json.dumps({"proposals": [], "lessons": [], "canary_proposals": [_SPEC]})
    assert _parse_refine_output(raw) == ([], [], False)
    assert "canary_proposals" not in REFINE_PROMPT


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def test_materialize_refuses_duplicates_and_invalid(tmp_path):
    from core.canary.parser import load_canary

    base = tmp_path / "c"
    name, err = materialize_canary(_SPEC, base=base)
    assert name == "regression-pin" and err == ""
    c = load_canary("regression-pin", base=base)
    assert c is not None and c.flaky is False and c.tags == []
    assert c.files == {"seed.txt": "fixture"}
    name2, err2 = materialize_canary(_SPEC, base=base)
    assert name2 is None and "already exists" in err2
    name3, err3 = materialize_canary(dict(_SPEC, name="valid-name", prompt=""), base=base)
    assert name3 is None and "prompt" in err3


# ---------------------------------------------------------------------------
# Gate-command allowlist (advisory warnings on the create API)
# ---------------------------------------------------------------------------


class TestGateAllowlist:
    def test_safe_commands(self):
        from core.canary.propose import is_gate_command_safe

        for cmd in (
            "grep -qx DONE out.txt",
            "python -m pytest tests/ -q",
            "python3 -m unittest discover tests",
            "diff expected.txt actual.txt",
            "test -f report.md",
            "cat out.txt",
        ):
            assert is_gate_command_safe(cmd) is None, cmd

    def test_unsafe_commands(self):
        from core.canary.propose import is_gate_command_safe

        for cmd in (
            "curl http://evil.example",  # binary not allowlisted
            "grep DONE out.txt; rm -rf /",  # chaining
            "cat out.txt | grep DONE",  # pipe
            "grep DONE > /dev/null",  # redirect + absolute path
            "python -c 'import os'",  # arbitrary code
            "python -m os",  # module not allowlisted
            "cat /etc/passwd",  # absolute path
            "cat ../../secrets.txt",  # traversal
            "cat ~/notes.txt",  # home expansion
            "/usr/bin/grep DONE out.txt",  # pathed binary
            "grep `whoami` out.txt",  # substitution
            "grep $HOME out.txt",  # env expansion
            "",
        ):
            assert is_gate_command_safe(cmd) is not None, cmd
