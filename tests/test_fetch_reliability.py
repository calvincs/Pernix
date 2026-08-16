"""Fetch reliability loop — per-domain fetch_ok emission and the deterministic
http_get reroute (session 2c2ea8b09218's "close the half-built loops" finding).

The Candor store had 817 per-domain fetch_ok observations that nothing emitted
anymore and nothing consumed: the scout brief could *mention* a bad domain, but
http_get still walked into the bot wall. These tests pin the closed loop: the
web tools record every real attempt, and http_get refuses a domain whose
calibrated rate is below threshold before spending a timeout on it.
"""

from __future__ import annotations

import pytest

import core.extensions.web as web
from config import settings


class _FakeBridge:
    def __init__(self, prediction=None):
        self.prediction = prediction
        self.recorded: list[list[dict]] = []
        self.predict_calls: list[tuple] = []

    def predict_sync(self, pred, args, timeout=20.0):
        self.predict_calls.append((pred, args))
        return self.prediction

    def record_nowait(self, observations):
        self.recorded.append(observations)


@pytest.fixture
def bridge(monkeypatch):
    fake = _FakeBridge()
    monkeypatch.setattr("core.extensions.candor.bridge.get_candor_bridge", lambda: fake)
    monkeypatch.setattr(settings, "candor_enabled", True, raising=False)
    monkeypatch.setattr(settings, "fetch_routing_enabled", True, raising=False)
    monkeypatch.setattr(settings, "fetch_routing_min_obs", 8, raising=False)
    monkeypatch.setattr(settings, "fetch_routing_threshold", 0.40, raising=False)
    return fake


# ---------------------------------------------------------------------------
# _fetch_domain
# ---------------------------------------------------------------------------


def test_fetch_domain_strips_www_and_lowercases():
    assert web._fetch_domain("https://WWW.CNBC.com/markets") == "cnbc.com"
    assert web._fetch_domain("https://finance.yahoo.com/quote/SPY") == "finance.yahoo.com"


def test_fetch_domain_rejects_non_sites():
    # IP literals, single-label hosts, and garbage carry no site reputation.
    assert web._fetch_domain("http://192.168.1.15/admin") is None
    assert web._fetch_domain("http://localhost:8090/workspace") is None
    assert web._fetch_domain("not a url") is None


# ---------------------------------------------------------------------------
# _reliability_reroute
# ---------------------------------------------------------------------------


def test_reroute_refuses_known_bad_domain(bridge):
    bridge.prediction = {"p": 0.20, "observations": 30, "ci": [0.1, 0.3], "caveats": []}
    msg = web._reliability_reroute("forbes.com")
    assert msg is not None
    assert "browse_web" in msg
    assert "force=true" in msg
    assert bridge.predict_calls == [("fetch_ok", ["forbes.com"])]


def test_reroute_passes_healthy_domain(bridge):
    bridge.prediction = {"p": 0.90, "observations": 50, "ci": [0.8, 0.95], "caveats": []}
    assert web._reliability_reroute("example.com") is None


def test_reroute_needs_enough_observations(bridge):
    # 3 failures out of 3 is noise, not reputation — never reroute on it.
    bridge.prediction = {"p": 0.0, "observations": 3, "ci": [0.0, 0.5], "caveats": []}
    assert web._reliability_reroute("newsite.com") is None


def test_reroute_inert_without_candor(bridge, monkeypatch):
    monkeypatch.setattr(settings, "candor_enabled", False)
    bridge.prediction = {"p": 0.0, "observations": 100}
    assert web._reliability_reroute("forbes.com") is None


def test_reroute_inert_when_routing_disabled(bridge, monkeypatch):
    monkeypatch.setattr(settings, "fetch_routing_enabled", False)
    bridge.prediction = {"p": 0.0, "observations": 100}
    assert web._reliability_reroute("forbes.com") is None


def test_reroute_fails_open_on_bridge_error(bridge, monkeypatch):
    # A degraded reliability oracle must never take the fetch path down.
    def _boom():
        raise RuntimeError("bridge down")

    monkeypatch.setattr("core.extensions.candor.bridge.get_candor_bridge", _boom)
    assert web._reliability_reroute("forbes.com") is None


def test_reroute_ignores_categorical_or_missing_prediction(bridge):
    bridge.prediction = None
    assert web._reliability_reroute("forbes.com") is None
    bridge.prediction = {"values": {"timeout": 0.5}, "observations": 20}
    assert web._reliability_reroute("forbes.com") is None


# ---------------------------------------------------------------------------
# _record_fetch
# ---------------------------------------------------------------------------


def test_record_fetch_emits_domain_and_aggregate(bridge):
    web._record_fetch("cnbc.com", True, method="http")
    assert len(bridge.recorded) == 1
    obs = bridge.recorded[0]
    assert {tuple(o["args"]) for o in obs} == {("cnbc.com",), ("*",)}
    for o in obs:
        assert o["pred"] == "fetch_ok"
        assert o["outcome"] is True
        assert o["ctx"]["method"] == "http"
    aggregate = next(o for o in obs if o["args"] == ["*"])
    assert aggregate["ctx"]["target"] == "cnbc.com"


def test_record_fetch_inert_without_candor(bridge, monkeypatch):
    monkeypatch.setattr(settings, "candor_enabled", False)
    web._record_fetch("cnbc.com", True, method="http")
    assert bridge.recorded == []


def test_record_fetch_skips_none_domain(bridge):
    web._record_fetch(None, True, method="browse")
    assert bridge.recorded == []


# ---------------------------------------------------------------------------
# http_get integration (no network: httpx and _validate_url are stubbed)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, text: str):
        self.text = text
        self.is_redirect = False
        self.headers: dict = {}

    def raise_for_status(self):
        pass


class _FakeClient:
    page = "hello world"

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return _FakeResp(self.page)


@pytest.fixture
def offline_http(monkeypatch):
    monkeypatch.setattr(web, "_validate_url", lambda url, allow_loopback=False: url)
    monkeypatch.setattr("httpx.Client", _FakeClient)


def test_http_get_skips_bad_domain_before_fetching(bridge, offline_http):
    bridge.prediction = {"p": 0.20, "observations": 30}
    out = web.http_get("https://forbes.com/article")
    assert out.startswith("Skipped:")
    # The refusal is not an attempt — nothing may be recorded against the domain.
    assert bridge.recorded == []


def test_http_get_force_overrides_reroute(bridge, offline_http):
    bridge.prediction = {"p": 0.20, "observations": 30}
    _FakeClient.page = "real content"
    out = web.http_get("https://forbes.com/article", force=True)
    assert out == "real content"
    assert len(bridge.recorded) == 1
    assert all(o["outcome"] is True for o in bridge.recorded[0])


def test_http_get_records_success(bridge, offline_http):
    bridge.prediction = None  # no admitted fact yet — first contact
    _FakeClient.page = "a normal page"
    assert web.http_get("https://example.com/") == "a normal page"
    assert len(bridge.recorded) == 1
    assert all(o["outcome"] is True for o in bridge.recorded[0])


def test_http_get_records_bot_wall_as_failure(bridge, offline_http):
    # Bot walls answer 200 with challenge HTML — that is a failed fetch.
    bridge.prediction = None
    _FakeClient.page = "<html>Checking your browser before accessing…</html>"
    out = web.http_get("https://example.com/")
    assert "Checking your browser" in out
    assert len(bridge.recorded) == 1
    assert all(o["outcome"] is False for o in bridge.recorded[0])


def test_http_get_records_http_error_as_failure(bridge, offline_http, monkeypatch):
    bridge.prediction = None

    class _ErrClient(_FakeClient):
        def get(self, url):
            raise RuntimeError("boom 503")

    monkeypatch.setattr("httpx.Client", _ErrClient)
    out = web.http_get("https://example.com/")
    assert out.startswith("Error fetching")
    assert len(bridge.recorded) == 1
    assert all(o["outcome"] is False for o in bridge.recorded[0])


def test_http_get_policy_block_records_nothing(bridge, monkeypatch):
    # An SSRF/policy refusal says nothing about the domain.
    def _blocked(url, allow_loopback=False):
        raise ValueError("Blocked: host resolves to a private/internal address")

    monkeypatch.setattr(web, "_validate_url", _blocked)
    out = web.http_get("https://internal.corp/")
    assert out.startswith("Error:")
    assert bridge.recorded == []


# ---------------------------------------------------------------------------
# Scout structural-gate rule
# ---------------------------------------------------------------------------


def test_scout_prompt_injects_gate_rule_when_gates_enabled(monkeypatch):
    from core.scout.runner import _scout_system_prompt

    monkeypatch.setattr(settings, "gates_enabled", False)
    monkeypatch.setattr(settings, "rlm_enabled", False)
    assert "STRUCTURAL SPECS" not in _scout_system_prompt()
    monkeypatch.setattr(settings, "gates_enabled", True)
    prompt = _scout_system_prompt()
    assert "STRUCTURAL SPECS" in prompt
    assert "add_gate" in prompt
    assert prompt.rstrip().endswith("/no_think")
    assert prompt.index("STRUCTURAL SPECS") < prompt.index("Do NOT use <think>")


def test_scout_prompt_has_live_state_rule():
    # Static rule (always on): stale memories about mutable operational state
    # (worker limits, cron jobs) produced phantom friction in session
    # 1e2806e0d2ea — live tools are the source of truth for such state.
    from core.scout.runner import SCOUT_SYSTEM_PROMPT

    idx = SCOUT_SYSTEM_PROMPT.index("LIVE STATE BEATS MEMORY")
    assert idx < SCOUT_SYSTEM_PROMPT.index("KNOWN FACTS BEAT EMPTY CONFIG")


def test_scout_prompt_stacks_conditional_rules(monkeypatch):
    from core.scout.runner import _scout_system_prompt

    monkeypatch.setattr(settings, "gates_enabled", True)
    monkeypatch.setattr(settings, "rlm_enabled", True)
    prompt = _scout_system_prompt()
    assert "RECURSIVE ANALYSIS" in prompt
    assert "STRUCTURAL SPECS" in prompt
    assert prompt.rstrip().endswith("/no_think")
