"""Regression tests for the 2026-09-04 trust-loop hardening, W6.

Trial arms are the only part of the loop that measures an adaptation instead
of assuming it, so what earns coverage here is everything that could quietly
turn the measurement back into an assumption:

* the coin — same turn and entry always on the same side, and the sides
  roughly even over many turns, because a biased coin is a biased experiment;
* the two prompts of one turn agreeing about it (a split decision would put
  the entry in the scout's plan and out of the agent's rules, and neither arm
  would mean anything);
* no trial rendering at all where no post-mortem can attribute it;
* the treatment record matching what the turn actually rendered, cap drops
  included, and the deferred grade using the SNAPSHOT's turn rather than the
  live session's, which has already moved on;
* outcome precedence (the user's thumb over their next message over the
  grader) and one record per turn, not per attempt;
* every branch of the sweep, including the TTL promotion that has to say
  `unproven` out loud;
* and the flag off leaving auto-applied entries exactly where they were.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.llm.types import ChatResponse, TokenUsage
from db import models as db
from db.database import connect_sessions

# ---------------------------------------------------------------------------
# Fixtures and factories
# ---------------------------------------------------------------------------


@pytest.fixture
def trial_mode(monkeypatch):
    """Adaptive on, trial arms on, small arms so fixtures stay readable."""
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_auto_apply", True)
    monkeypatch.setattr("config.settings.adaptive_trial_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_trial_min_arm", 10)
    monkeypatch.setattr("config.settings.adaptive_trial_ttl_days", 28)


def _entry(entry_id: str, kind: str = "policy", status: str = "trial", age_days: float = 1.0, **over) -> dict:
    from datetime import datetime, timedelta, timezone

    created = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    row = {
        "id": entry_id,
        "kind": kind,
        "scope": "global",
        "title": entry_id.replace("-", " "),
        "content": f"Always {entry_id}.",
        "risk": "low",
        "version": 1,
        "status": status,
        "source": "dream",
        "created_at": created,
        "updated_at": created,
    }
    row.update(over)
    db.adaptive_put_entry(row)
    return row


def _coin(key: str, entry_id: str) -> bool:
    """The rule as documented, computed independently of the module."""
    return int(hashlib.sha1((key + entry_id).encode()).hexdigest()[:8], 16) % 2 == 0


def _key_where(entry_id: str, rendered: bool, prefix: str = "s") -> str:
    """The first turn key that puts this entry in the arm we want."""
    for i in range(500):
        key = f"{prefix}:{i}"
        if _coin(key, entry_id) is rendered:
            return key
    raise AssertionError(f"no turn key found for {entry_id}")


def _pm(session_id: str, turn: int, verdict: str, *, rendered=(), held_out=(), **payload_over) -> str:
    """One graded turn, with the trial arms it carried."""
    payload = {"turn_user_msg_id": turn, "rendered_entries": list(rendered), "held_out_entries": list(held_out)}
    payload.update(payload_over)
    return db.add_post_mortem(
        session_id=session_id,
        attempt=1,
        verdict=verdict,
        failure_cause="none" if verdict == "pass" else "agent",
        confidence=0.9,
        reflect_model="m",
        reflect_latency_ms=1,
        scout_viability=None,
        execution_mode=None,
        payload_json=json.dumps(payload),
    )


def _signal(pm_id: str, signal: str) -> None:
    with connect_sessions() as conn:
        conn.execute("UPDATE post_mortems SET user_signal = ? WHERE id = ?", (signal, pm_id))


def _arms(session_id: str, entry_id: str, treated: tuple[int, int], control: tuple[int, int]) -> None:
    """treated=(successes, n), control=(successes, n) as graded turns."""
    turn = 0
    for successes, n, arm in ((treated[0], treated[1], "t"), (control[0], control[1], "c")):
        for i in range(n):
            turn += 1
            _pm(
                session_id,
                turn,
                "pass" if i < successes else "retry",
                rendered=[entry_id] if arm == "t" else [],
                held_out=[] if arm == "t" else [entry_id],
            )


# ---------------------------------------------------------------------------
# The coin
# ---------------------------------------------------------------------------


def test_the_coin_is_the_documented_hash():
    """Pinned to the rule itself, not to this implementation of it: the arm a
    turn is in must survive a rewrite of the module."""
    from core.adaptive.trial import renders_this_turn

    for key in ("sess-a:1", "sess-a:2", "sess-b:17"):
        for entry_id in ("be-terse", "use-ripgrep", "policy-x"):
            assert renders_this_turn(key, entry_id) is _coin(key, entry_id)


def test_the_same_turn_and_entry_always_land_on_the_same_side():
    from core.adaptive.trial import renders_this_turn

    first = renders_this_turn("sess:7", "be-terse")
    assert all(renders_this_turn("sess:7", "be-terse") is first for _ in range(50))
    # And the next turn is an independent draw, not a repeat of this one.
    assert any(renders_this_turn(f"sess:{i}", "be-terse") is not first for i in range(1, 40))


def test_the_coin_splits_the_turns_roughly_in_half():
    from core.adaptive.trial import renders_this_turn

    rendered = sum(1 for i in range(200) if renders_this_turn(f"sess:{i}", "be-terse"))
    assert 70 <= rendered <= 130, f"{rendered}/200 is not a fair coin"


def test_two_entries_do_not_share_one_turns_fate():
    """Per-entry randomisation: one turn can treat one entry and hold another,
    or every trial would really be a single trial of "the whole store"."""
    from core.adaptive.trial import renders_this_turn

    disagree = sum(
        1 for i in range(200) if renders_this_turn(f"s:{i}", "aaa") is not renders_this_turn(f"s:{i}", "bbb")
    )
    assert 70 <= disagree <= 130


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_both_prompts_of_a_turn_agree_on_the_same_half(trial_mode):
    """The scout prompt and the compiled prompt must never disagree about a
    trial entry: an entry in the plan but not in the rules is a treatment
    nobody administered."""
    from core.adaptive.render import build_adaptive_block, build_routing_hints_block

    _entry("trial-policy", kind="policy")
    _entry("trial-hint", kind="routing_hint")

    for i in range(40):
        key = f"sess:{i}"
        assert ("[trial-policy]" in build_adaptive_block("sess", key)) is _coin(key, "trial-policy")
        assert ("[trial-hint]" in build_routing_hints_block("sess", key)) is _coin(key, "trial-hint")


def test_no_turn_key_renders_no_trial_entry(trial_mode):
    """A worker build, a script, the mirror: nothing there will produce a
    post-mortem, so a trial entry rendered into it is an unmeasured effect.
    Active entries are untouched — the flag-off output byte for byte."""
    from core.adaptive.render import build_adaptive_block, build_routing_hints_block

    _entry("trial-policy", kind="policy")
    _entry("settled-policy", kind="policy", status="active")
    _entry("trial-hint", kind="routing_hint")
    _entry("settled-hint", kind="routing_hint", status="active")

    block, hints = build_adaptive_block("sess"), build_routing_hints_block("sess")
    assert "[settled-policy]" in block and "[trial-policy]" not in block
    assert "[settled-hint]" in hints and "[trial-hint]" not in hints


def test_a_held_out_entry_does_not_hold_a_capped_slot(trial_mode, monkeypatch):
    """The manipulation is absence. A held-out entry that still consumed one
    of the twelve policy slots would take the space its absence was supposed
    to free, and the control arm would measure a shorter prompt instead of a
    prompt without this rule. The trial entry here outranks the active one, so
    it wins the single slot whenever it is still in the running."""
    from core.adaptive.render import build_adaptive_block

    monkeypatch.setattr("core.adaptive.render._MAX_POLICIES", 1)
    _entry("outranked", kind="policy", status="active", source="dream")
    _entry("preferred", kind="policy", source="refine")

    treated = build_adaptive_block("sess", _key_where("preferred", rendered=True))
    assert "[preferred]" in treated and "[outranked]" not in treated

    control = build_adaptive_block("sess", _key_where("preferred", rendered=False))
    assert "[outranked]" in control and "[preferred]" not in control


def test_the_mirror_marks_a_trial_entry(trial_mode, tmp_path, monkeypatch):
    from core.adaptive.render import render_mirror

    monkeypatch.setattr("core.adaptive.render.MIRROR_PATH", tmp_path / "ADAPTIVE.md")
    _entry("trial-policy", kind="policy")
    _entry("settled-policy", kind="policy", status="active")

    render_mirror()
    text = (tmp_path / "ADAPTIVE.md").read_text()
    assert "trial policy [trial]" in text
    assert "settled policy\n" in text  # active entries carry no marker


# ---------------------------------------------------------------------------
# The treatment record on the grade
# ---------------------------------------------------------------------------


def _verdict(**overrides) -> ChatResponse:
    payload = {"verdict": "pass", "reasoning": "Task completed", "failure_cause": "none"}
    payload.update(overrides)
    return ChatResponse(
        content=json.dumps(payload),
        tool_calls=None,
        usage=TokenUsage(10, 5, 15),
        model="test",
        provider="fake",
        finish_reason="stop",
    )


def _turn() -> tuple[str, int, int]:
    sid = db.create_session(title="Trial arms")
    uid = db.add_message(sid, "user", "Fix the login bug")
    meta = json.dumps({"parent_user_msg_id": uid})
    db.add_message(sid, "assistant", "Fixed it in auth.py", metadata=meta)
    last = db.add_message(sid, "tool", "file written", metadata=meta)
    return sid, uid, last


async def _graded(sid: str, uid: int, last: int, snapshot_turn: int = 4, live_turn: int = 5) -> dict:
    """Run the deferred grade the way hooks does and return the post-mortem."""
    from sessions.hooks import _deferred_reflect_task, _DeferredGrade
    from sessions.state import AgentSession

    session_obj = AgentSession(session_id=sid)
    session_obj._deferred_reflect_seq = 1
    session_obj._turn_id = live_turn  # turn N+1 is already running
    snap = _DeferredGrade(
        session_id=sid, ticket=1, turn_id=snapshot_turn, turn_user_msg_id=uid, attempt=1, turn_last_msg_id=last
    )
    await _deferred_reflect_task(session_obj, snap)
    rows = db.list_post_mortems(session_id=sid)
    assert rows, "the turn was never graded"
    return json.loads(rows[0]["payload_json"])


@pytest.fixture
def graded_now(monkeypatch):
    monkeypatch.setattr("config.settings.reflect_defer_idle_s", 0)
    monkeypatch.setattr("config.settings.reflect_next_turn_grading", True)
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: None)


async def test_the_grade_records_the_arms_of_the_turn_it_graded(mock_llm_client, graded_now, trial_mode):
    """Both halves, and from the SNAPSHOT's turn: with next-turn grading this
    runs while turn N+1 is in flight, and the live session's key has moved."""
    _entry("trial-policy", kind="policy")
    _entry("trial-hint", kind="routing_hint")
    _entry("settled-policy", kind="policy", status="active")
    sid, uid, last = _turn()
    mock_llm_client.responses = [_verdict()]

    payload = await _graded(sid, uid, last, snapshot_turn=4, live_turn=5)

    key = f"{sid}:4"
    expected_rendered = sorted(e for e in ("trial-policy", "trial-hint") if _coin(key, e))
    expected_held = sorted(e for e in ("trial-policy", "trial-hint") if not _coin(key, e))
    assert payload["rendered_entries"] == expected_rendered
    assert payload["held_out_entries"] == expected_held
    # Active entries are in every prompt, so they are not an experiment.
    assert "settled-policy" not in payload["rendered_entries"] + payload["held_out_entries"]


async def test_a_turn_with_nothing_on_trial_records_no_arms(mock_llm_client, graded_now, trial_mode):
    """Flag on, nothing trialled: the payload is the one it always was."""
    _entry("settled-policy", kind="policy", status="active")
    sid, uid, last = _turn()
    mock_llm_client.responses = [_verdict()]

    payload = await _graded(sid, uid, last)

    assert "rendered_entries" not in payload and "held_out_entries" not in payload


async def test_the_evidence_only_names_policies_the_turn_rendered(mock_llm_client, graded_now, trial_mode):
    """A grader offered the id of a rule the agent never saw can cite it, and
    the citation attributes an outcome to prompt text that was not there."""
    _entry("trial-policy", kind="policy")
    sid, uid, last = _turn()
    mock_llm_client.responses = [_verdict()]

    payload = await _graded(sid, uid, last, snapshot_turn=4)
    evidence = mock_llm_client.calls[-1]["messages"][1]["content"]

    assert ("[trial-policy]" in evidence) is _coin(f"{sid}:4", "trial-policy")
    assert ("[trial-policy]" in evidence) is ("trial-policy" in payload["rendered_entries"])


# ---------------------------------------------------------------------------
# Outcome precedence and per-turn counting
# ---------------------------------------------------------------------------


def test_a_thumb_outranks_the_graders_verdict(trial_mode):
    from core.adaptive.trial import entry_stats

    _entry("pol")
    sid = db.create_session(title="precedence")
    _signal(_pm(sid, 1, "retry", rendered=["pol"]), "up")
    _signal(_pm(sid, 2, "pass", held_out=["pol"]), "down")

    stats = entry_stats("pol")
    assert stats["treated"] == {"n": 1, "successes": 1}
    assert stats["control"] == {"n": 1, "successes": 0}


def test_a_correction_outranks_the_graders_verdict(trial_mode):
    from core.adaptive.trial import entry_stats

    _entry("pol")
    sid = db.create_session(title="precedence")
    _pm(sid, 1, "pass", rendered=["pol"], next_msg_correction=True)
    _pm(sid, 2, "pass", held_out=["pol"], next_msg_correction=False)

    stats = entry_stats("pol")
    assert stats["treated"] == {"n": 1, "successes": 0}
    assert stats["control"] == {"n": 1, "successes": 1}


def test_a_thumb_outranks_a_correction(trial_mode):
    """The full ladder: user > next_turn > llm. A thumbs-up on a turn whose
    next message read as a correction is still a thumbs-up."""
    from core.adaptive.trial import entry_stats

    _entry("pol")
    sid = db.create_session(title="precedence")
    _signal(_pm(sid, 1, "retry", rendered=["pol"], next_msg_correction=True), "up")

    assert entry_stats("pol")["treated"] == {"n": 1, "successes": 1}


def test_a_retried_turn_counts_once(trial_mode):
    """post_mortems are per attempt. Counting all of them would weight the
    turns that went badly enough to be retried twice over."""
    from core.adaptive.trial import entry_stats

    _entry("pol")
    sid = db.create_session(title="attempts")
    _pm(sid, 1, "retry", rendered=["pol"])
    _pm(sid, 1, "pass", rendered=["pol"])  # same turn, second attempt

    assert entry_stats("pol")["treated"] == {"n": 1, "successes": 1}


def test_a_canary_turn_is_not_evidence(trial_mode):
    from core.adaptive.trial import entry_stats

    _entry("pol")
    sid = db.create_session(title="canary")
    _pm(sid, 1, "pass", rendered=["pol"], session_type="canary")

    assert entry_stats("pol")["treated"] == {"n": 0, "successes": 0}


# ---------------------------------------------------------------------------
# The sweep, one test per branch
# ---------------------------------------------------------------------------


def _status(entry_id: str) -> str:
    return (db.adaptive_get_entry(entry_id) or {})["status"]


def _sweep_event(entry_id: str) -> dict:
    events = [e for e in db.adaptive_list_events(entry_id=entry_id, limit=20) if e.get("actor") == "trial_sweep"]
    assert events, f"no journal row for {entry_id}"
    return events[0]


def test_a_measurably_worse_entry_is_retired(trial_mode):
    from core.adaptive.trial import sweep_trials

    _entry("harmful")
    _arms(db.create_session(title="worse"), "harmful", treated=(2, 12), control=(11, 12))

    out = sweep_trials()

    assert out["retired"] == ["harmful"]
    assert _status("harmful") == "deleted"
    evidence = json.loads(_sweep_event("harmful")["evidence_json"])[0]
    assert "treated 2/12" in evidence and "control 11/12" in evidence and "p=0.0" in evidence


def test_a_measurably_better_entry_is_promoted_early(trial_mode):
    from core.adaptive.trial import sweep_trials

    _entry("helpful")
    _arms(db.create_session(title="better"), "helpful", treated=(11, 12), control=(5, 12))

    out = sweep_trials()

    assert out["promoted"] == ["helpful"]
    assert _status("helpful") == "active"
    event = _sweep_event("helpful")
    assert event["action"] == "promote"
    evidence = json.loads(event["evidence_json"])[0]
    assert "treated 11/12" in evidence and "control 5/12" in evidence and "p=" in evidence


def test_an_entry_that_changed_nothing_keeps_running(trial_mode):
    from core.adaptive.trial import sweep_trials

    _entry("neutral")
    _arms(db.create_session(title="flat"), "neutral", treated=(6, 12), control=(6, 12))

    out = sweep_trials()

    assert out["waiting"] == ["neutral"] and _status("neutral") == "trial"
    assert "trial: running" in out["reasons"]["neutral"]
    assert not [e for e in db.adaptive_list_events(entry_id="neutral", limit=20) if e.get("actor") == "trial_sweep"]


def test_a_thin_sample_decides_nothing(trial_mode, monkeypatch):
    """The whole point of a minimum arm: 0/5 against 5/5 is significant on
    paper and is still five turns."""
    from core.adaptive.trial import sweep_trials

    monkeypatch.setattr("config.settings.adaptive_trial_min_arm", 40)
    _entry("thin")
    _arms(db.create_session(title="thin"), "thin", treated=(0, 5), control=(5, 5))

    assert sweep_trials()["waiting"] == ["thin"]
    assert _status("thin") == "trial"


def test_an_inconclusive_trial_is_promoted_unproven_at_its_ttl(trial_mode):
    """Most entries will never separate. The word `unproven` in the journal is
    what stops a promotion from being read as a result."""
    from core.adaptive.trial import sweep_trials

    _entry("quiet", age_days=40)

    out = sweep_trials()

    assert out["promoted"] == ["quiet"] and _status("quiet") == "active"
    evidence = json.loads(_sweep_event("quiet")["evidence_json"])[0]
    assert "unproven" in evidence and "treated 0/0" in evidence and "p=1.0000" in evidence


def test_a_trial_leaning_worse_is_not_promoted_at_its_ttl(trial_mode, monkeypatch):
    """Short of the retire alpha but pointing at harm: keep measuring rather
    than promote something the evidence leans against."""
    from core.adaptive.trial import sweep_trials

    monkeypatch.setattr("config.settings.adaptive_trial_min_arm", 40)
    _entry("leaning", age_days=40)
    _arms(db.create_session(title="leaning"), "leaning", treated=(2, 12), control=(9, 12))

    out = sweep_trials()

    assert out["waiting"] == ["leaning"] and _status("leaning") == "trial"
    assert "past its TTL but leaning worse" in out["reasons"]["leaning"]


def test_a_promotion_can_be_rolled_back(trial_mode):
    """Every channel has an undo — the promote event journals the before."""
    from core.adaptive.engine import rollback
    from core.adaptive.trial import sweep_trials

    _entry("helpful")
    _arms(db.create_session(title="undo"), "helpful", treated=(11, 12), control=(5, 12))
    sweep_trials()

    rollback(event_id=_sweep_event("helpful")["id"])

    assert _status("helpful") == "trial"


# ---------------------------------------------------------------------------
# /api/trust
# ---------------------------------------------------------------------------


async def _trust() -> dict:
    from api.routers import trust

    app = FastAPI()
    app.include_router(trust.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/trust")
    assert resp.status_code == 200
    return resp.json()


async def test_the_trust_endpoint_reports_the_arms(trial_mode):
    _entry("running-trial")
    _arms(db.create_session(title="trust"), "running-trial", treated=(6, 12), control=(4, 11))

    trials = (await _trust())["trials"]

    assert [t["entry_id"] for t in trials] == ["running-trial"]
    assert trials[0] == {
        "entry_id": "running-trial",
        "title": "running trial",
        "kind": "policy",
        "treated": {"n": 12, "successes": 6},
        "control": {"n": 11, "successes": 4},
        "p": pytest.approx(trials[0]["p"]),
        "status": "trial",
        "since": db.adaptive_get_entry("running-trial")["created_at"],
    }


async def test_the_trust_endpoint_keeps_settled_trials_visible(trial_mode):
    """A promotion that vanishes from the dashboard the moment it lands is a
    result nobody can check."""
    from core.adaptive.trial import sweep_trials

    _entry("helpful")
    _arms(db.create_session(title="settled"), "helpful", treated=(11, 12), control=(5, 12))
    sweep_trials()

    trials = (await _trust())["trials"]

    assert [(t["entry_id"], t["status"]) for t in trials] == [("helpful", "promoted")]
    assert trials[0]["treated"] == {"n": 12, "successes": 11}


# ---------------------------------------------------------------------------
# The flag off
# ---------------------------------------------------------------------------


def _apply(kind: str, title: str, content: str) -> str:
    from core.adaptive.engine import apply_batch, queue_edits

    queued = queue_edits(
        [{"action": "create", "kind": kind, "title": title, "content": content, "evidence": ["pm:1"]}],
        producer="refine",
    )
    assert queued["batch_id"], queued
    applied = apply_batch(queued["batch_id"])
    assert applied["applied"], applied
    return applied["applied"][0]


def test_trial_mode_off_leaves_an_auto_applied_entry_active(monkeypatch):
    from core.adaptive.render import build_routing_hints_block

    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_auto_apply", True)
    monkeypatch.setattr("config.settings.adaptive_trial_enabled", False)

    entry_id = _apply("routing_hint", "Use ripgrep", "Prefer rg over grep for repo-wide search.")

    assert _status(entry_id) == "active"
    # No turn key anywhere, and the hint still renders: the pre-W6 path.
    assert f"[{entry_id}]" in build_routing_hints_block()


def test_trial_mode_on_puts_the_same_entry_on_trial(trial_mode):
    from core.adaptive.render import build_routing_hints_block

    entry_id = _apply("routing_hint", "Use ripgrep", "Prefer rg over grep for repo-wide search.")

    assert _status(entry_id) == "trial"
    assert build_routing_hints_block() == ""  # no turn key, no trial rendering
    assert f"[{entry_id}]" in build_routing_hints_block("sess", _key_where(entry_id, rendered=True))


def test_a_human_writes_active_entries_even_in_trial_mode(trial_mode):
    """The author is the evidence. Trialling a rule the user typed would hide
    it from them on half their turns to measure a preference they stated."""
    from core.adaptive.engine import create_entry

    created = create_entry("prompt_note", "Answer in British English", "Use British spellings throughout.")

    assert created["status"] == "active"
    assert _status(created["entry_id"]) == "active"


def test_the_value_sweep_leaves_trials_alone_and_the_lint_does_not(trial_mode, monkeypatch):
    """A half-rendered entry looks unused long before it is — but prose that
    is not an instruction measures nothing in either arm."""
    from core.adaptive.retire import retire_lint_failures, retire_unused_entries

    monkeypatch.setattr("config.settings.adaptive_usage_retire_days", 1)
    db.set_snooze_state("adaptive_usage_epoch", _entry("unused-trial", age_days=30)["created_at"])
    _entry("narrative-trial", kind="prompt_note", age_days=30, content="The protocol remains ineffective.")

    assert retire_unused_entries()["retired"] == []
    assert _status("unused-trial") == "trial"
    assert retire_lint_failures()["retired"] == ["narrative-trial"]
