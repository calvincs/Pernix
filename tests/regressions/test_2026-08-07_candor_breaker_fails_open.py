"""Regression: a dead Candor oracle read to the scout as an all-clear.

Shipped defect (2026-08-07 introspective-stack review, §1): the bridge's
circuit breaker turns the store inert after 5 consecutive failures
(`bridge._guarded`), and the scout brief's contract is an EXCEPTION REPORT
where silence means healthy (`intel._HEADER`: "absence here means no known
problem"). A broken Candor and a perfectly healthy toolchain therefore
produced byte-identical scout input. Fail-open on a reliability oracle is
the wrong default.

The fix: once the breaker is open, both read paths emit an explicit DEGRADED
banner instead of the empty brief.

Kept as a regression pin because this is a silent-failure class — the whole
symptom of the bug is that nothing looks wrong.
"""

from __future__ import annotations

import pytest

from config import settings
from core.extensions.candor.bridge import CandorBridge
from core.extensions.candor.intel import DEGRADED_BRIEF


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "candor_enabled", True)


async def _trip(bridge, monkeypatch) -> None:
    def boom():
        raise RuntimeError("store corrupted")

    monkeypatch.setattr(bridge, "_ensure_open", boom)
    for _ in range(5):
        await bridge.record([{"pred": "tool_ok", "args": ["x"], "outcome": True}])
    assert bridge._broken is True


async def test_open_breaker_says_degraded_instead_of_nothing(tmp_path, enabled, monkeypatch):
    b = CandorBridge(store_dir=str(tmp_path / "candor"))
    try:
        assert await b.intel_brief() is None  # healthy + no exceptions to report
        await _trip(b, monkeypatch)
        brief = await b.intel_brief()
        assert brief == DEGRADED_BRIEF
        assert "DEGRADED" in brief
        # The banner must contradict the exception-report contract explicitly,
        # or the scout still reads the absence of warnings as health.
        assert "NOT evidence" in brief
    finally:
        await b.close()


async def test_cached_brief_does_not_serve_stale_confidence(tmp_path, enabled, monkeypatch):
    """A pre-breaker cached brief is the worst of both worlds: confident and
    unmaintained. The degraded banner wins on the cached path too."""
    b = CandorBridge(store_dir=str(tmp_path / "candor"))
    try:
        b._brief_cache = "[OPERATIONAL INTEL] - fetch_ok(x): 40% success over 30 obs"
        assert b.cached_brief().startswith("[OPERATIONAL INTEL] - fetch_ok")
        await _trip(b, monkeypatch)
        assert b.cached_brief() == DEGRADED_BRIEF
    finally:
        await b.close()


async def test_disabled_candor_is_not_reported_as_degraded(tmp_path, monkeypatch):
    """Off by operator choice is not a malfunction — claiming otherwise would
    put a permanent scare banner in every prompt of every user who left the
    add-on off."""
    monkeypatch.setattr(settings, "candor_enabled", False)
    b = CandorBridge(store_dir=str(tmp_path / "candor"))
    try:
        b._broken = True
        assert b.cached_brief() is None
        assert await b.intel_brief() is None
    finally:
        await b.close()
