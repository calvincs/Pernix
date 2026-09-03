"""Tests for the agent-ergonomics batch (docs/dev/agent-ergonomics-plan.md):
retro-lint sweep, turn-boundary ledger, provenance rendering, repair-tool
pairing, remember(supersede=), federated deep_recall sections, retention
distill-before-delete, agent_state digest, SYSTEM-MAP generation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from db import models as db


def _iso(days_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _adaptive_entry(entry_id: str, kind: str, source: str, content: str) -> None:
    stamp = _iso(10)
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
            "created_at": stamp,
            "updated_at": stamp,
        }
    )


# ---------------------------------------------------------------------------
# Retro-lint sweep (Tier 3.1)
# ---------------------------------------------------------------------------


@pytest.fixture
def _adaptive_on(monkeypatch):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)


def test_lint_sweep_retires_narrative_machine_entries(_adaptive_on):
    from core.adaptive.retire import _LINT_SWEEP_KEY, retire_lint_failures

    _adaptive_entry(
        "narrative-dream",
        "policy",
        "dream",
        "The protocol for handling duplicates remains ineffective, as P4 shows continued friction.",
    )
    _adaptive_entry("good-refine", "policy", "refine", "Verify the cwd with ls before any file move.")
    out = retire_lint_failures()
    assert out["retired"] == ["narrative-dream"]
    assert "narrative" in out["reasons"]["narrative-dream"]
    rows = {e["id"]: e for e in db.adaptive_list_entries(kind="policy", status=None)}
    assert rows["narrative-dream"]["status"] == "deleted"  # journaled soft-delete
    assert rows["good-refine"]["status"] == "active"
    assert db.get_snooze_state(_LINT_SWEEP_KEY)  # watermark stamped
    # The journal names the real actor + reason — not "human delete" (the
    # provenance bug the agent found live-validating the first sweep).
    ev = [e for e in db.adaptive_list_events(entry_id="narrative-dream") if e["action"] == "delete"][0]
    assert "lint_sweep delete" in (ev.get("evidence_json") or "")
    assert "human delete" not in (ev.get("evidence_json") or "")
    assert "narrative" in (ev.get("evidence_json") or "")


def test_lint_v2_catches_the_live_survivor_shapes():
    """The five narrative policies the first live sweep missed (2026-08-31),
    quoted from the box's store — each must fail the broadened lint."""
    from core.adaptive.lint import lint_edit

    survivors = [
        "Multiple memory entries prescribe 'informing the user' of the limitation, yet "
        "post-mortems record persistent failures to adhere to it.",
        "Stored lessons regarding fallback to Gemini when Qwen-VL stalls are ineffective "
        "because the system lacks evidence of actually switching models.",
        "The lesson M3 instructs respecting a max active worker limit of 2, but P1 and the "
        "newer memory M10 indicate the system successfully operated above it.",
        "The lesson M7 instructing the agent to strictly use provided JSON manifests "
        "was violated in the recorded run.",
        "The stored lessons on manual workflow recovery (M1, M9, M11) may be ineffective "
        "if the bug that they address has been resolved.",
    ]
    for content in survivors:
        assert lint_edit({"action": "create", "kind": "policy", "content": content}), content
    # Legitimate instructions still pass — including candor's model citizen.
    good = [
        "Verify the cwd with ls before any file move.",
        "Calibrated reliability for forget is 7% over 26 observations — prefer an "
        "alternative or verify its output; see why_reliability('tool_ok', 'forget').",
        "When a task defines an explicit deliverable list, emit every named deliverable " "before finishing.",
    ]
    for content in good:
        assert lint_edit({"action": "create", "kind": "policy", "content": content}) is None, content


def test_lint_sweep_exempts_user_entries_and_is_idempotent(_adaptive_on):
    from core.adaptive.lint import LINT_VERSION
    from core.adaptive.retire import _LINT_SWEEP_KEY, retire_lint_failures

    _adaptive_entry("calvins-observation", "policy", "user", "Despite everything, this keeps failing repeatedly.")
    assert retire_lint_failures()["retired"] == []  # human authority is unlinted
    # Watermarked: a second pass is a no-op even if a bad entry appears.
    _adaptive_entry("late-narrative", "policy", "dream", "This appears to be ineffective in practice.")
    assert retire_lint_failures()["retired"] == []
    # A lint version bump re-arms the sweep.
    db.set_snooze_state(_LINT_SWEEP_KEY, str(LINT_VERSION - 1))
    assert retire_lint_failures()["retired"] == ["late-narrative"]


# ---------------------------------------------------------------------------
# Provenance rendering (Tier 3.2)
# ---------------------------------------------------------------------------


def test_adaptive_block_renders_producer(_adaptive_on):
    from core.adaptive.render import build_adaptive_block, build_routing_hints_block

    _adaptive_entry("pol-x", "policy", "dream", "Always verify writes by reading the file back.")
    _adaptive_entry("note-y", "prompt_note", "refine", "Prefer captions over transcription for videos.")
    _adaptive_entry("hint-z", "routing_hint", "telos", "Use rlm_process when the budget allows it.")
    block = build_adaptive_block()
    assert "[pol-x] (dream)" in block or "(dream" in block.split("pol-x")[1][:40]
    assert "(refine)" in block
    hints = build_routing_hints_block()
    assert "(telos)" in hints


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


def test_federated_sections_surface_adaptive_hits(_adaptive_on):
    from core.tools.builtin.memory_tools import _federated_sections

    _adaptive_entry("yt-hint", "routing_hint", "refine", "Prefer youtube captions before whisper transcription.")
    out = _federated_sections("youtube captions workflow")
    assert "[adaptive/routing_hint · refine]" in out
    assert "RELATED IN OTHER STORES" in out
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
