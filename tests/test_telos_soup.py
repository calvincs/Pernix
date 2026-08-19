"""TELOS SOUP: parsing, testability gate, scheduler split, generation flow."""

from __future__ import annotations

import json

import pytest

from config import settings
from core.llm.types import ChatResponse, TokenUsage
from core.telos.soup import (
    gate,
    generate_for_next_question,
    next_question,
    observable_coverage,
    parse_soup_output,
)
from core.telos.store import TelosStore


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", True)
    s = TelosStore.open()
    s.ensure_root()
    return s


def _resp(payload) -> ChatResponse:
    return ChatResponse(
        content=json.dumps(payload),
        tool_calls=None,
        usage=TokenUsage(total_tokens=500),
        model="test",
        provider="fake",
        finish_reason="stop",
    )


def _hyp(band="far", falsifier=True, eig=0.4, cost=2000, statement=None):
    return {
        "band": band,
        "source_domain": "RF impedance matching",
        "target_domain": "queue backpressure",
        "relations": ["reflection at mismatch ≙ retry storms"],
        "statement": statement or "Retry storms at capacity discontinuities mirror RF reflection at mismatch.",
        "falsifier": {"observable": "p99 latency under load", "rule": "reject if p99 < 40ms"} if falsifier else None,
        "eig": eig,
        "cost_est_tokens": cost,
    }


# --- parsing ---------------------------------------------------------------


def test_parse_valid_and_fenced():
    raw = "```json\n" + json.dumps([_hyp()]) + "\n```"
    out = parse_soup_output(raw)
    assert len(out) == 1 and out[0]["band"] == "far"


def test_parse_rejects_bad_shapes():
    assert parse_soup_output("not json") == []
    assert parse_soup_output(json.dumps([{"band": "weird", "statement": "x" * 30}])) == []
    assert parse_soup_output(json.dumps([_hyp(statement="short")])) == []
    # malformed falsifier degrades to None, not rejection
    bad = _hyp()
    bad["falsifier"] = {"observable": "x"}  # missing rule
    out = parse_soup_output(json.dumps([bad]))
    assert len(out) == 1 and out[0]["falsifier"] is None


# --- gate ------------------------------------------------------------------


def test_gate_requires_falsifier_cost_eig():
    ok, _ = gate(_hyp())
    assert ok
    no_falsifier, reason = gate(_hyp(falsifier=False))
    assert not no_falsifier and "falsifier" in reason
    low_eig, reason = gate(_hyp(eig=0.05))
    assert not low_eig and "eig" in reason
    costly, reason = gate(_hyp(cost=10**9))
    assert not costly and "cost" in reason


def test_gate_stamps_the_probe_verdict_as_reachable(monkeypatch):
    """The coverage probe's boolean becomes a first-class field (E7) — the
    sweep and the calibration review read it instead of prefix-matching the
    reason string. Absent means the probe never ran, not reachable."""
    covered = _hyp()
    admitted, _ = gate(covered, evidence_probe=lambda _c: "p99 latency under load: 38ms observed")
    assert admitted and covered["reachable"] is True

    uncovered = _hyp()
    admitted, reason = gate(uncovered, evidence_probe=lambda _c: "tool reliability statistics only")
    assert not admitted and "observable absent" in reason
    assert uncovered["reachable"] is False

    # Pre-probe reject: the probe never ran, so no verdict is stamped.
    unprobed = _hyp(falsifier=False)
    gate(unprobed, evidence_probe=lambda _c: "anything")
    assert "reachable" not in unprobed


# --- scheduler -------------------------------------------------------------


def test_scheduler_prefers_goal_linked_with_serendipity_slice(store):
    for i in range(3):
        store.add_question(f"Goal-linked question number {i} about tool failures?", surprise=0.5 + i / 10)
    store.add_question("A serendipity question about something unrelated?", surprise=0.95, origin="serendipity")
    picks = []
    for _ in range(14):
        q = next_question(store)
        picks.append(q.get("origin"))
    # With budget 0.15 -> period 7: two serendipity picks in 14 pulls.
    assert picks.count("serendipity") == 2
    # Goal-linked picks favor highest surprise.
    q = next_question(store)
    if q.get("origin") != "serendipity":
        assert float(q.get("surprise")) == pytest.approx(0.7)


def test_scheduler_rotates_instead_of_pinning_the_top_question(store):
    """Regression: a surprise argmax is not a scheduler.

    Questions leave the pool only by being resolved or abandoned, so a pure
    max-by-surprise pull re-picks the same winner on every pass forever. On
    the live box that put one question at 63 passes and 186 hypotheses while
    five comparable questions sat at zero, never scheduled once.
    """
    hi = store.add_question("The highest-surprise question about tool failures?", surprise=0.95)
    mid = store.add_question("A middling question about memory recall drift?", surprise=0.90)
    lo = store.add_question("A lower-surprise question about scheduling jitter?", surprise=0.85)

    picks = []
    for _ in range(3):
        q = next_question(store)
        picks.append(q.id)
        # generate_for_next_question is what spends the attempt in production.
        store.update(q, attempts=int(q.get("attempts", 0)) + 1)

    # Surprise still decides who goes FIRST; attempts decide who goes NEXT.
    assert picks == [hi.id, mid.id, lo.id]
    assert len(set(picks)) == 3, "every open question must get scheduled"


def test_scheduler_empty_store(store):
    assert next_question(store) is None


# --- generation flow -------------------------------------------------------


async def test_generation_gates_and_pools(store, mock_llm_client):
    q = store.add_question("Why does p99 deviate under load class L?")
    mock_llm_client.responses = [
        _resp([_hyp(), _hyp(band="near", falsifier=False, statement="An untestable near-band hunch about latency.")])
    ]
    result = await generate_for_next_question(store, lambda: False)
    assert result == {"ran": True, "generated": 2, "gated": 1, "souped": 1}
    gated = store.list_hypotheses(status="gated")
    pooled = store.list_hypotheses(status="soup")
    assert len(gated) == 1 and len(pooled) == 1
    assert pooled[0].get("gate_reason") == "no falsifier"
    # Question advanced: spawned ids + attempt count.
    q2 = store.read("question", q.id)
    assert len(q2.get("spawned")) == 2 and q2.get("attempts") == 1
    # Spend attributed to the parent goal in the trace.
    spend = store.trace_events(days=1, types={"spend"})
    assert spend and spend[0]["goal"] == "g_root" and spend[0]["tokens"] == 500


async def test_dry_generations_abandon_question(store, mock_llm_client):
    store.add_question("A question the soup can never bite on?")
    mock_llm_client.responses = [_resp([])]
    for _ in range(settings.telos_question_max_attempts):
        await generate_for_next_question(store, lambda: False)
    qs = store.list_questions(state="abandoned")
    assert len(qs) == 1


async def test_productive_generation_still_spends_the_attempt_budget(store, mock_llm_client):
    """Regression: abandonment used to require a pass that generated nothing.

    A question whose hypotheses are all unresolvable keeps generating, so it
    never met that condition — it just ran forever. Generating is not
    progress; the budget must be spent per pass regardless of yield.
    """
    store.add_question("A question that always yields hypotheses but never resolves?")
    mock_llm_client.responses = [_resp([_hyp()]) for _ in range(settings.telos_question_max_attempts)]
    for _ in range(settings.telos_question_max_attempts):
        await generate_for_next_question(store, lambda: False)

    abandoned = store.list_questions(state="abandoned")
    assert len(abandoned) == 1
    assert abandoned[0].get("spawned"), "it was productive by volume — and still ran out"
    events = store.trace_events(days=1, types={"question_abandoned"})
    assert events and events[0]["reason"] == "attempt budget exhausted"


async def test_generation_no_questions_is_noop(store, mock_llm_client):
    result = await generate_for_next_question(store, lambda: False)
    assert result["ran"] is False
    assert mock_llm_client.call_count == 0


def test_supported_claim_edit_passes_adaptive_validation():
    """The telos→adaptive port's edit shape must clear validate_edit — it
    shipped without an 'action' key and with an unregistered source, which
    rejected every edit silently (polish review)."""
    from core.adaptive.engine import SOURCES, validate_edit

    assert "telos" in SOURCES
    edit = {
        "action": "create",
        "kind": "routing_hint",
        "scope": "global",
        "title": "telos: test claim",
        "content": "Supported hypothesis (c_0001, confidence 0.80): test statement",
        "evidence": ["c_0001", "h_0001", "q_0001"],
    }
    assert validate_edit(edit, "telos") is None


# --- observability probe ---------------------------------------------------


def test_observable_coverage_rejects_data_the_system_never_records():
    """The dominant waste mode: a falsifier naming data that does not exist.

    Every sampled inconclusive verdict on the live box was a variant of "the
    evidence consists of tool reliability statistics and does not contain the
    required <thing>" — two judge calls each, teaching nothing.
    """
    h = _hyp()
    h["falsifier"] = {
        "observable": "median DNS resolution and TCP connect latency to blog.example.com",
        "rule": "reject if within normal range",
    }
    evidence = "[candor] browse_web p=0.62 n=41\n[trace] tool_failed browse_web"
    ok, detail = observable_coverage(h, evidence)
    assert not ok and "missing" in detail


def test_observable_coverage_admits_a_falsifier_the_records_answer():
    h = _hyp()
    h["falsifier"] = {"observable": "browse_web failure count per turn", "rule": "reject if zero"}
    evidence = "[candor] browse_web p=0.62 n=41 failure rate rising\n[trace] browse_web failure per turn count 3"
    ok, _ = observable_coverage(h, evidence)
    assert ok


def test_gate_uses_the_probe_to_pool_untestable_hypotheses():
    h = _hyp()
    h["falsifier"] = {"observable": "kernel scheduler run-queue depth samples", "rule": "reject if flat"}
    admitted, reason = gate(h, evidence_probe=lambda _c: "[candor] fetch_ok p=0.5 n=20")
    assert not admitted and "observable absent" in reason
    # Same hypothesis, no probe: unchanged behaviour for callers that pass none.
    assert gate(h)[0] is True


def test_gate_admits_when_the_probe_raises():
    """A broken probe must not silently shut the gate."""

    def _boom(_c):
        raise RuntimeError("evidence store offline")

    assert gate(_hyp(), evidence_probe=_boom)[0] is True
