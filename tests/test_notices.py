"""Tiered notifications (2026-10 noise audit, phase 1): core/notices.py, the v42
notifications columns, the soft-dismiss/log DB layer and the /api/notifications
endpoints. Producer wiring is pinned by each producer's own tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core import notices
from db import models as db
from db.database import connect_sessions


class _Sink:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)

    emit = broadcast


@pytest.fixture
def wires(monkeypatch):
    sse, bus = _Sink(), _Sink()
    monkeypatch.setattr("sessions.manager.get_manager", lambda: sse)
    monkeypatch.setattr("core.events.get_event_bus", lambda: bus)
    return sse, bus


# --- tier resolution --------------------------------------------------------


def test_default_tier_and_session_type_map():
    assert notices.resolve_tier("sessions.reflect_attention") == "interrupt"
    assert notices.resolve_tier("sessions.reflect_attention", "normal") == "interrupt"
    assert notices.resolve_tier("sessions.reflect_attention", "cron") == "bell"
    assert notices.resolve_tier("sessions.reflect_attention", "canary") == "drop"
    assert notices.resolve_tier("sessions.reflect_attention", "worker") == "drop"
    assert notices.resolve_tier("sessions.reflect_attention", "snooze") == "log"


def test_category_override_beats_area_override_and_bad_values_are_ignored(monkeypatch):
    monkeypatch.setattr("config.settings.notify_tier_overrides", {"canary": "bell", "canary.contaminated": "drop"})
    assert notices.resolve_tier("canary.contaminated") == "drop"
    assert notices.resolve_tier("canary.maintenance") == "bell"
    monkeypatch.setattr("config.settings.notify_tier_overrides", {"canary": "loud"})
    assert notices.resolve_tier("canary.maintenance") == "log"


# --- notify() ----------------------------------------------------------------


def test_log_tier_is_a_row_and_nothing_else(wires):
    sse, bus = wires
    nid = notices.notify("dream.corrections_applied", "Dream: 2 corrections applied", "x")
    assert nid
    row = db.list_notifications("log")[0]
    assert (row["category"], row["tier"], row["urgency"], row["area"]) == (
        "dream.corrections_applied",
        "log",
        "low",
        "dream",
    )
    assert sse.events == [] and bus.events == []
    assert db.get_notifications() == []  # never in the bell


def test_bell_tier_broadcasts_but_never_reaches_the_bus(wires):
    sse, bus = wires
    notices.notify("canary.parked", "Canary parked: x", subject="x")
    assert len(sse.events) == 1 and sse.events[0]["tier"] == "bell"
    assert bus.events == []
    assert len(db.get_notifications()) == 1


def test_interrupt_tier_reaches_sse_and_the_bus(wires):
    sse, bus = wires
    notices.notify("jobs.failed", "Job failed: nightly", "boom", session_id="s1")
    assert len(sse.events) == 1 and len(bus.events) == 1
    assert bus.events[0]["tier"] == "interrupt" and bus.events[0]["session_id"] == "s1"
    assert bus.events[0]["urgency"] == "high"


def test_canary_session_reflect_is_recorded_nowhere(wires):
    sse, bus = wires
    assert notices.notify("sessions.reflect_attention", "Canary: x: Needs attention", session_type="canary") == ""
    assert sse.events == [] and bus.events == []
    assert db.list_notifications("log") == []


def test_session_type_is_looked_up_from_the_session_row(wires, monkeypatch):
    monkeypatch.setattr(db, "get_session", lambda sid: {"session_type": "canary"})
    assert notices.notify("sessions.timeout", "Session ran out of LLM time", session_id="abc") == ""
    monkeypatch.setattr(db, "get_session", lambda sid: {"session_type": "snooze"})
    notices.notify("sessions.timeout", "Session ran out of LLM time", session_id="abc")
    assert db.list_notifications("log")[0]["tier"] == "log"


def test_drop_writes_nothing(wires, monkeypatch):
    monkeypatch.setattr("config.settings.notify_tier_overrides", {"spaces": "drop"})
    assert notices.notify("spaces.suggested", "Suggested space: x") == ""
    assert db.list_notifications("log") == []


def test_daily_dedup_swallows_a_same_day_repeat(wires):
    first = notices.notify("adaptive.cap_reached", "cap", subject="refine")
    second = notices.notify("adaptive.cap_reached", "cap", subject="refine")
    other = notices.notify("adaptive.cap_reached", "cap", subject="dream")
    assert first and not second and other
    assert len(db.list_notifications("log")) == 2


def test_explicit_dedup_key_keeps_the_old_marker_format(wires):
    from datetime import datetime, timezone

    notices.notify("canary.maintenance", "m", dedup_key="canary_maintain:abc")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert db.get_snooze_state(f"notify_dedup:{day}:canary_maintain:abc")


def test_coalesce_folds_repeats_then_resolve_closes_and_a_new_row_follows(wires):
    a = notices.notify("system.mcp_down", "MCP server 'x' unreachable", "first", subject="x")
    b = notices.notify("system.mcp_down", "MCP server 'x' unreachable", "second", subject="x")
    assert a == b
    row = db.get_notifications()[0]
    assert row["occurrences"] == 2 and row["body"] == "second"
    assert notices.resolve("system.mcp_down", "x") == 1
    assert db.get_notifications() == []
    assert db.list_notifications("log")[0]["resolved_at"]
    c = notices.notify("system.mcp_down", "MCP server 'x' unreachable", subject="x")
    assert c and c != a  # a new episode is a new row


def test_unregistered_category_is_a_bell_item_not_a_crash(wires):
    assert notices.notify("nope.never_registered", "t")
    assert db.get_notifications()[0]["category"] == "nope.never_registered"


def test_kill_switch_restores_legacy_bell_rows_and_channels(wires, monkeypatch):
    sse, bus = wires
    monkeypatch.setattr("config.settings.notify_tiers_enabled", False)
    notices.notify("jobs.failed", "Job failed: x")  # legacy: bell + SSE + push
    notices.notify("adaptive.auto_approved", "auto-approved")  # legacy: bell row only
    rows = db.get_notifications()
    assert {r["category"]: (r["tier"], r["urgency"]) for r in rows} == {
        "jobs.failed": ("bell", "high"),
        "adaptive.auto_approved": ("bell", "normal"),
    }
    assert [e["category"] for e in sse.events] == ["jobs.failed"]
    assert [e["category"] for e in bus.events] == ["jobs.failed"]
    assert bus.events[0]["tier"] == "interrupt"  # the client still pops it, as before


def test_notify_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "add_notification", boom)
    assert notices.notify("jobs.failed", "x") == ""
    monkeypatch.setattr(db, "resolve_notifications", boom)
    assert notices.resolve("jobs.failed") == 0


def test_a_broken_broadcast_does_not_lose_the_row(monkeypatch):
    monkeypatch.setattr("sessions.manager.get_manager", lambda: (_ for _ in ()).throw(RuntimeError("no loop")))
    monkeypatch.setattr("core.events.get_event_bus", lambda: (_ for _ in ()).throw(RuntimeError("no bus")))
    assert notices.notify("jobs.failed", "x")
    assert len(db.get_notifications()) == 1


# --- DB layer ------------------------------------------------------------------


def test_soft_dismiss_keeps_the_row_in_the_log(wires):
    nid = notices.notify("jobs.failed", "Job failed: x")
    assert db.notification_counts()["needs_you"] == 1
    assert db.dismiss_notification(nid) is True
    assert db.get_notifications() == []
    assert db.notification_counts() == {"needs_you": 0, "bell": 0, "unread": 0}
    row = db.list_notifications("log")[0]
    assert row["id"] == nid and row["dismissed_at"] and row["read_at"]
    assert db.dismiss_notification(nid) is False  # already dismissed


def test_counts_separate_needs_you_from_bell_from_unread(wires):
    notices.notify("jobs.failed", "a")
    notices.notify("canary.parked", "b", subject="p")
    notices.notify("dream.corrections_applied", "c")
    assert db.notification_counts() == {"needs_you": 1, "bell": 1, "unread": 3}
    assert db.mark_notifications_read() == 3
    assert db.notification_counts()["unread"] == 0


def test_dismiss_all_clears_the_bell_but_not_the_log(wires):
    notices.notify("jobs.failed", "a")
    notices.notify("canary.parked", "b", subject="p")
    notices.notify("dream.corrections_applied", "c")
    assert db.dismiss_all_notifications() == 2
    assert db.get_notifications() == []
    assert len(db.list_notifications("log")) == 3


def test_log_view_filters_by_area_and_pages_with_before(wires):
    for i in range(3):
        notices.notify("dream.corrections_applied", f"d{i}")
    notices.notify("canary.parked", "c", subject="p")
    assert {r["area"] for r in db.list_notifications("log", area="dream")} == {"dream"}
    page = db.list_notifications("log", limit=2)
    older = db.list_notifications("log", before=page[-1]["created_at"], limit=10)
    assert all(r["created_at"] < page[-1]["created_at"] for r in older)


def test_link_round_trips_as_a_dict(wires):
    notices.notify("jobs.failed", "x", link={"kind": "tab", "tab": "jobs"})
    row = db.get_notifications()[0]
    assert row["link"] == {"kind": "tab", "tab": "jobs"} and "link_json" not in row


def test_prune_keeps_open_interrupts_and_caps_rows(wires, monkeypatch):
    old = "2020-01-01T00:00:00+00:00"
    keep = notices.notify("jobs.failed", "still waiting")
    gone_old = notices.notify("dream.corrections_applied", "old")
    gone_bell = notices.notify("canary.parked", "old bell", subject="p")
    with connect_sessions() as conn:
        conn.execute("UPDATE notifications SET created_at = ?", (old,))
    assert db.prune_notifications(30) == 2
    assert [r["id"] for r in db.list_notifications("log")] == [keep]
    assert gone_old and gone_bell
    monkeypatch.setattr(db, "_NOTIFICATION_MAX_ROWS", 3)
    for i in range(6):
        notices.notify("dream.queue_stalled", f"n{i}")
    db.prune_notifications(30)
    assert len(db.list_notifications("log")) == 4  # newest 3 + the open interrupt
    assert any(r["id"] == keep for r in db.list_notifications("log"))  # the open interrupt survived the cap


# --- migration -------------------------------------------------------------------


def test_v42_upgrades_a_v41_database_and_old_rows_become_bell_items(tmp_path, monkeypatch):
    from db import database

    monkeypatch.setattr("config.settings.db_path", str(tmp_path / "v41.db"))
    monkeypatch.setattr(database, "MIGRATIONS", [m for m in database.MIGRATIONS if m[0] <= 41])
    database.init_sessions_db()
    with connect_sessions() as conn:
        assert "tier" not in {r[1] for r in conn.execute("PRAGMA table_info(notifications)")}
        conn.execute(
            "INSERT INTO notifications (id, title, body, urgency, created_at) VALUES ('old1','Old','b','high','2026-09-30T00:00:00+00:00')"
        )

    monkeypatch.undo()
    monkeypatch.setattr("config.settings.db_path", str(tmp_path / "v41.db"))
    database.init_sessions_db()
    with connect_sessions() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(notifications)")}
        assert {
            "category",
            "tier",
            "subject",
            "occurrences",
            "read_at",
            "dismissed_at",
            "resolved_at",
            "link_json",
        } <= cols
        assert int(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]) >= 42
    row = db.get_notifications()[0]
    assert (row["id"], row["category"], row["tier"], row["occurrences"]) == ("old1", "legacy", "bell", 1)


def test_migrations_stay_strictly_ascending():
    from db import database

    versions = [m[0] for m in database.MIGRATIONS]
    assert versions == sorted(set(versions))  # a non-ascending list silently skips entries


# --- API -----------------------------------------------------------------------


def _app():
    from api.routers import questions

    app = FastAPI()
    app.include_router(questions.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_api_bell_log_counts_and_soft_dismiss(wires):
    nid = notices.notify("jobs.failed", "Job failed: x")
    notices.notify("dream.corrections_applied", "d")
    async with _app() as c:
        assert (await c.get("/api/notifications")).json()["notifications"][0]["id"] == nid
        assert len((await c.get("/api/notifications?view=log")).json()["notifications"]) == 2
        assert (await c.get("/api/notifications?view=bogus")).status_code == 400
        assert (await c.get("/api/notifications/counts")).json() == {"needs_you": 1, "bell": 0, "unread": 2}
        assert (await c.post(f"/api/notifications/{nid}/dismiss")).status_code == 200
        assert (await c.get("/api/notifications")).json()["notifications"] == []
        assert len((await c.get("/api/notifications?view=log")).json()["notifications"]) == 2
        assert (await c.post("/api/notifications/read-all")).json()["count"] == 1
        assert (await c.get("/api/notifications/counts")).json()["unread"] == 0


async def test_api_dismiss_all_and_notify_routing(wires):
    sse, bus = wires
    async with _app() as c:
        r = await c.post("/api/notify", json={"title": "FYI", "body": "b"})
        assert r.status_code == 200 and r.json()["notification_id"]
        assert bus.events == []  # normal urgency is a quiet bell item
        await c.post("/api/notify", json={"title": "Alert", "body": "b", "urgency": "high"})
        assert len(bus.events) == 1  # high is an interrupt: it may push
        assert (await c.get("/api/notifications/counts")).json()["needs_you"] == 1
        assert (await c.post("/api/notifications/dismiss-all")).json()["count"] == 2
        assert (await c.get("/api/notifications/counts")).json()["bell"] == 0


# --- registry hygiene ---------------------------------------------------------------

_SKIP_DIRS = {"tests", ".venv", ".venv.broken-py314", "node_modules", ".git", "data"}


def _py_sources():
    root = Path(__file__).resolve().parent.parent
    for p in root.rglob("*.py"):
        if _SKIP_DIRS & set(p.relative_to(root).parts):
            continue
        yield p.relative_to(root), p.read_text(encoding="utf-8", errors="ignore")


def test_every_notify_literal_is_a_registered_category():
    found = set()
    for _, text in _py_sources():
        found |= set(re.findall(r"notices\.notify\(\s*\"([a-z_]+\.[a-z_]+)\"", text))
        found |= set(re.findall(r"(?<![.\w])notify\(\s*\"([a-z_]+\.[a-z_]+)\"", text))
    assert found <= set(notices.CATEGORIES), sorted(found - set(notices.CATEGORIES))


def test_every_category_has_a_valid_tier_and_name():
    for name, cat in notices.CATEGORIES.items():
        assert re.fullmatch(r"[a-z_]+\.[a-z_]+", name), name
        assert cat.tier in notices.TIERS, name
        assert set(cat.by_session_type.values()) <= set(notices.TIERS), name
        assert cat.legacy_emit in ("none", "sse", "push"), name


def test_nothing_writes_notifications_except_the_policy_layer():
    """A producer that calls db.add_notification directly skips the tier policy
    (and so can put a receipt in the user's bell again). Only core/notices.py
    and the db package may."""
    allowed = {"core/notices.py"}
    offenders = []
    for rel, text in _py_sources():
        if rel.parts[0] == "db" or str(rel) in allowed:
            continue
        if re.search(r"\badd_notification\(", text):
            offenders.append(str(rel))
    assert not offenders, f"route these through core.notices.notify: {offenders}"
