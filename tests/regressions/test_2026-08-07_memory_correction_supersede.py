"""Regression: corrections were silently dropped by the dedup gate.

Shipped defect (architecture review 2026-08-07, §5): `add_entry`'s dedup gate
returns a supersede hint naming the blocking entry, but only the agent-facing
`remember` tool could act on it. Every automatic writer (distill, ingest,
refine, audit) called `is_duplicate` and `continue`d — so when the distiller
learned a *corrected* version of a stored fact, the correction was dropped and
the stale version survived. The similarity that makes a correction detectable
is exactly what made it rejected.

Fix: `MemoryStore.add_or_supersede_entry` keeps the gate but gives clear
corrections somewhere to go — when the blocking entry is the same statement
restated (`_supersede_reason`: ratio >= 0.82, the new text adds at least one
token, and the stored text loses at most two), the new content replaces it via
`update_entry` (epoch preserved, @updated stamped). Ambiguous duplicates are
still refused; distill and ingest write through the new path.
"""

from __future__ import annotations

import pytest

from core.memory.store import MemoryStore, _supersede_reason

_ORIGINAL = (
    "The Pernix API server listens on port 8090 and is reached at http://localhost:8090 "
    "from the host machine during local development."
)
# Same sentence, one value corrected.
_CORRECTION = (
    "The Pernix API server listens on port 9110 and is reached at http://localhost:9110 "
    "from the host machine during local development."
)
# Same facts, different words only.
_PARAPHRASE = (
    "The Pernix API server listens on port 8090 and is reachable at http://localhost:8090 "
    "from the host machine during local development."
)
_UNRELATED = (
    "Snooze yields immediately when a new session starts, so background maintenance "
    "never competes with a live turn for the model."
)


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memories"))


# ---------------------------------------------------------------------------
# The criterion itself
# ---------------------------------------------------------------------------


def test_correction_is_recognised():
    assert _supersede_reason(_CORRECTION, _ORIGINAL) == "correction"


def test_strict_superset_is_enrichment():
    extended = _ORIGINAL + " The port is configurable via settings."
    assert _supersede_reason(extended, _ORIGINAL) == "enrichment"


def test_content_adding_nothing_is_never_superseded():
    """The containment half of the guard: no new tokens, no rewrite."""
    assert _supersede_reason(_ORIGINAL, _ORIGINAL) == ""
    longer = _ORIGINAL + " It is also reachable from the LAN."
    assert _supersede_reason(_ORIGINAL, longer) == ""


def test_neighbouring_fact_is_not_superseded():
    """Two structured facts differing in several values must never overwrite."""
    prod = "Deployment uses prod key AAAA on host box.ventibean at port 8090 with TLS enabled always."
    dev = "Deployment uses dev key BBBB on host lab.ventibean at port 8091 with TLS disabled always."
    assert _supersede_reason(dev, prod) == ""


def test_topical_neighbour_below_ratio_is_not_superseded():
    """The write gate fires at 0.70 / Jaccard 0.55; supersede needs 0.82."""
    near = (
        "The Pernix API server exposes its REST surface on port 8090 for the web UI, "
        "and the SSE stream shares that listener."
    )
    assert _supersede_reason(near, _ORIGINAL) == ""


# ---------------------------------------------------------------------------
# End to end through the store
# ---------------------------------------------------------------------------


def test_pure_duplicate_is_still_dropped(store):
    store.add_entry(_ORIGINAL, file_name="pernix.config")
    result = store.add_or_supersede_entry(_ORIGINAL, file_name="pernix.config")

    assert result.startswith("Memory already contains")
    raw = store.read_file("pernix.config")
    assert raw.count(_ORIGINAL) == 1


def test_correction_rewrites_the_blocking_entry(store):
    first = store.add_entry(_ORIGINAL, file_name="pernix.config")
    epoch = int(first.split("epoch=")[1].rstrip(")"))

    result = store.add_or_supersede_entry(_CORRECTION, file_name="pernix.config")

    assert result.startswith("Superseded")
    assert f"pernix.config@{epoch}" in result

    raw = store.read_file("pernix.config")
    # The stale version is gone, the correction is in its place, and the entry
    # kept its identity so wiki-links still resolve.
    assert "8090" not in raw
    assert "9110" in raw
    assert f"<!-- @epoch: {epoch} -->" in raw
    assert "@updated:" in raw

    # The index followed the markdown — no stale row left behind.
    hits = store.search("9110", limit=5, _track_hits=False)
    assert any("9110" in h.entry.content for h in hits)
    assert not any("8090" in h.entry.content for h in hits)


def test_paraphrase_does_not_grow_the_store(store):
    """A synonym swap is lexically identical in shape to a one-value
    correction, so it may rewrite in place — but it must never append."""
    store.add_entry(_ORIGINAL, file_name="pernix.config")
    store.add_or_supersede_entry(_PARAPHRASE, file_name="pernix.config")

    files = {f.name: f.entry_count for f in store.list_files()}
    assert files["pernix.config"] == 1
    assert "8090" in store.read_file("pernix.config")


def test_unrelated_content_is_appended_normally(store):
    store.add_entry(_ORIGINAL, file_name="pernix.config")
    result = store.add_or_supersede_entry(_UNRELATED, file_name="pernix.config")

    assert result.startswith("Saved to pernix.config")
    files = {f.name: f.entry_count for f in store.list_files()}
    assert files["pernix.config"] == 2


def test_short_content_bypasses_the_gate_as_before(store):
    """Below 60 chars similarity is unreliable, so nothing is overwritten."""
    store.add_entry("Port is 8090", file_name="pernix.config")
    result = store.add_or_supersede_entry("Port is 9110", file_name="pernix.config")

    assert result.startswith("Saved to")
    files = {f.name: f.entry_count for f in store.list_files()}
    assert files["pernix.config"] == 2


def test_empty_content_rejected(store):
    assert store.add_or_supersede_entry("   ") == "Error: Empty content"
