"""Dream deep probe: corpus export, gating, and candidate ingest.

The engine itself is exercised by test_rlm_engine; here we cover the dream
side — what gets staged, when a probe may launch, and that probe output
passes through the same filters as cycle hypotheses with evidence resolved
to full content-hash refs.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import config
from core.dream.probe import _ingest_candidates, export_corpus, probe_due
from core.memory.store import MemoryStore
from db import models as db


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memories"))


@pytest.fixture
def probe_on(monkeypatch):
    monkeypatch.setattr(config.settings, "dream_enabled", True)
    monkeypatch.setattr(config.settings, "dream_rlm_probe", True)
    monkeypatch.setattr(config.settings, "rlm_enabled", True)


def test_export_corpus_marks_entries_and_skips_dream(store):
    store.add_entry("A fact about the alpha subsystem worth remembering.", file_name="deploy.guide", epoch=100)
    store.add_entry("A web-derived claim about beta.", file_name="runbook.ports", epoch=200, origin="external")
    store.add_entry("Dream conclusion that must not appear.", file_name="gamma.conclusions", source="dream")

    corpus, file_count = export_corpus(store)
    assert "deploy.guide@100 [" in corpus
    assert "origin=external" in corpus
    assert "must not appear" not in corpus
    assert file_count == 2


def test_probe_due_gates(probe_on, monkeypatch):
    assert probe_due()
    db.set_snooze_state("dream_last_probe", datetime.now(timezone.utc).isoformat())
    assert not probe_due()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    db.set_snooze_state("dream_last_probe", old)
    assert probe_due()
    monkeypatch.setattr(config.settings, "rlm_enabled", False)
    assert not probe_due()


async def test_ingest_resolves_refs_and_filters(store, probe_on):
    store.add_entry("Server X claims port 8080 in the deployment guide notes.", file_name="deploy.guide", epoch=100)
    store.add_entry(
        "Server X claims port 9090 in the runbook, which differs.",
        file_name="runbook.ports",
        epoch=200,
        skip_dedup=True,
    )

    answer = json.dumps(
        {
            "hypotheses": [
                {
                    "kind": "contradiction",
                    "statement": "Two files disagree about server X's port: 8080 vs 9090 for the same service.",
                    "evidence": [{"file": "deploy.guide", "epoch": 100}, {"file": "runbook.ports", "epoch": 200}],
                    "confidence": 0.7,
                },
                {
                    "kind": "memory_stale",
                    "statement": "This one cites an entry that does not exist anywhere at all.",
                    "evidence": [{"file": "ghost.file", "epoch": 1}],
                    "confidence": 0.9,
                },
                {
                    "kind": "contradiction",
                    "statement": "The API key is missing from the configuration entirely.",
                    "evidence": [{"file": "deploy.guide", "epoch": 100}],
                    "confidence": 0.9,
                },
            ]
        }
    )
    saved, dropped = await _ingest_candidates(store, answer)
    assert saved == 1 and dropped == 2

    rows = db.list_dream_hypotheses()
    assert len(rows) == 1
    assert rows[0]["origin"] == "rlm_probe"
    ev = json.loads(rows[0]["evidence_json"])
    assert {e["file"] for e in ev} == {"deploy.guide", "runbook.ports"}
    assert all(e.get("hash") and e.get("quote") for e in ev)


async def test_ingest_garbage_is_retryable_not_zero(store, probe_on):
    # None (not a clean zero) so the probe runner knows to retry the run.
    assert await _ingest_candidates(store, "not json at all") is None
    assert await _ingest_candidates(store, json.dumps({"hypotheses": "nope"})) is None
    # An empty-but-valid answer IS a clean zero — no retry.
    assert await _ingest_candidates(store, json.dumps({"hypotheses": []})) == (0, 0)
