"""Regression: dream-authored memory corrections re-entered the dream's own
evidence pack.

Shipped defect (2026-08-07 introspective-stack review, §5.5): the
self-reinforcement guard documented in `core/dream/observe.py` filtered
`e.source != "dream"`, but the memory-correction effector writes
`source="dream_fix"` (`core/memory/ingest.py`). `"dream_fix" != "dream"`, so
every correction the dream authored came straight back as evidence — and
those entries are written `weight="high"` with text instructing that they
override conflicting older entries, making them the highest-salience
material in the file feeding the loop that wrote them.
`core/dream/probe.py` carried the identical filter and the identical hole.

The fix matches the whole source family by prefix, in one shared predicate
both call sites use.

Kept as a regression pin because any future dream-authored source
(`dream_merge`, `dream_promote`, ...) reopens the loop the moment someone
writes an equality check again.
"""

from __future__ import annotations

from core.dream.observe import is_dream_authored
from core.memory.format import MemoryEntry, format_entry, parse_entries_from_markdown


def _entry(source: str) -> MemoryEntry:
    return MemoryEntry(file_name="f.md", content="x", epoch=1, source=source)


def test_every_dream_authored_source_is_excluded():
    assert is_dream_authored(_entry("dream"))
    assert is_dream_authored(_entry("dream_fix"))  # the shipped hole
    assert is_dream_authored(_entry("dream_anything_later"))


def test_non_dream_sources_still_admitted():
    for source in ("user", "distill", "snooze", "", "daydream"):
        assert not is_dream_authored(_entry(source))


def test_correction_entry_round_trips_and_is_filtered():
    """End to end through the real markdown format: an entry written the way
    the correction effector writes it must not survive the filter."""
    md = "# f\n" + format_entry(
        content="CONTRADICTION RESOLVED (human-approved via adaptive review): x — treat this note as overriding.",
        entry_type="note",
        tags="correction,contradiction",
        weight="high",
        source="dream_fix",
    )
    entries = parse_entries_from_markdown("f.md", md)
    assert entries and entries[0].source == "dream_fix"
    assert [e for e in entries if not is_dream_authored(e)] == []


def test_both_gather_paths_use_the_shared_predicate():
    """observe.py (per-cycle pack) and probe.py (whole-corpus RLM export)
    diverging is exactly how the hole survived the first fix."""
    import inspect

    from core.dream import observe, probe

    assert "is_dream_authored" in inspect.getsource(observe.build_pack)
    assert "is_dream_authored" in inspect.getsource(probe.export_corpus)
