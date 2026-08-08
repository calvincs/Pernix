"""Regression: the coverage audit was a growth pump, and consolidation's
trivial merge archived entries without the containment guard.

Two shipped defects from the architecture review of 2026-08-07, §3.

1. **The audit measured itself.** Coverage was `store.is_duplicate(fact)` and
   the repair then called `store.add_entry(fact)`, on the stated theory that
   "add_entry re-runs its own dedup gate, so a near-miss is refused there".
   That gate *is* `is_duplicate`, on the same content against the same corpus:
   if coverage said missing, the write succeeded by construction. Every audit
   wrote up to three LLM paraphrases of possibly-present facts and counted
   them all as recovered. Fix: repair must additionally fail `_is_absent`, a
   deliberately laxer wider-net BM25 scan, and only a write that actually
   landed counts as recovered.

2. **Consolidation's trivial merge had no containment guard.** It archived at
   SequenceMatcher > 0.82 with nothing else, while `sweeps._pairwise_dedup` —
   the same operation — required the archived entry's tokens to be a subset of
   the survivor's, precisely so structured facts differing in one key value
   aren't destroyed. Fix: both call the shared `loses_no_unique_token`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.llm.types import ChatResponse, TokenUsage
from core.memory.audit import _is_absent, run_audit
from core.memory.consolidate import plan_trivial_merge
from core.memory.format import MemoryEntry
from db import models as db

_FACT = "The nightly backup job writes to /storage/backups and prunes anything older than thirty days."


# ---------------------------------------------------------------------------
# 1. Audit repair gate
# ---------------------------------------------------------------------------


def _hit(content: str):
    return SimpleNamespace(entry=SimpleNamespace(content=content, file_name="pernix.ops", epoch=1))


class _Store:
    """Coverage gate and wider net are independently controllable — which is
    the whole point of the fix: they must be able to disagree."""

    def __init__(self, *, covered: bool, neighbours: list[str] | None = None):
        self.covered = covered
        self.neighbours = neighbours or []
        self.added: list[dict] = []
        self.searches = 0

    def is_duplicate(self, content: str) -> bool:
        return self.covered

    def search(self, query, **kwargs):
        self.searches += 1
        return [_hit(n) for n in self.neighbours]

    def add_entry(self, **kwargs) -> str:
        self.added.append(kwargs)
        return f"Saved to {kwargs.get('file_name') or 'pernix.notes'} (epoch=1)"


def _distilled_session() -> str:
    sid = db.create_session(title="Audit Target")
    for i in range(4):
        db.add_message(sid, "user", f"Question {i} about the backup schedule and retention " * 10)
        db.add_message(sid, "assistant", f"Answer {i} covering the nightly job specifics " * 10)
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with db.connect_sessions() as conn:
        conn.execute(
            "UPDATE sessions SET snooze_reviewed_at = ?, updated_at = ?, state = 'idle' WHERE id = ?",
            (past, past, sid),
        )
    return sid


def _facts_response(n: int = 2) -> ChatResponse:
    facts = [{"kind": "finding", "content": f"{_FACT} Detail number {i}."} for i in range(n)]
    return ChatResponse(
        content=json.dumps(facts),
        tool_calls=None,
        usage=TokenUsage(10, 5, 15),
        model="test",
        provider="fake",
        finish_reason="stop",
    )


def test_wider_net_recognises_a_near_paraphrase_the_gate_missed():
    store = _Store(covered=False, neighbours=["The nightly backup writes into /storage/backups, pruning past 30 days."])
    assert _is_absent(store, _FACT) is False


def test_wider_net_reports_absence_only_when_nothing_resembles():
    store = _Store(covered=False, neighbours=["Snooze yields to live sessions and never competes for the model."])
    assert _is_absent(store, _FACT) is True


def test_wider_net_treats_a_failed_scan_as_present():
    class _Broken(_Store):
        def search(self, query, **kwargs):
            raise RuntimeError("index locked")

    assert _is_absent(_Broken(covered=False), _FACT) is False


async def test_repair_is_blocked_when_the_wider_net_finds_the_fact(mock_llm_client):
    """The exact growth-pump case: gate says missing, store plainly has it."""
    _distilled_session()
    mock_llm_client.responses = [_facts_response(2)]
    store = _Store(covered=False, neighbours=[_FACT])

    out = await run_audit(store, lambda: False)

    assert out["missed"] == 2
    assert out["recovered"] == 0
    assert out["repair_blocked"] == 2
    assert store.added == []


async def test_genuine_misses_are_still_repaired(mock_llm_client):
    _distilled_session()
    mock_llm_client.responses = [_facts_response(2)]
    store = _Store(covered=False, neighbours=["Unrelated note about the scout preload character budget."])

    out = await run_audit(store, lambda: False)

    assert out == {"audited": 1, "facts": 2, "missed": 2, "recovered": 2, "repair_blocked": 0}
    assert len(store.added) == 2
    assert all(e["source"] == "audit" for e in store.added)


async def test_a_refused_write_is_not_counted_as_recovered(mock_llm_client):
    _distilled_session()
    mock_llm_client.responses = [_facts_response(1)]

    class _Refusing(_Store):
        def add_entry(self, **kwargs) -> str:
            self.added.append(kwargs)
            return "Memory already contains similar content — entry skipped (duplicate of pernix.ops@1: ...)."

    store = _Refusing(covered=False, neighbours=[])

    out = await run_audit(store, lambda: False)

    assert out["recovered"] == 0
    assert out["repair_blocked"] == 1


# ---------------------------------------------------------------------------
# 2. Consolidation containment guard
# ---------------------------------------------------------------------------


def _entry(file_name: str, epoch: int, content: str) -> MemoryEntry:
    return MemoryEntry(
        file_name=file_name,
        content=content,
        entry_type="finding",
        tags=[],
        weight="normal",
        epoch=epoch,
        source="test",
    )


class _ConsolidateStore:
    def __init__(self, entries: dict[str, list[MemoryEntry]]):
        self._entries = entries

    def read_file(self, name: str) -> str:
        return "stub"  # parse is monkeypatched

    def list_files(self):
        return [SimpleNamespace(name=n, keywords=[]) for n in self._entries]


def _plan(monkeypatch, entries: dict[str, list[MemoryEntry]]):
    # plan_trivial_merge imports the parser from core.memory.format at call
    # time, so that is where the stand-in has to go.
    import core.memory.format as fmt

    monkeypatch.setattr(fmt, "parse_entries_from_markdown", lambda name, raw: entries[name])
    return plan_trivial_merge(list(entries), _ConsolidateStore(entries))


def test_trivial_merge_keeps_facts_that_differ_in_a_key_value(monkeypatch):
    """0.9-similar, but each carries a token the other lacks."""
    prod = "The deploy key for prod is AAAA and the service answers on port 8090 behind TLS."
    dev = "The deploy key for dev is BBBB and the service answers on port 8091 behind TLS."
    decision = _plan(
        monkeypatch,
        {"pernix.deploy": [_entry("pernix.deploy", 100, prod)], "pernix_deploy": [_entry("pernix_deploy", 200, dev)]},
    )

    assert decision is not None
    assert decision.entries_to_archive == []
    assert len(decision.entries_to_keep) == 2


def test_trivial_merge_still_archives_a_true_subset(monkeypatch):
    longer = "The deploy key for prod is AAAA and the service answers on port 8090 behind TLS termination."
    shorter = "The deploy key for prod is AAAA and the service answers on port 8090 behind TLS."
    decision = _plan(
        monkeypatch,
        {
            "pernix.deploy": [_entry("pernix.deploy", 100, longer)],
            "pernix_deploy": [_entry("pernix_deploy", 200, shorter)],
        },
    )

    assert decision is not None
    assert decision.entries_to_archive == [("pernix_deploy", 200)]
