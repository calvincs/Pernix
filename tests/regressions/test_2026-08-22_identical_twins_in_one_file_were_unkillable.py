"""Regression — 2026-08-22, box.

Two consolidation passes merged the same entry into
pernix.ai_tech_daily_brief_workflow twice: same epoch, identical body, only
the @merged_from/@merged_at bookkeeping differed — 59 such pairs. Nothing
could remove them: delete_entry refuses an epoch that matches more than one
section ("legacy collision"), the exact-duplicate sweep sees one index row
per (file, epoch), and repair_epoch_collisions would have re-epoched the
twin into a second REAL entry. Pinned here: the repair drops an identical
twin and still re-epochs a genuine collision (different bodies).
"""

import re

from core.memory.store import get_memory_store


def _duplicate_last_section(md_path, extra_header: str, body_suffix: str = "") -> None:
    raw = md_path.read_text(encoding="utf-8")
    sections = raw.split("\n---\n")
    twin = sections[-1]
    epoch_line = re.search(r"<!-- @epoch: \d+ -->", twin).group(0)
    twin = twin.replace(epoch_line, epoch_line + "\n" + extra_header, 1) + body_suffix
    md_path.write_text(raw + "\n---\n" + twin, encoding="utf-8")


def test_repair_drops_identical_twins_and_re_epochs_real_collisions(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    store = get_memory_store()
    store.add_entry(
        content="Per-video output directories prevent collisions.", file_name="test.twins", entry_type="decision"
    )
    md_path = store._dir / "test.twins.md"
    epoch = int(re.search(r"<!-- @epoch: (\d+) -->", md_path.read_text()).group(1))

    # An identical twin (bookkeeping header differs) and a genuine collision (body differs).
    _duplicate_last_section(md_path, "<!-- @merged_from: pernix.daily_brief_workflow -->")
    _duplicate_last_section(
        md_path, "<!-- @merged_from: elsewhere -->", body_suffix="\nBut this one says something else."
    )
    assert md_path.read_text().count(f"<!-- @epoch: {epoch} -->") == 3
    assert store.delete_entry("test.twins", epoch).startswith("Error:")  # the guard that made twins unkillable

    assert store.repair_epoch_collisions() == 2  # one dropped, one re-epoched
    raw = md_path.read_text()
    assert raw.count(f"<!-- @epoch: {epoch} -->") == 1
    epochs = sorted(int(e) for e in re.findall(r"<!-- @epoch: (\d+) -->", raw))
    assert epochs == [epoch, epoch + 1]
    assert "something else" in store.get_entry("test.twins", epoch + 1).content
    assert store.delete_entry("test.twins", epoch).startswith("Deleted")
    hc = store.health_check(fix=False)
    assert hc["in_sync"] and hc["epoch_collisions"] == 0
