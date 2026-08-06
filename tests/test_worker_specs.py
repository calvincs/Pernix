"""Pernix — worker_spec consumption (follow-on to plan 4b).

A worker_spec is an approved adaptive entry whose YAML content templates a
worker: instructions, model, gates. spawn_worker(spec=...) consumes it; the
compiler renders the [WORKER SPECS] catalog for non-worker sessions.
"""

import pytest

from core.adaptive.specs import build_worker_specs_block, load_worker_spec, parse_worker_spec
from db import models as db

_SPEC_YAML = """\
instructions: |
  You review pull requests. Check style, tests, and edge cases.
model: qwen3:32b
gates:
  - name: tests
    command: python -m pytest -q
    watch_paths: [src/]
"""


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    import core.adaptive.render as render

    monkeypatch.setattr(render, "MIRROR_PATH", tmp_path / "ADAPTIVE.md")


def _put_spec(entry_id="pr-reviewer", content=_SPEC_YAML, status="active"):
    db.adaptive_put_entry(
        {
            "id": entry_id,
            "kind": "worker_spec",
            "scope": "global",
            "title": "PR reviewer",
            "content": content,
            "risk": "high",
            "version": 1,
            "status": status,
            "source": "user",
        }
    )


# ---------------------------------------------------------------------------
# Parsing + loading
# ---------------------------------------------------------------------------


def test_parse_yaml_spec():
    _put_spec()
    spec = load_worker_spec("pr-reviewer")
    assert "review pull requests" in spec["instructions"]
    assert spec["model"] == "qwen3:32b"
    assert spec["gates"] == [{"name": "tests", "command": "python -m pytest -q", "watch_paths": ["src/"]}]


def test_parse_prose_spec_falls_back_to_instructions():
    _put_spec(content="Just review things carefully.")
    spec = load_worker_spec("pr-reviewer")
    assert spec["instructions"] == "Just review things carefully."
    assert spec["model"] == "" and spec["gates"] == []


def test_load_rejects_wrong_kind_or_inactive():
    assert load_worker_spec("nope") is None
    _put_spec(status="deleted")
    assert load_worker_spec("pr-reviewer") is None
    db.adaptive_put_entry(
        {"id": "a-hint", "kind": "routing_hint", "scope": "global", "title": "h", "content": "c", "source": "user"}
    )
    assert load_worker_spec("a-hint") is None


# ---------------------------------------------------------------------------
# Catalog block
# ---------------------------------------------------------------------------


def test_specs_block_renders_and_omits_when_empty(monkeypatch):
    assert build_worker_specs_block() == ""
    _put_spec()
    block = build_worker_specs_block()
    assert "[WORKER SPECS]" in block and "pr-reviewer" in block
    assert "model=qwen3:32b" in block and "1 gate(s)" in block
    monkeypatch.setattr("config.settings.adaptive_enabled", False)
    assert build_worker_specs_block() == ""


def test_compiler_block_suppressed_for_workers():
    from core.context.compiler import _build_worker_specs_block

    _put_spec()
    normal = db.create_session(title="n")
    worker = db.create_session(title="w", session_type="worker")
    assert "[WORKER SPECS]" in _build_worker_specs_block(normal)
    assert _build_worker_specs_block(worker) == ""


# ---------------------------------------------------------------------------
# spawn_worker consumption
# ---------------------------------------------------------------------------


def test_spawn_worker_unknown_spec_errors_before_session_create():
    from core.extensions.orchestration import spawn_worker

    before = len(db.list_sessions(limit=500))
    out = spawn_worker("do things", spec="no-such-spec", _context={"session_id": "parent"})
    assert out.startswith("Error: No active worker_spec")
    assert len(db.list_sessions(limit=500)) == before


def test_spawn_worker_applies_spec(monkeypatch):
    """Spec supplies instructions + model + gates; explicit model overrides."""
    from types import SimpleNamespace

    import core.extensions.orchestration as orch

    _put_spec()
    parent_id = db.create_session(title="parent")
    created = {}

    class _Mgr:
        def get(self, sid):
            return None  # parent not in memory -> skips state/budget checks

        def create_session(self, title="", system_prompt="", session_type="worker", parent_session_id=None):
            sid = db.create_session(title=title, session_type=session_type, parent_session_id=parent_session_id)
            created["id"] = sid
            created["session"] = SimpleNamespace(model_override=None, active_goal_id=None)
            return sid

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _Mgr())
    # Model validation consults the live registry (absent in tests); it is
    # exception-tolerant by design, so a raising client skips it.
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: (_ for _ in ()).throw(RuntimeError("no llm")))
    out = orch.spawn_worker("review PR #42", spec="pr-reviewer", _context={"session_id": parent_id})
    # The spawn proceeds past gates into async dispatch which fails without
    # a loop — but the observable spec effects are already durable:
    wid = created["id"]
    prompt = (db.get_session(wid) or {}).get("system_prompt", "")
    assert "review pull requests" in prompt  # spec instructions embedded
    assert "running on model: qwen3:32b" in prompt  # spec model adopted
    gates = db.get_gates(wid)
    assert [g["name"] for g in gates] == ["tests"]
    assert "verified by deterministic gates: tests" in prompt
    assert out  # some result string returned either way


def test_spawn_worker_explicit_model_beats_spec(monkeypatch):
    from types import SimpleNamespace

    import core.extensions.orchestration as orch

    _put_spec()
    parent_id = db.create_session(title="parent")
    created = {}

    class _Mgr:
        def get(self, sid):
            return None

        def create_session(self, **kw):
            sid = db.create_session(
                title=kw.get("title", ""),
                session_type=kw.get("session_type", "worker"),
                parent_session_id=kw.get("parent_session_id"),
            )
            created["id"] = sid
            return sid

    monkeypatch.setattr("sessions.manager.get_manager", lambda: _Mgr())
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: (_ for _ in ()).throw(RuntimeError("no llm")))
    orch.spawn_worker("review", spec="pr-reviewer", model="llama3:8b", _context={"session_id": parent_id})
    prompt = (db.get_session(created["id"]) or {}).get("system_prompt", "")
    assert "running on model: llama3:8b" in prompt
    assert "qwen3:32b" not in prompt
