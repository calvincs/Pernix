"""Pernix — Local semantic retrieval (adaptation plan 1f).

Vectors are a rebuildable sidecar keyed (file_name, epoch); embedding is
batch/snooze work; search fuses BM25 and cosine via RRF and degrades to
lexical-only whenever anything is missing.
"""

import pytest

from core.llm import embeddings
from core.memory.store import MemoryStore

# Tiny deterministic "embedding space": axis 0 = coffee-ness, axis 1 =
# astronomy-ness, axis 2 = bias. Paraphrases land near each other without
# sharing tokens — exactly what BM25 can't do.
_FAKE_SPACE = {
    "espresso brewing needs finely ground beans": [0.9, 0.1, 0.1],
    "the barista tamps the grounds before pulling a shot": [0.95, 0.05, 0.1],
    "saturn's rings are made of ice and rock": [0.05, 0.9, 0.1],
    "telescopes reveal planetary detail": [0.1, 0.85, 0.1],
    "how do I make good coffee": [0.92, 0.08, 0.1],
}


def _fake_vec(text: str) -> list[float]:
    return _FAKE_SPACE.get(text, [0.0, 0.0, 1.0])


@pytest.fixture
def store():
    # conftest's isolate_data already points settings.memory_dir at a per-test
    # tmp dir and initializes the memory DB schema there.
    s = MemoryStore()
    s.add_entry("espresso brewing needs finely ground beans", "notes", skip_dedup=True)
    s.add_entry("the barista tamps the grounds before pulling a shot", "notes", skip_dedup=True)
    s.add_entry("saturn's rings are made of ice and rock", "notes", skip_dedup=True)
    s.add_entry("telescopes reveal planetary detail", "notes", skip_dedup=True)
    return s


def _embed_all(s: MemoryStore) -> int:
    pending = s.pending_embeddings()
    rows = [(p["file_name"], p["epoch"], p["content_hash"], _fake_vec(p["content"])) for p in pending]
    return s.store_embeddings(rows)


# ---------------------------------------------------------------------------
# Sidecar lifecycle
# ---------------------------------------------------------------------------


def test_pending_then_stored_then_none(store, monkeypatch):
    monkeypatch.setattr("config.settings.embedding_model", "fake-embed")
    pending = store.pending_embeddings()
    assert len(pending) == 4
    assert _embed_all(store) == 4
    assert store.pending_embeddings() == []


def test_model_change_marks_all_stale(store, monkeypatch):
    monkeypatch.setattr("config.settings.embedding_model", "fake-embed")
    _embed_all(store)
    assert store.pending_embeddings() == []
    monkeypatch.setattr("config.settings.embedding_model", "other-model")
    assert len(store.pending_embeddings()) == 4


def test_unset_model_means_nothing_pending(store, monkeypatch):
    monkeypatch.setattr("config.settings.embedding_model", "")
    assert store.pending_embeddings() == []
    assert store.store_embeddings([("notes", "1", "h", [0.1])]) == 0


def test_reindex_prunes_orphan_vectors(store, monkeypatch):
    monkeypatch.setattr("config.settings.embedding_model", "fake-embed")
    _embed_all(store)
    # Orphan: a vector for an entry that no longer exists in markdown.
    store.store_embeddings([("ghost-file", "12345", "deadbeef", [0.1, 0.2, 0.3])])
    store.reindex()

    conn = store._connect()
    try:
        remaining = {r["file_name"] for r in conn.execute("SELECT file_name FROM vectors")}
    finally:
        conn.close()
    assert "ghost-file" not in remaining
    assert "notes" in remaining  # live vectors survive reindex untouched


# ---------------------------------------------------------------------------
# Search fusion + degradation
# ---------------------------------------------------------------------------


def test_semantic_channel_finds_paraphrase(store, monkeypatch):
    """'how do I make good coffee' shares no tokens with the espresso
    entries — lexical alone misses them; the vector channel must not."""
    monkeypatch.setattr("config.settings.embedding_model", "fake-embed")
    _embed_all(store)
    monkeypatch.setattr(embeddings, "embed_query_sync", lambda q: _fake_vec(q))

    results = store.search("how do I make good coffee", limit=2)
    contents = [r.entry.content for r in results]
    assert any("espresso" in c or "barista" in c for c in contents)
    assert not any("saturn" in c for c in contents)


def test_lexical_unchanged_when_disabled(store, monkeypatch):
    monkeypatch.setattr("config.settings.embedding_model", "")
    called = []
    monkeypatch.setattr(embeddings, "embed_query_sync", lambda q: called.append(q) or [1, 0, 0])

    results = store.search("saturn rings", limit=3)
    assert called == []  # semantic path never engaged
    assert any("saturn" in r.entry.content for r in results)


def test_degrades_to_lexical_when_query_embed_fails(store, monkeypatch):
    monkeypatch.setattr("config.settings.embedding_model", "fake-embed")
    _embed_all(store)
    monkeypatch.setattr(embeddings, "embed_query_sync", lambda q: None)  # Ollama down

    results = store.search("saturn rings", limit=3)
    assert any("saturn" in r.entry.content for r in results)


def test_bm25_exact_match_still_wins_fusion(store, monkeypatch):
    """RRF fusion must not bury a strong keyword match under weak cosine."""
    monkeypatch.setattr("config.settings.embedding_model", "fake-embed")
    _embed_all(store)
    monkeypatch.setattr(embeddings, "embed_query_sync", lambda q: _fake_vec(q))

    results = store.search("telescopes reveal planetary detail", limit=2)
    assert results and "telescopes" in results[0].entry.content


def test_dimension_mismatch_degrades(store, monkeypatch):
    monkeypatch.setattr("config.settings.embedding_model", "fake-embed")
    _embed_all(store)
    # Query vector with the wrong dimensionality (model changed mid-flight).
    monkeypatch.setattr(embeddings, "embed_query_sync", lambda q: [0.5] * 8)

    results = store.search("saturn rings", limit=3)
    assert any("saturn" in r.entry.content for r in results)


# ---------------------------------------------------------------------------
# Batch embedding (snooze path)
# ---------------------------------------------------------------------------


async def test_embed_pending_batches_and_stops_on_failure(store, monkeypatch):
    monkeypatch.setattr("config.settings.embedding_model", "fake-embed")
    monkeypatch.setattr("config.settings.embedding_batch_size", 2)

    calls = []

    async def _fake_embed_texts(texts):
        calls.append(list(texts))
        return [_fake_vec(t) for t in texts]

    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed_texts)
    stored = await embeddings.embed_pending(store)
    assert stored == 4
    assert len(calls) == 2  # 4 entries / batch of 2
    assert store.pending_embeddings() == []

    # Failure mid-sweep: store nothing more, retry next cycle.
    store.add_entry("a brand new fact", "notes", skip_dedup=True)

    async def _failing(texts):
        return None

    monkeypatch.setattr(embeddings, "embed_texts", _failing)
    stored = await embeddings.embed_pending(store)
    assert stored == 0
    assert len(store.pending_embeddings()) == 1
