"""Pernix — Adaptive Layer integration (adaptation plan 4d/4e/4f).

Producers (contract + dream promotion), consumption (compiler block
placement + flag-off byte-identity, scout hints/search), the tripwire,
and snooze Activity 15.
"""

import json
from types import SimpleNamespace

import pytest

from db import models as db


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_auto_apply", True)
    import core.adaptive.render as render

    monkeypatch.setattr(render, "MIRROR_PATH", tmp_path / "ADAPTIVE.md")


def _apply_hint(title="use rg", content="prefer rg over grep", producer="refine"):
    from core.adaptive import apply_batch, queue_edits

    r = queue_edits(
        [{"action": "create", "kind": "routing_hint", "title": title, "content": content, "evidence": ["pm:1"]}],
        producer,
    )
    apply_batch(r["batch_id"])
    return r["batch_id"]


# ---------------------------------------------------------------------------
# Producer contract
# ---------------------------------------------------------------------------


def test_refine_parse_carries_adaptive_edits():
    from core.refine import _parse_refine_output

    raw = json.dumps(
        {
            "nothing_actionable": False,
            "proposals": [],
            "lessons": [],
            "adaptive_edits": [{"action": "create", "kind": "prompt_note", "title": "t", "content": "c"}],
        }
    )
    _, _, edits, _ = _parse_refine_output(raw)
    assert edits and edits[0]["kind"] == "prompt_note"


def test_snooze_reflect_parse_carries_adaptive_edits():
    from core.snooze_reflect import _parse_output

    raw = json.dumps({"proposals": [], "lessons": [], "adaptive_edits": [{"action": "create"}]})
    _, _, edits = _parse_output(raw)
    assert len(edits) == 1


def test_queue_producer_edits_stamps_session_evidence():
    from core.adaptive.contract import queue_producer_edits

    result = queue_producer_edits(
        [{"action": "create", "kind": "routing_hint", "title": "no refs", "content": "x", "evidence": []}],
        "refine",
        session_id="sess-1234",
    )
    assert result["queued"] == 1  # evidence auto-stamped, not refused
    from core.adaptive import apply_batch

    apply_batch(result["batch_id"])
    ev = db.adaptive_list_events(entry_id="no-refs")[0]
    assert "session:sess-1234" in json.loads(ev["evidence_json"])


def test_producer_prompt_suffix_gated_on_flag(monkeypatch):
    from core.adaptive.contract import ADAPTIVE_EDITS_PROMPT

    assert "adaptive_edits" in ADAPTIVE_EDITS_PROMPT
    # queue path no-ops entirely when the layer is off.
    monkeypatch.setattr("config.settings.adaptive_enabled", False)
    from core.adaptive.contract import queue_producer_edits

    out = queue_producer_edits([{"action": "create", "kind": "routing_hint", "title": "t", "content": "c"}], "refine")
    assert out["queued"] == 0 and db.adaptive_list_batches() == []


# ---------------------------------------------------------------------------
# Dream promotion
# ---------------------------------------------------------------------------


async def test_dream_promotion_mapping():
    from core.dream.promote import promote_validated

    h_tool = db.add_dream_hypothesis("tool_pattern", "http_get fails on js-heavy sites; use browse_web", "[]")
    h_lesson = db.add_dream_hypothesis("lesson_ineffective", "lesson X never changes outcomes", "[]")
    h_stale = db.add_dream_hypothesis("memory_stale", "entry about API v1 is outdated", "[]")
    for hid in (h_tool, h_lesson, h_stale):
        db.update_dream_hypothesis(hid, status="validated")

    promoted = await promote_validated(limit=10)
    assert promoted == 3

    rows = {r["id"]: r for r in db.list_dream_hypotheses(status="promoted", limit=10)}
    assert set(rows) == {h_tool, h_lesson, h_stale}
    # Dream global edits are proposal-gated (4b escalation wins over 4d
    # "auto-eligible" phrasing) — all three land as proposals, none auto.
    assert all(r["promoted_ref"].startswith("proposal:") for r in rows.values())
    assert db.adaptive_list_batches(status="pending") == []

    props = db.adaptive_list_proposals(status="pending")
    assert len(props) == 3
    # memory_stale is review-only: empty payload, rationale renders the claim.
    stale_prop = next(p for p in props if "memory review" in (p["rationale"] or ""))
    assert json.loads(stale_prop["payload_json"]) == []

    # Approving a review-only proposal acknowledges without applying.
    from core.adaptive import approve_proposal

    result = approve_proposal(stale_prop["id"])
    assert result.get("review_only") and result["batch_id"] is None
    assert db.adaptive_list_entries(status=None) == []  # nothing was written


async def test_dream_promotion_gated_on_flag(monkeypatch):
    monkeypatch.setattr("config.settings.adaptive_enabled", False)
    from core.dream.promote import promote_validated

    hid = db.add_dream_hypothesis("tool_pattern", "x", "[]")
    db.update_dream_hypothesis(hid, status="validated")
    assert await promote_validated() == 0
    assert db.list_dream_hypotheses(status="validated", limit=5)  # untouched


# ---------------------------------------------------------------------------
# Consumption: compiler block
# ---------------------------------------------------------------------------


def _system_text(sid):
    from core.context.compiler import compile_context

    return compile_context(sid).messages[0]["content"]


def test_compiler_flag_off_byte_identical(monkeypatch):
    sid = db.create_session(title="c")
    db.add_message(sid, "user", "hello")
    monkeypatch.setattr("config.settings.adaptive_enabled", False)
    baseline = _system_text(sid)

    # Entries exist but the flag is off → byte-identical output.
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    _apply_hint()
    from core.adaptive import apply_batch, queue_edits

    r = queue_edits(
        [{"action": "create", "kind": "prompt_note", "title": "note", "content": "always cite", "evidence": ["e"]}],
        "refine",
    )
    apply_batch(r["batch_id"])
    monkeypatch.setattr("config.settings.adaptive_enabled", False)
    assert _system_text(sid) == baseline

    # Enabled but EMPTY store → also byte-identical (block omitted).
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("db.models.adaptive_list_entries", lambda **kw: [])
    assert _system_text(sid) == baseline


def test_compiler_block_placement_and_content(monkeypatch):
    sid = db.create_session(title="c")
    db.add_message(sid, "user", "hello")
    from core.adaptive import apply_batch, queue_edits

    r = queue_edits(
        [
            {
                "action": "create",
                "kind": "prompt_note",
                "title": "cite",
                "content": "always cite files",
                "evidence": ["e"],
            },
            {"action": "create", "kind": "routing_hint", "title": "rg", "content": "prefer rg", "evidence": ["e"]},
        ],
        "refine",
    )
    apply_batch(r["batch_id"])
    text = _system_text(sid)
    assert "Adaptive notes (machine-curated)" in text
    assert "always cite files" in text
    assert "NEVER override" in text  # conflict rule in the header
    assert "prefer rg" not in text  # routing_hints are scout-only (I5)
    # Placement: after directives-ish content, before the skills catalog.
    if "[AVAILABLE SKILLS]" in text:
        assert text.index("Adaptive notes") < text.index("[AVAILABLE SKILLS]")


def test_session_scoped_note(monkeypatch):
    mine = db.create_session(title="mine")
    other = db.create_session(title="other")
    db.add_message(mine, "user", "hi")
    db.add_message(other, "user", "hi")
    from core.adaptive import apply_batch, queue_edits

    r = queue_edits(
        [
            {
                "action": "create",
                "kind": "prompt_note",
                "scope": f"session:{mine}",
                "title": "scoped",
                "content": "only for mine",
                "evidence": ["e"],
            }
        ],
        "refine",
    )
    apply_batch(r["batch_id"])
    assert "only for mine" in _system_text(mine)
    assert "only for mine" not in _system_text(other)


# ---------------------------------------------------------------------------
# Consumption: scout
# ---------------------------------------------------------------------------


def test_routing_hints_block_scout_only():
    from core.adaptive.render import build_routing_hints_block

    _apply_hint(title="rg wins", content="prefer rg for code search")
    block = build_routing_hints_block()
    assert "[ADAPTIVE ROUTING HINTS]" in block and "prefer rg" in block


def test_scout_search_adaptive_tool():
    from core.scout.runner import _exec_scout_tool

    _apply_hint(title="browse for js", content="js-heavy sites need browse_web not http_get")
    brief = SimpleNamespace(session_id="s")
    out = _exec_scout_tool("search_adaptive", {"query": "js-heavy browse"}, brief)
    assert "browse_web" in out and "routing_hint" in out
    out2 = _exec_scout_tool("search_adaptive", {"query": "zzz-no-match-zzz"}, brief)
    assert "No matching" in out2


# ---------------------------------------------------------------------------
# Tripwire
# ---------------------------------------------------------------------------


def _seed_canary_history(batch_id, baseline_pass=True, post_pass=False):
    # Trailing scheduled baseline (3 runs) strictly BEFORE the batch, then
    # the batch's post_batch sweep (backdating avoids same-second ties).
    from db.database import connect_sessions

    for _ in range(3):
        db.add_canary_run("t1", "scheduled", None, "[]", baseline_pass)
    with connect_sessions() as conn:
        conn.execute("UPDATE canary_runs SET created_at = '2026-01-01T00:00:00+00:00' WHERE trigger = 'scheduled'")
    db.adaptive_create_batch(batch_id, "refine", "[]", status="applied")
    db.add_canary_run("t1", "post_batch", None, "[]", post_pass, batch_id=batch_id)


def test_tripwire_flags_canary_regression(monkeypatch):
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    _seed_canary_history("ab-bad", baseline_pass=True, post_pass=False)
    actions = evaluate_tripwire()
    assert any(a["action"] == "flagged" and a["batch_id"] == "ab-bad" for a in actions)
    assert db.adaptive_get_batch("ab-bad")["status"] == "suspect"
    notes = db.get_notifications()
    assert any("tripwire" in (n.get("title") or "") for n in notes)


def test_tripwire_clears_on_clean_comparison(monkeypatch):
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    _seed_canary_history("ab-fine", baseline_pass=True, post_pass=True)
    db.adaptive_update_batch("ab-fine", status="suspect", flagged_reason="earlier flake")
    actions = evaluate_tripwire()
    assert any(a["action"] == "cleared" for a in actions)
    batch = db.adaptive_get_batch("ab-fine")
    assert batch["status"] == "applied" and batch["cleared_at"]


def test_tripwire_auto_rollback_when_enabled(monkeypatch):
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    monkeypatch.setattr("config.settings.adaptive_auto_rollback", True)
    # A real applied batch with an entry, then a regressing sweep.
    batch_id = _apply_hint(title="regressor", content="bad hint")
    for _ in range(3):
        db.add_canary_run("t1", "scheduled", None, "[]", True)
    # Backdate the scheduled baseline strictly before the batch's created_at.
    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute("UPDATE canary_runs SET created_at = '2026-01-01T00:00:00+00:00' WHERE trigger = 'scheduled'")
    db.add_canary_run("t1", "post_batch", None, "[]", False, batch_id=batch_id)

    actions = evaluate_tripwire()
    assert any(a["action"] == "auto_rolled_back" for a in actions)
    assert db.adaptive_get_entry("regressor") is None  # create reversed = hard delete
    assert db.adaptive_get_batch(batch_id)["status"] == "rolled_back"


def test_tripwire_flaky_canaries_never_trip(monkeypatch):
    from core.adaptive.tripwire import evaluate_tripwire

    flaky_def = SimpleNamespace(name="t1", flaky=True)
    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [flaky_def])
    _seed_canary_history("ab-flaky", baseline_pass=True, post_pass=False)
    actions = evaluate_tripwire()
    assert not any(a["action"] == "flagged" for a in actions)
    assert db.adaptive_get_batch("ab-flaky")["status"] == "applied"


# ---------------------------------------------------------------------------
# Activity 15
# ---------------------------------------------------------------------------


async def test_adaptive_step_drains_and_enqueues_sweeps(monkeypatch):
    from core.adaptive import queue_edits
    from core.snooze import SnoozeRunner

    monkeypatch.setattr("config.settings.canary_enabled", True)
    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: SimpleNamespace(has_active_work=lambda: False),
    )
    swept = []
    monkeypatch.setattr(
        "core.extensions.scheduling.enqueue_post_batch_sweep",
        lambda bid: swept.append(bid) or True,
    )
    r = queue_edits(
        [{"action": "create", "kind": "routing_hint", "title": "drained", "content": "x", "evidence": ["e"]}],
        "refine",
    )
    runner = SnoozeRunner.__new__(SnoozeRunner)
    runner._stats = {}
    runner._is_cancelled = lambda: False
    await SnoozeRunner._adaptive_step(runner)

    assert db.adaptive_get_entry("drained") is not None
    assert swept == [r["batch_id"]]
    assert any("auto-applied" in (n.get("title") or "") for n in db.get_notifications())
