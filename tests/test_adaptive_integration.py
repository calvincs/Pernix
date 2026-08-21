"""Pernix — Adaptive Layer integration (adaptation plan 4d/4e/4f).

Producers (contract + dream promotion), consumption (compiler block
placement + flag-off byte-identity, scout hints/search), the tripwire,
and snooze Activity 15.
"""

import json
from datetime import datetime, timedelta, timezone
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
    _, _, edits, _, _ = _parse_refine_output(raw)
    assert edits and edits[0]["kind"] == "prompt_note"


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

    # h_stale cites no memory file, so there is nothing an approval could
    # write. It resolves terminally without minting a proposal and is not
    # counted as a promotion — only the two with real effectors are.
    promoted = await promote_validated(limit=10)
    assert promoted == 2

    rows = {r["id"]: r for r in db.list_dream_hypotheses(status="promoted", limit=10)}
    assert set(rows) == {h_tool, h_lesson, h_stale}  # all three left the queue
    # Dream global edits are proposal-gated (4b escalation wins over 4d
    # "auto-eligible" phrasing) — the actionable ones land as proposals.
    assert rows[h_tool]["promoted_ref"].startswith("proposal:")
    assert rows[h_lesson]["promoted_ref"].startswith("proposal:")
    assert rows[h_stale]["promoted_ref"] == "reported:no-effector"
    assert db.adaptive_list_batches(status="pending") == []

    # Two proposals, both with a payload someone can actually approve. The
    # empty review-only variant is gone: 62 of 126 pending proposals on the
    # live box were no-ops, and a queue that is half no-ops is a queue nobody
    # finishes reading.
    props = db.adaptive_list_proposals(status="pending")
    assert len(props) == 2
    assert all(json.loads(p["payload_json"]) for p in props)


async def test_effectorless_finding_is_reported_not_queued(monkeypatch):
    """A validated hypothesis with a citable file still becomes a proposal —
    applied on the spot since 2026-08-21, so it is found under auto_applied."""
    from core.dream.promote import promote_validated

    monkeypatch.setattr(
        "core.memory.ingest.apply_memory_correction",
        lambda files, statement, source_ref="", kind="", approved_by="human": list(files),
    )
    monkeypatch.setattr("core.dream.journal.append_sync", lambda text: None)
    ev = json.dumps([{"type": "memory", "file": "pernix.config", "epoch": 1, "hash": "abc"}])
    hid = db.add_dream_hypothesis("contradiction", "two entries disagree about the port", ev)
    db.update_dream_hypothesis(hid, status="validated")

    assert await promote_validated(limit=10) == 1
    props = db.adaptive_list_proposals(status="auto_applied")
    assert len(props) == 1
    payload = json.loads(props[0]["payload_json"])
    assert payload[0]["action"] == "memory_correction"
    assert payload[0]["files"] == ["pernix.config"]


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


def test_tripwire_refuses_to_judge_against_a_zero_baseline(monkeypatch):
    """A 0% baseline means the suite is broken, not that the bar is strict.

    `drop = base - now` against base=0 can only come out <= 0, so every batch
    would be certified clean by a suite measuring nothing — the exact state
    the box was in for five days. The signal must report unavailable.
    """
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    _seed_canary_history("ab-blind", baseline_pass=False, post_pass=False)
    db.adaptive_update_batch("ab-blind", status="suspect", flagged_reason="earlier flake")
    actions = evaluate_tripwire()
    # Neither flagged nor cleared: with no usable baseline there is no verdict
    # to give, and a false all-clear would silently dismiss a real flag.
    assert not [a for a in actions if a["batch_id"] == "ab-blind"]
    assert db.adaptive_get_batch("ab-blind")["status"] == "suspect"


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


def _backdate(table, created_at, where, params=()):
    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute(f"UPDATE {table} SET created_at = ? WHERE {where}", (created_at, *params))


def _post_mortem(created_at, verdict):
    sid = db.create_session(title="tripwire-window-test")
    pm_id = db.add_post_mortem(sid, 1, verdict, "cause", 0.9, "m", 1, None, None, "{}")
    _backdate("post_mortems", created_at, "id = ?", (pm_id,))
    return pm_id


def test_tripwire_after_window_is_the_turns_right_after_the_apply(monkeypatch):
    """The passive window must be the OLDEST turns after the apply, not the
    newest turns overall — otherwise it drifts away from the batch."""
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    monkeypatch.setattr("config.settings.adaptive_tripwire_window_turns", 2)

    for t in ("2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00"):
        _post_mortem(t, "pass")
    batch_id = _apply_hint(title="drifter", content="x")
    _backdate("adaptive_batches", "2026-01-02T00:00:00+00:00", "batch_id = ?", (batch_id,))
    _backdate("adaptive_events", "2026-01-02T00:00:00+00:00", "batch_id = ?", (batch_id,))
    # The two turns immediately after the apply regressed...
    for t in ("2026-01-03T00:00:00+00:00", "2026-01-03T01:00:00+00:00"):
        _post_mortem(t, "retry")
    # ...and the system later recovered. Slicing the newest-first feed would
    # score the recovery and miss the regression entirely.
    for t in ("2026-01-09T00:00:00+00:00", "2026-01-09T01:00:00+00:00", "2026-01-09T02:00:00+00:00"):
        _post_mortem(t, "pass")

    actions = evaluate_tripwire()
    flagged = [a for a in actions if a["action"] == "flagged" and a["batch_id"] == batch_id]
    assert flagged and "post-mortem retry rate 100% vs 0%" in flagged[0]["detail"]


def test_tripwire_anchors_on_apply_time_not_queue_time(monkeypatch):
    """A batch can sit pending for days; the baseline boundary is the APPLY."""
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    batch_id = _apply_hint(title="late apply", content="x")
    _backdate("adaptive_batches", "2026-01-01T00:00:00+00:00", "batch_id = ?", (batch_id,))
    _backdate("adaptive_events", "2026-01-03T00:00:00+00:00", "batch_id = ?", (batch_id,))

    # Baseline runs land BETWEEN queue and apply — only an apply-anchored
    # boundary admits them, and without a baseline there is no signal at all.
    for _ in range(3):
        db.add_canary_run("t1", "scheduled", None, "[]", True)
    _backdate("canary_runs", "2026-01-02T00:00:00+00:00", "trigger = 'scheduled'")
    db.add_canary_run("t1", "post_batch", None, "[]", False, batch_id=batch_id)

    actions = evaluate_tripwire()
    assert any(a["action"] == "flagged" and a["batch_id"] == batch_id for a in actions)


async def test_tripwire_dismiss_is_durable(monkeypatch):
    """cleared_at is terminal: the sweep must not re-flag a dismissed batch."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers import adaptive as adaptive_router
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    _seed_canary_history("ab-dismissed", baseline_pass=True, post_pass=False)
    assert any(a["action"] == "flagged" for a in evaluate_tripwire())

    app = FastAPI()
    app.include_router(adaptive_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/adaptive/batches/ab-dismissed/dismiss")
    assert resp.status_code == 200 and resp.json()["cleared"]

    # Same evidence, next cycle — the dismiss holds instead of re-flagging.
    assert evaluate_tripwire() == []
    assert db.adaptive_get_batch("ab-dismissed")["status"] == "applied"


async def test_delete_entry_endpoint_frees_the_cap(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers import adaptive as adaptive_router
    from core.adaptive.render import build_routing_hints_block

    monkeypatch.setattr("config.settings.adaptive_max_entries_per_kind", 1)
    _apply_hint(title="wedged", content="stale hint")
    assert "stale hint" in build_routing_hints_block()

    app = FastAPI()
    app.include_router(adaptive_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/api/adaptive/entries/wedged")
        assert resp.status_code == 200 and resp.json()["status"] == "deleted"
        # Second delete is a 404 — the soft delete is idempotent-by-refusal.
        assert (await client.delete("/api/adaptive/entries/wedged")).status_code == 404

    assert build_routing_hints_block() == ""  # gone from the scout prompt
    assert db.adaptive_entry_count("routing_hint") == 0  # and from the cap
    ev = db.adaptive_list_events(entry_id="wedged")[0]
    assert ev["action"] == "delete" and ev["actor"] == "human" and ev["before_json"]

    # Cap freed: a fresh hint lands where the wedged one blocked it.
    _apply_hint(title="successor", content="fresh hint")
    assert db.adaptive_get_entry("successor")["status"] == "active"


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


async def test_adaptive_step_skips_sweep_and_notify_when_nothing_applied(monkeypatch):
    """A fully-rejected batch changed nothing: no sweep to join, no news."""
    from core.adaptive import queue_edits
    from core.snooze import SnoozeRunner

    monkeypatch.setattr("config.settings.canary_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_max_entries_per_kind", 0)
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
        [{"action": "create", "kind": "routing_hint", "title": "doomed", "content": "x", "evidence": ["e"]}],
        "refine",
    )
    runner = SnoozeRunner.__new__(SnoozeRunner)
    runner._stats = {}
    runner._is_cancelled = lambda: False
    await SnoozeRunner._adaptive_step(runner)

    assert db.adaptive_get_batch(r["batch_id"])["status"] == "rejected"
    assert swept == []
    assert runner._stats.get("adaptive_batches_applied") is None
    assert not any("auto-applied" in (n.get("title") or "") for n in db.get_notifications())


# ---------------------------------------------------------------------------
# Candor producer: mint AND retire
# ---------------------------------------------------------------------------


async def test_candor_retires_recovered_hints(monkeypatch):
    """Candor must release cap slots it took, or it wedges routing_hint."""
    from core.adaptive import apply_batch
    from core.snooze import SnoozeRunner

    degraded = [{"tool": "http_get", "p": 0.41, "n": 30}]

    class _Bridge:
        async def run_maintenance(self, cancelled):
            return {}

        async def degraded_tools(self):
            return list(degraded)

    monkeypatch.setattr("core.extensions.candor.bridge.get_candor_bridge", lambda: _Bridge())
    runner = SnoozeRunner.__new__(SnoozeRunner)
    runner._is_cancelled = lambda: False

    async def _pass():
        await SnoozeRunner._candor_maintenance(runner)
        pending = db.adaptive_list_batches(status="pending")
        return apply_batch(pending[0]["batch_id"], actor="user") if pending else None

    assert (await _pass())["applied"] == ["tool-http_get-degraded"]
    assert db.adaptive_entry_count("routing_hint") == 1

    # Still degraded → the live hint dedupes, nothing new is queued.
    await SnoozeRunner._candor_maintenance(runner)
    assert db.adaptive_list_batches(status="pending") == []

    # Reliability recovers → the tool drops out of the degraded set and its
    # hint is retired, freeing the slot. Same-producer delete stays low-risk,
    # so it rides an auto batch rather than a human-review proposal.
    degraded.clear()
    assert (await _pass())["applied"] == ["tool-http_get-degraded"]
    assert db.adaptive_get_entry("tool-http_get-degraded")["status"] == "deleted"
    assert db.adaptive_entry_count("routing_hint") == 0
    assert db.adaptive_list_proposals(status="pending") == []

    # A retired hint must be able to come back if the tool degrades again.
    degraded.append({"tool": "http_get", "p": 0.3, "n": 40})
    assert (await _pass())["applied"] == ["tool-http_get-degraded"]
    assert db.adaptive_get_entry("tool-http_get-degraded")["status"] == "active"


async def test_candor_does_not_mint_hints_for_names_that_are_not_tools(monkeypatch):
    """Candor's ledger is keyed by operation name, so cron jobs land in it.

    A hint reading "tool ai-tech-daily-brief degraded" advises scout about
    something it cannot call, while holding a slot against the per-kind cap —
    two of eleven live hints on the box were exactly this.
    """
    from core.snooze import SnoozeRunner
    from core.tools.registry import get_registry

    registry = get_registry()
    registry.register(
        name="http_get",
        func=lambda **kw: "",
        description="fetch a url",
        parameters={"type": "object", "properties": {}},
        category="web",
    )

    class _Bridge:
        async def run_maintenance(self, cancelled):
            return {}

        async def degraded_tools(self):
            return [
                {"tool": "http_get", "p": 0.41, "n": 30},
                {"tool": "ai-tech-daily-brief", "p": 0.20, "n": 12},  # a cron job
            ]

    monkeypatch.setattr("core.extensions.candor.bridge.get_candor_bridge", lambda: _Bridge())
    runner = SnoozeRunner.__new__(SnoozeRunner)
    runner._is_cancelled = lambda: False
    await SnoozeRunner._candor_maintenance(runner)

    pending = db.adaptive_list_batches(status="pending")
    queued = [e["entry_id"] for e in json.loads(pending[0]["payload_json"])]
    assert queued == ["tool-http_get-degraded"]


async def test_memory_correction_effector_writes_on_promotion(monkeypatch, tmp_path):
    """A validated dream contradiction with cited memory files writes its
    corrective entry when it is PROMOTED — no review queue in between. The
    proposal row is still minted for the audit trail, resolved `auto_applied`
    (audit P5 gave corrections an effector; 2026-08-21 removed the wait)."""
    import json as _json

    from core.dream.promote import promote_validated

    written_calls = []

    def _fake_correction(files, statement, source_ref="", kind="", approved_by="human"):
        written_calls.append((tuple(files), kind, approved_by))
        return list(files)

    import core.memory.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "apply_memory_correction", _fake_correction)
    monkeypatch.setattr("core.dream.journal.append_sync", lambda text: None)

    evidence = _json.dumps([{"type": "memory", "file": "test.corrections", "epoch": 1}])
    hid = db.add_dream_hypothesis("contradiction", "Entry A contradicts entry B about worker limits", evidence)
    db.update_dream_hypothesis(hid, status="validated")

    assert await promote_validated(limit=5) == 1
    assert db.adaptive_list_proposals(status="pending") == []
    prop = db.adaptive_list_proposals(status="auto_applied")[0]
    payload = _json.loads(prop["payload_json"])
    assert payload and payload[0]["action"] == "memory_correction"
    assert payload[0]["files"] == ["test.corrections"]
    assert written_calls == [(("test.corrections",), "contradiction", "dream")]
    rows = {r["id"]: r for r in db.list_dream_hypotheses(status="promoted", limit=10)}
    assert rows[hid]["promoted_ref"] == f"proposal:{prop['id']}"


# ---------------------------------------------------------------------------
# Proposal queue bounds
# ---------------------------------------------------------------------------


def test_proposal_queue_dedupes_identical_pending_payloads():
    """Re-deriving a finding from the same evidence is normal producer
    behaviour, not new information — it must not stack copies."""
    payload = json.dumps([{"action": "create", "kind": "policy", "title": "t"}])
    first = db.adaptive_add_proposal("dream", payload, "[]", "why")
    again = db.adaptive_add_proposal("dream", payload, "[]", "why")
    assert again == first
    assert db.adaptive_count_pending_proposals() == 1
    # A different producer with the same payload is a genuinely separate claim.
    assert db.adaptive_add_proposal("telos", payload, "[]", "why") != first
    assert db.adaptive_count_pending_proposals() == 2


def test_proposal_queue_refuses_past_the_cap():
    for i in range(3):
        db.adaptive_add_proposal("dream", json.dumps([{"n": i}]), "[]", f"r{i}", max_pending=3)
    assert db.adaptive_count_pending_proposals() == 3
    # Full: the next one is refused rather than silently deepening a queue
    # nobody is going to finish reading.
    assert db.adaptive_add_proposal("dream", json.dumps([{"n": 99}]), "[]", "r99", max_pending=3) is None
    assert db.adaptive_count_pending_proposals() == 3
    # Resolving one frees the slot.
    db.adaptive_resolve_proposal(db.adaptive_list_proposals(status="pending")[0]["id"], "rejected")
    assert db.adaptive_add_proposal("dream", json.dumps([{"n": 99}]), "[]", "r99", max_pending=3) is not None


def test_pending_proposals_lapse_after_the_ttl():
    """A proposal is a snapshot of evidence; approving a stale one blind is
    worse than letting the producer re-raise it from current evidence."""
    from db.database import connect_sessions

    fresh = db.adaptive_add_proposal("dream", json.dumps([{"a": 1}]), "[]", "fresh")
    stale = db.adaptive_add_proposal("dream", json.dumps([{"a": 2}]), "[]", "stale")
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE adaptive_proposals SET created_at = ? WHERE id = ?", (old, stale))

    assert db.adaptive_expire_stale_proposals(30) == 1
    assert db.adaptive_get_proposal(stale)["status"] == "expired"
    assert db.adaptive_get_proposal(fresh)["status"] == "pending"
    # Disabled by zero.
    assert db.adaptive_expire_stale_proposals(0) == 0


def test_one_producer_cannot_own_the_whole_review_queue():
    """Every one of the 126 backed-up proposals on the live box came from
    dream; once it filled the queue, Candor/Refine/Telos were refused too."""
    for i in range(3):
        assert db.adaptive_add_proposal(
            "dream", json.dumps([{"n": i}]), "[]", f"d{i}", max_pending=40, max_pending_per_producer=3
        )
    # dream is at its share — refused, even though the queue has room.
    assert (
        db.adaptive_add_proposal(
            "dream", json.dumps([{"n": 9}]), "[]", "d9", max_pending=40, max_pending_per_producer=3
        )
        is None
    )
    # A quieter producer still gets through.
    assert db.adaptive_add_proposal(
        "candor", json.dumps([{"n": 9}]), "[]", "c", max_pending=40, max_pending_per_producer=3
    )


async def test_paraphrased_tool_findings_promote_once():
    """Eleven of the sixty-four live proposals were one fetch_ok finding
    restated. Lexical dedup cannot see a paraphrase; the Candor evidence key
    is the claim's semantic identity, and promotion must use it."""
    from core.dream.promote import promote_validated

    ev = json.dumps([{"type": "candor", "pred": "fetch_ok", "args": ["*"], "quote": "p=0.49"}])
    first = db.add_dream_hypothesis("tool_pattern", "fetch_ok succeeds only about half the time overall", ev)
    second = db.add_dream_hypothesis("tool_pattern", "Fetching is unreliable, working roughly 50% of the time", ev)
    for hid in (first, second):
        db.update_dream_hypothesis(hid, status="validated")

    assert await promote_validated(limit=10) == 1  # the paraphrase adds nothing
    rows = {r["id"]: r for r in db.list_dream_hypotheses(status="promoted", limit=10)}
    assert rows[first]["promoted_ref"].startswith("proposal:")
    assert rows[second]["promoted_ref"] == "reported:duplicate-evidence"  # terminal
    assert len(db.adaptive_list_proposals(status="pending")) == 1


async def test_same_file_correction_is_not_proposed_twice(monkeypatch):
    """One conflicted memory file produced four separate proposals live. With
    corrections applying on promotion the dedup looks at what was applied
    this week, not just at what is pending (which is now momentary)."""
    from core.dream.promote import promote_validated

    monkeypatch.setattr(
        "core.memory.ingest.apply_memory_correction",
        lambda files, statement, source_ref="", kind="", approved_by="human": list(files),
    )
    monkeypatch.setattr("core.dream.journal.append_sync", lambda text: None)
    ev = json.dumps([{"type": "memory", "file": "pernix.versions", "epoch": 1, "hash": "a"}])
    a = db.add_dream_hypothesis("contradiction", "two entries disagree about the version", ev)
    b = db.add_dream_hypothesis("contradiction", "the recorded version numbers are inconsistent", ev)
    for hid in (a, b):
        db.update_dream_hypothesis(hid, status="validated")

    assert await promote_validated(limit=10) == 1
    assert len(db.adaptive_list_proposals(status="auto_applied")) == 1
    assert db.adaptive_list_proposals(status="pending") == []
    rows = {r["id"]: r for r in db.list_dream_hypotheses(status="promoted", limit=10)}
    assert rows[b]["promoted_ref"] == "reported:duplicate-evidence"


# ---------------------------------------------------------------------------
# Proposal auto-approval — the veto window (2026-08-15)
# ---------------------------------------------------------------------------


def _backdate_proposal(pid: int, hours: float) -> None:
    from db.database import connect_sessions

    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE adaptive_proposals SET created_at = ? WHERE id = ?", (old, pid))


_POLICY_EDIT = [
    {
        "action": "create",
        "kind": "policy",
        "title": "veto window test",
        "content": "auto-approved policies still apply through the engine",
        "evidence": ["pm:1"],
        "entry_id": "veto-window-test",
        "scope": "global",
        "risk": "high",
    }
]


def test_ripe_proposal_auto_approves_and_applies():
    """Past the veto window the system applies the proposal itself — same
    engine as a human approval, distinct terminal status for the audit trail."""
    from core.adaptive import auto_approve_stale_proposals

    pid = db.adaptive_add_proposal("dream", json.dumps(_POLICY_EDIT), "[]", "why")
    _backdate_proposal(pid, hours=25)

    out = auto_approve_stale_proposals()
    assert out["approved"] == [pid]
    assert db.adaptive_get_proposal(pid)["status"] == "auto_approved"
    entry = db.adaptive_get_entry("veto-window-test")
    assert entry is not None and entry["kind"] == "policy"


def test_fresh_proposal_stays_inside_the_veto_window():
    from core.adaptive import auto_approve_stale_proposals

    pid = db.adaptive_add_proposal("dream", json.dumps(_POLICY_EDIT), "[]", "why")
    _backdate_proposal(pid, hours=2)  # window is 24h

    out = auto_approve_stale_proposals()
    assert out["approved"] == []
    assert db.adaptive_get_proposal(pid)["status"] == "pending"


def test_canary_proposals_keep_their_human_gate():
    """Materializing a canary keeps invariant I6 — it never auto-approves,
    no matter how stale; canary_auto_admit is its graduated-autonomy path."""
    from core.adaptive import auto_approve_stale_proposals

    pid = db.adaptive_add_proposal("canary", json.dumps({"canary": {"name": "x"}}), "[]", "why")
    _backdate_proposal(pid, hours=200)

    out = auto_approve_stale_proposals()
    assert out["approved"] == []
    assert out["skipped_canary"] == 1
    assert db.adaptive_get_proposal(pid)["status"] == "pending"


def test_auto_approvals_respect_the_daily_cap(monkeypatch):
    from core.adaptive import auto_approve_stale_proposals

    monkeypatch.setattr("config.settings.adaptive_max_auto_approvals_per_day", 1)
    first = db.adaptive_add_proposal("dream", json.dumps([]), "[]", "older")
    second = db.adaptive_add_proposal("dream", json.dumps([{"a": 1}]), "[]", "newer")
    _backdate_proposal(first, hours=48)
    _backdate_proposal(second, hours=30)

    out = auto_approve_stale_proposals()
    assert out["approved"] == [first]  # oldest first
    assert db.adaptive_get_proposal(second)["status"] == "pending"
    # The cap counts terminal 'auto_approved' rows, so a second pass in the
    # same day has no budget left.
    assert auto_approve_stale_proposals()["approved"] == []


def test_zero_window_restores_the_human_gate(monkeypatch):
    from core.adaptive import auto_approve_stale_proposals

    monkeypatch.setattr("config.settings.adaptive_auto_approve_after_hours", 0)
    pid = db.adaptive_add_proposal("dream", json.dumps(_POLICY_EDIT), "[]", "why")
    _backdate_proposal(pid, hours=500)

    assert auto_approve_stale_proposals()["approved"] == []
    assert db.adaptive_get_proposal(pid)["status"] == "pending"
