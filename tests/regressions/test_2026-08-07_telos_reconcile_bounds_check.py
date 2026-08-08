"""Regression: autobiography "entailment" was a bounds check on the ref number.

Shipped defect (2026-08-07 introspective-stack review, §5.1):

    ok = all((m := _REF_RE.match(r)) and 1 <= int(m.group(1)) <= trace_count
             for r in c["refs"])

"Supported by the trace" meant the cited ref number was an integer between 1
and N. The trace event was never opened; nothing checked that `[T3]` bore on
the claim. The autobiography prompt instructs "Cite only refs shown" over a
pack numbered 1..<=60, so a competent model essentially never emits an
out-of-range ref — divergence went to 0, the `telos_divergence_max` alarm
never fired, and `coherence_series` (which the internals doc calls "the
identity metric") was a flat line measuring the model's ability to count.
Supported claims then commit as `observation_of_self` @ 0.9, escaping the
0.60 self_report cap.

The fix opens the cited event and applies a mechanical overlap test.

Kept as a regression pin because the corrigibility property the spec sells
hardest — "the agent's self-model cannot outvote its record" — is exactly
this check, and reverting it would restore a metric that always reads green.
"""

from __future__ import annotations

import pytest

from config import settings
from core.telos.reconcile import claim_shares_evidence, reconcile
from core.telos.store import TelosStore


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", True)
    s = TelosStore.open()
    s.ensure_root()
    return s


_EVENTS = [
    {"type": "hypothesis_resolved", "id": "h_0042", "verdict": "refuted", "question": "q_2026_0801_004"},
    {"type": "alarm", "id": "a_0003", "alarm_type": "binding", "target": "g_deploy"},
    {"type": "entropy_control", "novelty_entropy": 0.61, "far_share": 0.2, "adjusted": False},
]


def test_in_range_refs_are_no_longer_sufficient(store):
    """The exact shape of the shipped bug: every ref valid, nothing shared."""
    claims = [{"claim": "I grew more thoughtful about my work this week.", "refs": ["T1", "T2", "T3"]}]
    rec = reconcile(store, claims, _EVENTS)
    assert rec["unsupported"] == claims
    assert rec["divergence"] == 1.0


def test_a_claim_grounded_in_its_event_is_supported(store):
    claims = [{"claim": "I refuted hypothesis h_0042 against the record.", "refs": ["T1"]}]
    assert reconcile(store, claims, _EVENTS)["divergence"] == 0.0


def test_divergence_is_a_real_series_not_a_flat_line(store):
    """Half the claims grounded, half fluent — the metric must distinguish."""
    claims = [
        {"claim": "I raised a binding alarm on g_deploy.", "refs": ["T2"]},
        {"claim": "I became substantially more capable.", "refs": ["T2"]},
    ]
    rec = reconcile(store, claims, _EVENTS)
    assert rec["divergence"] == 0.5


def test_supporting_checks_are_all_three(store):
    event = _EVENTS[0]
    assert claim_shares_evidence("The hypothesis was resolved.", event)  # type token
    assert claim_shares_evidence("Nothing survived h_0042.", event)  # identifier
    assert claim_shares_evidence("I refuted a claim about q_2026_0801_004.", event)  # identifier
    assert not claim_shares_evidence("I improved.", event)


def test_out_of_range_refs_still_fail_closed(store):
    """The bounds check remains a precondition — it just is not the test."""
    rec = reconcile(store, [{"claim": "I refuted h_0042.", "refs": ["T1", "T99"]}], _EVENTS)
    assert rec["divergence"] == 1.0
