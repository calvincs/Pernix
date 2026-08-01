"""Tests for the rlm_process tool wiring, rlm_runs persistence, and migration v18."""

import json
from pathlib import Path

import pytest

from config import settings
from core.extensions.rlm import _resolve_sources, register, rlm_process, runs
from core.extensions.rlm.types import RLMRunResult
from core.tools.registry import ToolRegistry
from db import models as db
from db.database import connect_sessions

# =============================================================================
# migration v18
# =============================================================================


def test_v18_rlm_runs_schema():
    with connect_sessions() as conn:
        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(rlm_runs)").fetchall()}
    for expected in (
        "run_id",
        "session_id",
        "parent_run_id",
        "depth",
        "status",
        "task",
        "source_desc",
        "root_model",
        "sub_model",
        "iterations",
        "subcalls",
        "input_chars",
        "answer_preview",
        "error",
        "run_dir",
        "created_at",
        "finished_at",
    ):
        assert expected in cols, f"rlm_runs missing column {expected}"
    assert cols["status"]["dflt_value"] == "'running'"
    with connect_sessions() as conn:
        idx = {r["name"] for r in conn.execute("PRAGMA index_list(rlm_runs)").fetchall()}
    assert {"idx_rlm_runs_session", "idx_rlm_runs_status", "idx_rlm_runs_created"} <= idx


# =============================================================================
# db helpers
# =============================================================================


def _seed_run(run_id="ab12cd34", status=None, created_at=None, parent_run_id=None):
    db.create_rlm_run(
        run_id=run_id,
        session_id="sess-1",
        task="summarize",
        source_desc="big.txt",
        root_model="root-m",
        sub_model="sub-m",
        input_chars=1000,
        run_dir=f"rlm/{run_id}",
        parent_run_id=parent_run_id,
        depth=1 if parent_run_id else 0,
    )
    if status or created_at:
        with connect_sessions() as conn:
            if status:
                conn.execute("UPDATE rlm_runs SET status = ? WHERE run_id = ?", (status, run_id))
            if created_at:
                conn.execute("UPDATE rlm_runs SET created_at = ? WHERE run_id = ?", (created_at, run_id))


def test_rlm_run_lifecycle():
    _seed_run()
    row = db.list_rlm_runs(session_id="sess-1")[0]
    assert row["status"] == "running" and row["finished_at"] is None
    db.finish_rlm_run("ab12cd34", "completed", iterations=4, subcalls=9, answer_preview="the answer")
    row = db.list_rlm_runs()[0]
    assert row["status"] == "completed" and row["iterations"] == 4 and row["finished_at"]


def test_orphan_sweep_only_hits_running():
    _seed_run("run00001")
    _seed_run("run00002", status="completed")
    assert db.fail_orphaned_rlm_runs() == 1
    statuses = {r["run_id"]: r["status"] for r in db.list_rlm_runs()}
    assert statuses == {"run00001": "orphaned", "run00002": "completed"}


def test_retention_listing_excludes_running_and_nested():
    _seed_run("oldfinis", status="completed", created_at="2020-01-01T00:00:00")
    _seed_run("oldrunng", created_at="2020-01-01T00:00:00")  # still running
    _seed_run("oldnest1", status="completed", created_at="2020-01-01T00:00:00", parent_run_id="oldfinis")
    _seed_run("newfinis", status="completed")
    candidates = [r["run_id"] for r in db.list_rlm_runs_before("2021-01-01T00:00:00")]
    assert candidates == ["oldfinis"]
    # deleting the root also removes its nested rows
    assert db.delete_rlm_run("oldfinis") == 2


# =============================================================================
# runs.py artifacts
# =============================================================================


def test_run_dir_and_manifest_lifecycle():
    run_id, run_dir, run_rel = runs.mint_run_dir()
    assert run_dir.is_dir() and run_rel == f"rlm/{run_id}"
    runs.record_start(
        run_id,
        run_dir,
        run_rel,
        session_id="s",
        task="t",
        source_desc="d",
        root_model="rm",
        sub_model="sm",
        input_chars=5,
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "running" and manifest["root_model"] == "rm"
    assert db.list_rlm_runs()[0]["run_id"] == run_id

    result = RLMRunResult(answer="A" * 900, status="completed", iterations=2, subcalls=3, duration=1.5)
    runs.record_finish(run_id, run_dir, result)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["status"] == "completed" and manifest["subcalls"] == 3
    assert db.list_rlm_runs()[0]["answer_preview"] == "A" * 500


def test_nested_run_dir_lives_under_parent():
    _, parent_dir, _ = runs.mint_run_dir()
    sub_id, sub_dir, sub_rel = runs.mint_run_dir(parent_run_dir=parent_dir)
    assert sub_dir.is_dir() and sub_dir.parent == parent_dir / "sub"
    assert sub_rel.endswith(f"sub/{sub_id}")


# =============================================================================
# source resolution
# =============================================================================


def test_resolve_sources_inline_vs_file():
    ws = Path(settings.workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "doc.txt").write_text("file body")

    text, files, desc = _resolve_sources("doc.txt")
    assert text is None and [f.name for f in files] == ["doc.txt"]

    text, files, desc = _resolve_sources("this is just a prompt, not a path")
    assert text == "this is just a prompt, not a path" and files == [] and "inline" in desc

    with pytest.raises(ValueError, match="not found"):
        _resolve_sources(["doc.txt", "missing.txt"])
    with pytest.raises(ValueError, match="empty"):
        _resolve_sources("  ")


# =============================================================================
# tool gating + registration
# =============================================================================


def test_rlm_process_disabled_gate(monkeypatch):
    monkeypatch.setattr(settings, "rlm_enabled", False)
    assert "disabled" in rlm_process("task", "source")


def test_rlm_process_requires_task(monkeypatch):
    monkeypatch.setattr(settings, "rlm_enabled", True)
    assert rlm_process("  ", "source").startswith("Error: task is required")


def test_register_is_hard_gated(monkeypatch):
    reg = ToolRegistry()
    monkeypatch.setattr(settings, "rlm_enabled", False)
    register(reg)
    assert reg.get("rlm_process") is None

    monkeypatch.setattr(settings, "rlm_enabled", True)
    register(reg)
    tool = reg.get("rlm_process")
    assert tool is not None
    assert tool.long_poll and not tool.parallel_safe and tool.safety_level == "caution"
    assert tool.timeout == settings.rlm_timeout_seconds + 60


def test_rlm_process_end_to_end_with_stubbed_engine(monkeypatch, mock_llm_client):
    """Full tool path (source staging, run rows, result assembly) with the
    engine's run() stubbed — engine internals are covered by test_rlm_engine."""
    monkeypatch.setattr(settings, "rlm_enabled", True)
    ws = Path(settings.workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "big.txt").write_text("x" * 5000)

    canned = RLMRunResult(answer="the distilled answer", status="completed", iterations=3, subcalls=7, duration=2.0)
    monkeypatch.setattr("core.extensions.rlm.engine.RLMEngine.run", lambda self: canned)

    out = rlm_process("what is x?", "big.txt", _context={"_loop": object(), "session_id": ""})
    text = out[0] if isinstance(out, tuple) else out
    assert "the distilled answer" in text
    assert "completed, 3 iterations, 7 sub-calls" in text
    assert "trace.jsonl" in text

    row = db.list_rlm_runs()[0]
    assert row["status"] == "completed" and row["subcalls"] == 7
    assert row["task"] == "what is x?" and row["source_desc"] == "big.txt"
    run_dir = ws / row["run_dir"]
    assert (run_dir / "context" / "context_0.txt").read_text() == "x" * 5000


def test_rlm_process_size_gate(monkeypatch):
    monkeypatch.setattr(settings, "rlm_enabled", True)
    monkeypatch.setattr("core.extensions.rlm.MAX_SOURCE_BYTES", 100)
    out = rlm_process("t", "y" * 200, _context={"_loop": object()})
    assert out.startswith("Error: source is") and "cap is 100" in out


def test_rlm_process_budget_preflight_refuses_without_staging(monkeypatch, mock_llm_client):
    """Regression for session a45fa830cef9: runs fd5eea16/9e7d5270 staged
    ~300KB of context, spawned a child, and wrote run rows only to die on the
    session LLM time limit at iteration 0 — and the generic error invited a
    retry that failed identically. When the budget top-up doesn't take, the
    tool must refuse before creating anything, with an answer that tells the
    agent not to retry."""
    monkeypatch.setattr(settings, "rlm_enabled", True)
    monkeypatch.setattr("core.llm.client.ensure_session_budget", lambda sid, need: 0.0)
    monkeypatch.setattr("core.llm.client.session_seconds_remaining", lambda sid: 42.0)

    rlm_root = Path(settings.workspace_dir) / "rlm"
    dirs_before = set(rlm_root.iterdir()) if rlm_root.exists() else set()
    rows_before = len(db.list_rlm_runs())

    out = rlm_process(
        "compare things",
        "inline source text " * 20,
        _context={"_loop": object(), "session_id": "sess-budget-gone"},
    )
    assert isinstance(out, str) and out.startswith("Error:")
    assert "~42s" in out
    assert "Do not call rlm_process again this turn" in out
    assert len(db.list_rlm_runs()) == rows_before, "refused run must leave no rlm_runs row"
    dirs_after = set(rlm_root.iterdir()) if rlm_root.exists() else set()
    assert dirs_after == dirs_before, "refused run must not mint a run dir"


def test_rlm_process_budget_preflight_tops_up_and_proceeds(monkeypatch, mock_llm_client):
    """The pre-flight requests the run's full window (timeout + grace) via
    ensure_session_budget — relative-to-the-clock semantics, so back-to-back
    runs in one turn each get their full window — and proceeds once the
    top-up takes."""
    from core.extensions.rlm import _BUDGET_GRACE

    monkeypatch.setattr(settings, "rlm_enabled", True)
    asked: list[tuple[str, float]] = []

    def fake_ensure(sid, min_remaining):
        asked.append((sid, min_remaining))
        return 99999.0

    monkeypatch.setattr("core.llm.client.ensure_session_budget", fake_ensure)
    monkeypatch.setattr("core.llm.client.session_seconds_remaining", lambda sid: float("inf"))

    canned = RLMRunResult(answer="the topped-up answer", status="completed", iterations=1, subcalls=0, duration=1.0)
    monkeypatch.setattr("core.extensions.rlm.engine.RLMEngine.run", lambda self: canned)

    out = rlm_process("q?", "inline text body", _context={"_loop": object(), "session_id": "sess-budget-ok"})
    text = out[0] if isinstance(out, tuple) else out
    assert "the topped-up answer" in text and "completed" in text
    assert asked == [("sess-budget-ok", float(settings.rlm_timeout_seconds) + _BUDGET_GRACE)]


# =============================================================================
# discoverability wiring (all gated on rlm_enabled)
# =============================================================================


def test_base_system_prompt_gates_rlm_block(monkeypatch):
    from core.context.compiler import _build_base_system_prompt

    monkeypatch.setattr(settings, "rlm_enabled", False)
    assert "rlm_process" not in _build_base_system_prompt()
    monkeypatch.setattr(settings, "rlm_enabled", True)
    assert "RECURSIVE PROCESSING" in _build_base_system_prompt()


def test_scout_prompt_injects_rule_keeping_no_think_last(monkeypatch):
    from core.scout.runner import _scout_system_prompt

    monkeypatch.setattr(settings, "rlm_enabled", False)
    assert "RECURSIVE ANALYSIS" not in _scout_system_prompt()
    monkeypatch.setattr(settings, "rlm_enabled", True)
    prompt = _scout_system_prompt()
    assert "RECURSIVE ANALYSIS" in prompt
    assert prompt.rstrip().endswith("/no_think")
    assert prompt.index("RECURSIVE ANALYSIS") < prompt.index("Do NOT use <think>")


def test_truncation_nudge_gated_on_rlm_enabled(monkeypatch):
    from core.harness import nudges

    text = "⚠ TRUNCATED — showing 500 of 90000 lines. To read more, call file_read(...)"
    monkeypatch.setattr(settings, "rlm_enabled", False)
    assert nudges.evaluate("file_read", text, set()) is None
    monkeypatch.setattr(settings, "rlm_enabled", True)
    hint = nudges.evaluate("file_read", text, set())
    assert hint and "rlm_process" in hint
    # wrong tool -> no fire; second fire in same turn deduped
    assert nudges.evaluate("grep", text, set()) is None
    fired = {"truncated_input_rlm"}
    assert nudges.evaluate("file_read", text, fired) is None


def test_registry_cooccurrence_links_rlm():
    from core.tools.registry import SYNONYMS, TOOL_COOCCURRENCE

    assert "file_read" in TOOL_COOCCURRENCE["rlm_process"]
    assert "rlm_process" not in TOOL_COOCCURRENCE.get("file_read", [])
    assert "corpus" in SYNONYMS["rlm"]


# =============================================================================
# ops: retention + orphan sweep integration
# =============================================================================


async def test_snooze_cleanup_rlm_runs(monkeypatch):
    from core.snooze import SnoozeRunner

    ws = Path(settings.workspace_dir)

    def _seed_with_dir(run_id, status, created_at=None):
        _seed_run(run_id, status=status, created_at=created_at)
        d = ws / "rlm" / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "trace.jsonl").write_text("{}")
        return d

    old_done = _seed_with_dir("olddone1", "completed", "2020-01-01T00:00:00")
    old_running = _seed_with_dir("oldrun01", None, "2020-01-01T00:00:00")
    fresh = _seed_with_dir("freshfin", "completed")

    await SnoozeRunner()._cleanup_rlm_runs()

    assert not old_done.exists(), "old completed run dir should be purged"
    assert old_running.exists(), "running runs are never touched"
    assert fresh.exists(), "recent runs are kept"
    remaining = {r["run_id"] for r in db.list_rlm_runs()}
    assert remaining == {"oldrun01", "freshfin"}


async def test_snooze_cleanup_rlm_runs_zero_case():
    from core.snooze import SnoozeRunner

    await SnoozeRunner()._cleanup_rlm_runs()  # no rows — must not raise


# =============================================================================
# API router (read-only run history)
# =============================================================================


async def test_rlm_runs_api_list_and_detail():
    from fastapi import HTTPException

    from api.routers.rlm import get_rlm_run
    from api.routers.rlm import list_rlm_runs as api_list

    run_id, run_dir, run_rel = runs.mint_run_dir()
    runs.record_start(
        run_id,
        run_dir,
        run_rel,
        session_id="s1",
        task="t",
        source_desc="d",
        root_model="rm",
        sub_model="sm",
        input_chars=9,
    )
    (run_dir / "trace.jsonl").write_text("{}\n")

    listed = await api_list(session_id="s1", limit=5)
    assert listed["runs"][0]["run_id"] == run_id
    assert (await api_list(session_id="other", limit=5))["runs"] == []

    detail = await get_rlm_run(run_id)
    assert detail["manifest"]["root_model"] == "rm"
    assert detail["has_trace"] and detail["trace_path"].endswith("trace.jsonl")
    assert detail["answer_path"] is None  # no answer.txt yet

    with pytest.raises(HTTPException) as exc:
        await get_rlm_run("nope1234")
    assert exc.value.status_code == 404


async def test_rlm_runs_api_limit_clamped():
    from api.routers.rlm import list_rlm_runs as api_list

    out = await api_list(limit=99999)
    assert isinstance(out["runs"], list)  # no explosion; clamp applied internally
