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
    payload = [{"kind": "contradiction", "statement": "Entry A and entry B disagree about the port.", "evidence": ["M1", "M2"], "confidence": 0.7}]
    assert parse_hypotheses(json.dumps(payload))[0]["kind"] == "contradiction"
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    assert len(parse_hypotheses(fenced)) == 1
    # single dict coerced to list
    assert len(parse_hypotheses(json.dumps(payload[0]))) == 1


def test_parse_hypotheses_rejects_bad_shapes():
    assert parse_hypotheses("not json") == []
    assert parse_hypotheses(json.dumps([{"kind": "bogus", "statement": "long enough statement here", "evidence": ["M1"]}])) == []
    assert parse_hypotheses(json.dumps([{"kind": "contradiction", "statement": "short", "evidence": ["M1"]}])) == []
    assert parse_hypotheses(json.dumps([{"kind": "contradiction", "statement": "a statement without any evidence refs at all", "evidence": []}])) == []
    # confidence clamped
    h = parse_hypotheses(json.dumps([{"kind": "open_question", "statement": "a perfectly reasonable open question here", "evidence": ["P1"], "confidence": 7}]))
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
    assert parse_judge("```json\n{\"verdict\": \"holds\"}\n```")["verdict"] == "holds"
    assert parse_judge("nope") is None


def test_compose_report_sections():
    rows = [
        {"kind": "contradiction", "statement": "S1 conflicts with S2.", "status": "refuted", "confidence": 0.5,
         "validation_json": json.dumps({"method": "evidence_judge", "note": "did not hold"})},
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
    store.add_entry("The API server listens on port 8090 in production deployments.", file_name="pernix.config", epoch=1000)
    store.add_entry("The API server listens on port 9090 according to the deploy script.", file_name="pernix.config", epoch=2000, skip_dedup=True)


def _seed_failed_session() -> str:
    sid = db.create_session(title="failed one")
    db.add_message(sid, "user", "Summarize the weekly report please")
    db.add_message(sid, "assistant", "I could not do that.")
    db.add_post_mortem(
        session_id=sid, attempt=1, verdict="retry", failure_cause="agent", confidence=0.9,
        reflect_model="m", reflect_latency_ms=5, scout_viability="verified", execution_mode="inline",
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
    store.add_entry("A dream-authored conclusion that must not be dreamed about again.", file_name="pernix.config", source="dream")
    pack = await build_pack(store)
    assert not [i for i in pack.items if i.kind == "memory"]


async def test_generate_saves_filters_and_advances_cursors(store, dream_on, monkeypatch):
    _seed_memory(store)
    _seed_failed_session()
    good = {"kind": "contradiction", "statement": "Memory disagrees about the API port: 8090 vs 9090 for the same server.", "evidence": ["M1", "M2"], "confidence": 0.8}
    banned = {"kind": "memory_stale", "statement": "The deploy port is not configured anywhere in settings.", "evidence": ["M1"], "confidence": 0.9}
    ghost_refs = {"kind": "tool_pattern", "statement": "A tool pattern citing evidence that was never offered to the model.", "evidence": ["C9"], "confidence": 0.9}
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


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _mem_evidence(store, file_name, epoch):
    from core.memory.format import parse_entries_from_markdown

    entry = next(e for e in parse_entries_from_markdown(file_name, store.read_file(file_name)) if e.epoch == epoch)
    return {"type": "memory", "file": file_name, "epoch": epoch, "hash": content_hash(entry.content), "quote": entry.content[:100]}


async def test_validate_contradiction_holds(store, dream_on, monkeypatch):
    _seed_memory(store)
    ev = [_mem_evidence(store, "pernix.config", 1000), _mem_evidence(store, "pernix.config", 2000)]
    hid = db.add_dream_hypothesis("contradiction", "Port claims conflict between two entries.", json.dumps(ev))
    fake = FakeLLMClient(responses=[_resp('{"verdict": "holds", "note": "8090 vs 9090"}')])
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: fake)

    outcome = await validate_one(store, db.list_dream_hypotheses(status="pending"), _never_cancelled)
    assert outcome == "validated"
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

    outcome = await validate_one(store, db.list_dream_hypotheses(status="pending"), _never_cancelled)
    assert outcome == "expired"
    assert fake.call_count == 0, "expired refs must not spend an LLM call"


async def test_validate_tool_pattern_candor_disabled_expires_after_attempts(store, dream_on, monkeypatch):
    monkeypatch.setattr(config.settings, "candor_enabled", False)
    db.add_dream_hypothesis("tool_pattern", "browse_web fails at night according to the ledger.", json.dumps([{"type": "candor", "pred": "tool_ok", "args": ["browse_web"], "quote": "x"}]))

    out1 = await validate_one(store, db.list_dream_hypotheses(status="pending"), _never_cancelled)
    assert out1 == "skipped"
    out2 = await validate_one(store, db.list_dream_hypotheses(status="pending"), _never_cancelled)
    assert out2 == "expired"


async def test_replay_budget_zero_skips_lesson_hypotheses(store, dream_on, monkeypatch):
    monkeypatch.setattr(config.settings, "dream_validation_replays_per_day", 0)
    assert not replay_budget_left()
    db.add_dream_hypothesis("lesson_ineffective", "The retry lesson does not change planning at all.", json.dumps([{"type": "pm", "id": "x", "session_id": "y"}]))
    outcome = await validate_one(store, db.list_dream_hypotheses(status="pending"), _never_cancelled)
    assert outcome is None  # nothing actionable — caller falls through to generation


# ---------------------------------------------------------------------------
# Driver round-robin + flag off
# ---------------------------------------------------------------------------


async def test_run_step_disabled_is_inert(store):
    result = await run_step(_never_cancelled)
    assert result == {"dream_hypotheses": 0, "dream_validated": 0, "dream_refuted": 0, "dream_expired": 0, "dream_reports": 0}
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


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


async def test_report_first_call_starts_clock_then_writes(store, dream_on):
    assert await maybe_write_report() is None  # starts the clock
    assert db.get_snooze_state("dream_last_report")

    # Backdate the clock and add material.
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    db.set_snooze_state("dream_last_report", old)
    db.add_dream_hypothesis("open_question", "Is the staging box still used for anything at all?", json.dumps([{"type": "pm", "id": "x"}]))

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
