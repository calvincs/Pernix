"""User-profile facts grew without bound.

Reflect saves up to two observations about the user per turn, and snooze
extracts more; both used only add_entry's general duplicate gate, which
misses the same fact said differently. The profile grew about 60 entries a
day and spread over a hundred files.

Now one gate (core/memory/profile.save_fact): a correction replaces the
stored fact, a same-meaning fact is skipped, and a daily cap bounds automatic
writes. A snooze pass merges clusters of near-duplicate facts into fewer ones
and archives the originals.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from core.memory import profile
from core.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from db.database import init_memory_db

    init_memory_db()
    return MemoryStore(str(tmp_path / "memories"))


def _vec(store, fn, ep, v):
    from core.llm.embeddings import active_model

    a = np.asarray(v, dtype=np.float32)
    conn = store._connect()
    conn.execute(
        "INSERT OR REPLACE INTO vectors (file_name, epoch, model, dim, content_hash, vec) VALUES (?,?,?,?,?,?)",
        (fn, str(ep), active_model(), len(a), "h", a.tobytes()),
    )
    conn.commit()
    conn.close()


def _fact(store, text, ep, v=None, fn="user.profile"):
    store.add_entry(text, file_name=fn, entry_type="profile", epoch=ep, source="reflect", skip_dedup=True)
    if v is not None:
        _vec(store, fn, ep, v)


def _live_profile(store):
    conn = store._connect()
    try:
        return [r[0] for r in conn.execute("SELECT content FROM memory_fts WHERE entry_type='profile' ORDER BY epoch")]
    finally:
        conn.close()


def test_same_meaning_fact_is_skipped(store, monkeypatch):
    monkeypatch.setattr("config.settings.embedding_model", "test-embed")
    _fact(store, "The user likes answers that lead with the conclusion.", 1000, [1, 0, 0])
    monkeypatch.setattr("core.llm.embeddings.embed_query_sync", lambda t: [0.99, 0.05, 0])

    res = profile.save_fact(store, "User prefers the bottom line first in every reply.", source="reflect", tags="user")

    assert res.startswith("Skipped: same fact")
    assert len(_live_profile(store)) == 1


def test_a_new_fact_is_written_to_user_profile(store, monkeypatch):
    monkeypatch.setattr("config.settings.embedding_model", "")
    res = profile.save_fact(store, "The user reviews pull requests in the morning.", source="reflect", tags="user")
    assert res.startswith("Saved to user.profile")


def test_daily_cap_bounds_automatic_writes(store, monkeypatch):
    monkeypatch.setattr("config.settings.embedding_model", "")
    monkeypatch.setattr("config.settings.profile_daily_cap", 2)
    results = [
        profile.save_fact(
            store, f"Distinct user fact number {i} about tooling choice {i * 7}", source="reflect", tags="u"
        )
        for i in range(4)
    ]
    assert sum(r.startswith("Saved") for r in results) == 2
    assert results[-1] == "Skipped: daily profile cap reached"


def test_find_cluster_groups_near_duplicates(store):
    _fact(store, "Fact A first wording about reply length", 1000, [1, 0, 0])
    _fact(store, "Fact A second wording about reply length", 1001, [0.97, 0.2, 0])
    _fact(store, "Fact B unrelated to the others entirely", 1002, [0, 0, 1])
    conn = store._connect()
    try:
        cluster = profile.find_cluster(conn, threshold=0.9)
    finally:
        conn.close()
    assert sorted(ep for _, ep, _, _ in cluster) == ["1000", "1001"]


def _fake_llm(monkeypatch, facts):
    class _R:
        content = json.dumps({"facts": facts})

    class _C:
        async def chat(self, **kw):
            return _R()

    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: _C())


async def test_consolidation_merges_and_archives_originals(store, monkeypatch):
    _fact(store, "User wants short replies in chat sessions", 1000, [1, 0, 0])
    _fact(store, "User prefers brief chat replies, not essays", 1001, [0.98, 0.1, 0], fn="user.style_notes")
    _fact(store, "User keeps replies short; long answers annoy them", 1002, [0.97, 0.15, 0])
    _fact(store, "Unrelated: user works in a terminal most of the day", 1003, [0, 1, 0])
    _fake_llm(monkeypatch, ["The user prefers short chat replies over long answers."])

    retired = await profile.consolidate_step(store)

    assert retired == 3
    live = _live_profile(store)
    assert "The user prefers short chat replies over long answers." in live
    assert "Unrelated: user works in a terminal most of the day" in live
    assert len(live) == 2
    raw = (store._dir / "user.profile.md").read_text()
    assert "User wants short replies in chat sessions" in raw  # archived, still on file as the undo record


async def test_a_merge_that_does_not_reduce_is_rejected_and_not_retried(store, monkeypatch):
    from db import models as db

    _fact(store, "User likes tables for comparisons", 1000, [1, 0, 0])
    _fact(store, "User likes tables when comparing options", 1001, [0.99, 0.1, 0])
    _fake_llm(monkeypatch, ["one", "two"])  # not a reduction, and too short
    assert await profile.consolidate_step(store, db) == 0
    assert len(_live_profile(store)) == 2
    calls = []

    class _C:
        async def chat(self, **kw):
            calls.append(1)
            raise AssertionError("must not be called again within the week")

    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: _C())
    assert await profile.consolidate_step(store, db) == 0
    assert calls == []


def test_health_survey_counts_profile_entries(store):
    _fact(store, "A profile fact long enough to keep", 1000)
    assert store._survey()["profile_entries"] == 1


# ── labeling: only facts about the person (follow-up 2026-11-06) ────────


@pytest.mark.parametrize(
    "text",
    [
        "Findings worth persisting go to the findings memory file tagged by run.",
        "The user's charter enforces a strict 15 tool-call budget per run with an allow-list.",
        "User's API key is at its daily limit, so delegated workers fail with 403.",
    ],
)
def test_system_and_job_content_is_not_a_profile_fact(text):
    assert profile.observation_rejection(text)


@pytest.mark.parametrize(
    "text",
    [
        "The user prefers the conclusion first, then the reasoning.",
        "User corrects unit mistakes quickly and expects metric units.",
    ],
)
def test_person_facts_pass(text):
    assert profile.observation_rejection(text) == ""


async def test_reflect_writes_no_observations_from_automated_sessions(monkeypatch, store):
    from core import reflect
    from db import models as db

    monkeypatch.setattr("config.settings.embedding_model", "")
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: store)
    result = reflect.ReflectResult(verdict="pass", reasoning="")
    result.experience = {"user_observations": ["The user prefers short replies in every chat."]}
    for stype, expected in (("cron", 0), ("worker", 0), ("normal", 1)):
        sid = db.create_session(title=f"t-{stype}", session_type=stype)
        await reflect._save_user_observations(sid, result)
        assert len(_live_profile(store)) == expected, stype


def test_retype_entry_changes_markdown_and_index(store):
    _fact(store, "Findings go to the findings file each run.", 1000)
    assert store.retype_entry("user.profile", 1000, "note")
    assert _live_profile(store) == []
    from core.memory.format import parse_entries_from_markdown

    entries = parse_entries_from_markdown("user.profile", store.read_file("user.profile"))
    assert entries[0].entry_type == "note"
