"""Memory sweeps moved the same entries back and forth for months.

Box, 2026-11-06: the memory store was 143 MB with 9 MB live. user.profile.md
held 61 live entries in 45.5 MB (68,427 archived copies). Two loops:

  * split_file peeled profile entries out of user.profile (the largest file)
    and reroute's type rule pulled every one straight back;
  * consolidate_files merged sibling files and split_file broke the result
    apart again — one body moved 144 times in a month.

Every move left a tombstone, nothing removed tombstones, and nothing noticed.
Now every sweep move goes through store.relocate_entries (copy + retire +
ledger in one uncancellable call), the move ledger refuses cooldown /
reversal / breaker moves, split never splits a type home, consolidation skips
frozen clusters and never archives a file holding a refused entry, and the
compaction rung drops tombstones whose body is live.
"""

from __future__ import annotations

import json
import time

import pytest

from core.memory import compact, moves
from core.memory.format import parse_entries_from_markdown
from core.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    from db.database import init_memory_db

    init_memory_db()
    return MemoryStore(str(tmp_path / "memories"))


def _live(store, name):
    md = store.read_file(name) or ""
    return parse_entries_from_markdown(name, md)


def _live_count_everywhere(store, content):
    conn = store._connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM memory_fts WHERE content = ?", (content,)).fetchone()[0]
    finally:
        conn.close()


def _add(store, text, file_name, epoch, entry_type="note"):
    return store.add_entry(text, file_name=file_name, epoch=epoch, entry_type=entry_type, skip_dedup=True)


# ── relocate_entries ────────────────────────────────────────────────────


def test_relocate_leaves_one_live_copy_and_records_the_move(store):
    _add(store, "Fact alpha about the workspace layout", "pernix.alpha_notes", 1000)
    _add(store, "Something else entirely in the target", "pernix.beta_notes", 2000)

    res = store.relocate_entries("pernix.alpha_notes", "pernix.beta_notes", [1000], reason="test")

    assert res["moved"] == 1 and res["retired"] == {1000}
    assert _live(store, "pernix.alpha_notes") == []
    assert _live_count_everywhere(store, "Fact alpha about the workspace layout") == 1
    conn = store._connect()
    try:
        rows = conn.execute("SELECT src, dst, reason FROM memory_moves").fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in rows] == [("pernix.alpha_notes", "pernix.beta_notes", "test")]


def test_twin_in_target_is_not_copied_and_the_source_is_retired(store):
    _add(store, "The same body in both files", "pernix.alpha_notes", 1000)
    _add(store, "The same body in both files", "pernix.beta_notes", 2000)
    conn = store._connect()
    conn.execute(
        "INSERT INTO memory_hits (file_name, epoch, hit_count, last_hit_at) VALUES ('pernix.alpha_notes','1000',3,5)"
    )
    conn.execute(
        "INSERT INTO memory_hits (file_name, epoch, hit_count, last_hit_at) VALUES ('pernix.beta_notes','2000',2,9)"
    )
    conn.commit()
    conn.close()

    res = store.relocate_entries("pernix.alpha_notes", "pernix.beta_notes", [1000], reason="test")

    assert res["deduped"] == 1 and res["moved"] == 0
    assert _live_count_everywhere(store, "The same body in both files") == 1
    conn = store._connect()
    try:
        hits = conn.execute(
            "SELECT hit_count FROM memory_hits WHERE file_name='pernix.beta_notes' AND epoch='2000'"
        ).fetchone()
    finally:
        conn.close()
    assert hits[0] == 5


def test_ledger_refuses_cooldown_then_reversal(store, monkeypatch):
    _add(store, "A body that wants to bounce between files", "pernix.alpha_notes", 1000)
    store.relocate_entries("pernix.alpha_notes", "pernix.beta_notes", [1000], reason="test")
    epoch = _live(store, "pernix.beta_notes")[0].epoch

    res = store.relocate_entries("pernix.beta_notes", "pernix.gamma_notes", [epoch], reason="test")
    assert res["refused"] == [(epoch, "cooldown")]

    monkeypatch.setattr("config.settings.memory_move_cooldown_days", 0)
    res = store.relocate_entries("pernix.beta_notes", "pernix.alpha_notes", [epoch], reason="test")
    assert res["refused"] == [(epoch, "reversal")]
    assert _live_count_everywhere(store, "A body that wants to bounce between files") == 1


def test_breaker_trips_after_three_moves_in_30_days(store, monkeypatch):
    monkeypatch.setattr("config.settings.memory_move_cooldown_days", 0)
    _add(store, "A body moved too often", "pernix.hop0_file", 1000)
    for i in range(3):
        epoch = _live(store, f"pernix.hop{i}_file")[0].epoch
        assert store.relocate_entries(f"pernix.hop{i}_file", f"pernix.hop{i + 1}_file", [epoch], reason="t")["retired"]
    epoch = _live(store, "pernix.hop3_file")[0].epoch
    assert store.relocate_entries("pernix.hop3_file", "pernix.hop9_file", [epoch], reason="t")["refused"] == [
        (epoch, "breaker")
    ]


def test_a_move_into_the_type_home_is_allowed_after_the_cooldown(store, monkeypatch):
    monkeypatch.setattr("config.settings.memory_move_cooldown_days", 0)
    _add(store, "User prefers short replies", "user.profile", 1000, entry_type="profile")
    store.relocate_entries("user.profile", "pernix.style", [1000], reason="split")
    epoch = _live(store, "pernix.style")[0].epoch

    # A reversal, but into the home: allowed (the home is a fixed point).
    res = store.relocate_entries("pernix.style", "user.profile", [epoch], reason="reroute")
    assert res["retired"] == {epoch}


def test_explicit_moves_bypass_the_ledger(store):
    _add(store, "Moved by a person on purpose", "pernix.alpha_notes", 1000)
    store.relocate_entries("pernix.alpha_notes", "pernix.beta_notes", [1000], reason="test")
    epoch = _live(store, "pernix.beta_notes")[0].epoch
    res = store.relocate_entries(
        "pernix.beta_notes", "pernix.alpha_notes", [epoch], reason="user", enforce_ledger=False
    )
    assert res["retired"] == {epoch}


# ── placement rules agree ───────────────────────────────────────────────


def test_profile_entries_are_home_in_any_user_file():
    from core.memory.format import MemoryEntry
    from core.memory.sweeps import classify_entry

    e = MemoryEntry(
        file_name="user.communication_style", content="Prefers terse answers", epoch=1, entry_type="profile"
    )
    assert classify_entry(e, "user.communication_style", {"pernix.x": {"terse"}}) is None
    e2 = MemoryEntry(file_name="pernix.style", content="Prefers terse answers", epoch=1, entry_type="profile")
    assert classify_entry(e2, "pernix.style", {})["target_file"] == "user.profile"


def _fake_llm(monkeypatch, content_fn):
    class _Resp:
        def __init__(self, c):
            self.content = c
            self.finish_reason = "stop"

    class _Client:
        async def chat(self, messages, **kw):
            return _Resp(content_fn(messages))

    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: _Client())


async def test_split_never_splits_a_type_home(store, monkeypatch):
    for i in range(90):
        _add(store, f"User observation number {i} about preferences", "user.profile", 1000 + i, entry_type="profile")
    _fake_llm(monkeypatch, lambda m: json.dumps({"groups": [{"file": "pernix.style", "entries": list(range(20))}]}))
    from core.memory import sweeps

    used, moved = await sweeps.split_file(store, lambda: False)
    assert moved == 0
    assert len(_live(store, "user.profile")) == 90


async def test_split_and_reroute_no_longer_oscillate(store, monkeypatch):
    """The 11-06 loop: a mixed file the split sweep keeps choosing, with
    profile entries in it. Ten cycles of split + reroute stay bounded."""
    from core.memory import sweeps
    from db import models as db

    for i in range(60):
        _add(store, f"Operational note {i} about cron timing", "pernix.ops", 1000 + i)
    for i in range(30):
        _add(store, f"User prefers detail level {i}", "pernix.ops", 5000 + i, entry_type="profile")
    _fake_llm(
        monkeypatch,
        lambda m: json.dumps(
            {
                "groups": [
                    {"file": "pernix.ops_cron", "entries": list(range(0, 50, 2))},
                    {"file": "pernix.ops_misc", "entries": list(range(1, 50, 2))},
                ]
            }
        ),
    )
    for _ in range(10):
        await sweeps.split_file(store, lambda: False)
        db.set_snooze_state("last_reroute_scan", "")
        await sweeps.reroute_misplaced_entries(
            store, db, lambda: False, did_llm_already=True, llm_ready=lambda: False, interval_hours=24
        )
    conn = store._connect()
    try:
        per_body = conn.execute("SELECT body_key, COUNT(*) FROM memory_moves GROUP BY body_key").fetchall()
        live = conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
    finally:
        conn.close()
    assert live == 90  # nothing lost, nothing duplicated
    assert max(n for _, n in per_body) <= 1  # within the cooldown, nothing moves twice
    total_sections = sum((store._dir / f"{f.name}.md").read_text().count("<!-- @epoch:") for f in store.list_files())
    assert total_sections <= 90 * 2  # at most one tombstone per moved body, before compaction


# ── consolidation ───────────────────────────────────────────────────────


def test_consolidation_never_archives_a_file_holding_a_refused_entry(store):
    from core.memory.consolidate import MergeDecision, execute_merge

    _add(store, "Entry that recently moved here", "pernix.src", 1000)
    _add(store, "Entry that may move freely", "pernix.src", 1001)
    _add(store, "Target entry", "pernix.dst", 2000)
    conn = store._connect()
    moves.record(conn, [(moves.body_key("Entry that recently moved here"), "pernix.other", "pernix.src", "t")])
    conn.commit()
    conn.close()

    decision = MergeDecision(
        target_file="pernix.dst",
        source_files=["pernix.src"],
        strategy="trivial",
        entries_to_keep=[("pernix.src", 1000), ("pernix.src", 1001)],
        entries_to_archive=[],
        reason="test",
    )
    stats = execute_merge(store, decision)

    assert stats.get("sources_kept_live") == 1
    assert [e.content for e in _live(store, "pernix.src")] == ["Entry that recently moved here"]
    assert _live_count_everywhere(store, "Entry that may move freely") == 1


def test_a_cluster_with_a_recent_move_is_skipped_before_planning(store):
    from core.memory.sweeps import _cluster_frozen

    _add(store, "Split piece one", "pernix.charter_a", 1000)
    _add(store, "Other piece", "pernix.charter_b", 2000)
    assert _cluster_frozen(store, ["pernix.charter_a", "pernix.charter_b"]) == ""
    conn = store._connect()
    old = int(time.time()) - 30 * 86400
    conn.execute(
        "INSERT INTO memory_moves (body_key, src, dst, at, reason) VALUES (?, ?, ?, ?, 'split')",
        (moves.body_key("Split piece one"), "pernix.charter_b", "pernix.charter_a", old),
    )
    conn.commit()
    conn.close()
    assert _cluster_frozen(store, ["pernix.charter_a", "pernix.charter_b"]) == "would reverse a move"


# ── compaction ──────────────────────────────────────────────────────────


def _section(epoch, body, archived=0):
    return f"<!-- @epoch: {epoch} -->\n<!-- @type: note -->\n" + "<!-- @archived: true -->\n" * archived + body + "\n"


def test_compaction_keeps_live_entries_and_one_dead_copy():
    raw = "<!-- @file: pernix.x -->\n<!-- @created: 1 -->\n" + "\n---\n".join(
        [
            "",
            _section(1, "live body"),
            _section(2, "live body", archived=5),  # twin of a live body
            _section(3, "dead body", archived=3),  # sole record
            _section(4, "dead body", archived=1),  # extra copy
            _section(5, "live elsewhere", archived=1),
        ]
    )
    live_keys = {moves.body_key("live body"), moves.body_key("live elsewhere")}
    new_raw, stats = compact.compact_text("pernix.x", raw, live_keys, set())

    assert [(e.epoch, e.content) for e in parse_entries_from_markdown("pernix.x", new_raw)] == [(1, "live body")]
    assert stats == {"dropped_live_twin": 2, "dropped_extra_copy": 1, "kept_tombstones": 1, "collapsed_tags": 1}
    assert new_raw.count("dead body") == 1
    assert new_raw.count("<!-- @archived: true -->") == 1


def test_compaction_shares_dead_copies_store_wide(store):
    _add(store, "Live fact", "pernix.alpha_notes", 1)
    for name in ("pernix.alpha_notes", "pernix.beta_notes"):
        p = store._dir / f"{name}.md"
        if not p.exists():
            p.write_text("<!-- @file: pernix.beta_notes -->\n<!-- @created: 1 -->\n")
        p.write_text(p.read_text() + "\n---\n" + _section(9, "dead fact", archived=1))
    totals = compact.compact_files(store, ["pernix.alpha_notes", "pernix.beta_notes"], seen_dead=set())
    assert totals["kept_tombstones"] == 1 and totals["dropped_extra_copy"] == 1
    assert [e.content for e in _live(store, "pernix.alpha_notes")] == ["Live fact"]


def test_seed_reads_the_last_hop_from_live_entries(store):
    _add(store, "Seeded body", "pernix.alpha_notes", 1000)
    store.move_entries("pernix.alpha_notes", "pernix.beta_notes", [1000])
    assert moves.seed_from_store(store) == 1
    conn = store._connect()
    try:
        h = moves.load_history(conn, [moves.body_key("Seeded body")])
    finally:
        conn.close()
    assert h[moves.body_key("Seeded body")].last_src == "pernix.alpha_notes"


def test_health_survey_reports_churn_signals(store):
    _add(store, "Something", "pernix.alpha_notes", 1)
    survey = store._survey()
    assert {"store_mb", "archived_sections", "bloated_files", "moves_24h", "bodies_at_breaker"} <= set(survey)
