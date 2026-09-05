"""Regression tests for the 2026-09-04 trust-loop hardening, W4.

Four things earn their own coverage here, because each replaces a place the
loop was measuring nothing:

* the two-proportion z-test and the drift signal it feeds (the 20-turn retry
  ratio it replaced could be moved 15 points by three turns);
* the receipt grammar and its resolvers, one test per ref kind;
* unfounded proposals being held back by the veto-window sweep;
* the grader hold-out, including the assertion that running it leaves
  post_mortems and the memory corpus untouched.
"""

import json
from pathlib import Path

import pytest

from db import models as db

# ---------------------------------------------------------------------------
# Two-proportion z-test
# ---------------------------------------------------------------------------


def test_z_test_on_known_inputs():
    """Textbook values: 40/100 vs 60/100 is z=-2.83, p≈0.0047."""
    from core.adaptive.tripwire import two_proportion_z_test

    z, p = two_proportion_z_test(40, 100, 60, 100)
    assert z == pytest.approx(-2.8284, abs=1e-3)
    assert p == pytest.approx(0.00468, abs=1e-4)

    # Symmetric: swapping the samples flips the sign and keeps the p-value.
    z2, p2 = two_proportion_z_test(60, 100, 40, 100)
    assert z2 == pytest.approx(-z, abs=1e-9)
    assert p2 == pytest.approx(p, abs=1e-12)


def test_z_test_identical_rates_are_no_evidence():
    from core.adaptive.tripwire import two_proportion_z_test

    z, p = two_proportion_z_test(30, 60, 50, 100)  # 50% vs 50%
    assert z == 0.0 and p == pytest.approx(1.0)


def test_z_test_degenerate_inputs_never_claim_certainty():
    """Zero variance and empty samples return "no evidence", not infinity."""
    from core.adaptive.tripwire import two_proportion_z_test

    assert two_proportion_z_test(0, 0, 5, 10) == (0.0, 1.0)
    assert two_proportion_z_test(5, 10, 0, 0) == (0.0, 1.0)
    assert two_proportion_z_test(30, 30, 30, 30) == (0.0, 1.0)  # pooled rate 1.0
    assert two_proportion_z_test(0, 30, 0, 30) == (0.0, 1.0)  # pooled rate 0.0


def test_z_test_small_swing_over_20_turns_is_not_significant():
    """The bug this replaced: 50% vs 30% over 20 turns was a 'flag'."""
    from core.adaptive.tripwire import PM_DRIFT_ALPHA_FLAG, two_proportion_z_test

    _, p = two_proportion_z_test(10, 20, 14, 20)
    assert p > PM_DRIFT_ALPHA_FLAG


# ---------------------------------------------------------------------------
# Drift signal: thresholds, direction, and the minimum-n guard
# ---------------------------------------------------------------------------


def _backdate(table, created_at, where, params=()):
    from db.database import connect_sessions

    with connect_sessions() as conn:
        conn.execute(f"UPDATE {table} SET created_at = ? WHERE {where}", (created_at, *params))


def _pm(created_at, verdict, user_signal=None, payload="{}"):
    sid = db.create_session(title="drift-fixture")
    pm_id = db.add_post_mortem(sid, 1, verdict, "agent", 0.9, "m", 1, None, None, payload)
    _backdate("post_mortems", created_at, "id = ?", (pm_id,))
    if user_signal is not None:
        from db.database import connect_sessions

        with connect_sessions() as conn:
            conn.execute("UPDATE post_mortems SET user_signal = ? WHERE id = ?", (user_signal, pm_id))
    return pm_id


def _require_user_signal_column():
    """W2 adds post_mortems.user_signal in migration v36, in parallel.

    Checked inside the test, never at import: a module-level probe would
    open the real data/sessions.db before conftest redirects it.
    """
    from db.database import connect_sessions

    with connect_sessions() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(post_mortems)").fetchall()}
    if "user_signal" not in cols:
        pytest.skip("post_mortems.user_signal lands in migration v36 (W2)")


APPLIED_AT = "2026-02-10T00:00:00+00:00"


def _fill(before_pass, before_total, after_pass, after_total):
    for i in range(before_total):
        _pm(f"2026-02-09T00:{i:02d}:00+00:00", "pass" if i < before_pass else "retry")
    for i in range(after_total):
        _pm(f"2026-02-11T00:{i:02d}:00+00:00", "pass" if i < after_pass else "retry")


def test_drift_needs_thirty_graded_turns_on_each_side():
    """29 turns is not a sample. The signal stays None — batch untouched."""
    from core.adaptive.tripwire import _post_mortem_signal

    _fill(before_pass=29, before_total=29, after_pass=0, after_total=29)
    assert _post_mortem_signal({"batch_id": "b"}, APPLIED_AT) is None

    # One more turn on each side and it becomes measurable.
    _pm("2026-02-09T00:59:00+00:00", "pass")
    _pm("2026-02-11T00:59:00+00:00", "retry")
    assert _post_mortem_signal({"batch_id": "b"}, APPLIED_AT) is not None


def test_drift_flags_below_alpha_but_rollback_needs_the_stricter_one():
    """A real but modest regression flags; only a stark one is rollback-worthy."""
    from core.adaptive.tripwire import _post_mortem_signal

    # 90% -> 63% over 30+30: significant at 0.05, not at 0.01.
    _fill(before_pass=27, before_total=30, after_pass=19, after_total=30)
    flagged, rollback, detail = _post_mortem_signal({"batch_id": "b"}, APPLIED_AT)
    assert flagged and not rollback
    assert "19/30 (63%) succeeded after the apply vs 27/30 (90%) before" in detail
    assert "p=" in detail and "z=" in detail


def test_drift_rollback_threshold_on_a_stark_regression():
    from core.adaptive.tripwire import _post_mortem_signal

    _fill(before_pass=30, before_total=30, after_pass=5, after_total=30)
    flagged, rollback, _ = _post_mortem_signal({"batch_id": "b"}, APPLIED_AT)
    assert flagged and rollback


def test_drift_ignores_an_improvement():
    """Significance in the GOOD direction is never a flag."""
    from core.adaptive.tripwire import _post_mortem_signal

    _fill(before_pass=5, before_total=30, after_pass=30, after_total=30)
    flagged, rollback, detail = _post_mortem_signal({"batch_id": "b"}, APPLIED_AT)
    assert not flagged and not rollback
    assert "30/30 (100%) succeeded after the apply" in detail


def test_drift_excludes_canary_post_mortems():
    from core.adaptive.tripwire import _post_mortem_signal

    _fill(before_pass=30, before_total=30, after_pass=30, after_total=30)
    for i in range(40):  # a canary storm cannot manufacture a regression
        _pm(f"2026-02-11T01:{i:02d}:00+00:00", "retry", payload=json.dumps({"session_type": "canary"}))
    flagged, _, detail = _post_mortem_signal({"batch_id": "b"}, APPLIED_AT)
    assert not flagged
    assert "30/30 (100%) succeeded after the apply" in detail


def test_user_signal_outranks_the_verdict():
    """Ground truth precedence: a thumbs-down beats a 'pass' verdict."""
    from core.adaptive.tripwire import _post_mortem_signal, _turn_succeeded

    _require_user_signal_column()
    assert _turn_succeeded({"verdict": "retry", "user_signal": "up"}) is True
    assert _turn_succeeded({"verdict": "pass", "user_signal": "down"}) is False
    assert _turn_succeeded({"verdict": "pass", "user_signal": None}) is True

    for i in range(30):
        _pm(f"2026-02-09T00:{i:02d}:00+00:00", "pass")
    for i in range(30):  # reflect says pass, the user says otherwise
        _pm(f"2026-02-11T00:{i:02d}:00+00:00", "pass", user_signal="down")
    flagged, rollback, detail = _post_mortem_signal({"batch_id": "b"}, APPLIED_AT)
    assert flagged and rollback
    assert "0/30 (0%) succeeded after the apply" in detail


def test_turn_succeeded_falls_back_when_the_column_is_absent():
    """Pre-v36 rows have no user_signal key at all — verdict decides."""
    from core.adaptive.tripwire import _turn_succeeded

    assert _turn_succeeded({"verdict": "pass"}) is True
    assert _turn_succeeded({"verdict": "retry"}) is False
    assert _turn_succeeded({"verdict": "pass", "user_signal": ""}) is True


# ---------------------------------------------------------------------------
# Drift → rollback, through the real journal path
# ---------------------------------------------------------------------------


@pytest.fixture
def _adaptive_on(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_auto_apply", True)
    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    import core.adaptive.render as render

    monkeypatch.setattr(render, "MIRROR_PATH", tmp_path / "ADAPTIVE.md")


def _apply_hint(title="drifting hint"):
    from core.adaptive import apply_batch, queue_edits

    r = queue_edits(
        [
            {
                "action": "create",
                "kind": "routing_hint",
                "title": title,
                "content": "prefer rg over grep for code search",
                "evidence": ["session:x"],
            }
        ],
        "refine",
    )
    apply_batch(r["batch_id"])
    _backdate("adaptive_batches", APPLIED_AT, "batch_id = ?", (r["batch_id"],))
    _backdate("adaptive_events", APPLIED_AT, "batch_id = ?", (r["batch_id"],))
    return r["batch_id"]


def test_drift_alone_flags_but_does_not_roll_back(_adaptive_on, monkeypatch):
    """adaptive_auto_rollback on, pm-drift flag off: flagged, not reversed."""
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("config.settings.adaptive_auto_rollback", True)
    monkeypatch.setattr("config.settings.adaptive_pm_drift_rollback", False)
    batch_id = _apply_hint()
    _fill(before_pass=30, before_total=30, after_pass=2, after_total=30)

    actions = [a for a in evaluate_tripwire() if a["batch_id"] == batch_id]
    assert [a["action"] for a in actions] == ["flagged"]
    assert db.adaptive_get_batch(batch_id)["status"] == "suspect"
    assert db.adaptive_get_entry("drifting-hint")["status"] == "active"


def test_drift_rolls_back_when_both_flags_are_on(_adaptive_on, monkeypatch):
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("config.settings.adaptive_auto_rollback", True)
    monkeypatch.setattr("config.settings.adaptive_pm_drift_rollback", True)
    batch_id = _apply_hint()
    _fill(before_pass=30, before_total=30, after_pass=2, after_total=30)

    actions = [a["action"] for a in evaluate_tripwire() if a["batch_id"] == batch_id]
    assert actions == ["flagged", "auto_rolled_back"]
    assert db.adaptive_get_batch(batch_id)["status"] == "rolled_back"
    assert db.adaptive_get_entry("drifting-hint") is None  # created by the batch → hard-deleted
    notes = [n for n in db.get_notifications() if "auto-rolled-back" in n["title"]]
    assert notes and "post-mortem outcome drift" in notes[0]["body"]


# ---------------------------------------------------------------------------
# Receipts: parse
# ---------------------------------------------------------------------------


def test_parse_recognises_every_ref_kind_and_nothing_else():
    from core.adaptive.receipts import parse

    refs = parse(
        [
            "pm:abc123",
            "candor:tool_ok(browse_web)",
            "signal:tool_pattern/rg",
            "feedback:4711",
            "hypothesis:h-9",
            "session:s-1",
            "the agent kept re-reading the same file",
            "note: pm ids are useful",  # not at the start → free text
            "",
        ]
    )
    assert [(r.kind, r.value) for r in refs] == [
        ("pm", "abc123"),
        ("candor", "tool_ok(browse_web)"),
        ("signal", "tool_pattern/rg"),
        ("feedback", "4711"),
        ("hypothesis", "h-9"),
        ("session", "s-1"),
    ]


def test_parse_rejects_a_signal_without_both_halves_and_dedupes():
    from core.adaptive.receipts import parse

    assert parse(["signal:tool_pattern"]) == []
    assert parse(["signal:/rg"]) == []
    assert len(parse(["pm:a", "pm:a", "PM:a"])) == 1


# ---------------------------------------------------------------------------
# Receipts: resolve, one per kind
# ---------------------------------------------------------------------------


def _one(ref: str):
    from core.adaptive.receipts import parse, resolve

    refs = parse([ref])
    assert refs, f"{ref!r} did not parse"
    return resolve(refs[0])


def test_resolve_post_mortem():
    sid = db.create_session(title="x")
    pm_id = db.add_post_mortem(sid, 1, "pass", "none", 1.0, "m", 1, None, None, "{}")
    assert _one(f"pm:{pm_id}") is True
    assert _one("pm:does-not-exist") is False


def test_resolve_signal():
    db.upsert_signal("tool_pattern", "rg", delta_reinforcements=1)
    assert _one("signal:tool_pattern/rg") is True
    assert _one("signal:tool_pattern/ripgrep") is False


def test_resolve_feedback_survives_a_missing_table():
    """message_feedback arrives with W2's migration v36; until then the ref
    is unresolvable, never an exception."""
    from db.database import connect_sessions

    with connect_sessions() as conn:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_feedback'"
        ).fetchone()
    assert _one("feedback:999999") is False  # absent row, or absent table
    if has_table:
        with connect_sessions() as conn:
            conn.execute(
                "INSERT INTO message_feedback (session_id, message_id, signal, created_at) "
                "VALUES ('s', 4711, 'up', '2026-09-04T00:00:00+00:00')"
            )
        assert _one("feedback:4711") is True


def test_resolve_candor_is_unresolvable_while_candor_is_off(monkeypatch):
    monkeypatch.setattr("config.settings.candor_enabled", False)
    assert _one("candor:tool_ok(browse_web)") is False


def test_resolve_candor_reads_the_bridge(monkeypatch):
    monkeypatch.setattr("config.settings.candor_enabled", True)

    seen = {}

    class _Bridge:
        def predict_sync(self, pred, args):
            seen["call"] = (pred, args)
            return {"p": 0.4, "observations": 30} if pred == "tool_ok" else None

    monkeypatch.setattr("core.extensions.candor.bridge.get_candor_bridge", lambda: _Bridge())
    assert _one("candor:tool_ok(browse_web)") is True
    assert seen["call"] == ("tool_ok", ["browse_web"])
    assert _one("candor:fetch_ok(x)") is False
    assert _one("candor:not-a-fact-key") is False


def test_resolve_hypothesis_requires_its_own_receipt():
    """A hypothesis grounds an entry only when IT rests on a pm or candor
    ref — otherwise dream would be its own evidence."""
    sid = db.create_session(title="x")
    pm_id = db.add_post_mortem(sid, 1, "retry", "agent", 0.8, "m", 1, None, None, "{}")

    grounded = db.add_dream_hypothesis(
        "tool_pattern", "http_get fails on js-heavy pages", json.dumps([{"type": "pm", "id": pm_id}])
    )
    from_candor = db.add_dream_hypothesis(
        "tool_pattern", "fetch_ok is degraded", json.dumps([{"type": "candor", "pred": "fetch_ok", "args": ["*"]}])
    )
    memory_only = db.add_dream_hypothesis(
        "contradiction", "two entries disagree", json.dumps([{"type": "memory", "file": "pernix.config"}])
    )

    assert _one(f"hypothesis:{grounded}") is True
    assert _one(f"hypothesis:{from_candor}") is True
    assert _one(f"hypothesis:{memory_only}") is False
    assert _one("hypothesis:h-missing") is False


def test_session_refs_never_ground_anything():
    sid = db.create_session(title="real session")
    assert _one(f"session:{sid}") is False


# ---------------------------------------------------------------------------
# Receipts: grade
# ---------------------------------------------------------------------------


def test_grade_from_an_evidence_list():
    from core.adaptive.receipts import GROUNDED, UNFOUNDED, grade

    sid = db.create_session(title="x")
    pm_id = db.add_post_mortem(sid, 1, "pass", "none", 1.0, "m", 1, None, None, "{}")

    assert grade([f"pm:{pm_id}", "the agent was slow"]) == GROUNDED
    assert grade(["the agent was slow", f"session:{sid}"]) == UNFOUNDED
    assert grade([]) == UNFOUNDED
    assert grade(None) == UNFOUNDED
    assert grade(["pm:not-a-real-id"]) == UNFOUNDED


def test_grade_reads_an_entry_creating_event(_adaptive_on):
    from core.adaptive import apply_batch, queue_edits
    from core.adaptive.receipts import GROUNDED, UNFOUNDED, count_unfounded, grade

    sid = db.create_session(title="x")
    pm_id = db.add_post_mortem(sid, 1, "retry", "agent", 0.8, "m", 1, None, None, "{}")
    r = queue_edits(
        [
            {
                "action": "create",
                "kind": "routing_hint",
                "title": "grounded hint",
                "content": "prefer rg over grep for code search",
                "evidence": [f"pm:{pm_id}"],
            },
            {
                "action": "create",
                "kind": "routing_hint",
                "title": "story hint",
                "content": "prefer rg over grep for code search",
                "evidence": ["the agent kept re-reading files"],
            },
        ],
        "refine",
    )
    apply_batch(r["batch_id"])

    assert grade("grounded-hint") == GROUNDED
    assert grade("story-hint") == UNFOUNDED
    assert grade("no-such-entry") == UNFOUNDED
    assert count_unfounded() == 1


# ---------------------------------------------------------------------------
# Unfounded proposals are held by the veto-window sweep
# ---------------------------------------------------------------------------


def _backdate_proposal(pid, hours):
    from datetime import datetime, timedelta, timezone

    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    _backdate("adaptive_proposals", stamp, "id = ?", (pid,))


_EDIT = [
    {
        "action": "create",
        "kind": "policy",
        "scope": "global",
        "title": "receipts veto test",
        "content": "Before claiming a file is written: read it back.",
        "evidence": ["x"],
    }
]


def test_unfounded_proposal_is_not_taken_by_the_clock(_adaptive_on):
    from core.adaptive import auto_approve_stale_proposals

    pid = db.adaptive_add_proposal("dream", json.dumps(_EDIT), json.dumps(["the agent seemed sloppy"]), "why")
    _backdate_proposal(pid, hours=48)

    out = auto_approve_stale_proposals()
    assert out["approved"] == []
    assert out["skipped_unfounded"] == 1
    assert db.adaptive_get_proposal(pid)["status"] == "pending"  # a human can still approve
    notes = [n for n in db.get_notifications() if "no receipts" in n["title"]]
    assert len(notes) == 1 and f"#{pid}" in notes[0]["body"]

    # Held again on the next sweep, but announced only once.
    assert auto_approve_stale_proposals()["skipped_unfounded"] == 1
    assert len([n for n in db.get_notifications() if "no receipts" in n["title"]]) == 1


def test_grounded_proposal_still_flows_through_the_veto_window(_adaptive_on):
    from core.adaptive import auto_approve_stale_proposals

    sid = db.create_session(title="x")
    pm_id = db.add_post_mortem(sid, 1, "retry", "agent", 0.8, "m", 1, None, None, "{}")
    pid = db.adaptive_add_proposal("dream", json.dumps(_EDIT), json.dumps([f"pm:{pm_id}"]), "why")
    _backdate_proposal(pid, hours=48)

    out = auto_approve_stale_proposals()
    assert out["approved"] == [pid]
    assert out["skipped_unfounded"] == 0
    assert db.adaptive_get_proposal(pid)["status"] == "auto_approved"


def test_annotate_proposal_publishes_the_grade():
    from core.adaptive import annotate_proposal

    pid = db.adaptive_add_proposal("dream", json.dumps(_EDIT), json.dumps(["prose only"]), "why")
    assert annotate_proposal(db.adaptive_get_proposal(pid))["evidence_grade"] == "unfounded"


# ---------------------------------------------------------------------------
# Producers stamp receipts
# ---------------------------------------------------------------------------


def test_refine_stamps_a_pm_ref_per_graded_turn():
    from core.refine import _stamp_post_mortem_receipts

    sid = db.create_session(title="refined")
    a = db.add_post_mortem(sid, 1, "retry", "agent", 0.8, "m", 1, None, None, "{}")
    b = db.add_post_mortem(sid, 2, "pass", "none", 0.9, "m", 1, None, None, "{}")
    other = db.create_session(title="unrelated")
    db.add_post_mortem(other, 1, "pass", "none", 0.9, "m", 1, None, None, "{}")

    edits = _stamp_post_mortem_receipts([{"action": "create", "evidence": ["the agent re-read files"]}], sid)
    evidence = edits[0]["evidence"]
    assert set(evidence[:2]) == {f"pm:{a}", f"pm:{b}"}  # this session's turns only
    assert evidence[-1] == "the agent re-read files"  # the prose survives, after
    from core.adaptive.receipts import GROUNDED, grade

    assert grade(evidence) == GROUNDED


def test_refine_leaves_edits_alone_when_the_session_has_no_grades():
    from core.refine import _stamp_post_mortem_receipts

    sid = db.create_session(title="ungraded")
    edits = [{"action": "create", "evidence": ["prose"]}]
    assert _stamp_post_mortem_receipts(edits, sid) == edits


def test_dream_stamps_hypothesis_and_the_evidence_it_pinned():
    from core.dream.promote import _evidence_refs

    sid = db.create_session(title="x")
    pm_id = db.add_post_mortem(sid, 1, "retry", "agent", 0.8, "m", 1, None, None, "{}")
    hid = db.add_dream_hypothesis(
        "tool_pattern",
        "fetch_ok is degraded",
        json.dumps(
            [
                {"type": "pm", "id": pm_id, "session_id": sid},
                {"type": "candor", "pred": "fetch_ok", "args": ["*"]},
                {"type": "memory", "file": "pernix.config", "epoch": 1},
            ]
        ),
    )
    refs = _evidence_refs(db.list_dream_hypotheses(limit=5)[0])

    assert refs[0] == f"hypothesis:{hid}"
    assert f"pm:{pm_id}" in refs
    assert "candor:fetch_ok(*)" in refs
    # retire.py finds the author through this ref — it must survive.
    assert f"dream_hypothesis:{hid}" in refs
    assert "memory:pernix.config" in refs
    from core.adaptive.receipts import GROUNDED, grade

    assert grade(refs) == GROUNDED


def test_telos_pulls_receipts_out_of_its_evidence_blob():
    from core.telos.evaluate import _receipts_from_evidence

    blob = (
        "[memory:pernix.ops@1756] the box runs docker compose\n"
        '[trace:2026-09-01] {"event": "spend"}\n'
        "[candor] - tool_ok(browse_web): 41% success over 61 obs (CI 30%-53%)\n"
        "- fetch_ok(*): 49% success over 200 obs\n"
        "quoted pm:3f2a91bb0c4d in the trace\n"
    )
    refs = _receipts_from_evidence(blob)
    assert refs == ["candor:tool_ok(browse_web)", "candor:fetch_ok(*)", "pm:3f2a91bb0c4d"]
    assert _receipts_from_evidence("[memory:x@1] nothing structured here") == []


# ---------------------------------------------------------------------------
# Grader hold-out
# ---------------------------------------------------------------------------


def test_holdout_fixtures_are_well_formed():
    from core.reflect import FAILURE_CAUSES
    from core.reflect_holdout import load_fixtures

    fixtures = load_fixtures()
    assert 8 <= len(fixtures) <= 10
    assert len({f["id"] for f in fixtures}) == len(fixtures)
    for f in fixtures:
        assert f["expected_verdict"] in ("pass", "retry", "escalate")
        assert f["expected_failure_cause"] in FAILURE_CAUSES
        assert (f["expected_failure_cause"] == "none") == (f["expected_verdict"] == "pass")
        assert f.get("note")
    # The set has to be able to catch over-strictness as well as laxity.
    verdicts = {f["id"]: f["expected_verdict"] for f in fixtures}
    assert sum(1 for v in verdicts.values() if v == "pass") >= 3
    assert "escalate" in verdicts.values()


def test_build_evidence_uses_the_headings_the_rubric_names():
    from core.reflect_holdout import build_evidence, load_fixtures

    blob = build_evidence(load_fixtures()[1])
    assert "TOOL EXECUTION SUMMARY:" in blob
    assert "USER REQUEST:" in blob
    assert "AGENT FINAL RESPONSE:" in blob
    assert "ATTEMPT TRANSCRIPT" in blob


async def test_run_holdout_scores_each_case(monkeypatch, tmp_path):
    """A stubbed grader that gets one case wrong scores 2/3."""
    from core.reflect import ReflectResult
    from core.reflect_holdout import STATE_KEY, run_holdout

    monkeypatch.setattr("config.settings.llm_model", "stub-model")
    _write_fixture(tmp_path, "a", "pass", "none")
    _write_fixture(tmp_path, "b", "retry", "agent")
    _write_fixture(tmp_path, "c", "escalate", "task")

    answers = {"a": ("pass", "none"), "b": ("retry", "scout"), "c": ("escalate", "task")}

    async def _fake(evidence, model):
        case = evidence.split("USER REQUEST:\n")[1].split("\n")[0]
        verdict, cause = answers[case]
        return ReflectResult(verdict=verdict, failure_cause=cause)

    monkeypatch.setattr("core.reflect_holdout._grade_evidence", _fake)

    report = await run_holdout(tmp_path)
    assert report["n"] == 3
    assert report["accuracy"] == pytest.approx(2 / 3, abs=1e-4)
    assert report["model"] == "stub-model"
    assert report["by_case"]["b"] == {"expected": "retry/agent", "got": "retry/scout", "ok": False}
    assert report["by_case"]["a"]["ok"] is True
    # Cause only matters on a non-pass: "pass" is the whole answer.
    assert report["by_case"]["a"]["expected"] == "pass"
    assert json.loads(db.get_snooze_state(STATE_KEY))["accuracy"] == report["accuracy"]


async def test_run_holdout_survives_a_grader_that_throws(monkeypatch, tmp_path):
    from core.reflect_holdout import run_holdout

    monkeypatch.setattr("config.settings.llm_model", "stub-model")
    _write_fixture(tmp_path, "a", "pass", "none")

    async def _boom(evidence, model):
        raise RuntimeError("provider down")

    monkeypatch.setattr("core.reflect_holdout._grade_evidence", _boom)

    report = await run_holdout(tmp_path)
    assert report["n"] == 0 and report["accuracy"] is None
    assert report["by_case"]["a"]["error"] == "RuntimeError"


async def test_run_holdout_writes_nothing_into_the_loop(monkeypatch, tmp_path):
    """The hold-out must stay a hold-out: no post-mortems, no sessions, no
    memory, no workspace files. A fixture the loop can learn from is
    training data with a score attached."""
    from core.reflect import ReflectResult
    from core.reflect_holdout import run_holdout

    monkeypatch.setattr("config.settings.llm_model", "stub-model")

    async def _fake(evidence, model):
        return ReflectResult(verdict="pass", failure_cause="none")

    monkeypatch.setattr("core.reflect_holdout._grade_evidence", _fake)

    from config import settings
    from db.database import connect_sessions

    def _counts():
        with connect_sessions() as conn:
            return (
                conn.execute("SELECT COUNT(*) c FROM post_mortems").fetchone()["c"],
                conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"],
                conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"],
            )

    memory_dir = Path(settings.memory_dir)
    workspace_dir = Path(settings.workspace_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    before = _counts()
    memory_before = sorted(p.name for p in memory_dir.rglob("*"))
    workspace_before = sorted(p.name for p in workspace_dir.rglob("*"))

    report = await run_holdout()  # the real fixture set
    assert report["n"] >= 8

    assert _counts() == before
    assert sorted(p.name for p in memory_dir.rglob("*")) == memory_before
    assert sorted(p.name for p in workspace_dir.rglob("*")) == workspace_before


def _write_fixture(directory, case_id, verdict, cause):
    (Path(directory) / f"{case_id}.json").write_text(
        json.dumps(
            {
                "id": case_id,
                "user_request": case_id,
                "transcript_excerpt": "[ASSISTANT]\nx",
                "final_response": "x",
                "expected_verdict": verdict,
                "expected_failure_cause": cause,
                "note": "fixture",
            }
        ),
        encoding="utf-8",
    )


def test_load_fixtures_skips_malformed_files(tmp_path):
    from core.reflect_holdout import load_fixtures

    _write_fixture(tmp_path, "good", "pass", "none")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "incomplete.json").write_text(json.dumps({"id": "x"}), encoding="utf-8")

    assert [f["id"] for f in load_fixtures(tmp_path)] == ["good"]


def test_holdout_schedule_installs_from_settings(monkeypatch):
    from core.extensions import scheduling

    monkeypatch.setattr("config.settings.grader_holdout_enabled", True)
    monkeypatch.setattr("config.settings.grader_holdout_schedule", "30 3 * * *")

    added = {}

    class _Scheduler:
        def add_job(self, fn, **kwargs):
            added[kwargs.get("id")] = kwargs

    monkeypatch.setattr(scheduling, "_get_scheduler", lambda: _Scheduler())
    scheduling.ensure_grader_holdout_schedule()
    assert "_grader_holdout" in added
    assert added["_grader_holdout"]["kwargs"]["meta"]["kind"] == "grader_holdout"

    added.clear()
    monkeypatch.setattr("config.settings.grader_holdout_enabled", False)
    scheduling.ensure_grader_holdout_schedule()
    assert added == {}
