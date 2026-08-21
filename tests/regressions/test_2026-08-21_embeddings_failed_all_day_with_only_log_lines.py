"""Regression — 2026-08-21, box.

Every embedding call to the aibox embed server failed for a day (Vulkan
out-of-device-memory on 512-token inputs) and the only trace was a WARNING
per search saying "lexical-only for 60s" — recall, dedup and the semantic
channels of dream/candor ran on keyword matching with nobody told. A second
hazard sat next to it: Ollama cancels a model load when the requesting
client disconnects, so the 5s query timeout can keep a cold model cold
forever.

Pinned here: a sustained failure episode produces ONE operator notification
(repeat at most daily, cleared by a success), and a query-side timeout fires
one detached long-timeout warm-up, rate-limited.
"""

import threading

import httpx
import pytest

import core.llm.embeddings as emb
from db import models as db


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(emb.time, "monotonic", c)
    monkeypatch.setattr("config.settings.embedding_model", "nomic-embed-text-v2-moe:latest")
    monkeypatch.setattr("config.settings.llm_base_url", "http://aibox:11434")
    monkeypatch.setattr(emb, "_last_failure_at", 0.0)
    monkeypatch.setattr(emb, "_failing_since", 0.0)
    monkeypatch.setattr(emb, "_last_notified_at", 0.0)
    monkeypatch.setattr(emb, "_last_warm_at", 0.0)
    return c


def _failing_post(calls):
    def _post(url, json=None, timeout=None):
        calls.append({"url": url, "input": json["input"], "timeout": timeout})
        req = httpx.Request("POST", url)
        resp = httpx.Response(500, request=req, text="decode() failed: vk::Device::allocateMemory")
        raise httpx.HTTPStatusError("500", request=req, response=resp)

    return _post


def _ok_post(calls):
    def _post(url, json=None, timeout=None):
        calls.append({"url": url, "input": json["input"], "timeout": timeout})
        return httpx.Response(200, request=httpx.Request("POST", url), json={"embeddings": [[0.1, 0.2]]})

    return _post


def test_sustained_failure_notifies_once_a_day_and_a_success_clears_it(clock, monkeypatch):
    calls: list = []
    monkeypatch.setattr(httpx, "post", _failing_post(calls))

    assert emb.embed_query_sync("q") is None  # episode starts
    assert db.get_notifications() == []
    clock.t += 900  # 15 min in: still inside the grace period
    assert emb.embed_query_sync("q") is None
    assert db.get_notifications() == []

    clock.t += 1000  # 31 min of continuous failure → one notice
    assert emb.embed_query_sync("q") is None
    notes = db.get_notifications()
    assert len(notes) == 1
    assert notes[0]["title"] == "Embeddings unavailable — memory recall is lexical-only"
    assert "http://aibox:11434/api/embed" in notes[0]["body"]
    assert "nomic-embed-text-v2-moe:latest" in notes[0]["body"]
    assert "allocateMemory" in notes[0]["body"]

    clock.t += 3600  # an hour later, still failing → no second notice today
    assert emb.embed_query_sync("q") is None
    assert len(db.get_notifications()) == 1

    monkeypatch.setattr(httpx, "post", _ok_post(calls))
    clock.t += 100
    assert emb.embed_query_sync("q") == [0.1, 0.2]  # recovery clears the episode
    assert emb._failing_since == 0.0

    monkeypatch.setattr(httpx, "post", _failing_post(calls))
    clock.t += 100
    assert emb.embed_query_sync("q") is None  # a fresh episode starts quietly
    assert len(db.get_notifications()) == 1
    # Every query honoured the 5s bound; nothing in this path waits longer.
    assert all(c["timeout"] == emb._QUERY_TIMEOUT_S for c in calls)


def _join_warm_threads() -> None:
    for t in threading.enumerate():
        if t.name == "pernix-embed-warm":
            t.join(timeout=5)


def test_query_timeout_fires_one_long_warm_up_then_cools_down(clock, monkeypatch):
    calls: list = []

    def _timeout_post(url, json=None, timeout=None):
        calls.append({"input": json["input"], "timeout": timeout})
        if timeout == emb._QUERY_TIMEOUT_S:
            raise httpx.ReadTimeout("cold model", request=httpx.Request("POST", url))
        return httpx.Response(200, request=httpx.Request("POST", url), json={"embeddings": [[0.0]]})

    monkeypatch.setattr(httpx, "post", _timeout_post)

    assert emb.embed_query_sync("q") is None
    _join_warm_threads()
    warm = [c for c in calls if c["timeout"] == emb._WARM_TIMEOUT_S]
    assert len(warm) == 1 and warm[0]["input"] == ["warm"]

    clock.t += 120  # past the 60s query backoff, inside the 600s warm cooldown
    assert emb.embed_query_sync("q") is None
    _join_warm_threads()
    assert len([c for c in calls if c["timeout"] == emb._WARM_TIMEOUT_S]) == 1

    clock.t += 700  # cooldown over → one more warm-up allowed
    assert emb.embed_query_sync("q") is None
    _join_warm_threads()
    assert len([c for c in calls if c["timeout"] == emb._WARM_TIMEOUT_S]) == 2


def test_a_plain_500_does_not_spawn_a_warm_up(clock, monkeypatch):
    calls: list = []
    monkeypatch.setattr(httpx, "post", _failing_post(calls))
    assert emb.embed_query_sync("q") is None
    _join_warm_threads()
    assert [c["timeout"] for c in calls] == [emb._QUERY_TIMEOUT_S]
