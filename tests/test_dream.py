"""Dream add-on: pure rules + end-to-end drivers (temp DB, FakeLLMClient)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from core.dream import run_step
from core.dream.hypothesize import generate, is_banned_claim, is_duplicate, parse_hypotheses
from core.dream.observe import build_pack, content_hash
from core.dream.report import compose_report, maybe_write_report
from core.dream.validate import parse_judge, replay_budget_left, validate_one
from core.llm.types import ChatResponse, TokenUsage
from core.memory.store import MemoryStore
from db import models as db
from tests.conftest import FakeLLMClient


def _resp(content: str) -> ChatResponse:
    return ChatResponse(
        content=content,
        tool_calls=None,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="test-model",
        provider="fake",
        finish_reason="stop",
    )


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memories"))


@pytest.fixture
def dream_on(monkeypatch):
    monkeypatch.setattr(config.settings, "dream_enabled", True)
    monkeypatch.setattr(config.settings, "background_model", "test-model")


def _never_cancelled():
    return False


# ---------------------------------------------------------------------------
# Pure layer
# ---------------------------------------------------------------------------


def test_parse_hypotheses_valid_and_fenced():
    payload = [
        {
            "kind": "contradiction",
            "statement": "Entry A and entry B disagree about the port.",
            "evidence": ["M1", "M2"],
            "confidence": 0.7,
        }
    ]
    assert parse_hypotheses(json.dumps(payload))[0]["kind"] == "contradiction"
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    assert len(parse_hypotheses(fenced)) == 1
    # single dict coerced to list
    assert len(parse_hypotheses(json.dumps(payload[0]))) == 1


def test_parse_hypotheses_rejects_bad_shapes():
    assert parse_hypotheses("not json") == []
    assert (
        parse_hypotheses(json.dumps([{"kind": "bogus", "statement": "long enough statement here", "evidence": ["M1"]}]))
        == []
    )
    assert parse_hypotheses(json.dumps([{"kind": "contradiction", "statement": "short", "evidence": ["M1"]}])) == []
    assert (
        parse_hypotheses(
            json.dumps(
                [{"kind": "contradiction", "statement": "a statement without any evidence refs at all", "evidence": []}]
            )
        )
        == []
    )
    # confidence clamped
    h = parse_hypotheses(
        json.dumps(
            [
                {
                    "kind": "open_question",
                    "statement": "a perfectly reasonable open question here",
                    "evidence": ["P1"],
                    "confidence": 7,
                }
            ]
        )
    )
    assert h[0]["confidence"] == 1.0


def test_banned_claim_filter():
    assert is_banned_claim("The timezone is not configured for this session.")
    assert is_banned_claim("No location is set up in the user profile.")
    assert is_banned_claim("SESSIONS.md shows the API key is missing.")
    assert not is_banned_claim("browse_web fails on JavaScript-heavy pages in the evening.")
    assert not is_banned_claim("Two entries disagree about which port the server uses.")


def test_is_duplicate_matches_near_identical():
    existing = ["browse_web fails frequently on sites requiring JavaScript rendering."]
    assert is_duplicate("browse_web fails frequently on sites requiring JavaScript rendering!", existing)
    assert not is_duplicate("The memory store contains two conflicting port numbers.", existing)


def test_content_hash_normalizes_whitespace():
    assert content_hash("a  b\nc") == content_hash("a b c")
    assert content_hash("a b c") != content_hash("a b d")


def test_parse_judge():
    assert parse_judge('{"verdict": "holds", "note": "x"}')["verdict"] == "holds"
    assert parse_judge('```json\n{"verdict": "holds"}\n```')["verdict"] == "holds"
    assert parse_judge("nope") is None


def test_compose_report_sections():
    rows = [
        {
            "kind": "contradiction",
            "statement": "S1 conflicts with S2.",
            "status": "refuted",
            "confidence": 0.5,
            "validation_json": json.dumps({"method": "evidence_judge", "note": "did not hold"}),
        },
        {"kind": "tool_pattern", "statement": "browse_web degrades at night.", "status": "pending", "confidence": 0.6},
    ]
    text = compose_report("2026-07-01T00:00:00", "2026-07-30T00:00:00", rows)
    assert "Refuted this period" in text
    assert "New hypotheses" in text
    assert "browse_web degrades at night." in text
    assert "evidence_judge" in text
    assert "Hypotheses are not beliefs" in text


# ---------------------------------------------------------------------------
# Observe + generate (end-to-end with FakeLLMClient)
# ---------------------------------------------------------------------------


def _seed_memory(store):
    store.add_entry(
        "The API server listens on port 8090 in production deployments.", file_name="pernix.config", epoch=1000
    )
    store.add_entry(
        "The API server listens on port 9090 according to the deploy script.",
        file_name="pernix.config",
        epoch=2000,
        skip_dedup=True,
    )


def _seed_failed_session() -> str:
    sid = db.create_session(title="failed one")
    db.add_message(sid, "user", "Summarize the weekly report please")
    db.add_message(sid, "assistant", "I could not do that.")
    db.add_post_mortem(
        session_id=sid,
        attempt=1,
        verdict="retry",
        failure_cause="agent",
        confidence=0.9,
        reflect_model="m",
        reflect_latency_ms=5,
        scout_viability="verified",
        execution_mode="inline",
        payload_json=json.dumps({"what_failed": "agent never called the summarize tool"}),
    )
    return sid


async def test_build_pack_labels_and_cursors(store, dream_on):
    _seed_memory(store)
    _seed_failed_session()
    pack = await build_pack(store)
    ids = [i.ref_id for i in pack.items]
    assert "P1" in ids and "M1" in ids and "M2" in ids
    assert pack.memory_file == "pernix.config"
    assert pack.pm_high_water
    mem_ref = pack.refs_by_id()["M1"].ref
    assert mem_ref["file"] == "pernix.config" and mem_ref["hash"]


async def test_build_pack_excludes_dream_authored(store, dream_on):
    store.add_entry(
        "A dream-authored conclusion that must not be dreamed about again.", file_name="pernix.config", source="dream"
    )
    pack = await build_pack(store)
    assert not [i for i in pack.items if i.kind == "memory"]


async def test_generate_saves_filters_and_advances_cursors(store, dream_on, monkeypatch):
    _seed_memory(store)
    _seed_failed_session()
    good = {
        "kind": "contradiction",
        "statement": "Memory disagrees about the API port: 8090 vs 9090 for the same server.",
        "evidence": ["M1", "M2"],
        "confidence": 0.8,
    }
    banned = {
        "kind": "memory_stale",
        "statement": "The deploy port is not configured anywhere in settings.",
        "evidence": ["M1"],
        "confidence": 0.9,
    }
    ghost_refs = {
        "kind": "tool_pattern",
        "statement": "A tool pattern citing evidence that was never offered to the model.",
        "evidence": ["C9"],
        "confidence": 0.9,
    }
    fake = FakeLLMClient(responses=[_resp(json.dumps([good, banned, ghost_refs]))])
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake)

    saved = await generate(store, _never_cancelled)
    assert saved == 1
    rows = db.list_dream_hypotheses()
    assert len(rows) == 1 and rows[0]["kind"] == "contradiction"
    evidence = json.loads(rows[0]["evidence_json"])
    assert {e["type"] for e in evidence} == {"memory"}
    assert all(e.get("quote") for e in evidence)
    assert db.get_snooze_state("dream_pm_cursor")
    assert db.get_snooze_state("dream_mem_cursor") == "pernix.config"

    # Second run with identical output: dedup blocks it.
    saved2 = await generate(store, _never_cancelled)
    assert saved2 == 0
    assert len(db.list_dream_hypotheses()) == 1


def _fake_pack_items():
    from core.dream.observe import EvidenceItem

    return [
        EvidenceItem(
            ref_id="P1",
            kind="pm",
            render="[P1] post-mortem: verdict=retry",
            ref={"id": "pm1", "session_id": "sess1"},
        ),
        EvidenceItem(
            ref_id="M1",
            kind="memory",
            render="[M1] a stored lesson entry",
            ref={"file": "pernix.config", "epoch": 1000, "hash": "abcdefabcdef"},
        ),
        EvidenceItem(
            ref_id="C1",
            kind="candor",
            render="[C1] fetch_ok(*): 49% success over 562 obs",
            ref={"pred": "fetch_ok", "args": ["*"]},
        ),
        EvidenceItem(
            ref_id="C2",
            kind="candor",
            render="[C2] fetch_ok(forbes.com): 20% success over 8 obs",
            ref={"pred": "fetch_ok", "args": ["forbes.com"]},
        ),
    ]


async def test_generate_lesson_ineffective_requires_pm_ref(store, dream_on, monkeypatch):
    from core.dream.observe import EvidencePack

    pack = EvidencePack(items=_fake_pack_items(), memory_file="pernix.config")

    async def fake_build_pack(_store):
        return pack

    monkeypatch.setattr("core.dream.observe.build_pack", fake_build_pack)
    no_pm = {
        "kind": "lesson_ineffective",
        "statement": "The retry lesson is ignored by planning entirely, judging from memory alone.",
        "evidence": ["M1"],
        "confidence": 0.6,
    }
    with_pm = {
        "kind": "lesson_ineffective",
        "statement": "A recorded failure keeps recurring despite the stored lesson about summaries.",
        "evidence": ["P1", "M1"],
        "confidence": 0.6,
    }
    fake = FakeLLMClient(responses=[_resp(json.dumps([no_pm, with_pm]))])
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake)

    saved = await generate(store, _never_cancelled)
    assert saved == 1, "lesson_ineffective without a post-mortem ref is untestable and must be rejected"
    rows = db.list_dream_hypotheses()
    assert len(rows) == 1
    ev = json.loads(rows[0]["evidence_json"])
    assert any(e["type"] == "pm" and e.get("session_id") for e in ev)


async def test_generate_tool_pattern_dedups_on_candor_evidence(store, dream_on, monkeypatch):
    from core.dream.observe import EvidencePack

    # An existing hypothesis (any status) already rests on fetch_ok(*).
    db.add_dream_hypothesis(
        "tool_pattern",
        "fetch_ok success is globally degraded to about half of all attempts.",
        json.dumps([{"type": "candor", "pred": "fetch_ok", "args": ["*"], "quote": "x"}]),
    )
    pack = EvidencePack(items=_fake_pack_items(), memory_file="pernix.config")

    async def fake_build_pack(_store):
        return pack

    monkeypatch.setattr("core.dream.observe.build_pack", fake_build_pack)
    reworded_dup = {
        "kind": "tool_pattern",
        "statement": "The browse method shows markedly lower reliability than API-based retrieval overall.",
        "evidence": ["C1"],
        "confidence": 0.6,
    }
    fresh = {
        "kind": "tool_pattern",
        "statement": "Forbes fetches fail four times out of five, far below the global success rate.",
        "evidence": ["C2", "C1"],
        "confidence": 0.6,
    }
    fake = FakeLLMClient(responses=[_resp(json.dumps([reworded_dup, fresh]))])
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake)

    saved = await generate(store, _never_cancelled)
    assert saved == 1, "a paraphrase citing only already-hypothesized candor facts must be rejected"
    statements = [r["statement"] for r in db.list_dream_hypotheses()]
    assert any("Forbes" in s for s in statements)
    assert not any("browse method" in s for s in statements)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _mem_evidence(store, file_name, epoch):
    from core.memory.format import parse_entries_from_markdown

    entry = next(e for e in parse_entries_from_markdown(file_name, store.read_file(file_name)) if e.epoch == epoch)
    return {
        "type": "memory",
        "file": file_name,
        "epoch": epoch,
        "hash": content_hash(entry.content),
        "quote": entry.content[:100],
    }


async def test_validate_contradiction_holds(store, dream_on, monkeypatch):
    _seed_memory(store)
    ev = [_mem_evidence(store, "pernix.config", 1000), _mem_evidence(store, "pernix.config", 2000)]
    hid = db.add_dream_hypothesis("contradiction", "Port claims conflict between two entries.", json.dumps(ev))
    fake = FakeLLMClient(responses=[_resp('{"verdict": "holds", "note": "8090 vs 9090"}')])
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake)

    outcome, expired = await validate_one(store, db.list_dream_hypotheses(status="pending"), _never_cancelled)
    assert outcome == "validated" and expired == 0
    row = db.list_dream_hypotheses()[0]
    assert row["status"] == "validated"
    assert json.loads(row["validation_json"])["method"] == "evidence_judge"
    assert row["confidence"] >= 0.75


async def test_validate_expires_on_moved_evidence(store, dream_on, monkeypatch):
    _seed_memory(store)
    ev = [_mem_evidence(store, "pernix.config", 1000), _mem_evidence(store, "pernix.config", 2000)]
    ev[0]["hash"] = "deadbeef0000"  # entry "rewritten" since observation
    db.add_dream_hypothesis("contradiction", "Port claims conflict between two entries.", json.dumps(ev))
    fake = FakeLLMClient()
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake)

    outcome, expired = await validate_one(store, db.list_dream_hypotheses(status="pending"), _never_cancelled)
    assert outcome == "expired" and expired == 1
    assert fake.call_count == 0, "expired refs must not spend an LLM call"


async def test_validate_tool_pattern_candor_disabled_expires_after_attempts(store, dream_on, monkeypatch):
    monkeypatch.setattr(config.settings, "candor_enabled", False)
    db.add_dream_hypothesis(
        "tool_pattern",
        "browse_web fails at night according to the ledger.",
        json.dumps([{"type": "candor", "pred": "tool_ok", "args": ["browse_web"], "quote": "x"}]),
    )

    out1, _ = await validate_one(store, db.list_dream_hypotheses(status="pending"), _never_cancelled)
    assert out1 == "skipped"
    out2, expired = await validate_one(store, db.list_dream_hypotheses(status="pending"), _never_cancelled)
    assert out2 == "expired" and expired == 1


async def test_validate_expires_duplicate_evidence_and_continues(store, dream_on, monkeypatch):
    _seed_memory(store)
    dup_key_ev = json.dumps([{"type": "candor", "pred": "fetch_ok", "args": ["*"], "quote": "x"}])
    resolved_id = db.add_dream_hypothesis("tool_pattern", "fetch_ok globally degraded to half.", dup_key_ev)
    db.update_dream_hypothesis(resolved_id, status="validated")
    # Oldest pending: a paraphrase resting on the same candor fact.
    db.add_dream_hypothesis("tool_pattern", "Fetching succeeds only about half the time across domains.", dup_key_ev)
    # Newer pending: a real contradiction the judge will confirm.
    ev = [_mem_evidence(store, "pernix.config", 1000), _mem_evidence(store, "pernix.config", 2000)]
    db.add_dream_hypothesis("contradiction", "Port claims conflict between two entries.", json.dumps(ev))

    fake = FakeLLMClient(responses=[_resp('{"verdict": "holds", "note": "8090 vs 9090"}')])
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake)

    pending = db.list_dream_hypotheses(status="pending", oldest_first=True)
    outcome, expired = await validate_one(store, pending, _never_cancelled)
    # The duplicate expires without consuming the cycle's validation slot;
    # the pass continues and lands the real verdict.
    assert outcome == "validated" and expired == 1
    rows = db.list_dream_hypotheses()
    dup = next(r for r in rows if "half the time" in r["statement"])
    assert dup["status"] == "expired"
    assert json.loads(dup["validation_json"])["method"] == "duplicate_evidence"
    assert next(r for r in rows if r["kind"] == "contradiction")["status"] == "validated"


async def test_validate_tool_pattern_any_recovered_ref_refutes(store, dream_on, monkeypatch):
    monkeypatch.setattr(config.settings, "candor_enabled", True)

    class FakeBridge:
        async def predict(self, pred, args):
            if args == ["forbes.com"]:
                return {"p": 0.20, "observations": 8}
            return {"p": 0.80, "observations": 50}

    monkeypatch.setattr("core.extensions.candor.bridge.get_candor_bridge", lambda: FakeBridge())
    db.add_dream_hypothesis(
        "tool_pattern",
        "Fetches degrade globally and especially on forbes.com lately.",
        json.dumps(
            [
                {"type": "candor", "pred": "fetch_ok", "args": ["*"], "quote": "x"},
                {"type": "candor", "pred": "fetch_ok", "args": ["forbes.com"], "quote": "y"},
            ]
        ),
    )
    outcome, _ = await validate_one(store, db.list_dream_hypotheses(status="pending"), _never_cancelled)
    # The wildcard fact recovered: the claim as stated no longer holds,
    # even though the forbes-specific ref is still degraded.
    assert outcome == "refuted"
    note = json.loads(db.list_dream_hypotheses()[0]["validation_json"])["note"]
    assert "degradation gone" in note and "fetch_ok(*)" in note


async def test_replay_budget_zero_skips_lesson_hypotheses(store, dream_on, monkeypatch):
    monkeypatch.setattr(config.settings, "dream_validation_replays_per_day", 0)
    assert not replay_budget_left()
    db.add_dream_hypothesis(
        "lesson_ineffective",
        "The retry lesson does not change planning at all.",
        json.dumps([{"type": "pm", "id": "x", "session_id": "y"}]),
    )
    outcome, expired = await validate_one(store, db.list_dream_hypotheses(status="pending"), _never_cancelled)
    assert outcome is None and expired == 0  # nothing actionable — caller falls through to generation


# ---------------------------------------------------------------------------
# Driver round-robin + flag off
# ---------------------------------------------------------------------------


async def test_run_step_disabled_is_inert(store):
    result = await run_step(_never_cancelled)
    assert result == {
        "dream_hypotheses": 0,
        "dream_validated": 0,
        "dream_refuted": 0,
        "dream_expired": 0,
        "dream_reports": 0,
    }
    assert db.get_snooze_state("dream_last_action") is None


async def test_run_step_alternates_validate_and_generate(store, dream_on, monkeypatch):
    _seed_memory(store)
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: store)
    ev = [_mem_evidence(store, "pernix.config", 1000), _mem_evidence(store, "pernix.config", 2000)]
    db.add_dream_hypothesis("contradiction", "Port claims conflict between two entries.", json.dumps(ev))
    fake = FakeLLMClient(responses=[_resp('{"verdict": "does_not_hold", "note": "same port really"}'), _resp("[]")])
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake)

    r1 = await run_step(_never_cancelled)  # last_action empty -> validate wins
    assert r1["dream_refuted"] == 1
    assert db.get_snooze_state("dream_last_action") == "validate"

    r2 = await run_step(_never_cancelled)  # round-robin -> generate
    assert db.get_snooze_state("dream_last_action") == "generate"
    assert r2["dream_validated"] == 0 and r2["dream_refuted"] == 0


async def test_open_questions_cannot_starve_the_validation_window(store, dream_on, monkeypatch):
    """Regression: the kind exclusion must window inside the query.

    open_question rows are never validated and never expire. When the
    exclusion ran as a comprehension over an already-LIMITed result, enough
    of them filled the window and the validator saw an empty queue — so it
    generated forever while real candidates sat unreachable behind them.
    """
    _seed_memory(store)
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: store)

    # One real candidate, buried behind a window's worth of open questions.
    ev = [_mem_evidence(store, "pernix.config", 1000), _mem_evidence(store, "pernix.config", 2000)]
    db.add_dream_hypothesis("contradiction", "Port claims conflict between two entries.", json.dumps(ev))
    for i in range(250):
        db.add_dream_hypothesis("open_question", f"What is unmeasured about subsystem {i}?", "[]")

    # The query must reach past every open_question to the real candidate.
    window = db.list_dream_hypotheses(status="pending", limit=200, oldest_first=True, exclude_kinds=("open_question",))
    assert [r["kind"] for r in window] == ["contradiction"]

    fake = FakeLLMClient(responses=[_resp('{"verdict": "holds", "note": "8090 vs 9090"}')])
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake)
    result = await run_step(_never_cancelled)
    assert result["dream_validated"] == 1
    assert db.get_snooze_state("dream_last_action") == "validate"


def test_archive_stale_open_questions_is_scoped_and_terminal(store):
    """The TTL sweep retires only aged, pending rows of the named kind."""
    from db.database import connect_sessions

    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    fresh_oq = db.add_dream_hypothesis("open_question", "Recently raised open question.", "[]")
    aged_oq = db.add_dream_hypothesis("open_question", "Long-stale open question.", "[]")
    aged_other = db.add_dream_hypothesis("contradiction", "An aged contradiction row.", "[]")
    with connect_sessions() as conn:
        conn.execute("UPDATE dream_hypotheses SET created_at = ? WHERE id IN (?, ?)", (old, aged_oq, aged_other))

    assert db.archive_stale_dream_hypotheses("open_question", 14) == 1

    by_id = {r["id"]: r for r in db.list_dream_hypotheses(limit=50)}
    assert by_id[aged_oq]["status"] == "archived"  # aged + right kind
    assert by_id[fresh_oq]["status"] == "pending"  # inside the TTL
    assert by_id[aged_other]["status"] == "pending"  # has a validation path
    # Archived rows leave the validator's queue for good.
    assert db.count_dream_hypotheses(status="pending", kind="open_question") == 1


async def test_run_step_backpressure_pauses_generation(store, dream_on, monkeypatch):
    _seed_memory(store)
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: store)
    monkeypatch.setattr(config.settings, "dream_max_pending", 1)
    # Three stale-evidence rows: instant expiries, no LLM involved.
    ev = [_mem_evidence(store, "pernix.config", 1000), _mem_evidence(store, "pernix.config", 2000)]
    for i in range(3):
        bad = [dict(e) for e in ev]
        bad[0]["hash"] = f"deadbeef{i:04d}"
        db.add_dream_hypothesis(
            "contradiction", f"Conflicting port claims variant number {i} between entries.", json.dumps(bad)
        )
    db.set_snooze_state("dream_last_action", "validate")  # round-robin alone would generate next

    fake = FakeLLMClient()
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake)
    result = await run_step(_never_cancelled)
    # Above the cap the step validates regardless of round-robin, and the
    # expiry pass drains all three in one cycle; generation stays paused.
    assert result["dream_expired"] == 3
    assert result["dream_hypotheses"] == 0
    assert fake.call_count == 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


async def test_report_first_call_starts_clock_then_writes(store, dream_on):
    assert await maybe_write_report() is None  # starts the clock
    assert db.get_snooze_state("dream_last_report")

    # Backdate the clock and add material.
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    db.set_snooze_state("dream_last_report", old)
    db.add_dream_hypothesis(
        "open_question", "Is the staging box still used for anything at all?", json.dumps([{"type": "pm", "id": "x"}])
    )

    rel = await maybe_write_report()
    assert rel and rel.startswith("dreams/DREAM-")
    written = Path(config.settings.workspace_dir) / rel
    assert written.exists()
    assert "staging box" in written.read_text()
    assert len(db.list_dream_reports()) == 1
    assert db.get_snooze_state("dream_last_report") > old

    # Quiet period: no double write.
    db.set_snooze_state("dream_last_report", old)
    # all rows now older than the new backdated window? rows updated recently -> would write again;
    # instead verify the no-material path with a fresh window
    db.set_snooze_state("dream_last_report", datetime.now(timezone.utc).isoformat())
    assert await maybe_write_report() is None


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


async def test_journal_creates_day_session_and_notices(store, dream_on):
    from core.dream import journal

    await journal.append("◐ line one")
    await journal.append("💭 line two")
    sessions = [s for s in db.list_sessions(limit=20) if s.get("session_type") == "snooze"]
    assert len(sessions) == 1, "one day-keyed journal session"
    sid = sessions[0]["id"]
    msgs = db.get_messages(sid)
    assert [m["role"] for m in msgs] == ["notice", "notice"]
    assert "line two" in msgs[-1]["content"]
    # Appends bump recency: without it the journal reads as dead in the
    # session list (and misleads liveness checks) while actively written.
    assert sessions[0]["updated_at"] > sessions[0]["created_at"]
    # Reused on subsequent appends, not re-created.
    await journal.append("third")
    assert len([s for s in db.list_sessions(limit=20) if s.get("session_type") == "snooze"]) == 1


async def test_journal_inert_when_dream_disabled(store):
    from core.dream import journal

    await journal.append("should not exist")
    assert not [s for s in db.list_sessions(limit=20) if s.get("session_type") == "snooze"]


async def test_journal_invisible_to_fts_and_distill(store, dream_on):
    from core.dream import journal

    await journal.append("a unique zanzibar journal narration line for search")
    assert db.search_messages_fts("zanzibar") == [], "journal must never enter cross-session search"
    assert db.get_unreviewed_sessions(min_age_minutes=0) == [], "journal must never be a distill candidate"


async def test_journal_prune_keeps_window_and_today(store, dream_on, monkeypatch):
    from core.dream import journal

    monkeypatch.setattr(config.settings, "dream_journal_retention_days", 2)
    await journal.append("today's line")
    old_sid = db.create_session(title="Dream journal — 2020-01-01", session_type="snooze")
    with db.connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = '2020-01-02T00:00:00' WHERE id = ?", (old_sid,))
    deleted = journal.prune_old_journals_sync()
    assert deleted == 1
    remaining = [s for s in db.list_sessions(limit=50) if s.get("session_type") == "snooze"]
    assert len(remaining) == 1 and "2020" not in remaining[0]["title"]


def test_journal_listener_filters_routine_noise():
    from core.dream.journal import event_line

    # Routine ladder lines and healthy cycle completions stay out. The dream
    # step marker is written directly by the snooze cycle now (ordering),
    # so the listener must NOT also map it — that would double the line.
    assert event_line({"type": "snooze.start", "activity": "cycle"}) is None
    assert event_line({"type": "snooze.activity", "activity": "dedup", "detail": "x"}) is None
    assert event_line({"type": "snooze.activity", "activity": "dream", "detail": "Dreaming: x"}) is None
    assert event_line({"type": "snooze.done", "duration_ms": 1000, "outcome": "ran"}) is None
    # Anomalous outcomes are recorded.
    assert "yielded" in event_line({"type": "snooze.done", "duration_ms": 9000, "outcome": "yielded"})
    assert "backstop" in event_line({"type": "snooze.done", "duration_ms": 9000, "outcome": "backstop"})


async def test_chat_rejected_in_dream_journal_session():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers import chat as chat_router

    sid = db.create_session(title="Dream journal — 2026-07-31", session_type="snooze")
    app = FastAPI()
    app.include_router(chat_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/chat", json={"session_id": sid, "message": "hello?"})
        assert r.status_code == 400
        assert "read-only" in r.json()["detail"]
        r2 = await client.post("/api/chat/inject", json={"session_id": sid, "message": "context"})
        assert r2.status_code == 400


# ---------------------------------------------------------------------------
# Time-series rule (2026-08-19)
# ---------------------------------------------------------------------------


def test_prompts_carry_the_time_series_rule():
    """Dated snapshots of time-varying facts are records, not claims.

    Without this rule, every remembered market brief validated as
    contradiction/memory_stale — "S&P was X on May 6" vs "S&P was Y on May 31"
    — and 9 of the 18 findings validated on 2026-08-19 were this class, each
    one flowing through auto-approval into a weight-high corrective note in a
    memory file. The judge's old memory_stale rule ("newer evidence contradicts
    the entry") GUARANTEED a price series validates as stale.

    Pinned at both ends because generation adherence is best-effort on the
    background model; the judge is the enforcement point. This test is the
    contract that a future prompt edit cannot silently drop the rule.
    """
    from core.dream.hypothesize import DREAM_PROMPT
    from core.dream.validate import EVIDENCE_JUDGE_PROMPT

    for prompt in (DREAM_PROMPT, EVIDENCE_JUDGE_PROMPT):
        assert "time-varying" in prompt
        assert "supersede" in prompt  # supersedes / superseded
    assert "NEVER a contradiction" in DREAM_PROMPT
    # The judge must be told to refute BOTH noisy kinds, durability being the test.
    assert "refute those" in EVIDENCE_JUDGE_PROMPT
    assert "DURABLE" in EVIDENCE_JUDGE_PROMPT


async def test_validate_refutes_when_judge_does_not_hold(store, dream_on, monkeypatch):
    """does_not_hold → refuted, method recorded, and the row stays dead.

    The refute path had no direct test; it matters more now that it is the
    time-series enforcement point.
    """
    _seed_memory(store)
    ev = [_mem_evidence(store, "pernix.config", 1000), _mem_evidence(store, "pernix.config", 2000)]
    db.add_dream_hypothesis(
        "memory_stale",
        "The S&P level recorded on May 6 is stale relative to the May 31 close.",
        json.dumps(ev),
    )
    fake = FakeLLMClient(responses=[_resp('{"verdict": "does_not_hold", "note": "dated snapshots; newer supersedes"}')])
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake)

    outcome, expired = await validate_one(store, db.list_dream_hypotheses(status="pending"), _never_cancelled)
    assert outcome == "refuted" and expired == 0
    row = db.list_dream_hypotheses()[0]
    assert row["status"] == "refuted"
    assert json.loads(row["validation_json"])["method"] == "evidence_judge"


# ---------------------------------------------------------------------------
# Promotion-side queue health (2026-08-19)
# ---------------------------------------------------------------------------


def test_promotion_health_alarms_on_stalled_validated_queue():
    """The pending-queue check cannot see this stall: on 2026-08-19 the
    validator was healthy while 55 VALIDATED rows sat parked for days behind
    the proposal cap and the 10/day veto-window drain, logging one INFO line
    per cycle and alarming nowhere."""
    from core.dream import _STALL_DAYS, _check_promotion_health
    from db.database import connect_sessions

    hid = db.add_dream_hypothesis("contradiction", "Old finding parked beyond the stall line.", "[]")
    db.update_dream_hypothesis(hid, status="validated")
    # Backdate created_at past the threshold (the update stamps updated_at
    # only). Timezone-aware ISO like production stamps — sqlite's
    # datetime('now') would write a naive string the age math cannot subtract.
    from datetime import datetime, timedelta, timezone

    backdated = (datetime.now(timezone.utc) - timedelta(days=_STALL_DAYS + 1)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE dream_hypotheses SET created_at = ? WHERE id = ?", (backdated, hid))
    _check_promotion_health()
    titles = [n.get("title", "") for n in db.get_notifications()]
    assert any("not reaching promotion" in t for t in titles)


def test_promotion_health_quiet_on_fresh_validated_rows():
    hid = db.add_dream_hypothesis("contradiction", "Fresh finding, still inside the window.", "[]")
    db.update_dream_hypothesis(hid, status="validated")
    from core.dream import _check_promotion_health

    _check_promotion_health()
    titles = [n.get("title", "") for n in db.get_notifications()]
    assert not any("not reaching promotion" in t for t in titles)
