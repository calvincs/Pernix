"""Tests for core/memory/audit.py — the distillation coverage audit."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from config import settings
from core.llm.types import ChatResponse, TokenUsage
from core.memory.audit import audit_budget_left, parse_facts, run_audit
from db import models as db

# ---------------------------------------------------------------------------
# parse_facts
# ---------------------------------------------------------------------------

_LONG = "The user's production database is SQLite in WAL mode and migrations must stay backwards compatible."


def test_parse_facts_valid_array():
    facts = parse_facts(json.dumps([{"kind": "constraint", "content": _LONG}]))
    assert len(facts) == 1
    assert facts[0]["kind"] == "constraint"


def test_parse_facts_skip_and_garbage():
    assert parse_facts("SKIP") == []
    assert parse_facts("not json") == []
    assert parse_facts("") == []


def test_parse_facts_fences_and_dict():
    fenced = f'```json\n[{{"kind": "finding", "content": "{_LONG}"}}]\n```'
    assert len(parse_facts(fenced)) == 1
    assert len(parse_facts(json.dumps({"kind": "finding", "content": _LONG}))) == 1


def test_parse_facts_unknown_kind_coerced_and_stubs_dropped():
    facts = parse_facts(
        json.dumps(
            [
                {"kind": "revelation", "content": _LONG},
                {"kind": "finding", "content": "too short"},
            ]
        )
    )
    assert len(facts) == 1
    assert facts[0]["kind"] == "finding"


def test_parse_facts_capped_at_max():
    facts = parse_facts(json.dumps([{"kind": "finding", "content": _LONG + str(i)} for i in range(10)]))
    assert len(facts) == 6


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------


def test_audit_budget(monkeypatch):
    monkeypatch.setattr("config.settings.distill_audit_per_day", 2)
    assert audit_budget_left()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db.set_snooze_state(f"distill_audits:{today}", "2")
    assert not audit_budget_left()
    monkeypatch.setattr("config.settings.distill_audit_per_day", 0)
    assert not audit_budget_left()


# ---------------------------------------------------------------------------
# run_audit (integration against the real sessions DB)
# ---------------------------------------------------------------------------


class FakeStore:
    """Empty store: the coverage gate is dialled by `covered`, and the repair
    gate's wider-net scan finds nothing, so misses are genuinely absent. The
    two gates disagreeing is covered in
    tests/regressions/test_2026-08-07_audit_repair_and_containment.py."""

    def __init__(self, covered: bool):
        self.covered = covered
        self.added: list[dict] = []

    def is_duplicate(self, content: str) -> bool:
        return self.covered

    def search(self, query: str, **kwargs) -> list:
        return []

    def add_entry(self, **kwargs) -> str:
        self.added.append(kwargs)
        return f"Saved to {kwargs.get('file_name') or 'auto'} (epoch=1)"


def _make_distilled_session() -> str:
    sid = db.create_session(title="Audit Target")
    for i in range(4):
        db.add_message(sid, "user", f"Question {i} about database configuration details " * 10)
        db.add_message(sid, "assistant", f"Answer {i} covering SQLite WAL specifics " * 10)
    # Backdate + stamp as distilled + idle so _pick_session accepts it.
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with db.connect_sessions() as conn:
        conn.execute(
            "UPDATE sessions SET snooze_reviewed_at = ?, updated_at = ?, state = 'idle' WHERE id = ?",
            (past, past, sid),
        )
    return sid


def _fact_response(n: int = 2) -> ChatResponse:
    facts = [{"kind": "finding", "content": _LONG + f" Detail number {i}."} for i in range(n)]
    return ChatResponse(
        content=json.dumps(facts),
        tool_calls=None,
        usage=TokenUsage(10, 5, 15),
        model="test",
        provider="fake",
        finish_reason="stop",
    )


async def test_run_audit_misses_are_recovered(mock_llm_client):
    sid = _make_distilled_session()
    mock_llm_client.responses = [_fact_response(2)]
    store = FakeStore(covered=False)

    out = await run_audit(store, lambda: False)

    assert out == {"audited": 1, "facts": 2, "missed": 2, "recovered": 2, "repair_blocked": 0}
    assert len(store.added) == 2
    assert all(e["source"] == "audit" for e in store.added)
    # Watermark stamped — the session is never audited twice.
    assert db.get_snooze_state(f"distill_audit:{sid}")
    out2 = await run_audit(store, lambda: False)
    assert out2["audited"] == 0


async def test_run_audit_full_coverage_repairs_nothing(mock_llm_client):
    _make_distilled_session()
    mock_llm_client.responses = [_fact_response(3)]
    store = FakeStore(covered=True)

    out = await run_audit(store, lambda: False)

    assert out == {"audited": 1, "facts": 3, "missed": 0, "recovered": 0, "repair_blocked": 0}
    assert store.added == []


async def test_run_audit_skip_response(mock_llm_client):
    sid = _make_distilled_session()
    mock_llm_client.responses = [
        ChatResponse(
            content="SKIP",
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]
    store = FakeStore(covered=False)
    out = await run_audit(store, lambda: False)
    assert out == {"audited": 1, "facts": 0, "missed": 0, "recovered": 0, "repair_blocked": 0}
    assert db.get_snooze_state(f"distill_audit:{sid}")


async def test_run_audit_respects_budget(mock_llm_client):
    _make_distilled_session()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db.set_snooze_state(f"distill_audits:{today}", str(settings.distill_audit_per_day))
    store = FakeStore(covered=False)
    out = await run_audit(store, lambda: False)
    assert out["audited"] == 0
    assert mock_llm_client.call_count == 0


async def test_run_audit_nothing_due(mock_llm_client):
    # No distilled sessions at all.
    store = FakeStore(covered=False)
    out = await run_audit(store, lambda: False)
    assert out == {"audited": 0, "facts": 0, "missed": 0, "recovered": 0, "repair_blocked": 0}
