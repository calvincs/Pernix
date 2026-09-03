"""Regression: the cron-session pruner selected victims by title.

Shipped defect (db/models.py list_cron_sessions_before, consumed by
prune_cron_sessions): the weekly sweep matched `title LIKE 'Cron: %'`. The
title is a display string that both the user (rename) and the LLM titler
control, so it was neither necessary nor sufficient:

  * a NORMAL session someone renamed "Cron: …" was cascade-deleted — messages,
    workers and all — after seven idle days;
  * the scheduler's own "Job test: <name>" sessions, which it creates with
    session_type='cron' just like "Cron: <name>", never matched and piled up
    forever.

Fix: select on `session_type = 'cron'` (the column the scheduler stamps),
keep the age and pinned clauses, drop the title clause entirely.
"""

from __future__ import annotations

from db import models as db
from db.database import connect_sessions

_OLD = "2020-01-01T00:00:00+00:00"


def _age(*sids: str) -> None:
    with connect_sessions() as conn:
        for sid in sids:
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (_OLD, sid))


def test_renamed_normal_session_is_not_a_prune_candidate():
    renamed = db.create_session(title="Cron: my own notes", session_type="normal")
    db.add_message(renamed, "user", "these are the user's notes, not a cron run")
    _age(renamed)

    assert renamed not in {r["id"] for r in db.list_cron_sessions_before(max_age_days=7)}
    db.prune_cron_sessions(max_age_days=7)
    assert db.get_session(renamed) is not None, "a renamed normal session must survive the cron sweep"
    assert len(db.get_messages(renamed)) == 1


def test_job_test_sessions_are_pruned_like_any_other_cron_session():
    job_test = db.create_session(title="Job test: nightly-digest", session_type="cron")
    _age(job_test)

    assert job_test in {r["id"] for r in db.list_cron_sessions_before(max_age_days=7)}
    assert db.prune_cron_sessions(max_age_days=7) == 1
    assert db.get_session(job_test) is None


def test_type_keyed_sweep_keeps_age_and_pinned_semantics():
    stale = db.create_session(title="Cron: daily", session_type="cron")
    pinned = db.create_session(title="Cron: keep this run", session_type="cron")
    fresh = db.create_session(title="Cron: just ran", session_type="cron")
    _age(stale, pinned)
    db.set_session_meta(pinned, pinned=True)

    ids = {r["id"] for r in db.list_cron_sessions_before(max_age_days=7)}
    assert ids == {stale}
    # The digest line the retention sweep writes needs these three columns.
    row = next(r for r in db.list_cron_sessions_before(max_age_days=7) if r["id"] == stale)
    assert set(row) >= {"id", "title", "updated_at"}

    assert db.prune_cron_sessions(max_age_days=7) == 1
    assert db.get_session(stale) is None
    assert db.get_session(pinned) is not None
    assert db.get_session(fresh) is not None
