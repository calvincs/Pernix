"""Reflect becomes a debrief; worker trust comes from deterministic checks.

The 2026-11-05 review (three reviewers over live box data) found the LLM
verdict's retry rarely paid (52 hand-classified retries: 42% real fixes, 58%
cosmetic/wasted/wrong; canaries 0 wins), its bells were never acted on (45 sent,
41 bulk-dismissed, 22 parse-failure noise), and plain-text replies failed to
parse on 7 of 12 parallel probes. What it is good at is the record: concrete,
evidenced lessons a future turn can use. So:

1. An LLM verdict never re-runs a turn (reflect_llm_retry off). Gates — and a
   missing named deliverable on a worker — are the only automatic retry.
2. Reflect bells are off (reflect_notify).
3. Reflect asks for JSON-constrained decoding and, in debrief mode, records
   outcome + lessons; lessons go to memory as entry_type="lesson", deduped.
4. A worker's result header is filled by core/worker_checks (no LLM).
5. Archiving an already-archived memory entry no longer stacks another tag.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from core.llm.types import ChatResponse, TokenUsage
from db import models as db
from sessions.manager import SessionManager


@pytest.fixture
def mgr(monkeypatch):
    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    return fresh


def _resp(obj) -> ChatResponse:
    return ChatResponse(
        content=json.dumps(obj),
        tool_calls=None,
        usage=TokenUsage(10, 5, 15),
        model="test",
        provider="fake",
        finish_reason="stop",
    )


_DEBRIEF = {
    "outcome": "partial",
    "summary": "Asked for an RDAP lookup; got the org but not the abuse contact.",
    "failure_cause": "agent",
    "confidence": 0.8,
    "lessons": [
        {
            "kind": "tool",
            "lesson": "http_get rejects application/rdap+json responses as non-text; use bash curl for RDAP endpoints.",
            "evidence": "http_get: content-type application/rdap+json is not text",
        },
        {"kind": "process", "lesson": "be thorough", "evidence": ""},
    ],
    "user_observations": [],
    "what_worked": "curl",
    "what_failed": "http_get",
    "unverified_claims": [],
}


async def _drain(predicate, tries=50):
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


# --- 1/3. debrief: no LLM retry, detached, lessons ---------------------------


async def test_a_worker_retry_verdict_no_longer_reruns_the_turn(mgr, mock_llm_client, monkeypatch):
    from sessions.hooks import _maybe_reflect
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.memory_recall", False)
    sid = db.create_session(title="W", session_type="worker")
    db.add_message(sid, "user", "Look up the RDAP record")
    db.add_message(sid, "assistant", "Org is Squarespace")
    db.add_message(sid, "tool", "output")
    mock_llm_client.responses = [_resp(_DEBRIEF)]
    s = AgentSession(session_id=sid, session_type="worker")

    await _maybe_reflect(sid, db.get_session(sid), session_obj=s)

    assert s.turn.reflect_retry_requested is False
    assert s.turn.reflect_count == 0
    rows = lambda: [json.loads(m["content"]) for m in db.get_messages(sid) if m["role"] == "reflect"]  # noqa: E731
    assert await _drain(lambda: rows())
    row = rows()[-1]
    assert row["reflect_mode"] == "debrief"
    assert row["outcome"] == "partial"
    # The unevidenced, generic lesson is dropped; the concrete one kept.
    assert [lesson["kind"] for lesson in row["lessons"]] == ["tool"]
    # JSON-constrained decoding was requested, with the debrief prompt.
    call = mock_llm_client.calls[-1]
    assert call["response_format"] == {"type": "json_object"}
    assert "Debrief step" in call["messages"][0]["content"]


async def test_a_failing_gate_is_still_the_retry_trigger(mgr, mock_llm_client, monkeypatch):
    from core.gates import GateResult
    from sessions.hooks import _maybe_reflect
    from sessions.state import AgentSession

    monkeypatch.setattr("config.settings.reflect_min_messages", 2)
    monkeypatch.setattr("config.settings.memory_recall", False)
    sid = db.create_session(title="W", session_type="worker")
    db.add_message(sid, "user", "Make the tests pass")
    db.add_message(sid, "assistant", "Done")
    db.add_message(sid, "tool", "output")
    mock_llm_client.responses = [_resp({**_DEBRIEF, "outcome": "done"})]
    s = AgentSession(session_id=sid, session_type="worker")
    gate = GateResult(name="tests", command="pytest", passed=False, exit_code=1)

    await _maybe_reflect(sid, db.get_session(sid), session_obj=s, gate_results=[gate])

    assert s.turn.reflect_retry_requested is True
    assert s.turn.reflect_count == 1


async def test_debrief_lessons_are_written_once(monkeypatch):
    """The duplicate test reads the lesson text alone (the evidence quote
    differs every session), and a lesson already on file is not written."""
    from core.reflect import _save_debrief_lessons

    class _Dup:
        class entry:
            file_name, epoch = "pernix.lessons_tool", 1

    class _Store:
        def __init__(self):
            self.written: list[dict] = []
            self.queried: list[str] = []

        def find_duplicate(self, content):
            self.queried.append(content)
            return _Dup() if self.written else None

        def add_entry(self, **kw):
            self.written.append(kw)
            return "Saved"

    store = _Store()
    monkeypatch.setattr("config.settings.memory_recall", True)
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: store)
    sid = db.create_session(title="P")
    lesson = {"kind": "tool", "lesson": "x" * 50, "evidence": "seen in call 7"}

    assert await _save_debrief_lessons(sid, [lesson]) == 1
    assert await _save_debrief_lessons(sid, [lesson]) == 0
    assert len(store.written) == 1
    assert store.queried == [lesson["lesson"]] * 2
    assert store.written[0]["entry_type"] == "lesson"
    assert store.written[0]["file_name"] == "pernix.lessons"
    assert "seen in call 7" in store.written[0]["content"]


def test_outcome_maps_to_the_verdict_column():
    from core.reflect import _debrief_to_data

    assert _debrief_to_data({"outcome": "done"})["verdict"] == "pass"
    assert _debrief_to_data({"outcome": "blocked"})["verdict"] == "escalate"
    assert _debrief_to_data({"outcome": "nonsense"})["verdict"] == "pass"
    assert _debrief_to_data({})["_outcome"] == "unclear"


# --- 2. bells off ------------------------------------------------------------


def test_reflect_bells_are_off_by_default(monkeypatch):
    from sessions.hooks import _broadcast_reflect_notification

    sent = []
    monkeypatch.setattr("core.notices.notify", lambda *a, **k: sent.append(a))
    _broadcast_reflect_notification("s1", {"title": "T", "session_type": "normal"}, "Needs attention", "x")
    assert sent == []


# --- JSON mode reaches the wire --------------------------------------------


async def test_openai_provider_sends_response_format(monkeypatch):
    from core.llm.providers.openai import OpenAIProvider
    from core.llm.types import ProviderConfig

    sent = {}

    class _Resp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}}

    class _Client:
        async def post(self, url, json=None, headers=None):
            sent.update(json)
            return _Resp()

    p = OpenAIProvider(ProviderConfig(name="openai", base_url="http://llm-host:8000/v1", api_key="k"))
    monkeypatch.setattr(p, "_get_client", lambda: _Client())
    await p.chat([{"role": "user", "content": "hi"}], model="m", response_format={"type": "json_object"})
    assert sent["response_format"] == {"type": "json_object"}
    sent.clear()
    await p.chat([{"role": "user", "content": "hi"}], model="m")
    assert "response_format" not in sent


# --- 4. deterministic worker checks ------------------------------------------


def test_deliverable_paths_come_from_write_verbs_only():
    from core.worker_checks import deliverable_paths

    task = (
        "Read /app/sessions/state.py and then write ONE file, worker-output/state_fields.md, "
        "in a single file_write call. Fetch https://example.com/a.html. Save the raw data to data/raw.json."
    )
    assert deliverable_paths(task) == ["worker-output/state_fields.md", "data/raw.json"]
    assert deliverable_paths("Summarize https://www.sqlite.org/wal.html in 5 bullets") == []


def test_checks_find_a_file_by_basename_and_flag_a_missing_one(tmp_path):
    from core.worker_checks import checks_row, run_checks

    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "report.md").write_text("# report")
    task = "Write report.md and save summary.json"
    checks = run_checks("w", task=task, final_reply="done", attempt_msgs=[], workspace=tmp_path)
    assert list(checks["deliverables_found"]) == ["report.md"]
    assert checks["deliverables_missing"] == ["summary.json"]
    row = checks_row(checks)
    assert row["verdict"] == "flagged" and row["reflect_mode"] == "checks"
    assert "summary.json" in row["reasoning"]


def test_nothing_to_check_is_neutral_not_a_problem(tmp_path):
    from core.extensions.orchestration import TrustState
    from core.worker_checks import checks_row, run_checks

    row = checks_row(run_checks("w", task="Explain WAL mode", final_reply="...", attempt_msgs=[], workspace=tmp_path))
    assert row["verdict"] == "unchecked"
    t = TrustState(worker_id="w" * 12, verdict="unchecked", source="checks")
    assert t.verification_note() == ""


def test_a_flagged_check_reads_as_deterministic_in_the_header():
    from core.extensions.orchestration import TrustState

    t = TrustState(worker_id="abcdef123456", verdict="flagged", reasoning="missing deliverable: x.md", source="checks")
    note = t.verification_note()
    assert note.startswith("# CHECKS FLAGGED (deterministic checks")
    assert "missing deliverable: x.md" in note


async def test_a_missing_deliverable_gets_one_retry_then_a_flag(mgr, monkeypatch, tmp_path):
    from sessions.hooks import _run_worker_checks
    from sessions.state import AgentSession

    monkeypatch.setattr("core.reflect._effective_workspace", lambda sid: (str(tmp_path), False))
    sid = db.create_session(title="W", session_type="worker")
    uid = db.add_message(sid, "user", "Write worker-output/table.md with the results")
    db.add_message(sid, "assistant", "Here is the table (inline).")
    s = AgentSession(session_id=sid, session_type="worker")
    s.current_turn_user_msg_id = uid

    gates = await _run_worker_checks(sid, s, [])
    assert [g.name for g in gates] == ["deliverable_exists"] and not gates[0].passed

    s.turn.reflect_count = 1  # the retry attempt: no second synthetic gate
    assert await _run_worker_checks(sid, s, []) == []
    rows = [json.loads(m["content"]) for m in db.get_messages(sid) if m["role"] == "reflect"]
    assert rows[-1]["verdict"] == "flagged"


def test_the_worker_grade_skips_debrief_rows():
    from core.extensions.orchestration.report import run_scoped_reflect

    sid = db.create_session(title="W", session_type="worker")
    db.add_message(sid, "reflect", json.dumps({"verdict": "pass", "reflect_mode": "checks"}))
    db.add_message(sid, "reflect", json.dumps({"verdict": "retry", "reflect_mode": "debrief"}))
    current, _stale, _seq = run_scoped_reflect(sid)
    assert current["reflect_mode"] == "checks" and current["verdict"] == "pass"


# --- 5. memory archive tags --------------------------------------------------


def test_archiving_twice_does_not_stack_tags():
    from core.memory.sweeps import _archive_entries_in_file

    store_text = {"f": "<!-- @epoch: 5 -->\n<!-- @archived: true -->\n<!-- @archived: true -->\nbody\n"}

    class _Store:
        def rewrite_file(self, name, fn):
            store_text[name] = fn(store_text[name])
            return True

    for _ in range(3):
        _archive_entries_in_file(_Store(), "f", {5})
    assert store_text["f"].count("@archived: true") == 1


def test_grounding_findings_are_recorded_but_do_not_flag(tmp_path, monkeypatch):
    """Over the box's last 150 workers the grounding check flagged 9, mostly
    false (file:line citations, an id the page did contain)."""
    from core.worker_checks import checks_row, run_checks

    monkeypatch.setattr(
        "core.reflect._grounding_check",
        lambda final, msgs, req: {
            "checked": 3,
            "ungrounded": ["a.h:1-9", "b.h:2", "c.h:3"],
            "unverified_rows": ["x ↔ y"],
        },
    )
    checks = run_checks("w", task="Explain it", final_reply="see a.h:1-9", attempt_msgs=[], workspace=tmp_path)
    assert checks["ungrounded"] and checks["unverified_rows"]
    assert checks["flags"] == []
    assert checks_row(checks)["verdict"] == "unchecked"


def test_a_lesson_must_quote_friction():
    from core.reflect import _clean_lessons

    good = {"kind": "tool", "lesson": "x" * 50, "evidence": "content-type application/rdap+json is not text"}
    fine = {"kind": "tool", "lesson": "y" * 50, "evidence": "http_get returned 17364 chars of full page HTML"}
    assert _clean_lessons([good, fine]) == [good]


def test_deliverable_under_an_output_heading_is_found():
    """Field case afa58208c13a: the path sat on its own line under a heading."""
    from core.worker_checks import deliverable_paths

    task = "Then write EXACTLY 5 bullets.\n\nOutput file requirements (strict):\n- Path: /app/docs/isolation_summary.md (exact)"
    assert deliverable_paths(task) == ["/app/docs/isolation_summary.md"]
    assert deliverable_paths("Audit core/memory/store.py and report what you find.") == []
    assert deliverable_paths("Read from data/in.csv and write the result to out/result.csv") == ["out/result.csv"]
