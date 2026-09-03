"""Space suggestions (v35): the scan that proposes a space from a habit.

Pinned here: what a candidate is (archived counts, an unnamed chat does
not), that the prompt actually carries the contract the gate then enforces,
that every gate rule drops what it says it drops, that a refusal is durable
by topic AND by overlap, that a scheduled scan short-circuits on the two
watermarks while force does not, that a dry run leaves the DB exactly as it
found it, and that the whole feature is absent from the snooze ladder when
it is switched off.

No test here reaches a model: chat_with_backup is stubbed everywhere.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core import space_suggest as ss
from core import spaces as spaces_lib
from db import models as db
from db.database import connect_sessions


@pytest.fixture(autouse=True)
def _fresh_space_cache():
    spaces_lib.invalidate_space_cache()
    yield
    spaces_lib.invalidate_space_cache()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _mk(
    title="Check a claim",
    subtitle="fact checking",
    days_ago=1.0,
    *,
    space_id=None,
    task_type="research",
    first="  please verify\n  this claim  ",
    user_msgs=2,
    archived=False,
) -> str:
    """One candidate-shaped session, backdated into the window."""
    sid = db.create_session(title=title, space_id=space_id)
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE sessions SET created_at = ?, subtitle = ? WHERE id = ?",
            (_ago(days_ago), subtitle, sid),
        )
    for i in range(user_msgs):
        db.add_message(sid, "user", first if i == 0 else f"follow up {i}")
        db.add_message(sid, "assistant", "on it")
    if task_type:
        db.add_message(sid, "scout", json.dumps({"task_type": task_type, "viability": "ok"}))
    if archived:
        db.set_session_meta(sid, archived=True)
    return sid


def _cluster(ids, **over) -> dict:
    base = {
        "kind": "new",
        "existing_space_id": None,
        "topic_key": "fact-checking",
        "label": "Fact Checking",
        "why": "You keep verifying claims against sources.",
        "session_ids": list(ids),
        "directives": None,
    }
    base.update(over)
    return base


def _stub_llm(monkeypatch, payload):
    """Replace the one background call. Returns the recorded call kwargs."""
    seen: dict = {}

    class _Resp:
        content = payload if isinstance(payload, str) else json.dumps(payload)

    async def _fake(client, *, model="", messages=None, max_tokens=None, **kw):
        seen["model"] = model
        seen["messages"] = messages
        seen["max_tokens"] = max_tokens
        return _Resp()

    monkeypatch.setattr(ss, "chat_with_backup", _fake)
    monkeypatch.setattr(ss, "get_llm_client", lambda: object())
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")
    return seen


# ---------------------------------------------------------------------------
# collect_candidates
# ---------------------------------------------------------------------------


def test_candidates_carry_the_fields_the_prompt_line_needs():
    sp = db.create_space("Research", "#112233", "research")
    sid = _mk(space_id=sp["id"], days_ago=2)
    rows = ss.collect_candidates(30)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == sid
    assert row["title"] == "Check a claim"
    assert row["subtitle"] == "fact checking"
    assert row["space_id"] == sp["id"] and row["space_label"] == "Research"
    assert row["day"] == db.get_session(sid)["created_at"][:10]
    assert row["messages"] == 4
    assert row["task_type"] == "research"
    # Whitespace collapsed, capped.
    assert row["first_user"] == "please verify this claim"


def test_candidates_include_archived_and_exclude_the_unnameable():
    kept = _mk(title="Verify a stat", days_ago=1)
    archived = _mk(title="Old verifying", days_ago=20, archived=True)
    _mk(title="New session", subtitle="", days_ago=1)  # never got a title
    _mk(title="One-liner", days_ago=1, user_msgs=1)  # a single question
    _mk(title="Too old", days_ago=90)
    ids = {r["id"] for r in ss.collect_candidates(30)}
    assert ids == {kept, archived}


def test_candidate_task_type_is_the_scout_majority():
    sid = _mk(task_type="")
    db.add_message(sid, "scout", json.dumps({"task_type": "coding"}))
    db.add_message(sid, "scout", json.dumps({"task_type": "research"}))
    db.add_message(sid, "scout", json.dumps({"task_type": "research"}))
    db.add_message(sid, "scout", "not json at all")
    assert ss.collect_candidates(30)[0]["task_type"] == "research"


def test_candidate_without_scout_rounds_has_no_task_type():
    _mk(task_type="")
    assert ss.collect_candidates(30)[0]["task_type"] == ""


def test_collect_is_two_queries_regardless_of_session_count():
    """O(rows), not O(sessions): the box carries hundreds in a window, and a
    per-session round trip would make the idle scan quadratic in the
    sidebar."""
    for i in range(12):
        _mk(title=f"Claim {i}", days_ago=i % 5 + 1)

    statements: list[str] = []
    conn = connect_sessions()  # cached per path — the helper gets this one
    conn.set_trace_callback(statements.append)
    try:
        rows = db.list_space_suggest_candidates(_ago(30))
    finally:
        conn.set_trace_callback(None)

    assert len(rows) == 12
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 2, selects


# ---------------------------------------------------------------------------
# The prompt contract
# ---------------------------------------------------------------------------


def test_system_prompt_states_the_json_contract():
    for key in ("kind", "existing_space_id", "topic_key", "label", "why", "session_ids", "directives"):
        assert f'"{key}"' in ss.SYSTEM_PROMPT, key
    assert "KIND OF WORK" in ss.SYSTEM_PROMPT
    assert "SOUL, RULES or SESSIONS" in ss.SYSTEM_PROMPT
    assert ss.SYSTEM_PROMPT.rstrip().endswith("/no_think")


def test_user_message_lists_spaces_declined_and_one_line_per_session():
    sp = db.create_space("Pernix", "#7c9cff", "pernix")
    _mk(title="Check a claim", days_ago=1)
    candidates = ss.collect_candidates(30)
    spaces = db.list_spaces()
    declined = [{"topic_key": "weather-lookups", "label": "Weather", "session_ids": ["a", "b", "c"]}]

    messages = ss.build_messages(candidates, spaces, declined)
    assert [m["role"] for m in messages] == ["system", "user"]
    user = messages[1]["content"]

    assert f"{sp['id']} — Pernix — 0 sessions" in user
    assert ss.DECLINED_PREAMBLE in user
    assert "weather-lookups — Weather — 3 sessions" in user

    row = candidates[0]
    expected = (
        f"{row['id']} | {row['day']} | 4 msgs | research | space: - | "
        "Check a claim — fact checking | first: please verify this claim"
    )
    assert expected in user


def test_user_message_says_none_when_there_are_no_spaces_or_refusals():
    _mk()
    user = ss.build_messages(ss.collect_candidates(30), [], [])[1]["content"]
    assert "EXISTING SPACES: none yet." in user
    assert ss.DECLINED_PREAMBLE not in user


# ---------------------------------------------------------------------------
# parse_clusters
# ---------------------------------------------------------------------------


def test_parse_clusters_handles_fences_prose_and_garbage():
    payload = (
        '{"clusters": [{"kind": "new", "topic_key": "fact-checking", "label": "Fact Checking", '
        '"session_ids": ["a", "b"]}]}'
    )
    for text in (payload, f"```json\n{payload}\n```", f"Here is my answer:\n{payload}\nHope that helps."):
        got = ss.parse_clusters(text)
        assert len(got) == 1 and got[0]["topic_key"] == "fact-checking"
        assert got[0]["session_ids"] == ["a", "b"]

    for junk in ("", None, "I could not find any groupings.", "[[[", '{"clusters": "nope"}'):
        assert ss.parse_clusters(junk) == []


def test_parse_clusters_fills_defaults_and_drops_non_objects():
    got = ss.parse_clusters('{"clusters": [{"label": "Solo"}, "nope", 7]}')
    assert len(got) == 1
    assert got[0] == {
        "kind": "new",
        "existing_space_id": None,
        "topic_key": "",
        "label": "Solo",
        "why": "",
        "session_ids": [],
        "directives": None,
    }


def test_parse_clusters_keeps_only_well_shaped_directive_drafts():
    got = ss.parse_clusters(
        json.dumps(
            {
                "clusters": [
                    {
                        "label": "Fact Checking",
                        "topic_key": "fact-checking",
                        "directives": {
                            "RULES": {"addition": "## Sourcing\nCite everything.", "rationale": "why"},
                            "SOUL": {"addition": "   "},
                            "EVIL": {"addition": "## Nope"},
                        },
                    }
                ]
            }
        )
    )
    assert set(got[0]["directives"]) == {"RULES"}
    assert got[0]["directives"]["RULES"]["rationale"] == "why"


def test_parse_clusters_accepts_a_bare_list():
    assert len(ss.parse_clusters('[{"label": "Fact Checking"}]')) == 1


# ---------------------------------------------------------------------------
# Gate rules, one at a time
# ---------------------------------------------------------------------------


def _by_id(rows):
    return {r["id"]: r for r in rows}


def test_rule_known_members_drops_unknown_ids_and_normalizes_the_key():
    real = _mk()
    rows = ss.collect_candidates(30)
    out = ss.rule_known_members(_cluster([real, "ghost", real], topic_key="Fact Checking!!"), _by_id(rows), set())
    assert out["session_ids"] == [real]  # unknown dropped, duplicate collapsed
    assert out["topic_key"] == "fact-checking"


def test_rule_known_members_falls_back_to_the_label_then_gives_up():
    rows = ss.collect_candidates(30)
    out = ss.rule_known_members(_cluster([], topic_key="!!!", label="Fact Checking"), _by_id(rows), set())
    assert out["topic_key"] == "fact-checking"
    assert ss.rule_known_members(_cluster([], topic_key="!!!", label="???"), _by_id(rows), set()) is None


def test_rule_known_members_skips_sessions_already_in_the_target_space():
    sp = db.create_space("Pernix", "#7c9cff", "pernix")
    inside = _mk(title="already filed", space_id=sp["id"])
    loose = _mk(title="loose")
    rows = ss.collect_candidates(30)
    cluster = _cluster([inside, loose], kind="existing", existing_space_id=sp["id"])
    out = ss.rule_known_members(cluster, _by_id(rows), {sp["id"]})
    assert out["session_ids"] == [loose]


def test_rule_known_members_degrades_an_unknown_space_to_a_new_one():
    sid = _mk()
    rows = ss.collect_candidates(30)
    out = ss.rule_known_members(_cluster([sid], kind="existing", existing_space_id="ghost"), _by_id(rows), set())
    assert out["kind"] == "new" and out["existing_space_id"] is None
    assert out["session_ids"] == [sid]


def test_rule_big_enough_needs_members_and_distinct_days():
    same_day = [_mk(title=f"c{i}", days_ago=1) for i in range(5)]
    spread = [_mk(title=f"s{i}", days_ago=i + 1) for i in range(5)]
    by_id = _by_id(ss.collect_candidates(30))

    assert ss.rule_big_enough(_cluster(spread), by_id, 5, 3) is True
    assert ss.rule_big_enough(_cluster(spread[:4]), by_id, 5, 3) is False  # too few
    assert ss.rule_big_enough(_cluster(same_day), by_id, 5, 3) is False  # one afternoon


def test_rule_not_chatter_drops_conversational_majorities_and_stoplist_names():
    chatty = [_mk(title=f"hi {i}", task_type="conversational", days_ago=i + 1) for i in range(3)]
    worky = [_mk(title=f"job {i}", task_type="research", days_ago=i + 1) for i in range(3)]
    by_id = _by_id(ss.collect_candidates(30))

    assert ss.rule_not_chatter(_cluster(chatty), by_id) is False
    assert ss.rule_not_chatter(_cluster(worky), by_id) is True
    assert ss.rule_not_chatter(_cluster(worky, label="General Chat", topic_key="general-chat"), by_id) is False
    assert ss.rule_not_chatter(_cluster(worky, label="Daily Check-In", topic_key="daily-check-in"), by_id) is False


def test_rule_not_declined_binds_by_topic_key_and_by_overlap():
    ids = [f"s{i}" for i in range(4)]
    by_topic = [{"topic_key": "fact-checking", "label": "Fact Checking", "session_ids": ["zzz"]}]
    assert ss.rule_not_declined(_cluster(ids), by_topic) is False

    # Renamed, but half the same sessions — still declined.
    by_overlap = [{"topic_key": "something-else", "label": "Other", "session_ids": ids[:2]}]
    assert ss.rule_not_declined(_cluster(ids), by_overlap) is False

    below = [{"topic_key": "something-else", "label": "Other", "session_ids": ids[:1]}]
    assert ss.rule_not_declined(_cluster(ids), below) is True


def test_rule_not_pending_dedupes_against_unreviewed_rows():
    pending = [{"topic_key": "fact-checking"}]
    assert ss.rule_not_pending(_cluster(["a"]), pending) is False
    assert ss.rule_not_pending(_cluster(["a"], topic_key="weather"), pending) is True


def test_rule_no_slug_collision_only_bites_new_spaces():
    spaces = [{"id": "x", "slug": "fact-checking", "label": "Fact Checking"}]
    assert ss.rule_no_slug_collision(_cluster(["a"], label="Fact Checking"), spaces) is False
    assert ss.rule_no_slug_collision(_cluster(["a"], label="Fact Checking", kind="existing"), spaces) is True
    assert ss.rule_no_slug_collision(_cluster(["a"], label="Weather"), spaces) is True


def test_gate_sorts_by_size_caps_the_scan_and_respects_the_pending_room():
    big = [_mk(title=f"b{i}", days_ago=i % 5 + 1) for i in range(6)]
    small = [_mk(title=f"s{i}", days_ago=i % 5 + 1) for i in range(5)]
    third = [_mk(title=f"t{i}", days_ago=i % 5 + 1) for i in range(5)]
    rows = ss.collect_candidates(30)
    clusters = [
        _cluster(small, topic_key="weather", label="Weather"),
        _cluster(big),
        _cluster(third, topic_key="invoices", label="Invoices"),
    ]

    kept = ss.gate_clusters(clusters, rows, [], [], [], min_sessions=5, min_days=3)
    assert [c["topic_key"] for c in kept] == ["fact-checking", "weather"]  # biggest first, 2 per scan

    room = ss.gate_clusters(
        clusters, rows, [], [], [{"topic_key": "x"}] * (ss.MAX_PENDING - 1), min_sessions=5, min_days=3
    )
    assert len(room) == 1


def test_gate_colors_avoid_the_swatches_existing_spaces_already_wear():
    members = [_mk(title=f"m{i}", days_ago=i % 5 + 1) for i in range(5)]
    rows = ss.collect_candidates(30)
    spaces = [{"id": "a", "slug": "a", "label": "A", "color": ss.SWATCHES[0]}]
    kept = ss.gate_clusters([_cluster(members)], rows, spaces, [], [], min_sessions=5, min_days=3)
    assert kept[0]["color"] == ss.SWATCHES[1]


def test_gate_keeps_one_of_two_clusters_sharing_a_normalized_key():
    members = [_mk(title=f"m{i}", days_ago=i % 5 + 1) for i in range(6)]
    rows = ss.collect_candidates(30)
    clusters = [
        _cluster(members, topic_key="fact-checking"),
        _cluster(members[:5], topic_key="Fact Checking", label="Fact Checking Too"),
    ]
    kept = ss.gate_clusters(clusters, rows, [], [], [], min_sessions=5, min_days=3)
    assert len(kept) == 1


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_pending_rows_expire_past_the_ttl_and_terminal_rows_do_not():
    fresh = db.add_space_suggestion("new", "fresh", "Fresh", "#7c9cff", "why", ["a"])
    stale = db.add_space_suggestion("new", "stale", "Stale", "#7c9cff", "why", ["b"])
    done = db.add_space_suggestion("new", "done", "Done", "#7c9cff", "why", ["c"])
    db.set_space_suggestion_status(done["id"], "rejected")
    with connect_sessions() as conn:
        conn.execute(
            "UPDATE space_suggestions SET created_at = ? WHERE id IN (?, ?)", (_ago(40), stale["id"], done["id"])
        )

    assert db.expire_space_suggestions(_ago(14)) == 1
    assert db.get_space_suggestion(stale["id"])["status"] == "expired"
    assert db.get_space_suggestion(stale["id"])["resolved_at"]
    assert db.get_space_suggestion(fresh["id"])["status"] == "pending"
    assert db.get_space_suggestion(done["id"])["status"] == "rejected"


async def test_scan_expires_stale_pending_rows_before_it_decides(monkeypatch):
    _stub_llm(monkeypatch, {"clusters": []})
    stale = db.add_space_suggestion("new", "stale", "Stale", "#7c9cff", "why", ["b"])
    with connect_sessions() as conn:
        conn.execute("UPDATE space_suggestions SET created_at = ? WHERE id = ?", (_ago(40), stale["id"]))

    await ss.scan(force=True, dry_run=True)
    assert db.get_space_suggestion(stale["id"])["status"] == "expired"


# ---------------------------------------------------------------------------
# scan: short-circuits, force, dry_run
# ---------------------------------------------------------------------------


def _seed_a_habit(n=12):
    return [_mk(title=f"Claim {i}", days_ago=i % 5 + 1) for i in range(n)]


async def test_scan_short_circuits_on_the_interval_and_force_bypasses_it(monkeypatch):
    seen = _stub_llm(monkeypatch, {"clusters": []})
    _seed_a_habit()
    import time as _time

    db.set_snooze_state(ss.LAST_SCAN_KEY, str(_time.time()))

    assert (await ss.scan())["skipped"] == "interval"
    assert "messages" not in seen  # no model call at all
    assert "skipped" not in await ss.scan(force=True)


async def test_scan_short_circuits_when_nothing_new_arrived(monkeypatch):
    _stub_llm(monkeypatch, {"clusters": []})
    _seed_a_habit()
    db.set_snooze_state(ss.LAST_SEEN_KEY, _ago(365))  # everything is "new"
    assert "skipped" not in await ss.scan()

    # After that scan the watermark is now, so the same sessions are old news.
    assert (await ss.scan(force=False))["skipped"] in ("interval", "nothing_new")
    db.set_snooze_state(ss.LAST_SCAN_KEY, "0")
    assert (await ss.scan())["skipped"] == "nothing_new"
    assert "skipped" not in await ss.scan(force=True)


async def test_scan_pauses_while_the_user_has_a_backlog_to_review(monkeypatch):
    _stub_llm(monkeypatch, {"clusters": []})
    _seed_a_habit()
    for i in range(ss.MAX_PENDING):
        db.add_space_suggestion("new", f"t{i}", f"T{i}", "#7c9cff", "why", ["a"])
    assert (await ss.scan())["skipped"] == "pending_full"
    assert "skipped" not in await ss.scan(force=True)


async def test_scan_skips_a_window_too_thin_to_group(monkeypatch):
    _stub_llm(monkeypatch, {"clusters": []})
    _mk(title="only one")
    assert (await ss.scan(force=True))["skipped"] == "too_few_candidates"


async def test_dry_run_stores_nothing_and_stamps_no_watermark(monkeypatch):
    members = _seed_a_habit()
    _stub_llm(monkeypatch, {"clusters": [_cluster(members)]})

    result = await ss.scan(force=True, dry_run=True)
    assert result["dry_run"] is True
    assert len(result["kept"]) == 1 and result["scanned"] == 12
    assert db.list_space_suggestions() == []
    assert db.get_notifications() == []
    assert db.get_snooze_state(ss.LAST_SCAN_KEY) is None
    assert db.get_snooze_state(ss.LAST_SEEN_KEY) is None


async def test_a_stored_suggestion_raises_exactly_one_notification(monkeypatch):
    members = _seed_a_habit()
    _stub_llm(monkeypatch, {"clusters": [_cluster(members)]})

    result = await ss.scan(force=True)
    rows = db.list_space_suggestions("pending")
    assert len(rows) == 1
    row = rows[0]
    assert row["topic_key"] == "fact-checking" and row["session_ids"] == members
    assert result["kept"][0]["id"] == row["id"]

    notes = db.get_notifications()
    assert len(notes) == 1
    assert notes[0]["title"] == "Suggested space: Fact Checking"
    assert "Review it under Spaces in the sidebar." in notes[0]["body"]
    # The dedup key is what stops a re-scan re-ringing the same bell.
    assert db.get_snooze_state(
        f"notify_dedup:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}:space_suggest:{row['id']}"
    )
    assert db.get_snooze_state(ss.LAST_SCAN_KEY) and db.get_snooze_state(ss.LAST_SEEN_KEY)


async def test_an_existing_kind_suggestion_names_the_space_in_its_notification(monkeypatch):
    sp = db.create_space("Pernix", "#7c9cff", "pernix")
    members = _seed_a_habit()
    _stub_llm(monkeypatch, {"clusters": [_cluster(members, kind="existing", existing_space_id=sp["id"])]})

    await ss.scan(force=True)
    assert db.get_notifications()[0]["title"] == "12 chats belong in Pernix"


async def test_scan_stamps_watermarks_even_when_nothing_survives(monkeypatch):
    _seed_a_habit()
    _stub_llm(monkeypatch, {"clusters": []})
    result = await ss.scan(force=True)
    assert result["kept"] == [] and db.list_space_suggestions() == []
    assert db.get_snooze_state(ss.LAST_SCAN_KEY)


async def test_a_declined_topic_is_never_proposed_again(monkeypatch):
    members = _seed_a_habit()
    _stub_llm(monkeypatch, {"clusters": [_cluster(members)]})
    await ss.scan(force=True)
    row = db.list_space_suggestions("pending")[0]
    db.set_space_suggestion_status(row["id"], "rejected")

    # Same grouping, new name: overlap suppression catches it anyway.
    _stub_llm(monkeypatch, {"clusters": [_cluster(members, topic_key="claim-review", label="Claim Review")]})
    again = await ss.scan(force=True)
    assert again["kept"] == []
    assert db.list_space_suggestions("pending") == []


async def test_scan_returns_an_error_instead_of_raising(monkeypatch):
    _seed_a_habit()

    async def _boom(*a, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(ss, "chat_with_backup", _boom)
    monkeypatch.setattr(ss, "get_llm_client", lambda: object())
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    result = await ss.scan(force=True)
    assert "provider down" in result["error"]
    assert db.list_space_suggestions() == []


async def test_a_second_scan_is_refused_while_one_holds_the_lock():
    async with ss._scan_lock:
        assert ss.scan_running() is True
        assert (await ss.scan(force=True))["skipped"] == "scan_in_progress"
    assert ss.scan_running() is False


# ---------------------------------------------------------------------------
# The snooze rung
# ---------------------------------------------------------------------------


def _ladder(monkeypatch):
    """Run the ladder's gating without running any rung body."""
    from core.snooze import SnoozeRunner

    labels: list[str] = []

    async def _fake_rung(self, label, coro, *, default=None):
        labels.append(label)
        close = getattr(coro, "close", None)
        if close:
            close()
        return default

    monkeypatch.setattr(SnoozeRunner, "_rung", _fake_rung)
    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation
    monkeypatch.setattr(runner, "_llm_ready", lambda: True)
    return runner, labels


async def test_the_rung_is_absent_from_the_ladder_when_the_feature_is_off(monkeypatch):
    called = []

    async def _tripwire(**kw):
        called.append(kw)
        return {}

    monkeypatch.setattr(ss, "scan", _tripwire)
    monkeypatch.setattr("config.settings.space_suggest_enabled", False)
    runner, labels = _ladder(monkeypatch)
    await runner._do_cycle()
    assert "space_suggest" not in labels
    assert called == []


async def test_the_rung_runs_when_the_feature_is_on(monkeypatch):
    monkeypatch.setattr("config.settings.space_suggest_enabled", True)
    runner, labels = _ladder(monkeypatch)
    await runner._do_cycle()
    assert "space_suggest" in labels


async def test_the_step_bumps_the_stat_by_what_it_stored(monkeypatch):
    from core.snooze import SnoozeRunner

    async def _fake_scan(**kw):
        return {"kept": [{"id": "a"}, {"id": "b"}]}

    monkeypatch.setattr(ss, "scan", _fake_scan)
    runner = SnoozeRunner()
    await runner._space_suggest_step()
    assert runner.get_stats()["space_suggestions"] == 2


# ---------------------------------------------------------------------------
# Nothing model-supplied becomes a path or a slug
# ---------------------------------------------------------------------------


def test_a_hostile_topic_key_cannot_escape_the_slug_rules():
    cluster = _cluster(["a"], topic_key="../../etc/passwd", label="../../etc/passwd")
    key = ss.normalize_topic_key(cluster)
    assert key == "etc-passwd"
    assert "/" not in key and "." not in key
    assert len(ss.normalize_topic_key(_cluster(["a"], topic_key="x" * 400))) <= spaces_lib.MAX_SLUG_LEN
