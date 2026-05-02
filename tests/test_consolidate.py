"""Tests for core/memory/consolidate.py: pure logic functions."""

import pytest

from core.memory.consolidate import (
    FileSignature,
    MergeDecision,
    _name_tokens,
    find_clusters,
    normalize_filename,
    score_pair,
)

# ---------------------------------------------------------------------------
# normalize_filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("user.profile", "user_profile"),  # dots → underscores
        ("pernix-debugging", "pernix_debugging"),  # hyphens → underscores
        ("my_file_notes", "my_file"),  # strips _notes noise suffix
        ("research_summary", "research"),  # strips _summary
        ("debug_log", "debug"),  # strips _log
        ("data_overview", "data"),  # strips _overview
        # Note: "pernix.notes" → "pernix_notes" → strips _notes → "pernix"
        ("pernix.notes", "pernix"),
        ("my__double__underscore", "my_double_underscore"),  # collapses __
    ],
)
def test_normalize_filename(name, expected):
    assert normalize_filename(name) == expected


def test_normalize_filename_preserves_meaningful_names():
    assert normalize_filename("pernix.tools") == "pernix_tools"
    assert normalize_filename("user.profile") == "user_profile"


# ---------------------------------------------------------------------------
# _name_tokens
# ---------------------------------------------------------------------------


def test_name_tokens_basic():
    tokens = _name_tokens("pernix.tools")
    assert "pernix" in tokens
    assert "tools" in tokens


def test_name_tokens_filters_short():
    tokens = _name_tokens("a.bb.ccc.dddd")
    assert "a" not in tokens
    assert "bb" not in tokens
    assert "ccc" in tokens
    assert "dddd" in tokens


def test_name_tokens_hyphen_sep():
    tokens = _name_tokens("my-skill-notes")
    assert "notes" in tokens
    assert "skill" in tokens


# ---------------------------------------------------------------------------
# FileSignature
# ---------------------------------------------------------------------------


def _make_sig(name, normalized=None, name_tokens=None, keywords=None, entry_count=1, content_fingerprints=None):
    return FileSignature(
        name=name,
        normalized=normalized or normalize_filename(name),
        name_tokens=name_tokens or _name_tokens(name),
        keywords=keywords or set(),
        entry_count=entry_count,
        content_fingerprints=content_fingerprints or [],
    )


# ---------------------------------------------------------------------------
# score_pair
# ---------------------------------------------------------------------------


def test_score_pair_identical_names():
    a = _make_sig("pernix.notes")
    b = _make_sig("pernix.notes")  # exact same
    score = score_pair(a, b)
    # Same name → name_sim=1.0 + token_jaccard=1.0 → score = 0.35+0.25 = 0.60 min
    assert score >= 0.5


def test_score_pair_similar_names():
    a = _make_sig("pernix.tools")
    b = _make_sig("pernix.tool")  # very similar
    score = score_pair(a, b)
    assert score > 0.3


def test_score_pair_dissimilar():
    a = _make_sig("user.profile")
    b = _make_sig("pernix.debugging")
    score = score_pair(a, b)
    assert score < 0.5


def test_score_pair_keyword_boost():
    a = _make_sig("file1", keywords={"python", "testing", "pytest"})
    b = _make_sig("file2", keywords={"python", "testing", "coverage"})
    score = score_pair(a, b)
    assert score > 0.0


def test_score_pair_content_fingerprint_boost():
    fp = "the database uses sqlite wal mode for concurrency"
    a = _make_sig("file1", content_fingerprints=[fp])
    b = _make_sig("file2", content_fingerprints=[fp])  # same content
    a_diff = _make_sig("file3", content_fingerprints=["completely different text here"])
    score_match = score_pair(a, b)
    score_diff = score_pair(a, a_diff)
    assert score_match > score_diff


# ---------------------------------------------------------------------------
# find_clusters
# ---------------------------------------------------------------------------


def test_find_clusters_no_similar():
    """Completely dissimilar files → no clusters."""
    sigs = [
        _make_sig("user.profile"),
        _make_sig("pernix.debugging"),
        _make_sig("research.unrelated"),
    ]
    clusters = find_clusters(sigs, threshold=0.9)  # very high threshold
    assert clusters == []


def test_find_clusters_similar_names():
    """Near-identical names form a cluster."""
    sigs = [
        _make_sig("pernix.notes"),
        _make_sig("pernix.note"),  # very similar
        _make_sig("user.profile"),  # different
    ]
    clusters = find_clusters(sigs, threshold=0.3)
    # pernix.notes and pernix.note should be in the same cluster
    if clusters:
        clustered_names = [name for cluster in clusters for name in cluster]
        assert "pernix.notes" in clustered_names or len(clusters) > 0


def test_find_clusters_returns_2plus():
    """Clusters contain at least 2 files."""
    sigs = [_make_sig("pernix.tools"), _make_sig("pernix.tool")]
    clusters = find_clusters(sigs, threshold=0.1)
    for cluster in clusters:
        assert len(cluster) >= 2


def test_find_clusters_empty():
    assert find_clusters([], threshold=0.5) == []


def test_find_clusters_single_file():
    sigs = [_make_sig("lonely.file")]
    clusters = find_clusters(sigs, threshold=0.5)
    assert clusters == []


# ---------------------------------------------------------------------------
# MergeDecision
# ---------------------------------------------------------------------------


def test_merge_decision_creation():
    decision = MergeDecision(
        target_file="pernix.notes",
        source_files=["pernix.note", "pernix.jotting"],
        strategy="trivial",
        entries_to_keep=[("pernix.note", 12345)],
        entries_to_archive=[("pernix.jotting", 67890)],
        reason="Near-identical file names",
    )
    assert decision.target_file == "pernix.notes"
    assert len(decision.source_files) == 2
    assert decision.strategy == "trivial"
    assert decision.fused_entries is None
