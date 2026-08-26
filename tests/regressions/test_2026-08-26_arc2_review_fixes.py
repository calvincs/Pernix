"""ARC-2 campaign review fixes (sessions c00f6c4db9ff / 4846926f1a2b).

Five field failures from one night of ARC-AGI-2 work:
- the per-session LLM time budget error triggered a MODEL failover, whose own
  quota-403 then masked the budget error and hard-killed the turn (3x);
- a user cancel tore down an in-flight rlm_process and its 88 iterations were
  reported as "failed: Broken pipe" and never reached the agent;
- the workspace-state scout glob required the *_STATUS.md prefix spelling and
  missed a plain arc2/STATUS.md checkpoint;
- ~30 consecutive falsified-fit rounds inside one hypothesis class raised no
  hint (Signal 12 only watches file re-reads);
- a 1%-grounded distill candidate saved as "Solved ..." at @weight:high and
  was recalled as authoritative (the unverified-distill tag never renders).
"""

from __future__ import annotations

import re

import pytest

from core.llm.types import StreamEvent, StreamEventType

# ===========================================================================
# Stream ladder: budget exhaustion must not fail over; quota-capped fallback
# is skipped instead of paying one doomed request per turn.
# ===========================================================================


class _FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def has_capacity(self, model=""):
        return True

    def resolve_provider(self, model=""):
        return "ollama"

    def chat_stream(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        events = self.script.pop(0)

        async def _gen():
            for e in events:
                yield e

        return _gen()


async def _run_ladder(client, sid, **overrides):
    from core.llm.stream_ladder import stream_with_failover

    kwargs = dict(
        client=client,
        session_id=sid,
        emit=lambda e: None,
        messages=[{"role": "user", "content": "hi"}],
        base_messages=[{"role": "user", "content": "hi"}],
        static_prefix_chars=0,
        tools=None,
        model="test/model",
        max_output_cap=0,
        goal_id=None,
        sched_created_at=0.0,
        sched_priority=0,
    )
    kwargs.update(overrides)
    return await stream_with_failover(**kwargs)


@pytest.fixture(autouse=True)
def _fresh_quota_registry(monkeypatch):
    monkeypatch.setattr("core.llm.stream_ladder._quota_block_until", {})


BUDGET_ERR = "Session abcdef123456 has exceeded the 1800s LLM time limit"
QUOTA_ERR = 'OpenRouter 403: {"error":{"message":"Key limit exceeded (daily limit)","code":403}}'


async def test_budget_exhaustion_does_not_fail_over(monkeypatch):
    """The fallback cannot buy time on a spent clock — and its own failure
    used to REPLACE the budget error, so the soft-land never ran."""
    from db import models as db

    monkeypatch.setattr("config.settings.fallback_model", "other/model")
    sid = db.create_session(title="budget-no-failover")
    client = _FakeClient([[StreamEvent(type=StreamEventType.ERROR, error=BUDGET_ERR)]])
    out = await _run_ladder(client, sid)
    assert out.error == BUDGET_ERR, "budget error must propagate unmasked"
    assert not out.tried_fallback
    assert len(client.calls) == 1, "no retry, no fallback attempt"


async def test_quota_capped_fallback_is_skipped_within_cooldown(monkeypatch):
    from db import models as db

    monkeypatch.setattr("config.settings.fallback_model", "other/model")
    sid = db.create_session(title="quota-breaker")

    # Run 1: primary dies (non-retryable), fallback dies on its daily cap.
    client = _FakeClient(
        [
            [StreamEvent(type=StreamEventType.ERROR, error="401 Unauthorized")],
            [StreamEvent(type=StreamEventType.ERROR, error=QUOTA_ERR)],
        ]
    )
    out = await _run_ladder(client, sid)
    assert out.tried_fallback and out.error == QUOTA_ERR

    # Run 2 (same process, cooldown active): fallback is NOT attempted and
    # the ORIGINAL error survives instead of being masked by another 403.
    client2 = _FakeClient([[StreamEvent(type=StreamEventType.ERROR, error="401 Unauthorized")]])
    out2 = await _run_ladder(client2, sid)
    assert out2.error == "401 Unauthorized"
    assert not out2.tried_fallback
    assert len(client2.calls) == 1


async def test_quota_cooldown_expires(monkeypatch):
    from core.llm import stream_ladder as sl

    sl._note_quota_block("m/x")
    assert sl._quota_block_remaining("m/x") > 0
    sl._quota_block_until["m/x"] = 0.0  # deadline in the past
    assert sl._quota_block_remaining("m/x") == 0.0
    assert "m/x" not in sl._quota_block_until, "expired entries self-clean"


# ===========================================================================
# RLM engine: child death during an active cancel is a cancel, not a failure.
# ===========================================================================


def test_run_error_status_during_cancel_is_cancelled():
    from core.extensions.rlm.engine import _status_for_run_error

    assert _status_for_run_error(None) == "failed"
    assert _status_for_run_error(lambda: False) == "failed"
    assert _status_for_run_error(lambda: True) == "cancelled"

    def _boom():
        raise RuntimeError("cancel probe died")

    assert _status_for_run_error(_boom) == "failed", "a broken probe fails safe"


# ===========================================================================
# Orphaned-run surfacing: terminal depth-0 non-completed runs whose outcome
# never reached a transcript are query-able exactly once.
# ===========================================================================


def _insert_rlm_run(run_id, session_id, *, status, depth=0, finished=True, surfaced=False):
    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute(
            """INSERT INTO rlm_runs
               (run_id, session_id, depth, status, run_dir, created_at,
                finished_at, surfaced_at, iterations, subcalls, answer_preview)
               VALUES (?, ?, ?, ?, ?, '2026-08-26T00:00:00+00:00',
                       ?, ?, 88, 4, 'partial cursor model')""",
            (
                run_id,
                session_id,
                depth,
                status,
                f"rlm/{run_id}",
                "2026-08-26T01:00:00+00:00" if finished else None,
                "2026-08-26T01:00:00+00:00" if surfaced else None,
            ),
        )


def test_orphaned_rlm_runs_surface_once():
    from db import models as db

    sid = db.create_session(title="orphan-surfacing")
    _insert_rlm_run("aaaa0001", sid, status="cancelled")  # the orphan
    _insert_rlm_run("aaaa0002", sid, status="completed")  # returned via tool
    _insert_rlm_run("aaaa0003", sid, status="failed", depth=1)  # nested
    _insert_rlm_run("aaaa0004", sid, status="running", finished=False)  # live
    _insert_rlm_run("aaaa0005", sid, status="failed", surfaced=True)  # seen

    orphans = db.get_unsurfaced_rlm_runs(sid)
    assert [r["run_id"] for r in orphans] == ["aaaa0001"]
    assert orphans[0]["answer_preview"] == "partial cursor model"

    db.mark_rlm_run_surfaced("aaaa0001")
    assert db.get_unsurfaced_rlm_runs(sid) == []


# ===========================================================================
# Scout workspace-state glob: plain STATUS.md joins the *_STATUS.md spelling.
# ===========================================================================


def test_workspace_state_matches_plain_and_prefixed_spellings(tmp_path):
    from core.scout.runner import gather_workspace_state

    assert gather_workspace_state(tmp_path) is None

    (tmp_path / "vc33_STATUS.md").write_text("prefixed")
    sub = tmp_path / "arc2"
    sub.mkdir()
    (sub / "STATUS.md").write_text("plain, one level down")
    (sub / "NOTES.md").write_text("plain notes")

    block = gather_workspace_state(tmp_path)
    assert block is not None and "WORKSPACE STATE" in block
    assert "vc33_STATUS.md" in block
    assert "arc2/STATUS.md" in block, "the ARC-2 spelling the old glob missed"
    assert "arc2/NOTES.md" in block


# ===========================================================================
# Signal 13: hypothesis grind — consecutive falsified-fit compute results
# queue a one-time class-escalation hint.
# ===========================================================================


class _Registry:
    def exists(self, name):
        return True


FALSIFYING = "train 0: no match\ntrain 1: no formula\ntrain 2: FAIL"


def test_hypothesis_grind_hint_fires_once():
    from core.agent import StuckDetector

    sd = StuckDetector()
    for _ in range(8):
        sd.observe_result("repl", {}, FALSIFYING, False)
    sd.evaluate("", None, {}, _Registry())
    assert any("CLASS of hypothesis" in h for h in sd.pending_hints)

    sd.pending_hints.clear()
    for _ in range(5):
        sd.observe_result("repl", {}, FALSIFYING, False)
    sd.evaluate("", None, {}, _Registry())
    assert sd.pending_hints == [], "the hint is one-time per turn"


def test_hypothesis_grind_streak_resets_on_clean_result():
    from core.agent import StuckDetector

    sd = StuckDetector()
    for _ in range(7):
        sd.observe_result("bash", {}, FALSIFYING, False)
    sd.observe_result("repl", {}, "formula verified: 0 diffs on all trains", False)
    assert sd.falsified_fit_streak == 0
    sd.evaluate("", None, {}, _Registry())
    assert sd.pending_hints == []


def test_hypothesis_grind_errors_do_not_reset():
    from core.agent import StuckDetector

    sd = StuckDetector()
    for _ in range(4):
        sd.observe_result("repl", {}, FALSIFYING, False)
    sd.observe_result("repl", {}, "Traceback (most recent call last): KeyError", True)
    assert sd.falsified_fit_streak == 4, "a traceback mid-grind is not progress"


# ===========================================================================
# Distill grounding guard: the claim TEXT carries the verification status and
# high weight is clamped — tags alone never reached the reader.
# ===========================================================================


def test_low_grounding_claim_is_rewritten_and_declassed(monkeypatch):
    from core.memory import distill

    monkeypatch.setattr(distill, "_ENUMERATION_RE", re.compile("."))
    monkeypatch.setattr(distill, "_trigram_grounding", lambda c, t: 0.01)
    entry = {"weight": "high"}
    content, tags = distill._apply_grounding_guard("Solved ARC2 136b0064. Logic: ...", "arc", entry, "transcript")
    assert content.startswith("UNVERIFIED (distilled at 1% verbatim grounding")
    assert "Solved ARC2 136b0064" in content
    assert "unverified-distill" in tags
    assert entry["weight"] == "normal"


def test_well_grounded_claim_is_untouched(monkeypatch):
    from core.memory import distill

    monkeypatch.setattr(distill, "_ENUMERATION_RE", re.compile("."))
    monkeypatch.setattr(distill, "_trigram_grounding", lambda c, t: 0.9)
    entry = {"weight": "high"}
    content, tags = distill._apply_grounding_guard("Engine-verified win on ar25.", "arc", entry, "t")
    assert content == "Engine-verified win on ar25."
    assert tags == "arc"
    assert entry["weight"] == "high"


# ===========================================================================
# Recall header: the unverified-distill tag renders in the line the model reads.
# ===========================================================================


def test_recall_header_marks_unverified_distill():
    from core.memory.format import MemoryEntry
    from core.memory.search import SearchResult, format_result_line

    e = MemoryEntry(
        file_name="pernix.findings",
        content="UNVERIFIED (...): claim",
        epoch=1787694955,
        entry_type="skill",
        tags=["arc", "unverified-distill"],
        source="distill",
    )
    line = format_result_line(SearchResult(entry=e, score=1.0, source="bm25"))
    assert "UNVERIFIED-DISTILL" in line

    e2 = MemoryEntry(file_name="pernix.findings", content="fact", epoch=1, tags=["arc"], source="user")
    assert "UNVERIFIED-DISTILL" not in format_result_line(SearchResult(entry=e2, score=1.0, source="bm25"))
