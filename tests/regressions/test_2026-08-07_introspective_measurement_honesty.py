"""Regression: three introspective metrics that reported something other
than what they claimed to measure.

Shipped defects (2026-08-07 introspective-stack review):

1. **Hevel's dead gamma term** (§3). `D(G)` subtracted a re-open-rate penalty
   counted from `goal_reopened` trace events. Nothing in the codebase ever
   emitted that event and no goal-reopen path exists, so the penalty was
   structurally always zero and `D >= 0` always held. A dead coefficient in
   a published formula reads as a working brake.
2. **Entropy's internal contradiction** (§3). `realized_band_shares` carries
   an explicit comment that the mix it actuates on is the mix actually
   *executed*, then `novelty_entropy` counted `status == "gated"` — generated
   candidates that never ran. Generation emits ~3 per cycle to evaluation's
   ~1, so the gated pool dominated and the acedia detector mostly measured
   generation variety. Its bucket key was also a model-authored string,
   defeated by synonym rotation.
3. **Decorative confidence** (§1). `validate.py` stamped a hardcoded 0.75 on
   every validated hypothesis and `report.py` rendered it as
   `_(confidence 0.75)_` in a human-facing journal, where it reads as
   calibrated. It is not — nothing measures how often a validated hypothesis
   turns out to be right.

Kept as regression pins because all three failures are of the same kind: the
number is present, plausible, and wrong about its own provenance.
"""

from __future__ import annotations

import inspect
import json

import pytest

from config import settings
from core.telos.store import TelosObject, TelosStore


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "telos_enabled", True)
    s = TelosStore.open()
    s.ensure_root()
    return s


# ---------------------------------------------------------------------------
# 1. hevel gamma
# ---------------------------------------------------------------------------


def test_hevel_formula_carries_no_dead_term():
    import core.telos.hevel as hevel

    source = inspect.getsource(hevel)
    assert "goal_reopened" not in source.split('"""', 2)[2]  # not in code, only prose
    assert not hasattr(hevel, "_GAMMA")
    assert "_GAMMA" not in inspect.getsource(hevel.score_discharge)


def test_goal_reopened_is_emitted_nowhere():
    """If a re-open path is ever built, this test fails and the gamma term
    should come back with it — that is the intended signal."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "core"
    emitters = [
        p
        for p in root.rglob("*.py")
        if 'trace_append("goal_reopened"' in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert emitters == []


# ---------------------------------------------------------------------------
# 2. entropy
# ---------------------------------------------------------------------------


def _hyp(store, band, domain, status, files=None):
    meta = {"band": band, "status": status, "mapping": {"source_domain": domain}, "question": "q_1"}
    if files is not None:
        meta["context_files"] = files
    store.write(TelosObject(id=store.mint_id("hypothesis"), kind="hypothesis", meta=meta))


def test_gated_hypotheses_no_longer_inflate_novelty(store):
    from core.telos.entropy import novelty_entropy

    # One executed bucket, plus a diverse pile of never-run candidates. The
    # detector must see the flat executed reality, not the busy generator.
    for _ in range(3):
        _hyp(store, "near", "same", "supported")
    for domain in ("a", "b", "c", "d", "e", "f"):
        _hyp(store, "far", domain, "gated")
    assert novelty_entropy(store) == 0.0


def test_bucket_key_survives_synonym_rotation(store):
    """The old key was `band:source_domain`, a string the generating model
    chooses. Rotating labels over the same sampled file scored as maximal
    novelty with zero change in actual exploration."""
    from core.telos.entropy import novelty_entropy

    for domain in ("retry storms", "retry cascades", "repeated attempts", "backoff loops"):
        _hyp(store, "near", domain, "supported", files=["pernix.tools.md"])
    assert novelty_entropy(store) == 0.0


def test_distinct_sampled_files_still_read_as_novel(store):
    from core.telos.entropy import novelty_entropy

    for f in ("a.md", "b.md", "c.md", "d.md"):
        _hyp(store, "near", "same label every time", "supported", files=[f])
    assert novelty_entropy(store) == 1.0


def test_missing_context_files_falls_back_and_is_documented(store):
    """Older hypotheses have no sampled-file record. The fallback must work
    and must be stated in the docstring rather than silently pretended away."""
    from core.telos.entropy import _bucket_key, novelty_entropy

    _hyp(store, "near", "alpha", "supported")
    _hyp(store, "near", "beta", "supported")
    assert novelty_entropy(store) == 1.0
    assert "domain:" in _bucket_key(store.list_hypotheses()[0])
    assert "synonym" in _bucket_key.__doc__


# ---------------------------------------------------------------------------
# 3. confidence rendering
# ---------------------------------------------------------------------------


def test_report_never_renders_a_bare_confidence_score():
    from core.dream.report import compose_report
    from core.dream.validate import VALIDATION_PRIOR

    rows = [
        {
            "kind": "tool_pattern",
            "statement": "browse_web degrades at night.",
            "status": "validated",
            "confidence": VALIDATION_PRIOR,
            "validation_json": json.dumps({"method": "candor_predict_degradation", "note": "p=0.41"}),
        },
        {
            "kind": "contradiction",
            "statement": "S1 conflicts with S2.",
            "status": "pending",
            "confidence": 0.62,
        },
    ]
    text = compose_report("2026-07-01T00:00:00", "2026-07-30T00:00:00", rows)
    assert "_(confidence 0.75)_" not in text
    assert "fixed heuristic prior 0.75 — not calibrated" in text
    assert "model's own estimate 0.62 — not calibrated" in text


def test_validation_prior_is_one_named_constant():
    """Three inline 0.75s read as three independent estimates."""
    import core.dream.validate as validate

    body = inspect.getsource(validate).split('"""', 2)[2]
    assert "confidence=0.75" not in body
    assert validate.VALIDATION_PRIOR == 0.75
