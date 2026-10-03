"""Tests for the Candor operational-memory add-on (core/extensions/candor)."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from config import settings
from core.extensions.candor.bridge import CandorBridge
from core.extensions.candor.emit import build_turn_observations, classify_error
from sessions.state import TurnState

NOW_MS = int(time.time() * 1000)


def _summary(calls=3, failures=1, errors=None):
    return {
        "search_web": {
            "calls": calls,
            "failures": failures,
            "errors": errors if errors is not None else ["timed out after 30s"],
            "total_latency_ms": 100,
        }
    }


def _build(**overrides):
    kwargs = dict(
        tool_summary=_summary(),
        already_emitted={},
        termination_reason="complete",
        reflect_verdict="pass",
        failure_cause=None,
        model="test-model",
        session_kind="chat",
        is_retry=False,
        ts_ms=NOW_MS,
        max_obs=200,
    )
    kwargs.update(overrides)
    return build_turn_observations(**kwargs)


# ---------------------------------------------------------------------------
# emit.py — pure mapping
# ---------------------------------------------------------------------------


class TestEmit:
    def test_tool_outcomes_dual_granularity(self):
        obs, emitted = _build()
        per_tool = [o for o in obs if o["pred"] == "tool_ok" and o["args"] == ["search_web"]]
        aggregate = [o for o in obs if o["pred"] == "tool_ok" and o["args"] == ["*"]]
        assert len(per_tool) == 3 and len(aggregate) == 3
        assert sum(1 for o in per_tool if o["outcome"]) == 2
        assert all(o["ctx"]["target"] == "search_web" for o in aggregate)
        assert emitted["search_web"] == {"calls": 3, "failures": 1, "errors": 1}

    def test_turn_and_verdict_observations(self):
        obs, _ = _build(reflect_verdict="retry", failure_cause="tool")
        turn = [o for o in obs if o["pred"] == "turn_ok"]
        verdict = [o for o in obs if o["pred"] == "reflect_verdict"]
        assert len(turn) == 1 and turn[0]["outcome"] is True
        assert len(verdict) == 1
        assert verdict[0]["value"] == "retry"
        assert verdict[0]["ctx"]["failure_cause"] == "tool"
        assert verdict[0]["actor"] == "verifier:reflect"

    def test_failure_modes_bucketed(self):
        obs, _ = _build(tool_summary=_summary(errors=["HTTP 429 too many requests", "no such file: x"]))
        modes = [o for o in obs if o["pred"] == "tool_failure_mode"]
        assert sorted(o["value"] for o in modes) == ["not_found", "rate_limit"]

    def test_delta_tracking_prevents_double_observe(self):
        # Attempt 1 emitted 3 calls / 1 failure / 1 error; attempt 2 adds 2 calls, 1 failure, 1 new error.
        _, emitted = _build()
        obs2, emitted2 = _build(
            tool_summary=_summary(calls=5, failures=2, errors=["timed out after 30s", "401 unauthorized"]),
            already_emitted=emitted,
        )
        tool_obs = [o for o in obs2 if o["pred"] == "tool_ok" and o["args"] == ["search_web"]]
        assert len(tool_obs) == 2  # only the delta
        assert sum(1 for o in tool_obs if not o["outcome"]) == 1
        modes = [o for o in obs2 if o["pred"] == "tool_failure_mode"]
        assert [o["value"] for o in modes] == ["auth"]  # only the new error
        assert emitted2["search_web"] == {"calls": 5, "failures": 2, "errors": 2}

    def test_unchanged_summary_emits_no_tool_observations(self):
        _, emitted = _build()
        obs2, _ = _build(already_emitted=emitted, reflect_verdict=None)
        assert [o["pred"] for o in obs2] == ["turn_ok"]

    def test_per_tool_clamp_and_global_cap(self):
        obs, _ = _build(tool_summary=_summary(calls=500, failures=400, errors=[]))
        per_tool = [o for o in obs if o["args"] == ["search_web"]]
        assert len(per_tool) == 50  # 25 successes + 25 failures, clamped
        obs_capped, _ = _build(tool_summary=_summary(calls=500, failures=400, errors=[]), max_obs=10)
        assert len(obs_capped) == 10
        assert obs_capped[0]["pred"] == "turn_ok"  # small facts survive the cap

    def test_classify_error(self):
        assert classify_error("Read timed out") == "timeout"
        assert classify_error("403 Forbidden") == "auth"
        assert classify_error("connection reset by peer") == "network"
        assert classify_error("something exotic") == "other"


# ---------------------------------------------------------------------------
# emit.py — user-fact attestations
# ---------------------------------------------------------------------------


class TestMemoryEmit:
    def test_attest_dual_granularity_and_actor(self):
        from core.extensions.candor.emit import build_memory_observations

        obs = build_memory_observations(
            file_name="user.professional_background", event="attest", source="user", ts_ms=NOW_MS
        )
        assert len(obs) == 2
        per, agg = obs
        assert per["pred"] == "user_fact" and per["args"] == ["professional_background"]
        assert per["outcome"] is True and per["actor"] == "human:user"
        assert agg["args"] == ["*"] and agg["ctx"]["target"] == "professional_background"
        assert per["ctx"]["origin"] == "user"

    def test_agent_derived_attest_uses_agent_actor(self):
        from core.extensions.candor.emit import build_memory_observations

        obs = build_memory_observations(file_name="user.profile", event="attest", source="distill", ts_ms=NOW_MS)
        assert all(o["actor"] == "agent:pernix" for o in obs)
        assert obs[0]["ctx"]["origin"] == "distill"

    def test_revise_emits_negative_then_positive(self):
        from core.extensions.candor.emit import build_memory_observations

        obs = build_memory_observations(file_name="user.profile", event="revise", source="", ts_ms=NOW_MS)
        per_slug = [o for o in obs if o["args"] == ["profile"]]
        assert [o["outcome"] for o in per_slug] == [False, True]
        assert all(o["actor"] == "agent:pernix" for o in obs)

    def test_forget_is_negative(self):
        from core.extensions.candor.emit import build_memory_observations

        obs = build_memory_observations(file_name="user.profile", event="forget", source="", ts_ms=NOW_MS)
        assert [o["outcome"] for o in obs] == [False, False]

    def test_non_user_files_and_unknown_events_ignored(self):
        from core.extensions.candor.emit import build_memory_observations

        assert build_memory_observations(file_name="projects.pernix", event="attest", source="user", ts_ms=NOW_MS) == []
        assert build_memory_observations(file_name="user.profile", event="archive", source="", ts_ms=NOW_MS) == []


# ---------------------------------------------------------------------------
# bridge.py — lifecycle, buffering, maintenance
# ---------------------------------------------------------------------------


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "candor_enabled", True)


@pytest.fixture
async def bridge(tmp_path, enabled):
    b = CandorBridge(store_dir=str(tmp_path / "candor"))
    yield b
    await b.close()


class TestBridge:
    async def test_disabled_is_inert(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "candor_enabled", False)
        b = CandorBridge(store_dir=str(tmp_path / "candor"))
        assert await b.record([{"pred": "tool_ok", "args": ["x"], "outcome": True}]) is None
        assert await b.run_maintenance(lambda: False) == {}
        assert await b.intel_brief() is None
        assert not (tmp_path / "candor").exists()  # store never opened
        await b.close()

    async def test_buffer_then_gate_then_direct(self, bridge):
        obs, _ = _build()
        r1 = await bridge.record(obs)
        assert r1["observed"] == 0 and r1["buffered"] == len(obs)

        stats = await bridge.run_maintenance(lambda: False)
        assert stats["seeded"] > 0
        assert stats["drained"] == len(obs)

        r2 = await bridge.record(obs)
        assert r2["buffered"] == 0 and r2["observed"] == len(obs)

    async def test_drain_cursor_survives_abort(self, bridge):
        obs, _ = _build()
        await bridge.record(obs)
        # Abort after the first two phases (seed + gate) — nothing drained.
        calls = {"n": 0}

        def abort_after_two():
            calls["n"] += 1
            return calls["n"] > 2

        stats = await bridge.run_maintenance(abort_after_two)
        assert stats.get("drained", 0) == 0
        # A later full run drains everything exactly once.
        stats2 = await bridge.run_maintenance(lambda: False)
        assert stats2["drained"] == len(obs)
        result = await asyncio.to_thread(bridge.predict_sync, "tool_ok", ["search_web"])
        assert result["observations"] == 3

    async def test_pending_buffer_truncated_after_drain(self, bridge):
        obs, _ = _build()
        await bridge.record(obs)
        await bridge.run_maintenance(lambda: False)
        assert bridge._pending_path.read_text() == ""
        assert bridge._read_cursor() == 0

    async def test_intel_brief_flags_degraded_tool(self, bridge):
        summary = {"search_web": {"calls": 10, "failures": 9, "errors": ["timeout"], "total_latency_ms": 1}}
        obs, _ = _build(tool_summary=summary)
        await bridge.record(obs)
        await bridge.run_maintenance(lambda: False)
        brief = await bridge.intel_brief()
        assert brief and "OPERATIONAL INTEL" in brief
        assert "tool_ok(search_web)" in brief
        assert bridge.cached_brief() == brief

    async def test_predict_sync_unknown_fact_returns_none(self, bridge):
        await bridge.record([_build()[0][0]])
        assert await asyncio.to_thread(bridge.predict_sync, "tool_ok", ["never_observed"]) is None

    async def test_predict_sync_refuses_event_loop(self, bridge):
        with pytest.raises(RuntimeError, match="event loop"):
            bridge.predict_sync("tool_ok", ["*"])

    async def test_circuit_breaker_trips(self, tmp_path, enabled, monkeypatch):
        b = CandorBridge(store_dir=str(tmp_path / "candor"))

        def boom():
            raise RuntimeError("store corrupted")

        monkeypatch.setattr(b, "_ensure_open", boom)
        for _ in range(5):
            assert await b.record([{"pred": "tool_ok", "args": ["x"], "outcome": True}]) is None
        assert b._broken is True
        await b.close()

    async def test_close_releases_writer_lock(self, tmp_path, enabled):
        store = str(tmp_path / "candor")
        b1 = CandorBridge(store_dir=store)
        await b1.record(_build()[0])
        await b1.close()
        b2 = CandorBridge(store_dir=store)
        stats = await b2.run_maintenance(lambda: False)  # opens the same store
        assert stats.get("drained", 0) > 0
        await b2.close()


# ---------------------------------------------------------------------------
# extension registration + tools
# ---------------------------------------------------------------------------


class TestExtension:
    def test_register_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "candor_enabled", False)
        from core.extensions import candor as ext
        from core.tools.registry import ToolRegistry

        reg = ToolRegistry()
        ext.register(reg)
        assert not reg.all_tools()

    def test_register_adds_tools_when_enabled(self, enabled):
        from core.extensions import candor as ext
        from core.tools.registry import ToolRegistry

        reg = ToolRegistry()
        ext.register(reg)
        names = {t.name for t in reg.all_tools()}
        assert names == {"predict_reliability", "why_reliability", "reliability_questions"}
        assert all(t.safety_level == "safe" for t in reg.all_tools())

    def test_tools_report_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "candor_enabled", False)
        from core.extensions.candor import predict_reliability, reliability_questions, why_reliability

        assert "disabled" in predict_reliability("tool_ok", "*")
        assert "disabled" in why_reliability("tool_ok", "*")
        assert "disabled" in reliability_questions()

    async def test_predict_tool_end_to_end(self, tmp_path, enabled, monkeypatch):
        b = CandorBridge(store_dir=str(tmp_path / "candor"))
        monkeypatch.setattr("core.extensions.candor.bridge.get_candor_bridge", lambda: b)
        obs, _ = _build()
        await b.record(obs)
        await b.run_maintenance(lambda: False)

        from core.extensions.candor import predict_reliability

        out = await asyncio.to_thread(predict_reliability, "tool_ok", "search_web")
        payload = json.loads(out)
        assert payload["observations"] == 3
        assert 0.0 <= payload["p"] <= 1.0
        await b.close()


# ---------------------------------------------------------------------------
# emit.py — experience observations (reflect's intangibles read)
# ---------------------------------------------------------------------------


class TestExperienceObservations:
    @staticmethod
    def _build_exp(**overrides):
        from core.extensions.candor.emit import build_experience_observations

        kwargs = dict(
            experience={
                "user_sentiment": "frustrated",
                "clarification_loop": True,
                "first_response_sufficient": False,
                "friction": ["misread_intent", "tool_noise"],
                "user_observations": ["Prefers terse answers"],
                "note": "User re-asked twice.",
            },
            model="test-model",
            session_kind="chat",
            is_retry=False,
            ts_ms=NOW_MS,
        )
        kwargs.update(overrides)
        return build_experience_observations(**kwargs)

    def test_full_experience_maps_to_observations(self):
        obs = self._build_exp()
        by_pred = {}
        for o in obs:
            by_pred.setdefault(o["pred"], []).append(o)
        assert by_pred["user_sentiment"][0]["value"] == "frustrated"
        assert by_pred["user_sentiment"][0]["stmt_type"] == "categorical"
        # Polarity: clarification_loop=True (bad) inverts to a False outcome
        # on no_clarification_needed, so low p = degraded in the brief.
        assert by_pred["no_clarification_needed"][0]["outcome"] is False
        assert by_pred["first_response_sufficient"][0]["outcome"] is False
        assert [o["value"] for o in by_pred["friction_mode"]] == ["misread_intent", "tool_noise"]
        assert all(o["actor"] == "verifier:reflect" for o in obs)
        assert all(o["args"] == ["*"] for o in obs)

    def test_prose_never_enters_ledger(self):
        obs = self._build_exp()
        blob = json.dumps(obs)
        assert "Prefers terse answers" not in blob
        assert "re-asked twice" not in blob

    def test_unknown_sentiment_and_missing_booleans_skipped(self):
        obs = self._build_exp(experience={"user_sentiment": "unknown", "friction": []})
        assert obs == []

    def test_empty_experience_is_empty(self):
        obs = self._build_exp(experience={})
        assert obs == []

    def test_smooth_turn_emits_positive_signals(self):
        obs = self._build_exp(
            experience={
                "user_sentiment": "satisfied",
                "clarification_loop": False,
                "first_response_sufficient": True,
                "friction": [],
            }
        )
        outcomes = {o["pred"]: o.get("outcome") for o in obs if o["stmt_type"] == "frequency"}
        assert outcomes == {"no_clarification_needed": True, "first_response_sufficient": True}

    def test_retry_flag_in_ctx(self):
        obs = self._build_exp(is_retry=True)
        assert all(o["ctx"]["retry"] == "yes" for o in obs)
