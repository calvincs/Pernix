"""Tests for the agent-ergonomics batch (docs/dev/agent-ergonomics-plan.md):
turn-boundary ledger, repair-tool pairing, remember(supersede=), federated
deep_recall sections, retention distill-before-delete, agent_state digest,
SYSTEM-MAP generation. (The retro-lint sweep and provenance rendering went
with the adaptive layer in 3.2.)"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from db import models as db


def _iso(days_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _adaptive_entry(entry_id: str, kind: str, source: str, content: str) -> None:
    """A leftover adaptive_entries row (the table outlived the layer in 3.2)."""
    from db.database import connect_sessions

    stamp = _iso(10)
    with connect_sessions() as conn:
        conn.execute(
            "INSERT INTO adaptive_entries (id, kind, scope, title, content, risk, version, status, source, "
            "created_at, updated_at) VALUES (?, ?, 'global', ?, ?, 'low', 1, 'active', ?, ?, ?)",
            (entry_id, kind, entry_id, content, source, stamp, stamp),
        )


# ---------------------------------------------------------------------------
# Turn-boundary ledger (Tier 1)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_ledger_cache():
    from core.context import compiler

    compiler._ledger_cache.clear()
    yield
    compiler._ledger_cache.clear()


def test_ledger_anchor_and_snapshot():
    sid = db.create_session(title="main")
    m1 = db.add_message(sid, "user", "first ask")
    db.add_message(sid, "assistant", "did it")
    m3 = db.add_message(sid, "user", "second ask")
    anchor = db.ledger_anchor(sid, m3)
    assert anchor is not None
    assert db.ledger_anchor(sid, m1) is None  # first turn has no prior

    wid = db.create_session(title="Research helper", session_type="worker", parent_session_id=sid)
    with db.connect_sessions() as conn:
        conn.execute("UPDATE sessions SET state_v2 = 'idle_ready', updated_at = ? WHERE id = ?", (_iso(0), wid))
        conn.execute(
            "INSERT INTO jobs (id, session_id, name, command, state, exit_code, created_at, deadline_s, finished_at, log_path) "
            "VALUES ('job1', ?, 'probe', 'echo hi', 'done', 0, ?, 0, ?, '/tmp/x.log')",
            (sid, _iso(0.01), _iso(0)),
        )
    db.add_post_mortem(
        sid, 1, "retry", "agent", 0.8, "m", 10, None, None, json.dumps({"what_failed": "skipped the read-back"})
    )
    snap = db.ledger_snapshot(sid, anchor)
    assert [w["id"] for w in snap["finished_workers"]] == [wid]
    assert [j["id"] for j in snap["finished_jobs"]] == ["job1"]
    assert snap["last_verdict"]["verdict"] == "retry"


def test_turn_ledger_renders_and_gates(monkeypatch):
    from core.context.compiler import _build_turn_ledger

    monkeypatch.setattr("config.settings.turn_ledger_enabled", True)
    sid = db.create_session(title="main")
    db.add_message(sid, "user", "first ask")
    db.add_message(sid, "assistant", "done")
    m3 = db.add_message(sid, "user", "next")
    wid = db.create_session(title="Helper", session_type="worker", parent_session_id=sid)
    with db.connect_sessions() as conn:
        conn.execute("UPDATE sessions SET state_v2 = 'idle_ready', updated_at = ? WHERE id = ?", (_iso(0), wid))
    db.add_post_mortem(
        sid, 1, "retry", "agent", 0.8, "m", 10, None, None, json.dumps({"what_failed": "left the file unwritten"})
    )
    block = _build_turn_ledger(sid, m3)
    assert block.startswith("[SINCE YOUR LAST TURN]")
    assert f"get_worker_result('{wid}')" in block
    assert "retry (cause=agent)" in block
    assert "grader's opinion" in block

    # Canary sessions never see the ledger (isolation).
    cid = db.create_session(title="canary", session_type="canary")
    db.add_message(cid, "user", "a")
    db.add_message(cid, "assistant", "b")
    cm = db.add_message(cid, "user", "c")
    assert _build_turn_ledger(cid, cm) == ""

    # Disabled flag: empty string, byte-identical tail.
    monkeypatch.setattr("config.settings.turn_ledger_enabled", False)
    from core.context import compiler

    compiler._ledger_cache.clear()
    assert _build_turn_ledger(sid, m3) == ""


def test_turn_ledger_quiet_session_renders_nothing(monkeypatch):
    monkeypatch.setattr("config.settings.turn_ledger_enabled", True)
    from core.context.compiler import _build_turn_ledger

    sid = db.create_session(title="quiet")
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hello")
    m3 = db.add_message(sid, "user", "again")
    assert _build_turn_ledger(sid, m3) == ""


# ---------------------------------------------------------------------------
# Repair-tool pairing (Tier 4.5a)
# ---------------------------------------------------------------------------


def test_charter_allowlist_pairs_repair_tools():
    from core.extensions.scheduling import _pair_repair_tools

    out = _pair_repair_tools(frozenset({"remember", "bash"}))
    assert {"update_memory", "recall"} <= out
    # No remember → nothing added.
    assert _pair_repair_tools(frozenset({"bash"})) == frozenset({"bash"})


# ---------------------------------------------------------------------------
# remember(supersede=) (Tier 4.5b)
# ---------------------------------------------------------------------------


def test_remember_supersede_routes_to_update(monkeypatch):
    from core.tools.builtin import memory_tools

    calls = {}

    def fake_update(file, epoch, content, _context=None):
        calls["target"] = (file, epoch, content)
        return "UPDATED file=%s epoch=%s VERIFY=OK" % (file, epoch)

    monkeypatch.setattr(memory_tools, "update_memory", fake_update)
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: object())  # non-None: store available
    out = memory_tools.remember("fresh fact", supersede="pernix.decisions@1700000000")
    assert out.startswith("UPDATED")
    assert calls["target"] == ("pernix.decisions", 1700000000, "fresh fact")
    # Malformed target is a parameter error, not a silent append.
    bad = memory_tools.remember("fresh fact", supersede="nonsense")
    assert bad.startswith("NOT SAVED — supersede must be 'file@epoch'")


# ---------------------------------------------------------------------------
# Federated deep_recall sections (Tier 4.4)
# ---------------------------------------------------------------------------


def test_federated_sections_skip_retired_adaptive_entries():
    """The adaptive layer is retired (3.2): its rows stay as history but are
    no longer a federated store, so a matching entry must not surface."""
    from core.tools.builtin.memory_tools import _federated_sections

    _adaptive_entry("yt-hint", "routing_hint", "refine", "Prefer youtube captions before whisper transcription.")
    out = _federated_sections("youtube captions workflow")
    assert "[adaptive/" not in out
    assert "yt-hint" not in out
    # No hits → empty string, not a header over nothing.
    assert _federated_sections("zqxwv nonexistent") == ""


# ---------------------------------------------------------------------------
# Retention distill-before-delete (Tier 2.3)
# ---------------------------------------------------------------------------


async def test_worker_prune_digests_before_delete(monkeypatch):
    from core import retention

    captured = {}

    def fake_digest(label, lines):
        captured["label"] = label
        captured["lines"] = list(lines)

    monkeypatch.setattr(retention, "_digest_pruned", fake_digest)
    wid = db.create_session(title="Old worker", session_type="worker")
    with db.connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_iso(30), wid))
    pruned = await retention.prune_sessions_of_type("worker", 7, digest_label="worker")
    assert pruned == 1
    assert captured["label"] == "worker"
    assert any(wid in line and "Old worker" in line for line in captured["lines"])
    assert db.get_session(wid) is None  # actually deleted after the digest


def test_digest_pruned_writes_memory_entry(monkeypatch):
    from core import retention

    writes = []

    class _Store:
        def add_entry(self, **kw):
            writes.append(kw)
            return "SAVED file=retention.digested epoch=1"

    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: _Store())
    retention._digest_pruned("cron", ['abc "Cron: daily" (last active 2026-08-20)'])
    assert len(writes) == 1
    assert writes[0]["file_name"] == "retention.digested"
    assert writes[0]["skip_dedup"] is True
    assert "Cron: daily" in writes[0]["content"]
    # Empty prune → no memory churn.
    retention._digest_pruned("cron", [])
    assert len(writes) == 1


# ---------------------------------------------------------------------------
# agent_state digest (Tier 4.2) + SYSTEM-MAP (Tier 4.1)
# ---------------------------------------------------------------------------


def test_agent_state_smoke():
    from core.extensions.session_tools import agent_state

    sid = db.create_session(title="s")
    db.add_post_mortem(sid, 1, "pass", "none", 0.9, "m", 5, None, None, "{}")
    out = agent_state(_context={"session_id": sid})
    assert out.startswith("AGENT STATE")
    assert "RECENT VERDICTS" in out
    assert "SYSTEM-MAP.md" in out


def test_system_map_builds_and_writes(tmp_path, monkeypatch):
    from core.context.system_map import build_system_map, write_system_map

    text = build_system_map(None)
    assert "sessions(" in text  # real PRAGMA columns
    assert "session_id" in text
    assert "Context blocks" in text
    assert "[SINCE YOUR LAST TURN]" in text
    path = write_system_map(None)
    assert path.endswith("SYSTEM-MAP.md")
