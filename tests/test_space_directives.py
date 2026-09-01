"""Space directive overrides (v33): per-file fallback, compiler + scout
resolution, revert semantics, birthdate stays global."""

from __future__ import annotations

from pathlib import Path

import pytest

from core import spaces as spaces_lib
from db import models as db


@pytest.fixture(autouse=True)
def _fresh_space_cache():
    spaces_lib.invalidate_space_cache()
    yield
    spaces_lib.invalidate_space_cache()


@pytest.fixture()
def agent_env(tmp_path, monkeypatch):
    """A chdir'd sandbox with default directives + one space session."""
    monkeypatch.chdir(tmp_path)
    agent = tmp_path / "data" / "agent"
    agent.mkdir(parents=True)
    (agent / "SOUL.md").write_text("DEFAULT SOUL")
    (agent / "RULES.md").write_text("DEFAULT RULES")
    (agent / "SESSIONS.md").write_text("DEFAULT SESSIONS")

    sp = db.create_space("Lab", "#123456", "lab")
    sid = db.create_session(title="in lab", space_id=sp["id"])
    loose = db.create_session(title="loose")
    return {"space": sp, "sid": sid, "loose": loose, "agent": agent}


def test_directive_path_falls_back_per_file(agent_env):
    sp, sid = agent_env["space"], agent_env["sid"]
    override_dir = spaces_lib.space_agent_dir(sp)
    override_dir.mkdir(parents=True)
    (override_dir / "RULES.md").write_text("SPACE RULES")

    # Only RULES is overridden — SOUL and SESSIONS fall back to defaults.
    assert spaces_lib.directive_path("RULES.md", sid).read_text() == "SPACE RULES"
    assert spaces_lib.directive_path("SOUL.md", sid).read_text() == "DEFAULT SOUL"
    assert spaces_lib.directive_path("SESSIONS.md", sid).read_text() == "DEFAULT SESSIONS"
    # Non-space sessions always get the defaults.
    assert spaces_lib.directive_path("RULES.md", agent_env["loose"]).read_text() == "DEFAULT RULES"


def test_compiler_block_uses_space_overrides(agent_env):
    from core.context.compiler import _build_agent_directives_block

    sp, sid = agent_env["space"], agent_env["sid"]
    override_dir = spaces_lib.space_agent_dir(sp)
    override_dir.mkdir(parents=True)
    (override_dir / "RULES.md").write_text("SPACE RULES ONLY")

    block = _build_agent_directives_block(sid)
    assert "SPACE RULES ONLY" in block
    assert "DEFAULT SOUL" in block  # per-file fallback
    assert "DEFAULT SESSIONS" in block

    loose_block = _build_agent_directives_block(agent_env["loose"])
    assert "SPACE RULES ONLY" not in loose_block
    assert "DEFAULT RULES" in loose_block


def test_revert_takes_effect_next_compile(agent_env):
    from core.context.compiler import _build_agent_directives_block

    sp, sid = agent_env["space"], agent_env["sid"]
    override_dir = spaces_lib.space_agent_dir(sp)
    override_dir.mkdir(parents=True)
    override_path = override_dir / "SOUL.md"
    override_path.write_text("SPACE SOUL")
    assert "SPACE SOUL" in _build_agent_directives_block(sid)
    override_path.unlink()  # revert == delete the override
    assert "DEFAULT SOUL" in _build_agent_directives_block(sid)


def test_sessions_slot_instructions_fallback(agent_env):
    """No SESSIONS.md anywhere -> default INSTRUCTIONS.md still wins the slot."""
    from core.context.compiler import _build_agent_directives_block

    (agent_env["agent"] / "SESSIONS.md").unlink()
    (agent_env["agent"] / "INSTRUCTIONS.md").write_text("LEGACY INSTRUCTIONS")
    block = _build_agent_directives_block(agent_env["sid"])
    assert "LEGACY INSTRUCTIONS" in block


def test_space_block_only_for_space_sessions(agent_env):
    from core.context.compiler import _build_space_block

    block = _build_space_block(agent_env["sid"])
    assert "spaces/lab/" in block
    assert "pernix.space.lab." in block
    assert _build_space_block(agent_env["loose"]) == ""
    assert _build_space_block("") == ""


def test_temporal_context_reads_default_soul(agent_env):
    """Birthdate is deployment identity — a space SOUL override must not
    change it."""
    from core.context.compiler import _build_temporal_context

    (agent_env["agent"] / "SOUL.md").write_text("me\n<!-- @birthdate: 2026-01-15 -->")
    sp = agent_env["space"]
    override_dir = spaces_lib.space_agent_dir(sp)
    override_dir.mkdir(parents=True)
    (override_dir / "SOUL.md").write_text("other\n<!-- @birthdate: 1999-09-09 -->")

    out = _build_temporal_context()
    assert "1999" not in out


def test_directive_api_roundtrip(agent_env):
    """PUT creates the override file; DELETE reverts; GET reports both."""
    from fastapi.testclient import TestClient

    from api.app import app

    client = TestClient(app)
    sp = agent_env["space"]

    r = client.get(f"/api/spaces/{sp['id']}/directives")
    assert r.status_code == 200
    files = r.json()["files"]
    assert files["RULES"]["default"] == "DEFAULT RULES"
    assert files["RULES"]["override"] is None

    r = client.put(f"/api/spaces/{sp['id']}/directives/RULES", json={"content": "OVERRIDE R"})
    assert r.status_code == 200
    assert (spaces_lib.space_agent_dir(sp) / "RULES.md").read_text() == "OVERRIDE R"

    r = client.get(f"/api/spaces/{sp['id']}/directives")
    assert r.json()["files"]["RULES"]["override"] == "OVERRIDE R"

    r = client.delete(f"/api/spaces/{sp['id']}/directives/RULES")
    assert r.status_code == 200
    assert not (spaces_lib.space_agent_dir(sp) / "RULES.md").exists()

    # Traversal / unknown names are rejected by the fixed enum.
    r = client.put(f"/api/spaces/{sp['id']}/directives/evil", json={"content": "x"})
    assert r.status_code == 400
