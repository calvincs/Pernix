"""Item #7: per-session dedupe in synthesis.run()."""

import json
import time

from core import synthesis
from db import models as db


def _pm(session_id: str, *, attempt: int, skills: list[str], verdict: str = "pass", failure_cause: str = "none") -> str:
    payload = {"scout_summary": {"recommended_skills": skills, "from_fallback": False}}
    return db.add_post_mortem(
        session_id=session_id,
        attempt=attempt,
        verdict=verdict,
        failure_cause=failure_cause,
        confidence=0.9,
        reflect_model="test",
        reflect_latency_ms=10,
        scout_viability="verified",
        execution_mode="inline",
        payload_json=json.dumps(payload),
    )


def test_dedupe_same_session_multiple_attempts_collapses_to_latest():
    # Drain any prior state so signal counts are predictable.
    synthesis.run()

    sid = db.create_session(title="dedup-same-session")
    db.delete_signal("skill", "dedup-skill-x")
    pm1 = _pm(sid, attempt=1, skills=["dedup-skill-x"])
    pm2 = _pm(sid, attempt=2, skills=["dedup-skill-x"])
    pm3 = _pm(sid, attempt=3, skills=["dedup-skill-x"])

    stats = synthesis.run()
    # Only the latest (attempt 3) counted as processed; 1+2 superseded.
    assert stats.processed == 1
    assert stats.superseded == 2

    # Signal upserted exactly ONCE (successes==1), not 3 times.
    sig = db.get_signal("skill", "dedup-skill-x")
    assert sig is not None
    assert sig["successes"] == 1

    # All three rows marked synthesized — none should re-queue.
    for pid in (pm1, pm2, pm3):
        assert db.get_post_mortem(pid)["synthesized_at"] is not None


def test_different_sessions_each_contribute_signal():
    synthesis.run()  # drain

    sid_a = db.create_session(title="dedup-multi-sess-a")
    sid_b = db.create_session(title="dedup-multi-sess-b")
    sid_c = db.create_session(title="dedup-multi-sess-c")
    db.delete_signal("skill", "dedup-multi-skill")

    for sid in (sid_a, sid_b, sid_c):
        _pm(sid, attempt=1, skills=["dedup-multi-skill"])

    stats = synthesis.run()
    assert stats.processed == 3
    assert stats.superseded == 0

    sig = db.get_signal("skill", "dedup-multi-skill")
    assert sig["successes"] == 3


def test_tiebreak_same_attempt_later_created_at_wins():
    synthesis.run()  # drain

    sid = db.create_session(title="dedup-tiebreak")
    db.delete_signal("skill", "tiebreak-winner")
    db.delete_signal("skill", "tiebreak-loser")

    # Both rows have attempt=1; later insert should win the tiebreak.
    _pm(sid, attempt=1, skills=["tiebreak-loser"])
    time.sleep(0.01)  # ensure distinct created_at
    _pm(sid, attempt=1, skills=["tiebreak-winner"])

    synthesis.run()

    winner = db.get_signal("skill", "tiebreak-winner")
    loser = db.get_signal("skill", "tiebreak-loser")
    assert winner is not None and winner["successes"] == 1
    assert loser is None  # superseded — never attributed
