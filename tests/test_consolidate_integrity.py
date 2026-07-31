"""Consolidation integrity: metadata survival, fuse supersede, omission rescue.

Covers the dream-plan §13 defect fixes:
 - format roundtrip for non-default weight and updated
 - move_entries carries updated into target markdown
 - fused entries preserve type/tags/weight, bypass the dup gate, and
   supersede the target contributor
 - whole-file archival rescues entries the merge verdict omitted
 - archive stats count only entries actually retired
"""

import pytest

from core.memory.consolidate import MergeDecision, execute_merge
from core.memory.format import format_entry, parse_entries_from_markdown
from core.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memories"))


# ---------------------------------------------------------------------------
# Format roundtrip
# ---------------------------------------------------------------------------


def test_format_entry_roundtrips_low_weight_and_updated():
    md = format_entry(
        "A fact worth keeping around for the roundtrip test.",
        entry_type="lesson",
        weight="low",
        updated=1234567,
        epoch=100,
    )
    entries = parse_entries_from_markdown("f", "<!-- @file: f -->\n" + md)
    assert len(entries) == 1
    assert entries[0].weight == "low"
    assert entries[0].updated == 1234567


def test_format_entry_omits_default_weight_and_zero_updated():
    md = format_entry("Plain entry.", epoch=100)
    assert "@weight" not in md
    assert "@updated" not in md


def test_move_entries_preserves_updated(store):
    store.add_entry("Original fact that will be corrected later on.", file_name="src.file", epoch=100)
    store.update_entry("src.file", 100, "Corrected fact that must keep its correction date.")
    store.add_entry("Anchor so target exists already.", file_name="tgt.file", epoch=50)

    moved = store.move_entries("src.file", "tgt.file", [100])
    assert moved == 1

    entries = parse_entries_from_markdown("tgt.file", store.read_file("tgt.file"))
    moved_entry = next(e for e in entries if e.epoch == 100)
    assert moved_entry.updated > 0, "updated timestamp lost on move"


# ---------------------------------------------------------------------------
# add_entry skip_dedup
# ---------------------------------------------------------------------------


def test_add_entry_skip_dedup_bypasses_gate(store):
    content = "The deployment server listens on port 8090 with TLS enabled and a bearer token."
    assert store.add_entry(content, file_name="ops.notes").startswith("Saved to")
    near_dup = "The deployment server listens on port 8090 with TLS enabled and a bearer token!"
    refused = store.add_entry(near_dup, file_name="ops.notes")
    assert not refused.startswith("Saved to")
    accepted = store.add_entry(near_dup, file_name="ops.notes", skip_dedup=True)
    assert accepted.startswith("Saved to")


# ---------------------------------------------------------------------------
# execute_merge: fuse metadata + supersede
# ---------------------------------------------------------------------------


def _decision(**kw):
    base = dict(
        target_file="tgt.file",
        source_files=["src.file"],
        strategy="llm",
        entries_to_keep=[],
        entries_to_archive=[],
        fused_entries=None,
        target_description="d",
        reason="test",
    )
    base.update(kw)
    return MergeDecision(**base)


def test_fused_entry_preserves_metadata_and_supersedes_target(store):
    store.add_entry(
        "Server A hosts the production database on port 5432 for the main app.",
        file_name="tgt.file",
        entry_type="finding",
        tags="infra,db",
        weight="high",
        epoch=100,
    )
    # Fuse contributors are similar by nature — seed past the dup gate.
    store.add_entry(
        "Server A also hosts the staging database on port 5433 for testing.",
        file_name="src.file",
        entry_type="finding",
        tags="staging",
        epoch=200,
        skip_dedup=True,
    )

    fused_text = (
        "Server A hosts the production database on port 5432 for the main app "
        "and the staging database on port 5433 for testing."
    )
    stats = execute_merge(
        store,
        _decision(
            fused_entries=[{"file": "src.file", "epoch": 200, "fuse_target_epoch": 100, "fused_content": fused_text}]
        ),
    )

    assert stats["entries_fused"] == 1
    assert stats["fuse_failures"] == 0

    entries = parse_entries_from_markdown("tgt.file", store.read_file("tgt.file"))
    assert len(entries) == 1, "target contributor should be superseded by the fused entry"
    fused = entries[0]
    assert fused.content == fused_text
    assert fused.entry_type == "finding"
    assert fused.weight == "high", "high weight lost in fuse"
    assert set(fused.tags) >= {"infra", "db", "staging"}, "tag union lost in fuse"
    # Oldest contributor epoch preferred; collision bump is acceptable.
    assert fused.epoch >= 100


def test_fuse_failure_leaves_originals_and_rescues_source(store):
    store.add_entry(
        "Target entry with enough length to be a real memory entry for the test.",
        file_name="tgt.file",
        epoch=100,
    )
    store.add_entry(
        "Source entry describing a completely different subject: staging cron cadence.",
        file_name="src.file",
        epoch=200,
        skip_dedup=True,
    )

    stats = execute_merge(
        store,
        _decision(
            fused_entries=[
                # Empty fused_content → skipped upstream; whitespace content
                # reaches add_entry and fails → fuse_failure path.
                {"file": "src.file", "epoch": 200, "fuse_target_epoch": 100, "fused_content": "   "}
            ]
        ),
    )

    assert stats["fuse_failures"] == 1
    # Target contributor untouched.
    tgt_entries = parse_entries_from_markdown("tgt.file", store.read_file("tgt.file"))
    assert any(e.epoch == 100 for e in tgt_entries)
    # Source contributor rescued into target (not silently archived).
    assert stats["entries_rescued"] == 1
    assert any("Source entry" in e.content for e in tgt_entries)


# ---------------------------------------------------------------------------
# execute_merge: omission rescue + honest archive stats
# ---------------------------------------------------------------------------


def test_merge_rescues_unaddressed_source_entries(store):
    store.add_entry("Anchor entry so the target file exists beforehand.", file_name="tgt.file", epoch=50)
    store.add_entry("Kept entry about topic one with plenty of detail included.", file_name="src.file", epoch=100)
    store.add_entry("Omitted entry about topic two the planner forgot to mention.", file_name="src.file", epoch=200)
    store.add_entry("Another omitted entry about topic three, also forgotten.", file_name="src.file", epoch=300)

    stats = execute_merge(store, _decision(entries_to_keep=[("src.file", 100)]))

    assert stats["entries_kept"] == 1
    assert stats["entries_rescued"] == 2

    tgt_entries = parse_entries_from_markdown("tgt.file", store.read_file("tgt.file"))
    contents = " ".join(e.content for e in tgt_entries)
    assert "topic two" in contents and "topic three" in contents

    # Source is archived; rescued entries findable via search.
    results = store.search("topic three forgotten", mode="bm25", limit=5)
    assert any(r.entry.file_name == "tgt.file" for r in results)


def test_archive_stats_count_only_source_retirements(store):
    store.add_entry("Target duplicate entry that an archive verdict points at.", file_name="tgt.file", epoch=50)
    store.add_entry("Source entry that is genuinely redundant and archivable.", file_name="src.file", epoch=100)

    stats = execute_merge(
        store,
        _decision(entries_to_archive=[("src.file", 100), ("tgt.file", 50)]),
    )

    # Source archive verdict retired with the file; target verdict skipped.
    assert stats["entries_archived"] == 1
    tgt_entries = parse_entries_from_markdown("tgt.file", store.read_file("tgt.file"))
    assert any(e.epoch == 50 for e in tgt_entries), "target entry must be left live"


# ---------------------------------------------------------------------------
# Claim-origin provenance
# ---------------------------------------------------------------------------


def test_origin_roundtrips_and_survives_move(store):
    store.add_entry(
        "A fact scraped from a web page about framework release dates.",
        file_name="src.file",
        epoch=100,
        origin="external",
    )
    entries = parse_entries_from_markdown("src.file", store.read_file("src.file"))
    assert entries[0].origin == "external"

    store.add_entry("Anchor for target existence purposes.", file_name="tgt.file", epoch=50)
    store.move_entries("src.file", "tgt.file", [100])
    moved = next(e for e in parse_entries_from_markdown("tgt.file", store.read_file("tgt.file")) if e.epoch == 100)
    assert moved.origin == "external", "origin lost on move"


def test_fuse_taints_origin_external(store):
    store.add_entry(
        "Internal operational note about server A and its scheduled backup window.",
        file_name="tgt.file",
        epoch=100,
        origin="internal",
    )
    store.add_entry(
        "Web-sourced claim about server A backup best practices from a blog.",
        file_name="src.file",
        epoch=200,
        origin="external",
        skip_dedup=True,
    )
    execute_merge(
        store,
        _decision(
            fused_entries=[
                {
                    "file": "src.file",
                    "epoch": 200,
                    "fuse_target_epoch": 100,
                    "fused_content": "Server A backup window with best-practice notes merged together here.",
                }
            ]
        ),
    )
    fused = parse_entries_from_markdown("tgt.file", store.read_file("tgt.file"))[0]
    assert fused.origin == "external"


def test_session_used_web_tools_detection():
    from core.memory.distill import _session_used_web_tools

    assert _session_used_web_tools([{"role": "assistant", "tool_calls": '[{"function": {"name": "search_web"}}]'}])
    assert not _session_used_web_tools([{"role": "assistant", "tool_calls": '[{"function": {"name": "read_file"}}]'}])
    assert not _session_used_web_tools([{"role": "user", "content": "please search_web for me"}])
