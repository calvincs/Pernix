"""Tests for the skill self-healing loop.

Covers the 2026-08-31 fixes (session 83dc931a8596 post-mortem):
  1. Multi-signal active-skill attribution (path refs + scout mentions,
     not just load_skill).
  2. Re-armable refine watermarks (max message id, awaiting_user excluded).
  3. Failure-arc evidence extraction + machine signal in the refine prompt.
  4. Proposal dedupe across re-refines.
  5. Veto-window auto-apply with machine validation, backups, and day cap.
  6. Skill content-change sweep → memory_stale dream hypotheses.
  7. Migration v32 watermark conversion.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeSkillRegistry:
    def __init__(self, skills: dict, disabled=()):
        self._skills = skills  # name -> SimpleNamespace(name, path)
        self._disabled = set(disabled)

    def get(self, name):
        return self._skills.get(name)

    def exists(self, name):
        return name in self._skills

    def is_disabled(self, name):
        return name in self._disabled

    def all_skills(self):
        return list(self._skills.values())

    def enabled_skills(self):
        return [s for s in self._skills.values() if s.name not in self._disabled]

    def load_instructions(self, name):
        s = self._skills.get(name)
        if not s:
            return None
        md = s.path / "SKILL.md"
        return md.read_text(encoding="utf-8") if md.exists() else None

    def rescan(self, *a, **k):
        return len(self._skills)


def _make_skill_dir(tmp_path, name, body="## Usage\nRun the script.\n\n## Common Failures\nNone known.\n"):
    d = tmp_path / "skills" / name
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n\n{body}", encoding="utf-8")
    (d / "scripts" / "run.py").write_text("print('v1')\n", encoding="utf-8")
    return SimpleNamespace(name=name, path=d)


def _patch_registry(monkeypatch, registry):
    monkeypatch.setattr("core.skills.registry.get_skill_registry", lambda: registry)


def _backdate_session(sid, hours=2):
    from db.database import connect_sessions

    past = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (past, sid))


# ---------------------------------------------------------------------------
# 1. Multi-signal active-skill attribution
# ---------------------------------------------------------------------------


def test_identify_skill_from_bash_path_in_tool_calls(tmp_path, monkeypatch):
    """A session that runs `python ../skills/foo/scripts/run.py` via bash —
    the way scout-planned sessions actually invoke skills — must attribute
    the skill without any load_skill call."""
    from core.refine import _identify_active_skill
    from db import models as db

    skill = _make_skill_dir(tmp_path, "youtube-whisper")
    _patch_registry(monkeypatch, FakeSkillRegistry({"youtube-whisper": skill}))

    sid = db.create_session(title="Path attribution")
    db.add_message(sid, "user", "summarize this video")
    tool_calls = json.dumps(
        [
            {
                "id": "c1",
                "function": {
                    "name": "bash",
                    "arguments": json.dumps({"command": "python ../skills/youtube-whisper/scripts/run.py --url X"}),
                },
            }
        ]
    )
    db.add_message(sid, "assistant", "Running the skill script.", tool_calls=tool_calls)
    db.add_message(sid, "tool", "started", tool_call_id="c1")
    db.add_message(sid, "assistant", "Done.")

    assert _identify_active_skill(db.get_messages(sid)) == "youtube-whisper"


def test_identify_skill_from_scout_plan_mention(tmp_path, monkeypatch):
    """The scout recommends skills by name in its plan — that mention alone
    must be enough when no path ever appears."""
    from core.refine import _identify_active_skill
    from db import models as db

    skill = _make_skill_dir(tmp_path, "roku-cast")
    _patch_registry(monkeypatch, FakeSkillRegistry({"roku-cast": skill}))

    sid = db.create_session(title="Scout attribution")
    db.add_message(sid, "user", "cast this to the tv")
    db.add_message(sid, "scout", json.dumps({"approach": "Use the roku-cast procedure step by step."}))
    db.add_message(sid, "assistant", "Casting now.")

    assert _identify_active_skill(db.get_messages(sid)) == "roku-cast"


def test_identify_skill_ignores_unregistered_path(tmp_path, monkeypatch):
    """A path that matches skills/<name>/ but names no registered skill must
    not be attributed — a stray string can't route proposals into the void."""
    from core.refine import _identify_active_skill
    from db import models as db

    _patch_registry(monkeypatch, FakeSkillRegistry({}))

    sid = db.create_session(title="Unknown path")
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "see ../skills/not-a-real-skill/scripts/x.py")

    assert _identify_active_skill(db.get_messages(sid)) is None


def test_identify_skill_load_skill_still_wins(tmp_path, monkeypatch):
    """Explicit load_skill outranks path references."""
    from core.refine import _identify_active_skill
    from db import models as db

    a = _make_skill_dir(tmp_path, "skill-a")
    b = _make_skill_dir(tmp_path, "skill-b")
    _patch_registry(monkeypatch, FakeSkillRegistry({"skill-a": a, "skill-b": b}))

    sid = db.create_session(title="Precedence")
    db.add_message(sid, "user", "go")
    tool_calls = json.dumps(
        [{"id": "c1", "function": {"name": "load_skill", "arguments": json.dumps({"name": "skill-a"})}}]
    )
    db.add_message(sid, "assistant", "loading", tool_calls=tool_calls)
    db.add_message(sid, "tool", "loaded", tool_call_id="c1")
    db.add_message(sid, "assistant", "also touched ../skills/skill-b/scripts/x.py")

    assert _identify_active_skill(db.get_messages(sid)) == "skill-a"


# ---------------------------------------------------------------------------
# 2. Failure detection + arc extraction
# ---------------------------------------------------------------------------


def test_is_failure_content_markers():
    from core.refine import _is_failure_content

    assert _is_failure_content("Error: command failed")
    assert _is_failure_content("[j1] whisper | state=failed | elapsed=3s | exit=2")
    assert _is_failure_content("Traceback (most recent call last):\n  ...")
    assert not _is_failure_content("[j1] whisper | state=done | elapsed=3s | exit=0")
    assert not _is_failure_content("all good, 200 OK")


def test_failure_arc_dedupes_repolled_jobs():
    """Polling a failed job three times is ONE failure, not three."""
    from core.refine import _build_failure_arc

    messages = [
        {"role": "assistant", "content": "Launching transcription job."},
        {"role": "tool", "content": "[as of 01:20:07Z]\n[b6c9] whisper | state=failed | exit=1"},
        {"role": "tool", "content": "[as of 01:20:12Z]\n[b6c9] whisper | state=failed | exit=1"},
        {"role": "assistant", "content": "Retrying with the small model."},
        {"role": "tool", "content": "[as of 01:25:00Z]\n[8d24] whisper-small | state=failed | exit=1"},
    ]
    arc, count = _build_failure_arc(messages)
    assert count == 2
    assert "FAILURE 1" in arc and "FAILURE 2" in arc
    assert "Launching transcription job." in arc  # intent captured


def test_user_content_carries_machine_signal_and_task_head():
    from core.refine import _build_user_content

    messages = [
        {"role": "user", "content": "summarize the video please"},
        {"role": "assistant", "content": "Trying the skill."},
        {"role": "tool", "content": "Error: GPU OOM"},
        {"role": "assistant", "content": "Worked around it via CPU."},
    ]
    content = _build_user_content({"id": "s1", "title": "t"}, messages, None, "youtube-whisper", "## Usage\nx", {})
    assert "MACHINE SIGNAL" in content
    assert "1 failed tool/job result(s)" in content
    assert "Original task: summarize the video please" in content


def test_user_content_no_signal_without_failures():
    from core.refine import _build_user_content

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    content = _build_user_content({"id": "s1", "title": "t"}, messages, None, None, None, {})
    assert "MACHINE SIGNAL" not in content


# ---------------------------------------------------------------------------
# 3. Re-armable watermarks + awaiting_user exclusion
# ---------------------------------------------------------------------------


def test_unrefined_selection_rearms_on_growth():
    from db import models as db

    sid = db.create_session(title="Re-arm")
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hi back")
    _backdate_session(sid)

    rows = db.get_unrefined_sessions(min_idle_minutes=10, limit=10)
    match = [r for r in rows if r["id"] == sid]
    assert match, "fresh session should be eligible"
    max_id = match[0]["refine_max_message_id"]
    assert max_id > 0

    # Stamp at current max → ineligible.
    db.set_snooze_state(f"refined:{sid}", str(max_id))
    rows = db.get_unrefined_sessions(min_idle_minutes=10, limit=10)
    assert not any(r["id"] == sid for r in rows)

    # Session grows past the watermark → eligible again.
    db.add_message(sid, "user", "actually, one more thing")
    db.add_message(sid, "assistant", "handled it via a workaround")
    _backdate_session(sid)
    rows = db.get_unrefined_sessions(min_idle_minutes=10, limit=10)
    assert any(r["id"] == sid for r in rows), "growth past the watermark must re-arm refine"


def test_unrefined_selection_skips_awaiting_user():
    """A session parked on an unanswered ask_user is mid-task — refine must
    not spend its call on half a story (the 83dc931a8596 failure mode)."""
    from db import models as db
    from db.database import connect_sessions

    sid = db.create_session(title="Parked on ask_user")
    db.add_message(sid, "user", "do the thing")
    db.add_message(sid, "assistant", "I asked you a question and am waiting.")
    _backdate_session(sid)
    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET state_v2 = 'awaiting_user' WHERE id = ?", (sid,))

    rows = db.get_unrefined_sessions(min_idle_minutes=10, limit=10)
    assert not any(r["id"] == sid for r in rows)

    with connect_sessions() as conn:
        conn.execute("UPDATE sessions SET state_v2 = 'idle_ready' WHERE id = ?", (sid,))
    _backdate_session(sid)
    rows = db.get_unrefined_sessions(min_idle_minutes=10, limit=10)
    assert any(r["id"] == sid for r in rows)


def test_migration_v32_converts_iso_watermarks():
    """The v32 statement converts legacy ISO watermark values to the
    session's max message id — 'processed up to now', no refine storm."""
    from db import models as db
    from db.database import MIGRATIONS, connect_sessions

    sid = db.create_session(title="Legacy watermark")
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hi back")
    db.set_snooze_state(f"refined:{sid}", "2026-08-31T01:45:54+00:00")

    stmts = next(stmts for version, _desc, stmts in MIGRATIONS if version == 32)
    with connect_sessions() as conn:
        for stmt in stmts:
            conn.execute(stmt)
        max_id = conn.execute("SELECT MAX(id) FROM messages WHERE session_id = ?", (sid,)).fetchone()[0]

    assert db.get_snooze_state(f"refined:{sid}") == str(max_id)


# ---------------------------------------------------------------------------
# 4. Proposal dedupe across re-refines
# ---------------------------------------------------------------------------


async def test_refine_dedupes_repeat_proposal(mock_llm_client, monkeypatch, tmp_path):
    from core.llm.types import ChatResponse, TokenUsage
    from db import models as db

    monkeypatch.setattr("config.settings.memory_dir", str(tmp_path / "memories"))
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    skill = _make_skill_dir(tmp_path, "dedupe-skill")
    _patch_registry(monkeypatch, FakeSkillRegistry({"dedupe-skill": skill}))

    sid = db.create_session(title="Dedupe")
    db.add_message(sid, "user", "go")
    tool_calls = json.dumps(
        [{"id": "c1", "function": {"name": "load_skill", "arguments": json.dumps({"name": "dedupe-skill"})}}]
    )
    db.add_message(sid, "assistant", "loading", tool_calls=tool_calls)
    db.add_message(sid, "tool", "loaded", tool_call_id="c1")
    db.add_message(sid, "assistant", "done")

    payload = {
        "nothing_actionable": False,
        "proposals": [
            {
                "skill_name": "dedupe-skill",
                "section": "Common Failures",
                "problem": "GPU OOM path undocumented",
                "proposed_change": "On GPU OOM, retry with --device cpu.",
                "confidence": 0.8,
            }
        ],
        "lessons": [],
    }

    def _resp():
        return ChatResponse(
            content=json.dumps(payload),
            tool_calls=None,
            usage=TokenUsage(10, 20, 30),
            model="fake-bg-model",
            provider="fake",
            finish_reason="stop",
        )

    from core.refine import run_for_session

    mock_llm_client.responses = [_resp(), _resp()]
    stats1 = await run_for_session(sid)
    stats2 = await run_for_session(sid)

    assert stats1["proposals_saved"] == 1
    assert stats2["proposals_saved"] == 0
    assert stats2.get("proposals_deduped") == 1
    assert len(db.list_skill_proposals(skill_name="dedupe-skill")) == 1


# ---------------------------------------------------------------------------
# 5. Veto-window auto-apply
# ---------------------------------------------------------------------------


def _pending_proposal(skill_name, change="Add: on GPU OOM use --device cpu.", confidence=0.8, age_hours=48):
    from db import models as db
    from db.database import connect_sessions

    sid = db.create_session(title="proposal source")
    pid = db.add_skill_proposal(
        skill_name=skill_name,
        section="Common Failures",
        problem="OOM fallback undocumented",
        proposed_change=change,
        confidence=confidence,
        source_origin="refine",
        session_id=sid,
    )
    if age_hours:
        past = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
        with connect_sessions() as conn:
            conn.execute("UPDATE skill_improvement_proposals SET created_at = ? WHERE id = ?", (past, pid))
    return pid


@pytest.fixture
def quiet_manager(monkeypatch):
    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: SimpleNamespace(has_active_work=lambda strict=False: False),
    )


def test_auto_apply_applies_ripe_proposal_with_backup(tmp_path, monkeypatch, quiet_manager):
    from core.skills.proposals import auto_apply_ripe_proposals
    from db import models as db

    skill = _make_skill_dir(tmp_path, "heal-me")
    _patch_registry(monkeypatch, FakeSkillRegistry({"heal-me": skill}))
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr("config.settings.skill_proposal_auto_apply_after_hours", 24)

    pid = _pending_proposal("heal-me", age_hours=48)
    out = auto_apply_ripe_proposals()

    assert out["applied"] == [pid]
    body = (skill.path / "SKILL.md").read_text(encoding="utf-8")
    assert "--device cpu" in body
    assert db.get_skill_proposal(pid)["status"] == "auto_applied"
    backups = list((tmp_path / "skill_backups" / "heal-me").glob("SKILL.md.*"))
    assert len(backups) == 1
    assert "--device cpu" not in backups[0].read_text(encoding="utf-8")


def test_auto_apply_respects_veto_window(tmp_path, monkeypatch, quiet_manager):
    from core.skills.proposals import auto_apply_ripe_proposals
    from db import models as db

    skill = _make_skill_dir(tmp_path, "too-fresh")
    _patch_registry(monkeypatch, FakeSkillRegistry({"too-fresh": skill}))
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr("config.settings.skill_proposal_auto_apply_after_hours", 24)

    pid = _pending_proposal("too-fresh", age_hours=0)  # inside the window
    out = auto_apply_ripe_proposals()

    assert out["applied"] == []
    assert db.get_skill_proposal(pid)["status"] == "pending"


def test_auto_apply_disabled_when_window_zero(tmp_path, monkeypatch, quiet_manager):
    from core.skills.proposals import auto_apply_ripe_proposals
    from db import models as db

    skill = _make_skill_dir(tmp_path, "gated-off")
    _patch_registry(monkeypatch, FakeSkillRegistry({"gated-off": skill}))
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr("config.settings.skill_proposal_auto_apply_after_hours", 0)

    pid = _pending_proposal("gated-off", age_hours=48)
    out = auto_apply_ripe_proposals()

    assert out["applied"] == []
    assert db.get_skill_proposal(pid)["status"] == "pending"


def test_auto_apply_archives_proposal_for_missing_skill(tmp_path, monkeypatch, quiet_manager):
    from core.skills.proposals import auto_apply_ripe_proposals
    from db import models as db

    _patch_registry(monkeypatch, FakeSkillRegistry({}))
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr("config.settings.skill_proposal_auto_apply_after_hours", 24)

    pid = _pending_proposal("deleted-skill", age_hours=48)
    out = auto_apply_ripe_proposals()

    assert pid in out["archived"]
    assert db.get_skill_proposal(pid)["status"] == "archived"


def test_auto_apply_skips_disabled_skill(tmp_path, monkeypatch, quiet_manager):
    from core.skills.proposals import auto_apply_ripe_proposals
    from db import models as db

    skill = _make_skill_dir(tmp_path, "off-skill")
    _patch_registry(monkeypatch, FakeSkillRegistry({"off-skill": skill}, disabled={"off-skill"}))
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr("config.settings.skill_proposal_auto_apply_after_hours", 24)

    pid = _pending_proposal("off-skill", age_hours=48)
    out = auto_apply_ripe_proposals()

    assert out["applied"] == []
    assert out["skipped"] == 1
    assert db.get_skill_proposal(pid)["status"] == "pending"


def test_auto_apply_honors_day_cap(tmp_path, monkeypatch, quiet_manager):
    from core.skills.proposals import auto_apply_ripe_proposals
    from db import models as db

    skills = {}
    for i in range(3):
        s = _make_skill_dir(tmp_path, f"cap-skill-{i}")
        skills[s.name] = s
    _patch_registry(monkeypatch, FakeSkillRegistry(skills))
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr("config.settings.skill_proposal_auto_apply_after_hours", 24)
    monkeypatch.setattr("config.settings.skill_proposal_max_auto_applies_per_day", 2)

    pids = [_pending_proposal(f"cap-skill-{i}", change=f"Change {i}: add note.", age_hours=48) for i in range(3)]
    out = auto_apply_ripe_proposals()

    assert len(out["applied"]) == 2
    statuses = [db.get_skill_proposal(p)["status"] for p in pids]
    assert statuses.count("auto_applied") == 2
    assert statuses.count("pending") == 1


def test_auto_apply_defers_when_sessions_active(tmp_path, monkeypatch):
    from core.skills.proposals import auto_apply_ripe_proposals
    from db import models as db

    skill = _make_skill_dir(tmp_path, "busy-box")
    _patch_registry(monkeypatch, FakeSkillRegistry({"busy-box": skill}))
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr("config.settings.skill_proposal_auto_apply_after_hours", 24)
    monkeypatch.setattr(
        "sessions.manager.get_manager",
        lambda: SimpleNamespace(has_active_work=lambda strict=False: True),
    )

    pid = _pending_proposal("busy-box", age_hours=48)
    out = auto_apply_ripe_proposals()

    assert out["applied"] == []
    assert out["deferred"] >= 1
    assert db.get_skill_proposal(pid)["status"] == "pending"


# ---------------------------------------------------------------------------
# 6. Skill content-change sweep → memory re-validation
# ---------------------------------------------------------------------------


def _sweep_runner():
    from core.snooze import SnoozeRunner

    runner = SnoozeRunner()
    runner._cycle_generation = runner._cancel_generation  # not cancelled
    return runner


async def test_skill_change_sweep_baselines_silently(tmp_path, monkeypatch):
    from db import models as db

    skill = _make_skill_dir(tmp_path, "watched-skill")
    _patch_registry(monkeypatch, FakeSkillRegistry({"watched-skill": skill}))
    monkeypatch.setattr("config.settings.dream_enabled", True)

    runner = _sweep_runner()
    await runner._sweep_skill_content_changes()

    assert db.get_snooze_state("skill_content_hash:watched-skill") is not None
    assert db.count_dream_hypotheses(kind="memory_stale") == 0


async def test_skill_change_sweep_enqueues_stale_hypotheses(tmp_path, monkeypatch):
    from core.memory.store import MemoryStore
    from db import models as db

    skill = _make_skill_dir(tmp_path, "watched-skill")
    _patch_registry(monkeypatch, FakeSkillRegistry({"watched-skill": skill}))
    monkeypatch.setattr("config.settings.dream_enabled", True)

    store = MemoryStore(memory_dir=str(tmp_path / "memories"))
    store.add_entry(
        content="The watched-skill script lacks a CPU flag; only GPU works.",
        entry_type="lesson",
        tags="watched-skill,gpu",
        source="test",
    )
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: store)

    runner = _sweep_runner()
    await runner._sweep_skill_content_changes()  # baseline
    assert db.count_dream_hypotheses(kind="memory_stale") == 0

    (skill.path / "SKILL.md").write_text(
        "---\nname: watched-skill\n---\n\n## Usage\nNow supports --device cpu.\n", encoding="utf-8"
    )
    await runner._sweep_skill_content_changes()

    rows = db.list_dream_hypotheses(kind="memory_stale", status="pending", limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["origin"] == "skill_change_sweep"
    assert "watched-skill" in row["statement"]
    evidence = json.loads(row["evidence_json"])
    types = {e.get("type") for e in evidence}
    assert types == {"memory", "skill_change"}
    mem_ref = next(e for e in evidence if e["type"] == "memory")
    assert mem_ref.get("hash"), "memory ref must be content-hash guarded for the validator"

    # Re-running with no further change must not duplicate.
    await runner._sweep_skill_content_changes()
    assert db.count_dream_hypotheses(kind="memory_stale") == 1


async def test_skill_change_sweep_dream_disabled_still_tracks_hash(tmp_path, monkeypatch):
    from db import models as db

    skill = _make_skill_dir(tmp_path, "quiet-skill")
    _patch_registry(monkeypatch, FakeSkillRegistry({"quiet-skill": skill}))
    monkeypatch.setattr("config.settings.dream_enabled", False)

    runner = _sweep_runner()
    await runner._sweep_skill_content_changes()  # baseline
    first = db.get_snooze_state("skill_content_hash:quiet-skill")

    (skill.path / "scripts" / "run.py").write_text("print('v2')\n", encoding="utf-8")
    await runner._sweep_skill_content_changes()

    second = db.get_snooze_state("skill_content_hash:quiet-skill")
    assert first != second, "hash must advance even when dream is off (no flood later)"
    assert db.count_dream_hypotheses(kind="memory_stale") == 0


async def test_skill_change_sweep_own_cap_not_global_backlog(tmp_path, monkeypatch):
    """A flooded GLOBAL dream backlog must not starve skill-change
    re-validation; only the sweep's own pending rows count against its cap."""
    from core.memory.store import MemoryStore
    from db import models as db

    skill = _make_skill_dir(tmp_path, "capped-skill")
    _patch_registry(monkeypatch, FakeSkillRegistry({"capped-skill": skill}))
    monkeypatch.setattr("config.settings.dream_enabled", True)

    store = MemoryStore(memory_dir=str(tmp_path / "memories"))
    store.add_entry(content="capped-skill only works on GPU.", entry_type="lesson", tags="capped-skill", source="test")
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: store)

    # Flood the general queue with foreign-origin pending hypotheses.
    for i in range(40):
        db.add_dream_hypothesis(
            kind="memory_stale",
            statement=f"unrelated backlog row {i}",
            evidence_json="[]",
            origin="dream_cycle",
        )

    runner = _sweep_runner()
    await runner._sweep_skill_content_changes()  # baseline
    (skill.path / "SKILL.md").write_text(
        "---\nname: capped-skill\n---\n\n## Usage\nNow works on CPU too.\n", encoding="utf-8"
    )
    await runner._sweep_skill_content_changes()

    mine = [
        r
        for r in db.list_dream_hypotheses(kind="memory_stale", status="pending", limit=100)
        if r.get("origin") == "skill_change_sweep"
    ]
    assert len(mine) == 1, "own-origin cap must not be blocked by the foreign backlog"


async def test_skill_change_sweep_respects_own_cap(tmp_path, monkeypatch):
    from core.memory.store import MemoryStore
    from core.snooze import SKILL_SWEEP_MAX_PENDING
    from db import models as db

    skill = _make_skill_dir(tmp_path, "saturated-skill")
    _patch_registry(monkeypatch, FakeSkillRegistry({"saturated-skill": skill}))
    monkeypatch.setattr("config.settings.dream_enabled", True)

    store = MemoryStore(memory_dir=str(tmp_path / "memories"))
    store.add_entry(
        content="saturated-skill has a broken flag.", entry_type="lesson", tags="saturated-skill", source="test"
    )
    monkeypatch.setattr("core.memory.store.get_memory_store", lambda: store)

    for i in range(SKILL_SWEEP_MAX_PENDING):
        db.add_dream_hypothesis(
            kind="memory_stale",
            statement=f"own backlog row {i}",
            evidence_json="[]",
            origin="skill_change_sweep",
        )

    runner = _sweep_runner()
    await runner._sweep_skill_content_changes()  # baseline
    (skill.path / "SKILL.md").write_text("---\nname: saturated-skill\n---\n\n## Usage\nchanged.\n", encoding="utf-8")
    await runner._sweep_skill_content_changes()

    total = db.count_dream_hypotheses(kind="memory_stale", status="pending")
    assert total == SKILL_SWEEP_MAX_PENDING, "sweep at its own cap must not enqueue more"
