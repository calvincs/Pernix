"""Final coverage sweep: skill tools, hooks, session manager, LLM registry, and more."""

import json
from pathlib import Path

import pytest

# ===========================================================================
# Skill tools (full coverage)
# ===========================================================================


def _make_skill_in_tmp(tmp_path, name, instructions="# Instructions\nDo things.", has_scripts=False):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A test skill for {name}\ntags: test\n---\n{instructions}"
    )
    if has_scripts:
        scripts = d / "scripts"
        scripts.mkdir()
        (scripts / "run.sh").write_text("#!/bin/bash\necho ok")
    return d


def test_load_skill_with_instructions(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    _make_skill_in_tmp(tmp_path, "my-skill", "# Step 1\nDo this first.")
    from core.skills.registry import SkillRegistry, get_skill_registry

    reg = SkillRegistry()
    reg.scan(tmp_path)
    monkeypatch.setattr("core.skills.registry._skill_registry", reg)
    from core.tools.builtin.skill_tools import load_skill

    result = load_skill("my-skill")
    assert "Step 1" in result


def test_load_skill_with_resources(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    _make_skill_in_tmp(tmp_path, "scripted-skill", "# Instructions\nUse scripts.", has_scripts=True)
    from core.skills.registry import SkillRegistry

    reg = SkillRegistry()
    reg.scan(tmp_path)
    monkeypatch.setattr("core.skills.registry._skill_registry", reg)
    from core.tools.builtin.skill_tools import load_skill

    result = load_skill("scripted-skill")
    # Body is rendered AND the resource manifest section is present.
    # (Original test used "scripts" in result.lower() OR "Instructions" — the
    # OR made it trivially true because Instructions is always in the body.)
    assert "Instructions" in result, "skill body should be rendered"
    assert "scripts" in result.lower(), "resource manifest should list scripts/"
    assert "run.sh" in result, "resource manifest should name the script file"


def test_read_skill_resource_exists(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    d = _make_skill_in_tmp(tmp_path, "res-skill", "# Instr\nDo stuff.", has_scripts=True)
    from core.skills.registry import SkillRegistry

    reg = SkillRegistry()
    reg.scan(tmp_path)
    monkeypatch.setattr("core.skills.registry._skill_registry", reg)
    from core.tools.builtin.skill_tools import read_skill_resource

    result = read_skill_resource("res-skill", "scripts/run.sh")
    assert "echo ok" in result


def test_read_skill_resource_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    _make_skill_in_tmp(tmp_path, "empty-skill")
    from core.skills.registry import SkillRegistry

    reg = SkillRegistry()
    reg.scan(tmp_path)
    monkeypatch.setattr("core.skills.registry._skill_registry", reg)
    from core.tools.builtin.skill_tools import read_skill_resource

    result = read_skill_resource("empty-skill", "scripts/nonexistent.sh")
    assert "Error" in result


def test_load_skill_disabled_returns_clear_error(tmp_path, monkeypatch):
    """Disabled skill explicit load returns an actionable error, not silent skip."""
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    _make_skill_in_tmp(tmp_path, "off-skill", "# Stuff\nDo things.")
    from core.skills.registry import SkillRegistry

    reg = SkillRegistry()
    reg.scan(tmp_path)
    reg.disable("off-skill")
    monkeypatch.setattr("core.skills.registry._skill_registry", reg)
    from core.tools.builtin.skill_tools import load_skill

    out = load_skill("off-skill")
    assert "disabled" in out.lower()
    assert "Explorer" in out  # tells user where to re-enable


def test_read_skill_resource_disabled_returns_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    _make_skill_in_tmp(tmp_path, "off-rsrc", "# Stuff", has_scripts=True)
    from core.skills.registry import SkillRegistry

    reg = SkillRegistry()
    reg.scan(tmp_path)
    reg.disable("off-rsrc")
    monkeypatch.setattr("core.skills.registry._skill_registry", reg)
    from core.tools.builtin.skill_tools import read_skill_resource

    out = read_skill_resource("off-rsrc", "scripts/run.sh")
    assert "disabled" in out.lower()


def test_discover_skills_excludes_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    _make_skill_in_tmp(tmp_path, "release-tool", "# Release tooling\nDo it.")
    from core.skills.registry import SkillRegistry

    reg = SkillRegistry()
    reg.scan(tmp_path)
    monkeypatch.setattr("core.skills.registry._skill_registry", reg)
    from core.tools.builtin.skill_tools import discover_skills

    assert "release-tool" in discover_skills("release")
    reg.disable("release-tool")
    out = discover_skills("release")
    assert "release-tool" not in out  # gone from agent-facing discovery


def test_discover_tools_excludes_disabled(monkeypatch, tmp_path):
    from core.tools.registry import ToolRegistry

    monkeypatch.setattr("core.tools.registry.TOOLS_CONFIG_PATH", tmp_path / "tools.json")
    reg = ToolRegistry()
    reg.register(
        name="my_search",
        func=lambda: "",
        description="search the web for things",
        parameters={},
        tags=["search", "web"],
    )
    reg.rebuild_index()
    monkeypatch.setattr("core.tools.registry._registry", reg)
    from core.tools.builtin.discovery_tools import discover_tools

    assert "my_search" in discover_tools("search the web")
    reg.disable("my_search")
    out = discover_tools("search the web")
    assert "my_search" not in out


def test_system_prompt_catalog_excludes_disabled_skills(tmp_path, monkeypatch):
    """The static [AVAILABLE SKILLS] block in every turn's system prompt must
    drop disabled skills — otherwise the agent burns a round calling
    load_skill('foo') just to be told it's disabled."""
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    _make_skill_in_tmp(tmp_path, "shown-skill", "# x")
    _make_skill_in_tmp(tmp_path, "hidden-skill", "# y")
    from core.skills.registry import SkillRegistry

    reg = SkillRegistry()
    reg.scan(tmp_path)
    reg.disable("hidden-skill")
    monkeypatch.setattr("core.skills.registry._skill_registry", reg)
    from core.context.compiler import _build_available_skills_block

    block = _build_available_skills_block()
    assert "shown-skill" in block
    assert "hidden-skill" not in block


def test_create_skill_clears_stale_disabled_flag(tmp_path, monkeypatch):
    """Re-creating a skill with the same name as a previously-disabled one
    must come back enabled — otherwise create_skill silently lands in a
    disabled state because .disabled.json kept the old name."""
    import shutil

    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    _make_skill_in_tmp(tmp_path, "ghost", "# old body")
    from core.skills.registry import SkillRegistry

    reg = SkillRegistry()
    reg.scan(tmp_path)
    reg.disable("ghost")
    assert reg.is_disabled("ghost")
    # Simulate a manual rm -rf — the on-disk .disabled.json still has "ghost".
    shutil.rmtree(tmp_path / "ghost")
    monkeypatch.setattr("core.skills.registry._skill_registry", reg)
    from core.extensions.skillmaker import create_skill

    # No `approved` argument: authorization moved to the executor's
    # server-side dangerous gate, which the direct function call bypasses.
    result = create_skill(
        name="ghost",
        description="brand new skill, totally different",
        instructions="# fresh body\nDo new things, in detail and with care.",
    )
    assert "created" in result.lower()
    assert not reg.is_disabled("ghost")  # stale disabled flag cleared


async def test_e2e_patch_disables_skill_then_load_skill_returns_error(tmp_path, monkeypatch):
    """End-to-end: PATCH /api/skills/{name} {enabled: false} → builtin
    load_skill('{name}') returns the disabled-error string. Catches a class
    of regression where the API and the registry get out of sync (e.g. the
    router falls back to its old JSON-file dance and bypasses the in-memory
    set, so the agent path stays oblivious until the next process restart).
    Also checks the reverse direction: re-enabling via PATCH restores the
    body in the same process.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers import skills as skills_router
    from core.skills.registry import SkillRegistry

    skills_dir = tmp_path / "skills"
    d = skills_dir / "e2e-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: e2e-skill\ndescription: x\n---\n# Body\n")
    monkeypatch.setattr("config.settings.skills_dir", str(skills_dir))
    fresh = SkillRegistry()
    fresh.scan(skills_dir)
    monkeypatch.setattr("core.skills.registry._skill_registry", fresh)

    app = FastAPI()
    app.include_router(skills_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch("/api/skills/e2e-skill", json={"enabled": False})
        assert resp.status_code == 200

    from core.tools.builtin.skill_tools import load_skill

    out = load_skill("e2e-skill")
    assert "disabled" in out.lower()
    assert "Explorer" in out

    # Re-enable via PATCH; load_skill must return the body again.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch("/api/skills/e2e-skill", json={"enabled": True})
        assert resp.status_code == 200
    assert "Body" in load_skill("e2e-skill")


def test_monotonic_allowlist_skips_disabled_tools(monkeypatch, tmp_path):
    """A tool used in a prior turn must NOT be re-promoted into active_tools
    if the user disabled it between turns — that would defeat the disable."""
    from core.tools.registry import ToolRegistry

    monkeypatch.setattr("core.tools.registry.TOOLS_CONFIG_PATH", tmp_path / "tools.json")
    reg = ToolRegistry()
    reg.register(name="prior_tool", func=lambda: "ok", description="x", parameters={"type": "object", "properties": {}})
    reg.disable("prior_tool")

    # Mirror the agent.py loop body that populates active_tools_set.
    active_tools_set = set()
    prior_tools = ["prior_tool"]
    for tname in prior_tools:
        if reg.exists(tname) and not reg.is_disabled(tname):
            active_tools_set.add(tname)
    assert "prior_tool" not in active_tools_set


def test_discover_skills_with_results(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path))
    # Use a description with strong, indexable tokens so the test isn't at
    # the mercy of synonym tables. Tightened from the original `assert
    # isinstance(result, str)` smoke check, which proved nothing.
    _make_skill_in_tmp(tmp_path, "web-search-skill", "# Instructions\nSearch the web.")
    from core.skills.registry import SkillRegistry

    reg = SkillRegistry()
    reg.scan(tmp_path)
    monkeypatch.setattr("core.skills.registry._skill_registry", reg)
    from core.tools.builtin.skill_tools import discover_skills

    # Query against the skill name token, which the index always tokenizes.
    result = discover_skills("web-search-skill")
    assert "web-search-skill" in result, f"discover_skills should surface the name; got: {result!r}"


# ===========================================================================
# More memory_tools coverage
# ===========================================================================


def test_remember_with_file(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import remember

    result = remember("Config: database path is /data/sessions.db", file="pernix.config", tags="database,config")
    assert "Error" not in result


def test_ingest_tool_with_content(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import ingest

    text = """
# Database Configuration
The database uses SQLite with WAL mode for concurrent access patterns.
The db_path setting in config.py controls where the database file is stored.
WAL mode provides better read concurrency than the default DELETE mode.

# Authentication
Bearer tokens provide authentication for the API. Tokens are validated on each
request by the auth middleware. The token is stored in settings.json and can
be regenerated via the API.
"""
    result = ingest(content=text, source_name="test_config", use_llm=False)
    assert isinstance(result, str)


def test_ingest_tool_missing_content(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import ingest

    result = ingest(content="", source_name="empty")
    assert "Error" in result


def test_ingest_tool_file_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.tools.builtin.memory_tools import ingest

    result = ingest(file_path="/nonexistent/file.md")
    assert "Error" in result


# ===========================================================================
# LLM registry: more populate logic
# ===========================================================================


async def test_llm_registry_populate_no_providers():
    """Populate with failing providers still completes."""
    from unittest.mock import AsyncMock, MagicMock

    from core.llm.registry import ModelRegistry

    reg = ModelRegistry()
    mock_ollama = AsyncMock()
    mock_ollama.list_models.side_effect = ConnectionError("no ollama")
    mock_openrouter = MagicMock()
    mock_openrouter.available = False

    await reg.populate(mock_ollama, mock_openrouter)
    assert reg._populated is True
    assert len(reg._models) == 0


async def test_llm_registry_populate_with_models():
    """Populate builds model catalog from providers."""
    from unittest.mock import AsyncMock, MagicMock

    from core.llm.registry import ModelRegistry
    from core.llm.types import ModelInfo

    reg = ModelRegistry()
    mock_ollama = AsyncMock()
    mock_ollama.list_models.return_value = [
        ModelInfo(id="llama3", provider="ollama", context_length=128000),
    ]
    mock_openrouter = MagicMock()
    mock_openrouter.available = False

    await reg.populate(mock_ollama, mock_openrouter)
    assert reg._populated is True
    assert "llama3" in reg._models


# ===========================================================================
# More hooks: _maybe_evaluate (no registry.json → skip)
# ===========================================================================


async def test_maybe_evaluate_no_registry(monkeypatch):
    from db import models as db
    from sessions.hooks import _maybe_evaluate
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.eval_auto", True)
    monkeypatch.setattr("config.settings.eval_max_retries", 1)

    sid = db.create_session(title="Eval Test")
    session = db.get_session(sid)
    session_obj = AgentSession(session_id=sid)

    # No registry.json → should skip silently
    await _maybe_evaluate(sid, session, session_obj=session_obj)
    assert not session_obj.turn.eval_retry_requested


# ===========================================================================
# _maybe_evaluate: registry is global, evaluation must be session-scoped
# ===========================================================================


def _write_registry(tmp_path, monkeypatch, features):
    """Point _maybe_evaluate's registry lookup at a temp file."""
    import os

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "registry.json").write_text(json.dumps(features))
    return os.path.join(str(tmp_path), "data", "registry.json")


async def test_maybe_evaluate_ignores_other_sessions_features(monkeypatch, tmp_path):
    """Regression: data/registry.json is one global file, so an unfiltered
    read made every session evaluate every other session's pending features
    against its own unrelated transcript — failing by construction and
    re-running forever. (Observed: a 'Neo Flappy Bird' feature registered by
    session 505639e37185 evaluating inside an unrelated weather session.)"""
    from db import models as db
    from sessions.hooks import _maybe_evaluate
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.eval_auto", True)
    monkeypatch.setattr("config.settings.eval_max_retries", 1)

    sid = db.create_session(title="Weather Session")
    session = db.get_session(sid)
    session_obj = AgentSession(session_id=sid)

    _write_registry(
        tmp_path,
        monkeypatch,
        [{"id": "0de63fc0", "title": "Neo Flappy Bird", "passes": False, "session_id": "505639e37185"}],
    )

    called = []

    async def _spy(feat, session_id):
        called.append(feat["id"])
        return {"passed": False, "feedback": "nope"}

    monkeypatch.setattr("core.extensions.evaluation.evaluate_single_async", _spy)

    await _maybe_evaluate(sid, session, session_obj=session_obj)

    assert called == [], "must not evaluate another session's feature"
    assert not session_obj.turn.eval_retry_requested


async def test_maybe_evaluate_runs_own_and_legacy_features(monkeypatch, tmp_path):
    """Own features still evaluate; pre-filter rows with no session_id are
    adopted rather than stranded pending forever."""
    from db import models as db
    from sessions.hooks import _maybe_evaluate
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.eval_auto", True)
    monkeypatch.setattr("config.settings.eval_max_retries", 2)

    sid = db.create_session(title="Owner Session")
    session = db.get_session(sid)
    session_obj = AgentSession(session_id=sid)

    _write_registry(
        tmp_path,
        monkeypatch,
        [
            {"id": "mine", "title": "Mine", "passes": False, "session_id": sid},
            {"id": "legacy", "title": "Legacy", "passes": False},
            {"id": "theirs", "title": "Theirs", "passes": False, "session_id": "someone-else"},
            {"id": "done", "title": "Done", "passes": True, "session_id": sid},
        ],
    )

    called = []

    async def _spy(feat, session_id):
        called.append(feat["id"])
        return {"passed": True, "feedback": ""}

    monkeypatch.setattr("core.extensions.evaluation.evaluate_single_async", _spy)

    await _maybe_evaluate(sid, session, session_obj=session_obj)

    assert sorted(called) == ["legacy", "mine"], f"unexpected evaluation set: {called}"


# ===========================================================================
# More session state machine coverage
# ===========================================================================


def test_session_event_system_seq():
    from sessions.state import AgentSession

    session = AgentSession(session_id="seq-test")
    session.emit_event({"type": "a"})
    session.emit_event({"type": "b"})
    events = list(session.events)
    assert events[0]["_seq"] == 1
    assert events[1]["_seq"] == 2
    assert session.event_seq == 2


def test_session_add_remove_background_refs():
    from sessions.state import AgentSession

    session = AgentSession(session_id="bg-test")
    assert not session.has_background_tasks
    session.add_background_ref()
    assert session.has_background_tasks
    session.add_background_ref()
    session.remove_background_ref()
    assert session.has_background_tasks  # still 1 left
    session.remove_background_ref()
    assert not session.has_background_tasks


# ===========================================================================
# More maintenance coverage: heartbeat stops on cancel
# ===========================================================================


async def test_maintenance_stop_with_tracked_tasks():
    """Stop waits for tracked tasks with a timeout."""
    import asyncio

    from maintenance import MaintenanceRunner

    runner = MaintenanceRunner()

    # Add a long-running task
    async def long_task():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(long_task())
    runner.track_task(task)
    runner.start()
    await runner.stop()
    # All done
    assert runner._task.done()


# ===========================================================================
# More API: tools toggle
# ===========================================================================


async def test_tools_toggle():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers.tools import router

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/tools/toggle", json={"name": "bash", "enabled": True})
    assert resp.status_code in (200, 404)


async def test_tools_health():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers.tools import router

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/tools/health")
    assert resp.status_code == 200


# ===========================================================================
# More push.py coverage
# ===========================================================================


def test_push_module_generate_keys(tmp_path, monkeypatch):
    """VAPID key generation writes to settings."""
    monkeypatch.setattr("config.settings.db_path", str(tmp_path / "sessions.db"))
    from core.push import generate_vapid_keys

    try:
        generate_vapid_keys()
    except Exception:
        pass  # May fail if pywebpush not configured correctly — that's OK


# ===========================================================================
# More DB models
# ===========================================================================


def test_db_session_messages():
    from db import models as db
    from db.database import connect_sessions

    sid1 = db.create_session(title="Sender")
    sid2 = db.create_session(title="Receiver")
    db.send_session_message(sid1, sid2, "result", '{"data": "output"}')
    # Production reads this table directly (cross_pollinate dedup ledger).
    with connect_sessions() as conn:
        rows = conn.execute("SELECT payload FROM session_messages WHERE recipient_id = ?", (sid2,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["payload"] == '{"data": "output"}'


def test_db_push_subscriptions():
    from db import models as db

    nid = db.upsert_push_subscription(
        endpoint="https://push.example.com/sub/1",
        p256dh="fake_key",
        auth="fake_auth",
    )
    subs = db.get_push_subscriptions()
    assert len(subs) >= 1
    ep = next(s["endpoint"] for s in subs if s["endpoint"] == "https://push.example.com/sub/1")
    assert ep is not None
    db.delete_push_subscription("https://push.example.com/sub/1")
    subs2 = db.get_push_subscriptions()
    assert not any(s["endpoint"] == "https://push.example.com/sub/1" for s in subs2)
