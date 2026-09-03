"""Space memory (v33): bucket routing, explicit-name contract, order-only
search priority with untouched scores, and sweep cross-bucket guards."""

from __future__ import annotations

import pytest

from core.memory.routing import space_bucket
from core.memory.store import MemoryStore


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(tmp_path / "memories")


def _seed(store, file_name, content, **kw):
    res = store.add_entry(content, file_name=file_name, skip_dedup=True, **kw)
    assert res.startswith("Saved"), res
    return res


# ---------------------------------------------------------------------------
# space_bucket
# ---------------------------------------------------------------------------


def test_space_bucket_parsing():
    assert space_bucket("pernix.space.lab.research") == "lab"
    assert space_bucket("pernix.space.my-lab.notes") == "my-lab"
    assert space_bucket("pernix.research") is None
    assert space_bucket("user.profile") is None
    assert space_bucket("") is None


# ---------------------------------------------------------------------------
# Write routing
# ---------------------------------------------------------------------------


def test_auto_route_prefixes_into_space(store):
    res = store.add_entry("Research finding: llamas hum at dusk", space_slug="lab")
    assert "pernix.space.lab." in res


def test_auto_route_fallback_is_space_notes(store):
    res = store.add_entry("zzz qqq completely unmatched gibberish", space_slug="lab")
    assert "pernix.space.lab.notes" in res


def test_explicit_file_name_stays_a_contract(store):
    """A space session naming a GLOBAL file gets that file, verbatim."""
    res = store.add_entry("deployment-wide fact", file_name="pernix.config", space_slug="lab")
    assert "Saved to pernix.config" in res


def test_jaccard_never_maps_across_spaces(store):
    """pernix.space.alpha.research vs pernix.space.beta.research are 0.6
    token-similar — the bucket guard must stop the mapping."""
    _seed(store, "pernix.space.alpha.research", "alpha findings about ravens and mirrors")
    res = store.add_entry(
        "beta findings about crows and windows",
        file_name="pernix.space.beta.research",
        skip_dedup=True,
    )
    assert "pernix.space.beta.research" in res
    assert "alpha" not in res


# ---------------------------------------------------------------------------
# Search priority: order changes, scores never do
# ---------------------------------------------------------------------------


def _entry_names(results):
    return [r.entry.file_name for r in results]


def test_space_hits_surface_first_with_identical_scores(store):
    _seed(store, "pernix.research", "the flamingo protocol handles retries with backoff")
    _seed(store, "pernix.space.lab.research", "the flamingo protocol variant used in lab experiments")

    plain = store.search("flamingo protocol", limit=5, _track_hits=False)
    scoped = store.search("flamingo protocol", limit=5, _track_hits=False, space_slug="lab")

    assert _entry_names(scoped)[0] == "pernix.space.lab.research"
    assert "pernix.research" in _entry_names(scoped)  # globals still present

    # Score contract: byte-identical scores for the same (file, epoch).
    plain_scores = {(r.entry.file_name, r.entry.epoch): r.score for r in plain}
    for r in scoped:
        key = (r.entry.file_name, r.entry.epoch)
        if key in plain_scores:
            assert r.score == plain_scores[key]


def test_scoped_search_without_space_files_matches_plain(store):
    _seed(store, "pernix.research", "quantum kelp measurement rig details")
    plain = store.search("quantum kelp", limit=5, _track_hits=False)
    scoped = store.search("quantum kelp", limit=5, _track_hits=False, space_slug="ghost")
    assert _entry_names(plain) == _entry_names(scoped)


def test_default_search_is_unchanged_without_slug(store):
    _seed(store, "pernix.space.lab.research", "heliotrope tuning notes")
    _seed(store, "pernix.research", "heliotrope calibration baseline")
    res = store.search("heliotrope", limit=5, _track_hits=False)
    # No slug -> no reordering pass; both appear on merit only.
    assert len(res) == 2


def test_file_prefix_filter_in_bm25(store):
    from core.memory.search import search_bm25

    _seed(store, "pernix.space.lab.research", "peregrine dataset quirks")
    _seed(store, "pernix.research", "peregrine dataset baseline")
    conn = store._connect()
    try:
        hits = search_bm25(conn, "peregrine dataset", limit=10, file_prefix="pernix.space.lab.")
        assert hits and all(r.entry.file_name.startswith("pernix.space.lab.") for r in hits)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sweep guards
# ---------------------------------------------------------------------------


def test_consolidation_score_pair_zero_across_buckets(store):
    from core.memory.consolidate import build_signatures, score_pair

    _seed(store, "pernix.space.alpha.research", "one two three four five six seven")
    _seed(store, "pernix.space.beta.research", "one two three four five six seven")
    _seed(store, "pernix.research", "one two three four five six seven")
    sigs = {s.name: s for s in build_signatures(store)}
    assert score_pair(sigs["pernix.space.alpha.research"], sigs["pernix.space.beta.research"]) == 0.0
    assert score_pair(sigs["pernix.space.alpha.research"], sigs["pernix.research"]) == 0.0
    assert score_pair(sigs["pernix.space.alpha.research"], sigs["pernix.space.alpha.research"]) > 0.0


def test_reroute_targets_stay_in_bucket():
    from core.memory.sweeps import classify_entry

    class _E:
        entry_type = "note"
        tags = ["woodwork"]
        content = "carving techniques for chairs"

    file_keywords = {
        "pernix.space.lab.research": set(),
        "pernix.tools": {"woodwork", "carving"},
    }
    # A space entry scores 0 in its own bucket and high in a global file —
    # the guard must still refuse the cross-bucket move.
    out = classify_entry(_E(), "pernix.space.lab.research", file_keywords)
    assert out is None


def test_profile_reroute_skipped_for_space_entries():
    from core.memory.sweeps import classify_entry

    class _E:
        entry_type = "profile"
        tags = []
        content = "the lab operator prefers dark mode"

    out = classify_entry(_E(), "pernix.space.lab.notes", {"pernix.space.lab.notes": set()})
    assert out is None


# ---------------------------------------------------------------------------
# delete_file (cascade support)
# ---------------------------------------------------------------------------


def test_delete_file_removes_markdown_and_index(store):
    _seed(store, "pernix.space.lab.notes", "ephemeral scratch fact")
    assert store.delete_file("pernix.space.lab.notes") is True
    assert store.read_file("pernix.space.lab.notes") is None
    assert store.search("ephemeral scratch", limit=5, _track_hits=False) == []
    assert store.delete_file("pernix.space.lab.notes") is False  # idempotent
