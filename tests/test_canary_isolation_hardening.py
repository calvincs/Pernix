"""Regression tests for the 2026-09-04 trust-loop hardening, W5.

Plan principle §5 — "eval data stays out of memory, memory stays out of eval"
— was aspirational in three places: refine could be pointed at a canary
session by any direct caller, the scout preloaded the whole memory index into
every canary's plan, and `list_gates` handed the agent its own answer key.
Each closed leak gets an assertion here, and the generated sentinels get a
correctness sweep across seeds so "the fixture varies per run" is a fact
rather than a claim.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from db import models as db

# ---------------------------------------------------------------------------
# (a) Canary transcripts are never distilled into memory
# ---------------------------------------------------------------------------


async def test_distill_session_refuses_a_canary_transcript(monkeypatch):
    """The guard lives on distill_session itself, not only on its callers:
    it is the single funnel every distill path passes through."""
    from core.memory import distill

    touched: list[str] = []

    def _boom():
        touched.append("store")
        raise AssertionError("canary transcript reached the memory store")

    monkeypatch.setattr("core.memory.store.get_memory_store", _boom)

    messages = [
        {"role": "user", "content": "x" * 400},
        {"role": "assistant", "content": "y" * 400},
    ]
    await distill.distill_session("s1", "Canary: gen-grep-count", messages, session_type="canary")
    assert touched == []


async def test_distill_session_still_runs_for_normal_sessions(monkeypatch):
    """The guard is type-scoped — it must not silently disable distillation."""
    from core.memory import distill

    touched: list[str] = []

    def _seen():
        touched.append("store")
        return None  # distill bails right after, which is all we need to observe

    monkeypatch.setattr("core.memory.store.get_memory_store", _seen)
    await distill.distill_session("s1", "Real work", [{"role": "user", "content": "hi"}], session_type="normal")
    assert touched == ["store"]


def test_snooze_catchup_selector_excludes_canary_sessions():
    canary = db.create_session(title="Canary: gen-file-create", session_type="canary")
    normal = db.create_session(title="Real chat", session_type="normal")
    for sid in (canary, normal):
        for i in range(3):
            db.add_message(sid, "user", "please do the thing " * 30)
            db.add_message(sid, "assistant", "done " * 60)
    picked = {r["id"] for r in db.get_unreviewed_sessions(min_age_minutes=0, limit=50)}
    assert canary not in picked


def test_user_insight_sweep_sql_excludes_canary_sessions():
    """core/snooze.py::_extract_user_insights excluded workers only — a
    canary session with a stamped snooze_reviewed_at qualified for profiling
    straight into user.profile."""
    import core.snooze

    src = Path(core.snooze.__file__).read_text(encoding="utf-8")
    assert "AND s.session_type NOT IN ('worker', 'canary')" in src
    assert "AND s.session_type != 'worker'" not in src


# ---------------------------------------------------------------------------
# (b) Refine skips canary sessions
# ---------------------------------------------------------------------------


async def test_refine_skips_a_canary_session():
    from core.refine import run_for_session

    sid = db.create_session(title="Canary: gen-json-transform", session_type="canary")
    db.add_message(sid, "user", "aggregate the orders")
    db.add_message(sid, "assistant", "wrote output.json")

    stats = await run_for_session(sid)
    assert stats["skipped_reason"] == "canary_session"
    assert stats["proposals_saved"] == 0 and stats["lessons_saved"] == 0


async def test_refine_still_names_worker_skips_the_old_way():
    from core.refine import run_for_session

    sid = db.create_session(title="worker", session_type="worker")
    db.add_message(sid, "user", "go")
    db.add_message(sid, "assistant", "done")
    assert (await run_for_session(sid))["skipped_reason"] == "worker_session"


def test_refine_selector_excludes_canary_sessions():
    canary = db.create_session(title="Canary: x", session_type="canary")
    normal = db.create_session(title="Real chat", session_type="normal")
    for sid in (canary, normal):
        db.add_message(sid, "user", "do it")
        db.add_message(sid, "assistant", "did it")
    picked = {r["id"] for r in db.get_unrefined_sessions(min_idle_minutes=0, limit=50)}
    assert canary not in picked


# ---------------------------------------------------------------------------
# (c) No memory preload / deep_recall inside a canary session's scout
# ---------------------------------------------------------------------------


def _brief(session_type: str):
    from core.scout.report import SessionBrief

    return SessionBrief(session_id="s", is_fresh=True, session_type=session_type)


def test_memory_recall_denied_only_for_canary_sessions():
    from core.scout.runner import memory_recall_denied

    assert memory_recall_denied(_brief("canary")) is True
    for kind in ("normal", "worker", "cron", "snooze", "rlm"):
        assert memory_recall_denied(_brief(kind)) is False


def test_scout_tool_schema_drops_search_memory_for_canary_sessions():
    from core.scout.runner import _SCOUT_TOOLS, scout_tools_for

    normal = {t["function"]["name"] for t in scout_tools_for(_brief("normal"))}
    canary = {t["function"]["name"] for t in scout_tools_for(_brief("canary"))}
    assert "search_memory" in normal
    assert "search_memory" not in canary
    # Everything else survives — this is a memory fence, not a lobotomy.
    assert canary == normal - {"search_memory"}
    assert "submit_report" in canary
    assert len(_SCOUT_TOOLS) == len(normal)


def test_scout_tool_executor_refuses_search_memory_in_a_canary_session(monkeypatch):
    """Backstop for a model that calls a tool its schema no longer offers."""
    from core.scout import runner as scout_runner

    monkeypatch.setattr(
        "core.memory.store.get_memory_store",
        lambda: (_ for _ in ()).throw(AssertionError("memory store touched in a canary session")),
    )
    out = scout_runner._exec_scout_tool("search_memory", {"query": "the answer"}, _brief("canary"))
    assert "not available" in out.lower()


async def _run_scout_counting_memory(session_type: str) -> dict:
    """Run the real preload with the memory surfaces instrumented."""
    from tests.conftest import FakeLLMClient

    calls = {"store": 0, "fts": 0}

    def _store():
        calls["store"] += 1
        return None

    def _fts(*a, **kw):
        calls["fts"] += 1
        return []

    fake = FakeLLMClient()
    fake.has_capacity = MagicMock(return_value=True)

    with (
        patch("core.memory.store.get_memory_store", _store),
        patch("db.models.search_messages_fts", _fts),
        patch("core.llm.client.get_llm_client", return_value=fake),
        patch("core.scout.runner.settings") as mock_settings,
    ):
        mock_settings.background_model = ""
        mock_settings.llm_model = "test-model"
        mock_settings.workspace_dir = "/tmp/nonexistent-w5"
        mock_settings.scout_preload_memory_char_limit = 300
        mock_settings.candor_enabled = False
        mock_settings.candor_scout_brief = False
        mock_settings.adaptive_enabled = False

        from core.scout.runner import _run_scout_llm

        await _run_scout_llm("what is the error count", _brief(session_type))
    return calls


async def test_canary_scout_preload_reads_no_memory_and_no_other_sessions():
    canary = await _run_scout_counting_memory("canary")
    assert canary == {"store": 0, "fts": 0}


async def test_normal_scout_preload_still_reads_memory():
    """Proves the counters above would have fired — otherwise the canary
    assertion passes for the wrong reason."""
    normal = await _run_scout_counting_memory("normal")
    assert normal["store"] > 0
    assert normal["fts"] > 0


def test_scout_fallback_report_recalls_nothing_for_canary_sessions(monkeypatch):
    from core.scout.runner import _build_fallback_report

    monkeypatch.setattr(
        "core.memory.store.get_memory_store",
        lambda: (_ for _ in ()).throw(AssertionError("fallback recall ran in a canary session")),
    )
    report = _build_fallback_report("count the errors", _brief("canary"))
    assert report.memory_context == ""


# ---------------------------------------------------------------------------
# (c)/(d) The canary tool allowlist
# ---------------------------------------------------------------------------


def test_canary_allowlist_has_no_memory_tool_at_all():
    from core.canary.runner import CANARY_TOOL_ALLOWLIST

    for denied in ("remember", "ingest", "update_memory", "forget", "recall", "deep_recall"):
        assert denied not in CANARY_TOOL_ALLOWLIST, denied


def test_canary_allowlist_hides_the_answer_key():
    """`list_gates` prints each gate's command verbatim, and a canary gate
    command IS the expected answer. Generated fixtures put the whole
    expectation in that string, so the tool cannot stay."""
    from core.canary.runner import CANARY_TOOL_ALLOWLIST

    assert "list_gates" not in CANARY_TOOL_ALLOWLIST


def test_canary_allowlist_keeps_the_treatment_and_the_work_tools():
    from core.canary.runner import CANARY_TOOL_ALLOWLIST

    for kept in ("bash", "file_read", "file_write", "grep", "glob", "repl"):
        assert kept in CANARY_TOOL_ALLOWLIST, kept
    # Skills are part of the treatment being measured, so discovery stays.
    for kept in ("discover_skills", "load_skill", "read_skill_resource"):
        assert kept in CANARY_TOOL_ALLOWLIST, kept


async def test_memory_write_tools_are_refused_inside_a_canary_session(monkeypatch):
    """Belt (registry denial) and braces (session allowlist) — assert both,
    through the real executor."""
    from types import SimpleNamespace

    from core.canary.runner import CANARY_TOOL_ALLOWLIST
    from core.tools.builtin import memory_tools
    from core.tools.executor import _execute_single
    from core.tools.registry import ToolRegistry

    reg = ToolRegistry()
    memory_tools.register(reg)
    session = SimpleNamespace(
        session_type="canary",
        workspace_override=None,
        tool_allowlist=CANARY_TOOL_ALLOWLIST,
    )
    monkeypatch.setattr("sessions.manager.get_manager", lambda: SimpleNamespace(get=lambda sid: session))

    for name in ("remember", "ingest", "update_memory", "forget", "recall", "deep_recall"):
        result = await _execute_single(name, {}, {"session_id": "s"}, reg)
        assert result.was_error, name


# ---------------------------------------------------------------------------
# (e) Every other learning sweep
# ---------------------------------------------------------------------------


def test_space_suggestions_only_ever_see_normal_sessions():
    canary = db.create_session(title="Canary: gen-grep-count", session_type="canary")
    normal = db.create_session(title="Weekly report", session_type="normal")
    db.update_session(normal, subtitle="reporting")
    db.update_session(canary, subtitle="scored run")
    for sid in (canary, normal):
        db.add_message(sid, "user", "do the thing")
        db.add_message(sid, "assistant", "done")
    ids = {r["id"] for r in db.list_space_suggest_candidates("1970-01-01T00:00:00+00:00")}
    assert canary not in ids


async def test_auto_title_never_fires_for_a_canary_session(monkeypatch):
    """Titles are LLM-written only for sessions still called 'New session';
    the runner names every canary at creation, so the titler never sees one."""
    from sessions import hooks

    called: list[str] = []

    async def _titler(session_id, emit=None):
        called.append(session_id)

    monkeypatch.setattr(hooks, "_auto_title", _titler)
    monkeypatch.setattr("config.settings.memory_recall", False)
    monkeypatch.setattr("config.settings.gates_enabled", False)
    monkeypatch.setattr("config.settings.reflect_enabled", False)
    monkeypatch.setattr("config.settings.eval_auto", False)
    monkeypatch.setattr("config.settings.candor_enabled", False)
    monkeypatch.setattr("config.settings.telos_enabled", False)

    sid = db.create_session(title="Canary: gen-file-create", session_type="canary")
    await hooks.run_post_task_hooks(sid)
    assert called == []


def test_session_fts_hides_canary_messages_from_other_sessions():
    canary = db.create_session(title="Canary: gen-grep-count", session_type="canary")
    normal = db.create_session(title="Real chat", session_type="normal")
    db.add_message(canary, "assistant", "the zarquon count is fourteen")
    db.add_message(normal, "assistant", "the zarquon count is fourteen")
    hits = db.search_messages_fts("zarquon", limit=20)
    assert {h["session_id"] for h in hits} == {normal}
