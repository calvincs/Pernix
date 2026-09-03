"""Pernix — H2 learned model routing (§12.4) + H4 wiki-links (§12.5).

H2: post-mortems → model_route counters inside synthesis → exception brief
in the scout prompt. H4: [[file-name]]/[[file@epoch]] refs expand one hop
at recall, labeled source="link"; sanitize + consolidation preserve them.
"""

import json

from db import models as db

# ---------------------------------------------------------------------------
# H2: attribution
# ---------------------------------------------------------------------------


def _pm_row(verdict="retry", agent_model="qwen3:32b", task_category="tasks", **payload_extra):
    payload = {
        "agent_model": agent_model,
        "task_category": task_category,
        "tool_summary": {},
        **payload_extra,
    }
    return {
        "verdict": verdict,
        "failure_cause": "agent",
        "confidence": 0.9,
        "payload_json": json.dumps(payload),
        "session_id": "s",
        "attempt": 1,
    }


def test_attribute_emits_model_route():
    from core.synthesis import attribute

    attrs = attribute(_pm_row(verdict="retry"))
    routes = [a for a in attrs if a.signal_type == "model_route"]
    assert len(routes) == 1
    assert routes[0].subject == "qwen3:32b|tasks"
    assert routes[0].delta_failures == 1 and routes[0].delta_successes == 0

    attrs = attribute(_pm_row(verdict="pass"))
    routes = [a for a in attrs if a.signal_type == "model_route"]
    assert routes[0].delta_successes == 1

    # No model recorded -> no route attribution; category defaults to general.
    assert not [a for a in attribute(_pm_row(agent_model="")) if a.signal_type == "model_route"]
    attrs = attribute(_pm_row(task_category=""))
    assert attrs[-1].subject == "qwen3:32b|general"


def test_attribute_model_route_inherits_canary_exclusion():
    from core.synthesis import attribute

    assert attribute(_pm_row(session_type="canary")) == []


def test_reflect_reads_live_model_override(monkeypatch):
    """§12.4 scan finding: agent_model must come from the in-memory session."""
    from types import SimpleNamespace

    from core.reflect import ReflectResult, _write_post_mortem

    sid = db.create_session(title="m")
    live = SimpleNamespace(model_override="anthropic/claude-sonnet-4")
    monkeypatch.setattr("sessions.manager.get_manager", lambda: SimpleNamespace(get=lambda _s: live))
    _write_post_mortem(sid, 1, ReflectResult(verdict="pass", reasoning="ok"), None, {})
    payload = json.loads(db.list_post_mortems(session_id=sid)[0]["payload_json"])
    assert payload["agent_model"] == "anthropic/claude-sonnet-4"


# ---------------------------------------------------------------------------
# H2: brief
# ---------------------------------------------------------------------------


def _seed_route(subject, wins, losses):
    for _ in range(wins):
        db.upsert_signal("model_route", subject, delta_successes=1)
    for _ in range(losses):
        db.upsert_signal("model_route", subject, delta_failures=1)


def test_brief_is_exception_report():
    from core.synthesis import build_model_routing_brief

    assert build_model_routing_brief() is None  # no signal -> no bytes
    _seed_route("good-model|coding", 9, 1)  # healthy -> omitted
    _seed_route("bad-model|coding", 2, 6)  # degraded -> listed
    _seed_route("thin-model|coding", 0, 2)  # under min obs -> omitted
    brief = build_model_routing_brief()
    assert brief is not None
    assert "[MODEL ROUTING INTEL]" in brief
    assert "bad-model on coding: 25% pass over 8 turns" in brief
    assert "good-model" not in brief and "thin-model" not in brief


def test_scout_gather_uses_brief(monkeypatch):
    _seed_route("bad-model|coding", 1, 7)
    from core.synthesis import build_model_routing_brief

    assert "bad-model" in build_model_routing_brief()


# ---------------------------------------------------------------------------
# H4: wiki-links
# ---------------------------------------------------------------------------


def test_extract_links_and_sanitize_preserves():
    from core.memory.format import extract_links, sanitize_entry_content

    content = "See [[pernix.decisions]] and [[pernix.lessons@1700000000]]; also [[pernix.decisions]] again."
    assert extract_links(content) == ["pernix.decisions", "pernix.lessons@1700000000"]
    assert extract_links("no links here") == []
    # sanitize only rewrites bare --- lines; links survive verbatim.
    assert "[[pernix.decisions]]" in sanitize_entry_content(content)


def test_recall_expands_file_and_entry_refs():
    # Conftest already isolates + initializes memory_dir per test; a fresh
    # monkeypatched dir would point at an uninitialized DB.
    import core.memory.store as store_mod
    from core.memory.store import get_memory_store

    store_mod._store_instance = None
    store = get_memory_store()

    store.add_entry(
        content="Deploys always go through staging first. [[release-notes]]", file_name="ops.rules", skip_dedup=True
    )
    store.add_entry(content="v2.3 shipped the new gate runner.", file_name="release-notes", skip_dedup=True)

    hits = store.search("staging deploys", mode="bm25", expand_wikilinks=True, _track_hits=False)
    sources = {r.source for r in hits}
    files = {r.entry.file_name for r in hits}
    assert "ops.rules" in files
    assert "release-notes" in files  # pulled by reference, not by keyword
    assert "link" in sources
    # Without the flag: no expansion.
    hits2 = store.search("staging deploys", mode="bm25", _track_hits=False)
    assert "release-notes" not in {r.entry.file_name for r in hits2}
    store_mod._store_instance = None


def test_dangling_link_degrades_silently():
    import core.memory.store as store_mod
    from core.memory.store import get_memory_store

    store_mod._store_instance = None
    store = get_memory_store()
    store.add_entry(content="Refers to [[gone-file]] which was rerouted away.", file_name="ops.rules", skip_dedup=True)
    hits = store.search("rerouted refers", mode="bm25", expand_wikilinks=True, _track_hits=False)
    assert all(r.source != "link" for r in hits)  # no expansion, no crash
    store_mod._store_instance = None


def test_consolidation_prompt_preserves_links():
    from core.memory.consolidate import _LLM_MERGE_PROMPT

    assert "[[file-name]]" in _LLM_MERGE_PROMPT and "verbatim" in _LLM_MERGE_PROMPT


# ---------------------------------------------------------------------------
# Task-type taxonomy (the outcome-stats axis) + decoupled resource channels
# ---------------------------------------------------------------------------


def test_task_category_stamps_from_scout_task_type():
    from core.reflect import ReflectResult, _write_post_mortem
    from core.scout.report import ScoutReport

    sid = db.create_session(title="tt")
    report = ScoutReport(task_type="research", execution_mode="inline")
    _write_post_mortem(sid, 1, ReflectResult(verdict="pass", reasoning="ok"), report, {})
    payload = json.loads(db.list_post_mortems(session_id=sid)[0]["payload_json"])
    assert payload["task_category"] == "research"
    assert payload["scout_summary"]["task_type"] == "research"


def test_task_category_falls_back_to_execution_mode():
    """Reports predating the field (cache/fallback/older deploys) keep the
    legacy stamp so rows never lose their category outright."""
    from core.reflect import ReflectResult, _write_post_mortem
    from core.scout.report import ScoutReport

    sid = db.create_session(title="tt2")
    report = ScoutReport(execution_mode="tasks")  # no task_type
    _write_post_mortem(sid, 1, ReflectResult(verdict="pass", reasoning="ok"), report, {})
    payload = json.loads(db.list_post_mortems(session_id=sid)[0]["payload_json"])
    assert payload["task_category"] == "tasks"


def test_extract_report_clamps_task_type():
    from core.scout.runner import _extract_report

    base = {"recommended_tools": [], "approach_guidance": "x" * 40}
    assert _extract_report({**base, "task_type": "coding"}).task_type == "coding"
    assert _extract_report({**base, "task_type": "Research"}).task_type == "research"
    assert _extract_report({**base, "task_type": "galactic"}).task_type == ""
    assert _extract_report(base).task_type == ""


def test_turn_metrics_stamp_from_token_usage():
    from core.reflect import ReflectResult, _write_post_mortem

    sid = db.create_session(title="tm")
    msg_id = db.add_message(sid, "user", "do the thing")
    db.add_token_usage(session_id=sid, model="m1", prompt_tokens=1000, completion_tokens=200, total_tokens=1200)
    db.add_token_usage(session_id=sid, model="m1", prompt_tokens=2000, completion_tokens=300, total_tokens=2300)
    _write_post_mortem(sid, 1, ReflectResult(verdict="pass", reasoning="ok"), None, {}, turn_user_msg_id=msg_id)
    payload = json.loads(db.list_post_mortems(session_id=sid)[0]["payload_json"])
    tm = payload.get("turn_metrics")
    assert tm is not None
    assert tm["tokens"] == 3500 and tm["llm_calls"] == 2
    assert tm["wall_ms"] >= 0


def test_attribute_model_route_carries_metrics():
    from core.synthesis import attribute

    attrs = attribute(_pm_row(verdict="pass", turn_metrics={"tokens": 42000, "wall_ms": 65000}))
    route = [a for a in attrs if a.signal_type == "model_route"][0]
    assert route.metrics == {"tokens": 42000, "wall_ms": 65000}
    # Absent/zero metrics -> None (no accumulator churn from empty stamps).
    attrs = attribute(_pm_row(verdict="pass"))
    assert [a for a in attrs if a.signal_type == "model_route"][0].metrics is None


def test_apply_attributions_accumulates_and_preserves_metrics():
    from core.synthesis import Attribution, apply_attributions

    subj = "metered-model|research"
    a = Attribution("model_route", subj, delta_failures=1, rationale="r", metrics={"tokens": 10000, "wall_ms": 30000})
    b = Attribution("model_route", subj, delta_failures=1, rationale="r", metrics={"tokens": 20000, "wall_ms": 60000})
    apply_attributions([a, b])
    payload = json.loads(db.get_signal("model_route", subj)["payload_json"])
    assert payload["m_tokens_total"] == 30000
    assert payload["m_wall_ms_total"] == 90000
    assert payload["m_count"] == 2
    # A metric-less observation must NOT wipe the accumulators.
    apply_attributions([Attribution("model_route", subj, delta_failures=1, rationale="old row")])
    payload = json.loads(db.get_signal("model_route", subj)["payload_json"])
    assert payload["m_count"] == 2 and payload["m_tokens_total"] == 30000


def test_brief_renders_resource_channels():
    from core.synthesis import Attribution, apply_attributions, build_model_routing_brief

    subj = "slow-model|research"
    apply_attributions(
        [
            Attribution(
                "model_route", subj, delta_failures=1, rationale="r", metrics={"tokens": 40000, "wall_ms": 90000}
            )
            for _ in range(6)
        ]
    )
    brief = build_model_routing_brief()
    assert brief is not None
    assert "slow-model on research: 0% pass over 6 turns" in brief
    assert "avg ~40k tok" in brief and "~90s/turn" in brief


def test_brief_skips_stale_rows():
    from core.synthesis import build_model_routing_brief

    _seed_route("old-model|inline", 0, 8)  # would render if fresh
    with db.connect_sessions() as conn:
        conn.execute(
            "UPDATE scout_signals SET last_reinforced_at = '2020-01-01T00:00:00+00:00' "
            "WHERE signal_type='model_route' AND subject='old-model|inline'"
        )
    brief = build_model_routing_brief()
    assert brief is None or "old-model" not in brief


def test_turn_metrics_window_is_bounded_by_turn_end():
    """Deferred reflect grades minutes after the turn — wall_ms must anchor
    on the turn's last message, and a next turn's tokens must not leak in."""
    from core.reflect import ReflectResult, _write_post_mortem

    sid = db.create_session(title="tm-bounded")
    u1 = db.add_message(sid, "user", "turn one")
    db.add_token_usage(session_id=sid, model="m1", total_tokens=1000)
    a1 = db.add_message(sid, "assistant", "done")
    u2 = db.add_message(sid, "user", "turn two already running")
    db.add_token_usage(session_id=sid, model="m1", total_tokens=7777)  # next turn's spend
    with db.connect_sessions() as conn:
        conn.execute("UPDATE messages SET created_at='2026-01-01T10:00:00+00:00' WHERE id=?", (u1,))
        conn.execute("UPDATE messages SET created_at='2026-01-01T10:01:00+00:00' WHERE id=?", (a1,))
        conn.execute("UPDATE messages SET created_at='2026-01-01T10:05:00+00:00' WHERE id=?", (u2,))
        conn.execute(
            "UPDATE token_usage SET created_at='2026-01-01 10:00:30' WHERE session_id=? AND total_tokens=1000", (sid,)
        )
        conn.execute(
            "UPDATE token_usage SET created_at='2026-01-01 10:05:30' WHERE session_id=? AND total_tokens=7777", (sid,)
        )
    _write_post_mortem(sid, 1, ReflectResult(verdict="pass", reasoning="ok"), None, {}, turn_user_msg_id=u1)
    payload = json.loads(db.list_post_mortems(session_id=sid)[0]["payload_json"])
    tm = payload["turn_metrics"]
    assert tm["tokens"] == 1000  # the 7777 next-turn row is outside the window
    assert tm["wall_ms"] == 60_000  # turn end anchor, not grade time
