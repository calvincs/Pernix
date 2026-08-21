"""Regression — 2026-08-21, box.

Two days of a dead remote embedding server left Pernix with no semantic
recall at all. Calvin: consider a small, well-known local CPU model as a
fallback. Pinned here: after `embedding_fallback_after_minutes` of
continuous remote failure the active model becomes "local:<fallback>",
queries and batch embeds run on the CPU, the store and search read/write
that model's space; the remote is probed from the snooze sweep and wins
back only after answering for `embedding_fallback_recover_minutes`. Also:
one poison batch no longer parks the rest of the backlog, and the sweep
logs the real backlog.
"""

import sys
import types

import httpx
import pytest

import core.llm.embeddings as emb
from db import models as db


class _Clock:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t


class _FakeTextEmbedding:
    instances: list = []

    def __init__(self, model_name: str, cache_dir: str = "") -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        _FakeTextEmbedding.instances.append(self)

    def embed(self, texts):
        for t in texts:
            yield [0.5, float(len(t))]


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(emb.time, "monotonic", c)
    monkeypatch.setattr("config.settings.embedding_model", "nomic-embed-text-v2-moe:latest")
    monkeypatch.setattr("config.settings.llm_base_url", "http://aibox:11434")
    monkeypatch.setattr("config.settings.embedding_fallback_model", "BAAI/bge-small-en-v1.5")
    monkeypatch.setattr("config.settings.embedding_fallback_after_minutes", 30)
    monkeypatch.setattr("config.settings.embedding_fallback_recover_minutes", 60)
    for name in (
        "_last_failure_at",
        "_failing_since",
        "_last_notified_at",
        "_last_warm_at",
        "_degraded_since",
        "_remote_ok_since",
    ):
        monkeypatch.setattr(emb, name, 0.0)
    import core.llm.local_embed as le

    monkeypatch.setattr(le, "_model", None)
    monkeypatch.setattr(le, "_model_name", "")
    monkeypatch.setattr(le, "_unavailable_logged", False)
    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = _FakeTextEmbedding
    _FakeTextEmbedding.instances = []
    monkeypatch.setitem(sys.modules, "fastembed", fake)
    return c


def _failing_post(calls):
    def _post(url, json=None, timeout=None):
        calls.append(timeout)
        req = httpx.Request("POST", url)
        raise httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req, text="oom"))

    return _post


def test_remote_outage_switches_to_the_local_model_and_back(clock, monkeypatch):
    calls: list = []
    monkeypatch.setattr(httpx, "post", _failing_post(calls))

    assert emb.active_model() == "nomic-embed-text-v2-moe:latest"
    assert emb.embed_query_sync("q") is None  # episode starts at t=5000
    clock.t += 20 * 60
    assert emb.embed_query_sync("q") is None  # 20 min: still remote
    assert not emb.degraded()

    clock.t += 11 * 60  # 31 min of failure → fallback
    assert emb.embed_query_sync("q") is None  # this call still went remote and failed …
    assert emb.degraded() and emb.active_model() == "local:BAAI/bge-small-en-v1.5"
    titles = [n["title"] for n in db.get_notifications()]
    assert "Embeddings switched to the local CPU fallback" in titles

    # … and from now on queries never touch httpx: they embed on the CPU.
    remote_calls_before = len(calls)
    vec = emb.embed_query_sync("hello")
    assert vec == [0.5, 5.0] and len(calls) == remote_calls_before
    assert _FakeTextEmbedding.instances[0].model_name == "BAAI/bge-small-en-v1.5"
    assert _FakeTextEmbedding.instances[0].cache_dir.endswith("models/fastembed")

    # Recovery: the remote must answer for the whole recovery window.
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, json=None, timeout=None: httpx.Response(
            200, request=httpx.Request("POST", url), json={"embeddings": [[1.0]]}
        ),
    )
    assert emb.check_remote_recovery() is False  # first good probe starts the window
    assert emb.degraded()
    clock.t += 30 * 60
    assert emb.check_remote_recovery() is False  # 30 of 60 minutes
    clock.t += 31 * 60
    assert emb.check_remote_recovery() is True
    assert not emb.degraded() and emb.active_model() == "nomic-embed-text-v2-moe:latest"
    assert "Embeddings back on the remote server" in [n["title"] for n in db.get_notifications()]

    # A failed probe resets the window.
    emb._degraded_since = clock.t
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, json=None, timeout=None: httpx.Response(
            200, request=httpx.Request("POST", url), json={"embeddings": [[1.0]]}
        ),
    )
    assert emb.check_remote_recovery() is False
    monkeypatch.setattr(httpx, "post", _failing_post(calls))
    assert emb.check_remote_recovery() is False and emb._remote_ok_since == 0.0


def test_no_fallback_without_fastembed_or_without_a_model(clock, monkeypatch):
    monkeypatch.setattr(httpx, "post", _failing_post([]))
    monkeypatch.delitem(sys.modules, "fastembed")
    import builtins

    real_import = builtins.__import__

    def _no_fastembed(name, *a, **k):
        if name == "fastembed":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_fastembed)
    emb.embed_query_sync("q")
    clock.t += 40 * 60
    emb.embed_query_sync("q")
    assert not emb.degraded()  # fastembed missing → remote-only, as before

    monkeypatch.setattr(builtins, "__import__", real_import)
    monkeypatch.setattr("config.settings.embedding_fallback_model", "")
    clock.t += 40 * 60
    emb.embed_query_sync("q")
    assert not emb.degraded() and emb.active_model() == "nomic-embed-text-v2-moe:latest"


class _FakeStore:
    def __init__(self, n: int) -> None:
        self.rows = [
            {"file_name": "f", "epoch": str(i), "content": f"entry {i}", "content_hash": f"h{i}"} for i in range(n)
        ]
        self.stored: list = []

    def pending_embeddings(self, limit=256):
        return self.rows[:limit]

    def store_embeddings(self, rows):
        self.stored.extend(rows)
        return len(rows)


async def test_a_poison_batch_no_longer_parks_the_backlog(clock, monkeypatch, caplog):
    monkeypatch.setattr("config.settings.embedding_batch_size", 1)
    store = _FakeStore(6)
    outcomes = iter([None, [[0.1]], [[0.2]], [[0.3]], None, [[0.5]]])

    async def _embed_texts(texts):
        return next(outcomes)

    monkeypatch.setattr(emb, "embed_texts", _embed_texts)
    with caplog.at_level("INFO", logger="pernix.llm.embeddings"):
        stored = await emb.embed_pending(store, max_entries=6)
    assert stored == 4 and [r[1] for r in store.stored] == ["1", "2", "3", "5"]
    assert (
        "Embedded 4 memory entries with nomic-embed-text-v2-moe:latest (2 skipped in 2 failed batches; 2 still pending)"
        in caplog.text
    )


async def test_three_consecutive_failed_batches_stop_the_sweep(clock, monkeypatch):
    monkeypatch.setattr("config.settings.embedding_batch_size", 1)
    store = _FakeStore(10)
    seen: list = []

    async def _embed_texts(texts):
        seen.append(texts[0])
        return None

    monkeypatch.setattr(emb, "embed_texts", _embed_texts)
    assert await emb.embed_pending(store, max_entries=10) == 0
    assert seen == ["entry 0", "entry 1", "entry 2"]  # the server, not the batch → stop
