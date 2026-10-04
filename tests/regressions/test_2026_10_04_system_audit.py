"""System audit: proposal decisions/recovery, bounded I/O and durable persistence."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from config import settings
from core.skills import proposals
from core.skills.registry import SkillRegistry
from db import models as db


@pytest.fixture
def skill_env(tmp_path, monkeypatch):
    root = tmp_path / "custom-skills"
    skill = root / "audit"
    skill.mkdir(parents=True)
    md = skill / "SKILL.md"
    md.write_text("---\nname: audit\ndescription: audit fixture\n---\n## Usage\nOriginal.\n")
    monkeypatch.setattr(settings, "skills_dir", str(root))
    monkeypatch.setattr(settings, "skill_proposal_auto_apply", True)
    registry = SkillRegistry()
    registry.scan(root)
    monkeypatch.setattr("core.skills.registry._skill_registry", registry)
    monkeypatch.setattr(
        "sessions.manager.get_manager", lambda: SimpleNamespace(has_active_work=lambda strict=False: False)
    )
    return md, registry


def proposal(change="Use the verified result."):
    return db.add_skill_proposal("audit", "Usage", "Missing guidance", change, 0.9)


def test_exact_backups_and_rollback_preserve_later_edits(skill_env):
    md, registry = skill_env
    original = md.read_bytes()
    a, b = proposal("First change."), proposal("Second change.")
    proposals.apply_proposal(a)
    proposals.apply_proposal(b)
    assert db.get_skill_proposal(a)["backup_name"] != db.get_skill_proposal(b)["backup_name"]
    with pytest.raises(proposals.ProposalApplyError, match="changed since"):
        proposals.restore_skill_backup(a)
    assert db.get_skill_proposal(a)["status"] == "applied"
    proposals.restore_skill_backup(b)
    assert "First change." in md.read_text() and "Second change." not in md.read_text()
    proposals.restore_skill_backup(a)
    assert md.read_bytes() == original
    assert registry.get("audit") is not None


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_failed_backup_refuses_file_mutation(skill_env, monkeypatch, operation):
    md, _ = skill_env
    pid = proposal()
    if operation == "rollback":
        proposals.apply_proposal(pid)
    before, status = md.read_bytes(), db.get_skill_proposal(pid)["status"]
    monkeypatch.setattr(proposals, "_backup_skill_md", lambda *a, **k: None)
    with pytest.raises(proposals.ProposalApplyError, match="Backup failed"):
        (proposals.apply_proposal if operation == "apply" else proposals.restore_skill_backup)(pid)
    assert md.read_bytes() == before
    assert db.get_skill_proposal(pid)["status"] == status


def test_rejection_wins_before_durable_claim(skill_env, monkeypatch):
    md, _ = skill_env
    before = md.read_bytes()
    pid = proposal()
    entered, release = Event(), Event()
    backup = proposals._backup_skill_md

    def delayed(*args, **kwargs):
        result = backup(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(proposals, "_backup_skill_md", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(proposals.apply_proposal, pid, "auto_applied")
        try:
            assert entered.wait(5)
            assert db.resolve_skill_proposal(pid, "rejected")
        finally:
            release.set()
        with pytest.raises(proposals.ProposalApplyError, match="changed before"):
            task.result()
    assert md.read_bytes() == before
    assert db.get_skill_proposal(pid)["status"] == "rejected"


def test_two_proposals_on_one_skill_do_not_lose_updates(skill_env):
    md, _ = skill_env
    ids = [proposal("First update."), proposal("Second update.")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(proposals.apply_proposal, ids))
    assert "First update." in md.read_text() and "Second update." in md.read_text()
    assert all(db.get_skill_proposal(pid)["status"] == "applied" for pid in ids)


def test_interrupted_apply_can_be_recovered_and_cannot_be_rejected(skill_env, monkeypatch):
    md, _ = skill_env
    before = md.read_bytes()
    pid = proposal()
    resolve = db.resolve_skill_proposal
    with monkeypatch.context() as patch:
        patch.setattr(db, "resolve_skill_proposal", lambda *a, **k: False)
        with pytest.raises(proposals.ProposalApplyError, match="finalize"):
            proposals.apply_proposal(pid)
    assert db.get_skill_proposal(pid)["status"] == "applying"
    assert not resolve(pid, "rejected")
    proposals.restore_skill_backup(pid)
    assert md.read_bytes() == before


def test_legacy_backup_is_never_guessed(skill_env):
    pid = proposal()
    db.resolve_skill_proposal(pid, "applied")
    with pytest.raises(proposals.ProposalApplyError, match="legacy"):
        proposals.restore_skill_backup(pid)


def test_goal_work_blocks_auto_apply(skill_env, monkeypatch):
    from sessions.manager import SessionManager
    from sessions.state import AgentSession
    from sessions.state_v2 import SessionStateV2

    mgr = SessionManager()
    session = AgentSession(session_id="goal")
    session.goal_continuation_active = True
    session._state_v2 = SessionStateV2.PROCESSING
    mgr._sessions[session.session_id] = session
    monkeypatch.setattr("sessions.manager.get_manager", lambda: mgr)
    pid = proposal()
    with pytest.raises(proposals.ProposalApplyError, match="active"):
        proposals.apply_proposal(pid, status_label="auto_applied")
    assert db.get_skill_proposal(pid)["status"] == "pending"


def test_archive_and_review_reach_beyond_the_old_page_limit(skill_env, monkeypatch):
    from core.skills.review import count_review_pending

    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    for i in range(1005):
        pid = proposal(str(i))
        if i == 0:
            with db.connect_sessions() as conn:
                conn.execute("UPDATE skill_improvement_proposals SET created_at=? WHERE id=?", (old, pid))
            oldest = pid
    monkeypatch.setattr(settings, "skill_proposal_auto_apply", False)
    assert count_review_pending() == 1005
    assert proposals.archive_stale_skill_proposals() == [oldest]
    assert count_review_pending() == 1004


def test_registry_reuses_validation_and_publishes_complete_scan(skill_env, monkeypatch):
    md, registry = skill_env
    script = md.parent / "scripts" / "run.py"
    script.parent.mkdir()
    script.write_text("print('valid')\n")
    validate = registry._validate
    calls = []

    def observed(skill):
        assert registry.get("audit") is not None
        calls.append(skill.name)
        return validate(skill)

    monkeypatch.setattr(registry, "_validate", observed)
    registry.rescan()
    registry.rescan()
    assert calls == ["audit"]
    script.write_text("def invalid(:\n")
    registry.rescan()
    assert calls == ["audit", "audit"]
    assert not registry.is_valid("audit")
    assert not list(script.parent.rglob("*.pyc"))


async def test_skills_scan_does_not_block_the_event_loop(skill_env, monkeypatch):
    from api.routers.skills import list_skills

    _, registry = skill_env
    started, release = Event(), Event()
    scan = registry.rescan

    def slow(*args):
        started.set()
        assert release.wait(5)
        return scan(*args)

    monkeypatch.setattr(registry, "rescan", slow)
    task = asyncio.create_task(list_skills())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        # The loop can resume this task while the scan is still blocked.
        assert not task.done()
    finally:
        release.set()
    assert (await task)["skills"][0]["name"] == "audit"


def test_cron_loader_skips_non_objects_and_still_catches_up(tmp_path, monkeypatch):
    from core.extensions import scheduling as sched

    path = tmp_path / "cron.json"
    valid = {"name": "valid", "cron_expr": "* * * * *", "prompt": "go"}
    path.write_text(json.dumps([None, [], valid]))
    added, caught = [], []
    monkeypatch.setattr(sched, "CRON_PATH", path)
    monkeypatch.setattr(sched, "_add_job_internal", lambda *a, **k: added.append(a[0]))
    monkeypatch.setattr(sched, "_schedule_coalesced_catchup", caught.extend)
    sched._load_jobs()
    assert added == ["valid"] and caught == [valid]


def test_failed_cron_replace_keeps_the_previous_file(tmp_path, monkeypatch):
    from core.extensions import scheduling as sched

    path = tmp_path / "cron.json"
    original = '[{"name": "old"}]'
    path.write_text(original)
    monkeypatch.setattr(sched, "CRON_PATH", path)
    monkeypatch.setattr(sched, "_get_scheduler", lambda: SimpleNamespace(get_jobs=lambda: []))

    def fail(*args):
        raise OSError("simulated interrupted replace")

    monkeypatch.setattr("core.tools.atomic.os.replace", fail)
    with pytest.raises(OSError):
        sched._save_jobs()
    assert path.read_text() == original
    assert not list(tmp_path.glob(".cron.json.*.tmp"))


def test_rlm_preview_reads_only_its_byte_budget(tmp_path, monkeypatch):
    from io import BytesIO

    from api.routers import rlm

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    run = tmp_path / "run"
    run.mkdir()
    answer = run / "answer.txt"
    answer.touch()
    opened = Path.open

    class Bounded(BytesIO):
        def read(self, size=-1):
            assert size == rlm._ANSWER_INLINE_LIMIT
            return super().read(size)

    monkeypatch.setattr(Path, "open", lambda p, *a, **k: Bounded(b"x" * 250000) if p == answer else opened(p, *a, **k))
    monkeypatch.setattr(db, "list_rlm_run_children", lambda _: [])
    result = rlm._run_detail({"run_id": "x", "run_dir": "run", "status": "done"})
    assert len(result["answer"]) == rlm._ANSWER_INLINE_LIMIT


def test_interrupted_rollback_can_be_finished(skill_env, monkeypatch):
    md, _ = skill_env
    before = md.read_bytes()
    pid = proposal()
    proposals.apply_proposal(pid)
    with monkeypatch.context() as patch:
        patch.setattr(db, "resolve_skill_proposal", lambda *a, **k: False)
        with pytest.raises(proposals.ProposalApplyError, match="during rollback"):
            proposals.restore_skill_backup(pid)
    assert md.read_bytes() == before
    proposals.restore_skill_backup(pid)
    assert db.get_skill_proposal(pid)["status"] == "rolled_back"


def test_failed_apply_replace_preserves_file_and_releases_claim(skill_env, monkeypatch):
    md, _ = skill_env
    before = md.read_bytes()
    pid = proposal()
    atomic_write = proposals.atomic_write

    def fail_target(path, content):
        if path == md:
            raise OSError("replacement failed")
        return atomic_write(path, content)

    monkeypatch.setattr(proposals, "atomic_write", fail_target)
    with pytest.raises(proposals.ProposalApplyError, match="Failed to write"):
        proposals.apply_proposal(pid)
    assert md.read_bytes() == before
    assert db.get_skill_proposal(pid)["status"] == "pending"


def test_auto_apply_reaches_old_proposal_behind_500_fresh_ones(skill_env):
    pid = proposal("The old eligible change.")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with db.connect_sessions() as conn:
        conn.execute("UPDATE skill_improvement_proposals SET created_at=? WHERE id=?", (old, pid))
    for i in range(501):
        proposal(f"Fresh change {i}.")
    assert proposals.auto_apply_ripe_proposals()["applied"] == [pid]


def test_archival_cannot_overwrite_a_human_approval(skill_env):
    pid = proposal()
    assert db.resolve_skill_proposal(pid, "approved")
    assert not db.resolve_skill_proposal(pid, "archived")
    assert db.get_skill_proposal(pid)["status"] == "approved"


async def test_recovery_history_and_decision_conflicts_are_exposed(skill_env):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routers import skills

    pid = proposal()
    proposals.apply_proposal(pid)
    app = FastAPI()
    app.include_router(skills.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get("/api/skills/audit")
        assert detail.json()["proposal_history"][0]["can_rollback"] is True
        conflict = await client.post(f"/api/skills/proposals/{pid}/reject")
        assert conflict.status_code == 409
        undone = await client.post(f"/api/skills/proposals/{pid}/rollback")
        assert undone.status_code == 200


def test_skill_history_rollback_action_and_conflict_feedback(tmp_path):
    from tests.js_harness import NODE, run_js

    if not NODE:
        pytest.skip("node is required")
    result = run_js(
        r"""
import { fns, mod, makeContext, run, report } from './sandbox.mjs';
const buttons = [], calls = [], errors = [];
function el(tag, attrs, children=[]) {
    const node = { tag, attrs, children, events: {}, appendChild(c) { this.children.push(c); },
        addEventListener(name, fn) { this.events[name] = fn; } };
    if (tag === 'button') buttons.push(node);
    return node;
}
let fail = false;
const {ctx} = makeContext({el, text: x => x, encodeURIComponent,
    post: async path => { if (fail) throw new Error('Skill changed'); calls.push(path); },
    viewSkill: async name => calls.push(name), notify: (tier, message) => errors.push(message)});
run(ctx, fns(['renderSkillProposalHistory'], mod('components/file-panel.js')));
ctx.container = el('div', {});
ctx.data = {name: 'audit', proposal_history: [
    {id: 'a', status: 'applied', can_rollback: true},
    {id: 'b', status: 'applying', can_rollback: true},
    {id: 'legacy', status: 'applied', can_rollback: false},
    {id: 'done', status: 'rolled_back', can_rollback: true},
]};
run(ctx, 'renderSkillProposalHistory(container, data)');
await buttons[0].events.click();
fail = true;
await buttons[1].events.click();
report({count: buttons.length, calls, errors, enabled: buttons.every(b => b.disabled === false)});
""",
        tmp_path,
    )
    assert result["count"] == 2
    assert result["calls"] == ["/api/skills/proposals/a/rollback", "audit"]
    assert result["errors"] == ["Could not roll back: Skill changed"]
    assert result["enabled"] is True
