"""L06 — the tripwire re-judged two dead batches forever.

`ab-0f4a6cbd1725` and `ab-614c36e552b9` (candor, applied 2026-08-13, no
`cleared_at`) were still being swept a month later. The active signal is
per-task canary testimony and a task may only testify when its trailing runs
before the apply were green; the suite behind those batches was retired on
08-27, so that precondition can never be met again. `_canary_signal`
returned None, nothing cleared the batch, and the same WARNING was logged on
every maintenance tick — 164 lines between 09-10 and 09-12.

Two changes are pinned here: the wait for a verdict is bounded by
`adaptive_tripwire_window_hours` and ends in an honest "unjudged" settlement
(not an all-clear — status stays `applied`, so a human can still roll it
back), and the warning fires once per batch instead of once per tick.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from db import models as db


@pytest.fixture(autouse=True)
def _adaptive_on(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("config.settings.adaptive_tripwire_window_hours", 72)
    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    import core.adaptive.render as render

    monkeypatch.setattr(render, "MIRROR_PATH", tmp_path / "ADAPTIVE.md")


def _batch(batch_id: str, age_hours: float) -> str:
    """An applied batch with no events, aged by backdating created_at.

    No adaptive_events means `_applied_at` falls back to created_at, which is
    the state the two live batches are in.
    """
    from db.database import connect_sessions

    db.adaptive_create_batch(batch_id, "candor", "[]", status="applied")
    stamp = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE adaptive_batches SET created_at = ? WHERE batch_id = ?", (stamp, batch_id))
    return batch_id


def _retired_suite_rows(batch_id: str) -> None:
    """Post-batch rows from a task with no green history — the live shape.

    The rows exist (so the sweep ran), but the task can never satisfy the
    green precondition, so nothing in them can testify either way.
    """
    db.add_canary_run("gone-task", "post_batch", None, "[]", False, batch_id=batch_id, outcome="gate_fail")


def test_an_old_batch_nothing_could_judge_is_settled_once(monkeypatch):
    from core.adaptive.tripwire import NO_SIGNAL_REASON, evaluate_tripwire

    bid = _batch("ab-0f4a6cbd1725", age_hours=24 * 30)
    _retired_suite_rows(bid)

    actions = evaluate_tripwire()
    assert [a for a in actions if a["batch_id"] == bid and a["action"] == "unjudged"]
    row = db.adaptive_get_batch(bid)
    assert row["cleared_at"]
    assert row["flagged_reason"] == NO_SIGNAL_REASON
    assert row["flagged_reason"].startswith("no-signal")
    # Not an all-clear and not a rollback: a human can still undo the batch.
    assert row["status"] == "applied"

    # Exactly once: cleared_at is terminal, so the next tick does not see it.
    second = evaluate_tripwire()
    assert not [a for a in second if a["batch_id"] == bid]


def test_a_young_batch_is_left_alone(monkeypatch):
    from core.adaptive.tripwire import evaluate_tripwire

    bid = _batch("ab-young", age_hours=1)
    _retired_suite_rows(bid)

    actions = evaluate_tripwire()
    assert not [a for a in actions if a["batch_id"] == bid]
    row = db.adaptive_get_batch(bid)
    assert not row["cleared_at"] and not row["flagged_reason"]


def test_a_batch_inside_the_window_settles_when_the_window_passes(monkeypatch):
    """The window is the knob, not the age: shrink it and the same batch settles."""
    from core.adaptive.tripwire import evaluate_tripwire

    bid = _batch("ab-window", age_hours=5)
    _retired_suite_rows(bid)
    evaluate_tripwire()
    assert not db.adaptive_get_batch(bid)["cleared_at"]

    monkeypatch.setattr("config.settings.adaptive_tripwire_window_hours", 1)
    evaluate_tripwire()
    assert db.adaptive_get_batch(bid)["cleared_at"]


def test_the_no_signal_warning_fires_once_per_batch(monkeypatch, caplog):
    from core.adaptive.tripwire import evaluate_tripwire

    # Inside the window, so the batch survives to be re-swept.
    bid = _batch("ab-noisy", age_hours=1)
    _retired_suite_rows(bid)

    def _warnings() -> list[str]:
        return [r.message for r in caplog.records if r.levelno == logging.WARNING and "could testify" in r.message]

    with caplog.at_level(logging.WARNING, logger="pernix.adaptive"):
        for _ in range(5):
            evaluate_tripwire()
    assert len(_warnings()) == 1, "the per-tick metronome is back"
    assert db.get_snooze_state(f"adaptive_nosignal_warned:{bid}")


def test_a_suspect_batch_is_not_settled_as_unjudged(monkeypatch):
    """Unjudged is for batches nobody flagged; a suspect flag has its own TTL."""
    from core.adaptive.tripwire import evaluate_tripwire

    bid = _batch("ab-suspect", age_hours=24 * 30)
    db.adaptive_update_batch(bid, status="suspect", flagged_reason="canary regression: t1 (confirmed)")
    _retired_suite_rows(bid)

    evaluate_tripwire()
    row = db.adaptive_get_batch(bid)
    assert row["status"] == "suspect" and not row["cleared_at"]


def test_the_reason_reads_as_a_sentence_for_the_adaptive_panel():
    """The panel renders flagged_reason verbatim, so it has to say something."""
    from core.adaptive.tripwire import NO_SIGNAL_REASON

    assert NO_SIGNAL_REASON.startswith("no-signal")
    assert "unjudged (no canary could testify)" in NO_SIGNAL_REASON
