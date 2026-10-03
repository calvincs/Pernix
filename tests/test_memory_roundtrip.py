"""Memory round trip: what `remember` saves, `recall` finds (3.2).

This used to be a candidate canary. A canary cannot test it — canary
sessions are denied every memory tool, by design — and a full pipeline run
is the expensive way to check a store round trip anyway. So it is a plain
test through the real tool functions against a temp memory directory.
"""

from __future__ import annotations

from pathlib import Path


def test_remember_then_recall_through_the_tools():
    from core.tools.builtin.memory_tools import recall, remember

    fact = "The quarry pylon runs its backup at 04:17 and keeps eleven copies."
    saved = remember(fact, file="pernix.roundtrip")
    assert saved.startswith("SAVED"), saved

    found = recall("quarry pylon backup copies")
    assert "pernix.roundtrip" in found
    assert "04:17" in found and "eleven copies" in found


def test_the_saved_entry_is_on_disk_and_survives_a_fresh_store():
    from core.memory.store import MemoryStore, get_memory_store
    from core.tools.builtin.memory_tools import remember

    fact = "The cobalt heron dashboard refreshes every ninety seconds on weekdays."
    assert remember(fact, file="pernix.roundtrip_disk").startswith("SAVED")

    # The process-wide store keeps the directory it was first opened on.
    memory_dir = Path(get_memory_store()._dir)
    on_disk = [
        p for p in memory_dir.rglob("*") if p.is_file() and "cobalt heron dashboard" in p.read_text(errors="ignore")
    ]
    assert on_disk, "remember() reported SAVED but nothing reached the memory directory"

    # A store opened fresh on that directory (a restart) finds it again.
    hits = MemoryStore(str(memory_dir)).search("cobalt heron dashboard", limit=3)
    assert any("ninety seconds" in h.entry.content for h in hits)
