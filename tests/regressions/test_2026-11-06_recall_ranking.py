"""Memory recall ranked the right entry low and called it weak.

Box, 2026-11-06, 40 paraphrased queries with one known answer each:
- The right entry's cosine had a median of 0.53, which the linear cos × 4.0 map
  turned into 2.1 — "weak" on the shared scale, under the 3.0 strong
  line internal_recall and the deep_recall prompt act on.
- Profile and skill entries (≈3.3k of 9.5k) filled whole top-k lists; the
  lesson starvation fixed in search_lessons was one case of it.

Now the vector score maps between two measured anchors, and no entry type
takes more than half of one result set while other types have candidates.
"""

from __future__ import annotations

from core.memory.format import MemoryEntry
from core.memory.search import SearchResult, _rrf_fuse, cap_per_type, vector_to_scale


def _r(name, entry_type, score=1.0, source="bm25", epoch=None):
    e = MemoryEntry(
        file_name=name, content=name, epoch=epoch or abs(hash(name)) % 10**9, entry_type=entry_type, tags=[]
    )
    return SearchResult(entry=e, score=score, source=source)


# ── score scale ─────────────────────────────────────────────────────────


def test_typical_right_answer_cosine_reads_strong():
    assert vector_to_scale(0.53) > 3.0  # median right answer, 2026-11-06


def test_off_topic_best_hit_cosine_reads_noise_or_weak():
    assert vector_to_scale(0.30) <= 1.0
    assert vector_to_scale(0.10) == 0.0


def test_scale_is_monotonic_and_capped():
    xs = [i / 20 for i in range(21)]
    ys = [vector_to_scale(x) for x in xs]
    assert ys == sorted(ys)
    assert max(ys) <= 6.0


def test_vector_only_hit_gets_mapped_score_and_bm25_score_is_untouched():
    lex = [_r("a", "note", score=4.2)]
    sem = [_r("b", "note", score=0.65, source="vector"), _r("a", "note", score=0.9, source="vector")]
    out = {r.entry.file_name: r for r in _rrf_fuse(lex, sem, 5)}
    assert out["a"].score == 4.2  # lexical score always wins the scale
    assert out["b"].score == vector_to_scale(0.65)
    assert out["b"].source == "vector"


# ── per-type cap ────────────────────────────────────────────────────────


def test_profiles_cannot_take_every_slot_when_findings_matched():
    ranked = [_r(f"user.p{i}", "profile") for i in range(30)] + [_r("pernix.f1", "finding"), _r("pernix.f2", "finding")]
    out = cap_per_type(ranked, 10)
    types = [r.entry.entry_type for r in out]
    assert len(out) == 10
    assert types.count("profile") <= 8  # 5 by cap + backfill only after others are in
    assert {"pernix.f1", "pernix.f2"} <= {r.entry.file_name for r in out}


def test_single_type_corpus_still_returns_a_full_list():
    ranked = [_r(f"user.p{i}", "profile") for i in range(12)]
    out = cap_per_type(ranked, 10)
    assert [r.entry.file_name for r in out] == [f"user.p{i}" for i in range(10)]


def test_rank_order_is_preserved_after_cap_and_backfill():
    ranked = [_r(f"s{i}", "skill") for i in range(7)] + [_r("n0", "note")]
    out = cap_per_type(ranked, 6)
    names = [r.entry.file_name for r in out]
    assert names == sorted(names, key=lambda n: [r.entry.file_name for r in ranked].index(n))
    assert "n0" in names
    assert names.count("n0") == 1 and len(names) == 6


# ── embed format ────────────────────────────────────────────────────────
# nomic models expect task prefixes; the Ollama modelfile adds none. Query
# and corpus prefixes come from one function, the local fallback gets none,
# and the stored hash covers the embedded text so a format change re-embeds.


def test_nomic_query_and_document_both_get_their_prefix(monkeypatch):
    from core.llm import embeddings as emb

    monkeypatch.setattr(emb, "active_model", lambda: "nomic-embed-text-v2-moe:latest")
    assert emb.query_text("deploy the box") == "search_query: deploy the box"
    assert emb.document_text("Deploy with compose.").startswith("search_document: ")


def test_local_fallback_and_other_models_get_no_prefix(monkeypatch):
    from core.llm import embeddings as emb

    for model in ("local:BAAI/bge-small-en-v1.5", "mxbai-embed-large"):
        monkeypatch.setattr(emb, "active_model", lambda m=model: m)
        assert emb.query_text("q") == "q"
        assert emb.document_text("body") == "body"


def test_provenance_line_is_not_embedded(monkeypatch):
    from core.llm import embeddings as emb

    monkeypatch.setattr(emb, "active_model", lambda: "x")
    text = emb.document_text("Use compose to deploy.\n[Provenance: session abc123def456, turn 4]")
    assert "Provenance" not in text and "abc123" not in text


def test_a_format_change_marks_existing_vectors_stale(tmp_path, monkeypatch):
    from core.llm import embeddings as emb
    from core.memory.store import MemoryStore
    from db.database import init_memory_db

    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.embedding_model", "nomic-embed-text-v2-moe:latest")
    init_memory_db()
    s = MemoryStore(str(tmp_path / "memories"))
    s.add_entry("Deploy with docker compose up --build.", file_name="pernix.deploy", skip_dedup=True)
    pending = s.pending_embeddings()
    assert len(pending) == 1 and pending[0]["text"].startswith("search_document: ")
    # A vector stored under the old bare-content hash is stale now.
    s.store_embeddings([(p["file_name"], p["epoch"], emb.content_hash(p["content"]), [0.1, 0.2]) for p in pending])
    assert len(s.pending_embeddings()) == 1
    s.store_embeddings([(p["file_name"], p["epoch"], p["content_hash"], [0.1, 0.2]) for p in pending])
    assert s.pending_embeddings() == []
