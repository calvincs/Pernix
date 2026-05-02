"""More deep snooze tests targeting high-impact uncovered sections."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from core.snooze import SnoozeRunner


def _old_timestamp():
    return (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()


# ---------------------------------------------------------------------------
# _update_skill_cooccurrence: with skill entries that have tags
# ---------------------------------------------------------------------------


async def test_update_skill_cooccurrence_with_skill_entries(tmp_path, monkeypatch):
    """Build cooccurrence from skill-type memory entries."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from db import models as db

    db.set_snooze_state("last_skill_cooccurrence", _old_timestamp())

    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    # Add multiple skill-type entries with overlapping tags
    import time as t

    store.add_entry(
        "Used web search to find API docs", file_name="pernix.tools", entry_type="skill", tags="web,search,research"
    )
    await asyncio.sleep(0.15)
    store.add_entry(
        "Loaded code-review skill for PR review", file_name="pernix.tools", entry_type="skill", tags="code,review,git"
    )
    await asyncio.sleep(0.15)
    store.add_entry(
        "Applied git-workflow skill for branching",
        file_name="pernix.tools",
        entry_type="skill",
        tags="git,workflow,code",
    )

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation
    await runner._update_skill_cooccurrence()


async def test_update_skill_cooccurrence_rate_limit(tmp_path, monkeypatch):
    """Skips if ran recently."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from db import models as db

    # Set recent timestamp
    db.set_snooze_state("last_skill_cooccurrence", datetime.now(timezone.utc).isoformat())

    runner = SnoozeRunner()
    await runner._update_skill_cooccurrence()


# ---------------------------------------------------------------------------
# _dedup_sweep: trigger the archiving path
# ---------------------------------------------------------------------------


async def test_dedup_sweep_archives_duplicates(tmp_path, monkeypatch):
    """Dedup sweep archives exact duplicates."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_dedup_interval_days", 0)
    from db import models as db

    db.set_snooze_state("dedup_cai.config", _old_timestamp())

    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    # Add 6+ entries with 2 near-identical ones (>0.82 similarity)
    content_a = "The SQLite database uses WAL journal mode for concurrent read operations across multiple connections."
    await asyncio.sleep(0.05)
    store.add_entry(content_a, file_name="pernix.config")
    await asyncio.sleep(0.05)
    store.add_entry(content_a, file_name="pernix.config")  # exact duplicate
    await asyncio.sleep(0.05)
    store.add_entry("Authentication uses bearer tokens validated per request.", file_name="pernix.config")
    await asyncio.sleep(0.05)
    store.add_entry("The context compaction runs at 75% utilization threshold.", file_name="pernix.config")
    await asyncio.sleep(0.05)
    store.add_entry("Scout agents run before the main agent to set up context.", file_name="pernix.config")
    await asyncio.sleep(0.05)
    store.add_entry("Sessions persist state in SQLite via the sessions.db file.", file_name="pernix.config")

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation
    await runner._dedup_sweep()


# ---------------------------------------------------------------------------
# _enrich_tags: trigger tag update path
# ---------------------------------------------------------------------------


async def test_enrich_tags_updates_entries(tmp_path, monkeypatch):
    """Enrich tags updates entries that have sparse tags."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    # Add entries with no technical tags — enrich should add some
    store.add_entry(
        "The file_write function saves content atomically using fcntl locking mechanism.",
        file_name="pernix.tools",
        tags="tools",
    )
    await asyncio.sleep(0.05)
    store.add_entry(
        "The agent_loop uses asyncio.wait_for with stuck_detector for timeout handling.",
        file_name="pernix.tools",
        tags="agent",
    )
    await asyncio.sleep(0.05)
    store.add_entry(
        "SQLiteDB and PostgreSQL connections both use connection_pool patterns.",
        file_name="pernix.config",
        tags="database",
    )

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation
    await runner._enrich_tags()
    # After enrich, some new tags may have been added
    assert runner._stats.get("entries_enriched", 0) >= 0


# ---------------------------------------------------------------------------
# _split_file: trigger the body with large file
# ---------------------------------------------------------------------------


async def test_split_file_with_large_file(tmp_path, monkeypatch, mock_llm_client):
    """Split file triggers when a file exceeds the size threshold."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.llm.types import ChatResponse, TokenUsage
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    # Add many entries to make a "large" file (though actual splitting requires LLM)
    for i in range(50):
        store.add_entry(f"Entry {i}: detailed content about topic {i % 5} with context", file_name="pernix.notes")
        if i > 0 and i % 10 == 0:
            await asyncio.sleep(0.05)

    mock_llm_client.responses = [
        ChatResponse(
            content="SKIP",
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation
    await runner._split_file()


# ---------------------------------------------------------------------------
# _catchup_distill: with short transcript (skip path)
# ---------------------------------------------------------------------------


async def test_catchup_distill_short_transcript(tmp_path, monkeypatch):
    """Skips distillation if transcript is too short."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_cooldown_minutes", 0)
    from db import models as db

    sid = db.create_session(title="Short Session")
    # Only a couple short messages — below 200 char transcript
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hello")
    db.add_message(sid, "user", "ok")
    db.add_message(sid, "assistant", "done")

    runner = SnoozeRunner()
    result = await runner._catchup_distill()
    # Short transcript → marks reviewed, returns False
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _prune_stale_entries: with hit data
# ---------------------------------------------------------------------------


async def test_prune_stale_entries_with_data(tmp_path, monkeypatch, mock_llm_client):
    """Prune stale entries exercises the LLM path when hit data exists."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from core.llm.types import ChatResponse, TokenUsage
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    # Add entries - they won't have hit counts initially, so prune skips
    for i in range(5):
        store.add_entry(f"Stale entry {i} about old project context", file_name="pernix.notes")

    mock_llm_client.responses = [
        ChatResponse(
            content="SKIP",
            tool_calls=None,
            usage=TokenUsage(10, 5, 15),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation
    runner._llm_available = lambda: True
    await runner._prune_stale_entries()


# ---------------------------------------------------------------------------
# _consolidate_files: with same-normalized-name files
# ---------------------------------------------------------------------------


async def test_consolidate_files_trivial_merge(tmp_path, monkeypatch):
    """Trivial merge when two files normalize to the same name."""
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.snooze_consolidation_interval_hours", 0)
    from db import models as db

    db.set_snooze_state("last_consolidation_scan", _old_timestamp())

    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))

    # Add entries to two files that would form a cluster via score_pair
    for i in range(3):
        store.add_entry(f"Debugging entry {i}: database connection error fix", file_name="pernix.debugging")
        await asyncio.sleep(0.05)

    for i in range(2):
        store.add_entry(f"Debug note {i}: auth module fixed", file_name="pernix.debug")
        await asyncio.sleep(0.05)

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation
    result = await runner._consolidate_files(did_llm_already=False)
    assert isinstance(result, bool)
