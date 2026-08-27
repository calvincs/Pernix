"""Tests for v3.1 adaptive authorship: user create_entry + the agent's
adaptive_note tool, and the rendering caps both feed into."""

from __future__ import annotations

import pytest

from core.adaptive.engine import AdaptiveError, create_entry
from db import models as db


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)


# ---------------------------------------------------------------------------
# User authorship (create_entry + POST /api/adaptive/entries)
# ---------------------------------------------------------------------------


def test_create_entry_is_active_immediately_and_journaled():
    out = create_entry("prompt_note", "verify before claiming", "Before asserting completion: read the file on disk.")
    assert out["status"] == "active" and out["version"] == 1
    row = db.adaptive_get_entry(out["entry_id"])
    assert row["source"] == "user" and row["status"] == "active"
    events = db.adaptive_list_events(entry_id=out["entry_id"])
    assert events and events[0]["action"] == "create" and events[0]["actor"] == "user"
    # Rollback by event restores the pre-create world (hard delete).
    from core.adaptive.engine import rollback

    rollback(event_id=events[0]["id"], actor="user")
    assert db.adaptive_get_entry(out["entry_id"]) is None


def test_create_entry_is_unlinted_by_design():
    """The lint substitutes for human judgment, not the other way around —
    a human may store an observation if they want one stored."""
    out = create_entry("policy", "a note to self", "Despite everything, remember the context here.")
    assert out["status"] == "active"


def test_create_entry_refuses_duplicates_and_full_caps(monkeypatch):
    create_entry("routing_hint", "same title", "Prefer x when y.")
    with pytest.raises(AdaptiveError, match="already exists"):
        create_entry("routing_hint", "same title", "Prefer z when w.")
    monkeypatch.setattr("config.settings.adaptive_max_entries_per_kind", 1)
    with pytest.raises(AdaptiveError, match="cap"):
        create_entry("routing_hint", "one too many", "Prefer q when r.")


async def test_create_entry_api_route():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers import adaptive as adaptive_router

    app = FastAPI()
    app.include_router(adaptive_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/adaptive/entries",
            json={"kind": "prompt_note", "title": "from the ui", "content": "When x: do y."},
        )
        assert resp.status_code == 200 and resp.json()["status"] == "active"
        resp = await client.post("/api/adaptive/entries", json={"kind": "nope", "title": "t", "content": "c"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Agent authorship (adaptive_note tool)
# ---------------------------------------------------------------------------


def _notes_on(monkeypatch):
    monkeypatch.setattr("config.settings.adaptive_agent_notes_enabled", True)


def test_adaptive_note_gated_on_its_flag():
    from core.tools.builtin.adaptive_tools import adaptive_note

    assert "adaptive_agent_notes_enabled is off" in adaptive_note("prompt_note", "t", "When x: do y.")


def test_adaptive_note_never_mints_policy(monkeypatch):
    from core.tools.builtin.adaptive_tools import adaptive_note

    _notes_on(monkeypatch)
    assert "never mint policy" in adaptive_note("policy", "t", "When x: do y.")


def test_adaptive_note_passes_the_lint(monkeypatch):
    from core.tools.builtin.adaptive_tools import adaptive_note

    _notes_on(monkeypatch)
    out = adaptive_note("prompt_note", "narrative", "Despite lessons, the agent repeatedly fails to comply.")
    assert out.startswith("Rejected: lint:")
    # A refusal must not consume the daily budget.
    ok = adaptive_note("prompt_note", "real note", "Before asserting completion: verify the file on disk.")
    assert "Queued" in ok


def test_adaptive_note_daily_cap(monkeypatch):
    from core.tools.builtin.adaptive_tools import adaptive_note

    _notes_on(monkeypatch)
    assert "Queued" in adaptive_note("prompt_note", "note one", "When a: do b.")
    assert "Queued" in adaptive_note("routing_hint", "note two", "Prefer c when d.")
    assert "cap is reached" in adaptive_note("prompt_note", "note three", "When e: do f.")


def test_adaptive_note_registration_gated(monkeypatch):
    from core.tools.builtin import adaptive_tools
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    monkeypatch.setattr("config.settings.adaptive_agent_notes_enabled", False)
    adaptive_tools.register(reg)
    assert reg.get("adaptive_note") is None
    monkeypatch.setattr("config.settings.adaptive_agent_notes_enabled", True)
    adaptive_tools.register(reg)
    tool = reg.get("adaptive_note")
    assert tool is not None and tool.denied_session_types == {"canary", "worker"}


# ---------------------------------------------------------------------------
# Rendering caps (both blocks)
# ---------------------------------------------------------------------------


def _put(entry_id: str, kind: str, source: str, content: str = "Prefer x when y.") -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    db.adaptive_put_entry(
        {
            "id": entry_id,
            "kind": kind,
            "scope": "global",
            "title": entry_id,
            "content": content,
            "risk": "low",
            "version": 1,
            "status": "active",
            "source": source,
            "created_at": now,
            "updated_at": now,
        }
    )


def test_policy_block_caps_and_keeps_the_humans_entries():
    from core.adaptive.render import _MAX_POLICIES, build_adaptive_block

    for i in range(_MAX_POLICIES + 3):
        _put(f"dream-policy-{i:02d}", "policy", "dream")
    _put("calvins-rule", "policy", "user")

    block = build_adaptive_block()
    assert "calvins-rule" in block  # the human's entry always renders
    assert "not rendered" in block  # dropped-count marker
    assert block.count("### Policy") == _MAX_POLICIES
    # Deterministic: identical store → identical bytes (I8).
    assert build_adaptive_block() == block


def test_hints_block_caps_ranks_by_usage_and_marks_truncation():
    from core.adaptive.render import _HINTS_MAX_LINES, build_routing_hints_block

    for i in range(_HINTS_MAX_LINES + 4):
        _put(f"hint-{i:02d}", "routing_hint", "refine")
    # The most-used hint must render even though its id sorts last.
    _put("hint-zz-used", "routing_hint", "refine")
    for _ in range(3):
        db.upsert_signal("adaptive_entry", "hint-zz-used")

    block = build_routing_hints_block()
    assert "hint-zz-used" in block
    assert "more hints" in block and "search_adaptive" in block
    assert block.count("\n- [") <= _HINTS_MAX_LINES