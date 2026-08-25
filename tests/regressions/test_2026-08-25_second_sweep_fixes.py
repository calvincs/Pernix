"""Second ARC-3 sweep fixes (approved batch): bash-aware grind detection,
bash log-noise collapse, reflect contentless/missing-verdict guards,
questions audit trail, round-cap continuation setting.

Field basis: 21-session sweep — workers at repl:0, rlm_process at 1 use ever
(grind counter blind to bash reads), 54 identical log banners drowning solver
output, contentless confidence-0.0 passes, an always-empty questions table,
and pervasive round-budget anxiety ("I'm at 24 rounds...").
"""

import json

from core.agent import StuckDetector
from core.tools.builtin.core_tools import _collapse_repeated_lines


class _Reg:
    def exists(self, name):
        return True


def _bash_call(cmd):
    return {"name": "bash", "arguments": json.dumps({"command": cmd})}


def test_signal12_counts_bash_mediated_reads(monkeypatch):
    monkeypatch.setattr("config.settings.rlm_enabled", True)
    d = StuckDetector()
    for i in range(5):
        d.evaluate("", [_bash_call(f"sed -n '{i*100},{i*100+99}p' arc3/game.py")], {}, _Reg())
    assert len(d.pending_hints) == 1
    assert "rlm_process" in d.pending_hints[0] and "arc3/game.py" in d.pending_hints[0]


def test_signal12_ignores_non_read_bash(monkeypatch):
    monkeypatch.setattr("config.settings.rlm_enabled", True)
    d = StuckDetector()
    for _ in range(6):
        d.evaluate("", [_bash_call("python3 arc3/solver.py")], {}, _Reg())
    assert d.pending_hints == []


def test_collapse_squashes_repeated_banners():
    banner = "2026-08-25 | INFO | Got anonymous API key: abc"
    lines = []
    for i in range(20):
        lines.append(f"real output {i}")
        lines.append(banner)
    out = _collapse_repeated_lines("\n".join(lines))
    assert out.count(banner) == 3 + 1  # 3 kept + 1 mention in the omission marker
    assert "17 more identical lines omitted" in out
    for i in range(20):
        assert f"real output {i}" in out  # nothing real is lost


def test_collapse_leaves_quiet_output_alone():
    text = "\n".join(f"line {i}" for i in range(50))
    assert _collapse_repeated_lines(text) == text


def test_reflect_missing_verdict_coerces_to_retry():
    from core.reflect import _result_from_data

    r = _result_from_data({"reasoning": "looks fine"}, "m", 100)
    assert r.verdict == "retry"

    r2 = _result_from_data({}, "m", 100)
    assert r2.verdict == "retry"
    assert "no verdict" in r2.reasoning

    r3 = _result_from_data({"verdict": "pass", "reasoning": "did the thing"}, "m", 100)
    assert r3.verdict == "pass"


def test_questions_survive_answering_as_audit_rows():
    from datetime import datetime, timezone

    from db import models as db
    from db.database import connect_sessions

    sid = db.create_session(title="q audit")
    qid = db.add_question(sid, "which way?", session_title="q audit")
    assert any(q["id"] == qid for q in db.get_questions(sid))

    with connect_sessions() as conn:
        cur = conn.execute(
            "UPDATE questions SET answer = ?, answered_at = ? WHERE id = ? AND answered_at IS NULL",
            ("left", datetime.now(timezone.utc).isoformat(), qid),
        )
        assert cur.rowcount == 1
        # double-answer guard
        cur = conn.execute(
            "UPDATE questions SET answer = ?, answered_at = ? WHERE id = ? AND answered_at IS NULL",
            ("right", datetime.now(timezone.utc).isoformat(), qid),
        )
        assert cur.rowcount == 0

    # gone from the pending queue, kept in the table with the answer
    assert not any(q["id"] == qid for q in db.get_questions(sid))
    with connect_sessions() as conn:
        row = conn.execute("SELECT answer, answered_at FROM questions WHERE id = ?", (qid,)).fetchone()
    assert row["answer"] == "left" and row["answered_at"]


def test_round_cap_continuation_setting_exists():
    from config import settings

    assert settings.round_cap_auto_continue >= 1
