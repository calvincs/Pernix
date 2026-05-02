"""Tests for the workflows feature: parser, registry, validator, apply, run_workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.workflows.parser import (
    StepDef,
    WorkflowDef,
    WorkflowParseError,
    parse_workflow_md,
)

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _wf(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name / "WORKFLOW.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_parse_happy_path(tmp_path):
    path = _wf(
        tmp_path,
        "wf1",
        """---
name: wf1
description: A test workflow
tags: [a, b]
version: "1.0"
steps:
  - id: step-a
    type: instruction
    description: First step
    instructions: Do thing one.
    output_file: a.md
    depends_on: []
  - id: step-b
    type: instruction
    description: Second step
    output_file: b.md
    depends_on: [step-a]
---
Optional body.
""",
    )
    frontmatter, body = parse_workflow_md(path)
    assert frontmatter["name"] == "wf1"
    assert frontmatter["description"] == "A test workflow"
    assert frontmatter["tags"] == ["a", "b"]
    steps = frontmatter["_parsed_steps"]
    assert [s.id for s in steps] == ["step-a", "step-b"]
    assert steps[1].depends_on == ["step-a"]
    assert "Optional body" in body


def test_parse_step_model_override(tmp_path):
    """Steps can declare `model: <name>` to override the default LLM for
    that worker. Empty string when omitted."""
    path = _wf(
        tmp_path,
        "wf-m",
        """---
name: wf-m
description: model override
steps:
  - id: heavy
    type: instruction
    description: heavy reasoning step
    output_file: out.md
    model: qwen3.5:122b-a10b-q4_K_M
    depends_on: []
  - id: light
    type: instruction
    description: light step (default model)
    output_file: light.md
    depends_on: [heavy]
---
""",
    )
    frontmatter, _ = parse_workflow_md(path)
    steps = {s.id: s for s in frontmatter["_parsed_steps"]}
    assert steps["heavy"].model == "qwen3.5:122b-a10b-q4_K_M"
    assert steps["light"].model == ""


def test_run_workflow_passes_step_model_to_spawn_worker(
    tmp_path,
    monkeypatch,
    stub_worker_pipeline,
):
    """Per-step `model` from WORKFLOW.md must reach spawn_worker so the
    worker session is created on that model. Without this plumbing the
    declaration would be silent dead config."""
    wf_yaml = """---
name: model_plumb
description: plumb test
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    model: qwen3.5:35b
    depends_on: []
  - id: b
    type: instruction
    description: b (default model)
    output_file: b.md
    depends_on: [a]
---
"""
    _install_wf(tmp_path, monkeypatch, "model_plumb", wf_yaml)

    captured: list[tuple[str, str]] = []

    def capturing_spawn(task_description, title="", model="", auto_resume_parent=False, _context=None):
        # Extract step id from title format "[wfname] step-id: ..."
        sid = title.split("] ", 1)[1].split(":", 1)[0] if "] " in title else ""
        captured.append((sid, model))
        # Return a unique synthetic id matching the stub_worker_pipeline's pattern
        wid = f"worker-for-{sid}"
        # The stub fixture's worker_to_step dict needs this entry too — the
        # production fixture handles that. Here we just need run_workflow to
        # see a successful spawn so it proceeds.
        return f"Worker spawned: {wid}"

    from core.extensions import orchestration as orch

    monkeypatch.setattr(orch, "spawn_worker", capturing_spawn)
    # Bypass everything else: simulate workers that immediately complete with
    # output present.
    monkeypatch.setattr(orch, "await_workers", lambda **kw: "0 worker(s) complete")
    monkeypatch.setattr(orch, "_latest_reflect", lambda wid: {"verdict": "pass", "reasoning": "ok"})
    # Pre-create the output files so verdict=pass passes the gate.
    (tmp_path / "ws").mkdir(exist_ok=True)
    # We'll need to create files inside the run_dir, but we don't know
    # run_id ahead of time. Hook into _write_manifest's invocation by
    # patching _finalize_step's side effects: create output files on the
    # fly when verdict is checked.
    real_finalize = orch._finalize_step

    def stubby_finalize(step, wid, manifest, run_dir, ctx=None):
        # Write the output file so verdict=pass survives the gate.
        (run_dir / step.output_file).write_text("ok", encoding="utf-8")
        return real_finalize(step, wid, manifest, run_dir, ctx)

    monkeypatch.setattr(orch, "_finalize_step", stubby_finalize)

    from core.extensions.orchestration import run_workflow

    run_workflow("model_plumb", "", _context={"session_id": "s1"})

    by_id = dict(captured)
    assert by_id.get("a") == "qwen3.5:35b", f"step 'a' should spawn with model=qwen3.5:35b; got {by_id}"
    assert by_id.get("b") == "", f"step 'b' has no model override; should pass empty string, got {by_id}"


def test_parse_missing_frontmatter(tmp_path):
    path = _wf(tmp_path, "wf2", "No frontmatter here.\n")
    with pytest.raises(WorkflowParseError, match="frontmatter"):
        parse_workflow_md(path)


def test_parse_missing_name(tmp_path):
    path = _wf(
        tmp_path,
        "wf3",
        """---
description: no name
steps:
  - id: x
    description: hi
    output_file: x.md
    depends_on: []
---
""",
    )
    with pytest.raises(WorkflowParseError, match="name"):
        parse_workflow_md(path)


def test_parse_duplicate_step_ids(tmp_path):
    path = _wf(
        tmp_path,
        "wf4",
        """---
name: wf4
description: dup ids
steps:
  - id: x
    type: instruction
    description: a
    output_file: x.md
    depends_on: []
  - id: x
    type: instruction
    description: b
    output_file: x2.md
    depends_on: []
---
""",
    )
    with pytest.raises(WorkflowParseError, match="Duplicate"):
        parse_workflow_md(path)


def test_parse_unknown_dependency(tmp_path):
    path = _wf(
        tmp_path,
        "wf5",
        """---
name: wf5
description: bad dep
steps:
  - id: x
    type: instruction
    description: a
    output_file: x.md
    depends_on: [nope]
---
""",
    )
    with pytest.raises(WorkflowParseError, match="unknown step"):
        parse_workflow_md(path)


def test_parse_cycle(tmp_path):
    path = _wf(
        tmp_path,
        "wf6",
        """---
name: wf6
description: cyclic
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: [b]
  - id: b
    type: instruction
    description: b
    output_file: b.md
    depends_on: [a]
---
""",
    )
    with pytest.raises(WorkflowParseError, match="ycle"):
        parse_workflow_md(path)


# ---------------------------------------------------------------------------
# topological_waves
# ---------------------------------------------------------------------------


def _mk_wf(steps: list[StepDef]) -> WorkflowDef:
    return WorkflowDef(
        name="t",
        description="t",
        path=Path("/tmp"),
        tags=[],
        version="1",
        steps=steps,
        body="",
    )


def _step(sid: str, deps: list[str]) -> StepDef:
    return StepDef(
        id=sid,
        type="instruction",
        description=sid,
        instructions="",
        skill=None,
        output_file=f"{sid}.md",
        depends_on=deps,
    )


def test_topological_linear():
    wf = _mk_wf([_step("a", []), _step("b", ["a"]), _step("c", ["b"])])
    waves = wf.topological_waves()
    assert [[s.id for s in w] for w in waves] == [["a"], ["b"], ["c"]]


def test_topological_parallel():
    wf = _mk_wf([_step("a", []), _step("b", []), _step("c", ["a", "b"])])
    waves = wf.topological_waves()
    assert [sorted(s.id for s in w) for w in waves] == [["a", "b"], ["c"]]


def test_topological_diamond():
    # a -> b,c -> d
    wf = _mk_wf(
        [
            _step("a", []),
            _step("b", ["a"]),
            _step("c", ["a"]),
            _step("d", ["b", "c"]),
        ]
    )
    waves = wf.topological_waves()
    assert [sorted(s.id for s in w) for w in waves] == [["a"], ["b", "c"], ["d"]]


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def test_validate_content_happy_path():
    from core.workflows.validator import validate_content

    content = """---
name: ok
description: ok
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: []
---
"""
    result = validate_content(content, check_skills=False)
    assert result.valid, result.to_agent_text()


def test_validate_content_bad_yaml():
    from core.workflows.validator import validate_content

    content = "---\nname: x\n  bad-indent: oops\ndescription: y\n---\n"
    result = validate_content(content, check_skills=False)
    assert not result.valid


# ---------------------------------------------------------------------------
# Apply helpers — section find + insert
# ---------------------------------------------------------------------------


def test_insert_under_existing_section():
    from core.workflows.apply import _insert_under_section

    body = "## Usage\n\nOriginal.\n\n## Other\n\nStuff.\n"
    new, existed = _insert_under_section(body, "Usage", "Added note.")
    assert existed
    assert "Original." in new
    assert "Added note." in new
    # Added note appears before ## Other
    assert new.index("Added note.") < new.index("## Other")


def test_insert_appends_new_section_when_missing():
    from core.workflows.apply import _insert_under_section

    body = "## Usage\n\nOriginal.\n"
    new, existed = _insert_under_section(body, "Common Failures", "New block.")
    assert not existed
    assert "## Common Failures" in new
    assert "New block." in new
    # New section is after the existing content
    assert new.index("Common Failures") > new.index("Original.")


def test_insert_case_insensitive_section_match():
    from core.workflows.apply import _find_section_bounds

    body = "## Common Failures\n\nstuff\n"
    assert _find_section_bounds(body, "common failures") is not None
    assert _find_section_bounds(body, "COMMON FAILURES") is not None


# ---------------------------------------------------------------------------
# run_workflow — integration test with stubbed worker calls.
# Verifies: (a) waves run in topological order, (b) upstream failure short-circuits
# downstream, (c) verdict=retry triggers retry_worker within the budget,
# (d) verdict=pass but missing output file is downgraded to failed.
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_worker_pipeline(monkeypatch, tmp_path):
    """Plug stub spawn_worker / await_workers / _latest_reflect into orchestration.

    Each spawn returns a unique synthetic worker id. The fake reflect verdicts
    and output-file behaviour are controlled per-step by the test via
    `step_behaviour` dict.
    """
    # Isolate workspace so the run directory lands in tmp_path
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path / "ws"))

    from core.extensions import orchestration as orch

    worker_counter = {"n": 0}
    spawned: list[tuple[str, str]] = []  # (title, worker_id)
    # step-id-to-behaviour — test sets this dict before invoking
    step_behaviour: dict[str, dict] = {}
    # worker_id -> verdict/output-exists
    worker_to_step: dict[str, str] = {}

    def fake_spawn_worker(task_description, title="", model="", auto_resume_parent=False, _context=None):
        wid = f"worker{worker_counter['n']:04x}"
        worker_counter["n"] += 1
        spawned.append((title, wid))
        # Extract step id from title: "[wfname] step-id: description..."
        step_id = ""
        if title.startswith("["):
            try:
                step_id = title.split("] ", 1)[1].split(":", 1)[0]
            except Exception:
                step_id = ""
        worker_to_step[wid] = step_id
        # If the step's behaviour says spawn_error, return an error
        beh = step_behaviour.get(step_id, {})
        if beh.get("spawn_error"):
            return "Error: Simulated spawn failure"
        return f"Worker spawned: {wid}"

    def fake_await_workers(stale_threshold=120, worker_ids=None, min_done=0, suspend=False, _context=None):
        # Side-effect: for each worker we're supposed to await, simulate the
        # worker's output-file write (if the step behaviour asks for it).
        for wid in worker_ids or []:
            step_id = worker_to_step.get(wid, "")
            beh = step_behaviour.get(step_id, {})
            if beh.get("write_output", True) and not beh.get("skip_output_on_verdict", False):
                # Write the expected output file for this step
                # We need the run_dir — scan workspace/workflows for the manifest
                run_dirs = list((tmp_path / "ws" / "workflows").rglob("manifest.json"))
                if run_dirs:
                    manifest_path = run_dirs[-1]
                    manifest = json.loads(manifest_path.read_text())
                    step_info = manifest["steps"].get(step_id, {})
                    output_file = step_info.get("output_file")
                    if output_file:
                        (manifest_path.parent / output_file).write_text(
                            f"output from {step_id}",
                            encoding="utf-8",
                        )
        return f"{len(worker_ids or [])} worker(s) complete"

    def fake_latest_reflect(worker_id):
        step_id = worker_to_step.get(worker_id, "")
        beh = step_behaviour.get(step_id, {})
        return {
            "verdict": beh.get("verdict", "pass"),
            "reasoning": beh.get("reasoning", ""),
            "failure_cause": beh.get("failure_cause", "none"),
        }

    def fake_retry_worker(worker_id, new_instructions="", reason="", _context=None):
        step_id = worker_to_step.get(worker_id, "")
        # Upgrade the step's verdict on retry (simulate success after retry)
        beh = step_behaviour.setdefault(step_id, {})
        beh["retry_count"] = beh.get("retry_count", 0) + 1
        # After one retry, default to pass unless the test overrides retry_verdict
        if "retry_verdict" in beh:
            beh["verdict"] = beh["retry_verdict"]
        else:
            beh["verdict"] = "pass"
        # Spawn the replacement worker
        return fake_spawn_worker(
            task_description="[retry]",
            title=f"[retry] {step_id}: retrying",
            _context=_context,
        )

    # db helpers we need to stub to avoid touching a real DB
    def fake_create_workflow_run(**kwargs):
        pass

    def fake_finish_workflow_run(*args, **kwargs):
        pass

    def fake_update_workflow_run_proposals(*args, **kwargs):
        pass

    monkeypatch.setattr(orch, "spawn_worker", fake_spawn_worker)
    monkeypatch.setattr(orch, "await_workers", fake_await_workers)
    monkeypatch.setattr(orch, "_latest_reflect", fake_latest_reflect)
    monkeypatch.setattr(orch, "retry_worker", fake_retry_worker)
    monkeypatch.setattr(orch.db, "create_workflow_run", fake_create_workflow_run)
    monkeypatch.setattr(orch.db, "finish_workflow_run", fake_finish_workflow_run)
    monkeypatch.setattr(orch.db, "update_workflow_run_proposals", fake_update_workflow_run_proposals)
    # Short-circuit post-workflow reflect — tested separately
    monkeypatch.setattr("core.workflows.reflect.workflow_reflect", lambda *a, **kw: 0)

    return {
        "spawned": spawned,
        "step_behaviour": step_behaviour,
        "worker_to_step": worker_to_step,
    }


def _install_wf(tmp_path, monkeypatch, name: str, yaml_body: str) -> None:
    """Install a workflow into a temp workflows dir and rescan the registry."""
    wf_dir = tmp_path / "workflows"
    target = wf_dir / name / "WORKFLOW.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml_body, encoding="utf-8")
    from core.workflows.registry import get_workflow_registry

    reg = get_workflow_registry()
    reg.rescan(wf_dir)


def test_run_workflow_happy_path(tmp_path, monkeypatch, stub_worker_pipeline):
    """All steps pass; manifest ends with all 'complete'."""
    wf_yaml = """---
name: happy
description: happy path
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: []
  - id: b
    type: instruction
    description: b
    output_file: b.md
    depends_on: [a]
---
"""
    _install_wf(tmp_path, monkeypatch, "happy", wf_yaml)

    from core.extensions.orchestration import run_workflow

    result = run_workflow("happy", "some inputs", _context={"session_id": "s1"})

    # Find the manifest
    manifests = list((tmp_path / "ws" / "workflows").rglob("manifest.json"))
    assert manifests, "no manifest written"
    manifest = json.loads(manifests[-1].read_text())

    assert manifest["steps"]["a"]["status"] == "complete"
    assert manifest["steps"]["b"]["status"] == "complete"
    assert "complete" in result.lower()
    assert "2/2" in result


def test_run_workflow_upstream_failure_short_circuits_downstream(
    tmp_path,
    monkeypatch,
    stub_worker_pipeline,
):
    """If step 'a' fails, downstream 'b' must be skipped, not run."""
    wf_yaml = """---
name: sc
description: short-circuit
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: []
  - id: b
    type: instruction
    description: b
    output_file: b.md
    depends_on: [a]
---
"""
    _install_wf(tmp_path, monkeypatch, "sc", wf_yaml)
    stub_worker_pipeline["step_behaviour"]["a"] = {
        "verdict": "fail",
        "failure_cause": "skill",
    }

    from core.extensions.orchestration import run_workflow

    result = run_workflow("sc", "", _context={"session_id": "s1"})
    manifest = json.loads(list((tmp_path / "ws" / "workflows").rglob("manifest.json"))[-1].read_text())

    assert manifest["steps"]["a"]["status"] == "failed"
    assert manifest["steps"]["b"]["status"] == "skipped"
    assert "dependency 'a'" in manifest["steps"]["b"].get("skipped_reason", "")
    # Only one worker was spawned (for step a) — b was skipped before spawn
    assert len(stub_worker_pipeline["spawned"]) == 1


def test_run_workflow_retry_then_pass(tmp_path, monkeypatch, stub_worker_pipeline):
    """verdict=retry triggers retry_worker; after the retry, verdict flips to pass."""
    wf_yaml = """---
name: rt
description: retry
steps:
  - id: only
    type: instruction
    description: only step
    output_file: only.md
    depends_on: []
---
"""
    _install_wf(tmp_path, monkeypatch, "rt", wf_yaml)
    stub_worker_pipeline["step_behaviour"]["only"] = {
        "verdict": "retry",
        "failure_cause": "skill",
        "reasoning": "need to try again",
    }

    from core.extensions.orchestration import run_workflow

    result = run_workflow("rt", "", _context={"session_id": "s1"})
    manifest = json.loads(list((tmp_path / "ws" / "workflows").rglob("manifest.json"))[-1].read_text())

    assert manifest["steps"]["only"]["status"] == "complete"
    # attempts should be 2: initial spawn + one retry
    assert manifest["steps"]["only"]["attempts"] == 2
    # Two workers were spawned (original + retry replacement)
    assert len(stub_worker_pipeline["spawned"]) == 2


def test_run_workflow_pass_but_no_output_downgrades_to_failed(
    tmp_path,
    monkeypatch,
    stub_worker_pipeline,
):
    """If verdict=pass but the output file wasn't written, step is downgraded to failed."""
    wf_yaml = """---
name: nout
description: pass but no output
steps:
  - id: step1
    type: instruction
    description: step1
    output_file: step1.md
    depends_on: []
---
"""
    _install_wf(tmp_path, monkeypatch, "nout", wf_yaml)
    stub_worker_pipeline["step_behaviour"]["step1"] = {
        "verdict": "pass",
        "write_output": False,  # simulate: worker claims pass but never wrote
    }

    from core.extensions.orchestration import run_workflow

    run_workflow("nout", "", _context={"session_id": "s1"})
    manifest = json.loads(list((tmp_path / "ws" / "workflows").rglob("manifest.json"))[-1].read_text())

    assert manifest["steps"]["step1"]["status"] == "failed"
    assert manifest["steps"]["step1"].get("failure_reason") == "pass-but-no-output"


def test_run_workflow_uses_300s_stale_threshold(
    tmp_path,
    monkeypatch,
    stub_worker_pipeline,
):
    """Regression for workflow run e8c94b86 (2026-04-27 transcribe): the
    transcribe worker's scout retry took 143s, exceeded the default 120s
    stale threshold by 23s, and the orchestrator finalized the worker
    before its first round ran. Workflow lost.

    Fix: run_workflow's await_workers calls pass stale_threshold=300s,
    which gives slow-Ollama scouts (including one retry) enough headroom
    without stretching to "genuinely stuck" territory.
    """
    captured: list = []

    wf_yaml = """---
name: thresh
description: stale threshold test
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: []
---
"""
    _install_wf(tmp_path, monkeypatch, "thresh", wf_yaml)

    from core.extensions import orchestration as orch

    real_await = orch.await_workers

    def capturing_await(stale_threshold=120, worker_ids=None, min_done=0, suspend=False, _context=None):
        captured.append(stale_threshold)
        # Defer to the existing stub_worker_pipeline behavior for output writing.
        return real_await(
            stale_threshold=stale_threshold,
            worker_ids=worker_ids,
            min_done=min_done,
            suspend=suspend,
            _context=_context,
        )

    monkeypatch.setattr(orch, "await_workers", capturing_await)

    from core.extensions.orchestration import run_workflow

    run_workflow("thresh", "", _context={"session_id": "s1"})

    assert captured, "await_workers was never called"
    assert captured[0] == 300, f"run_workflow should pass stale_threshold=300 to await_workers; " f"got {captured[0]}"


def test_run_workflow_handles_stalled_workers_when_llm_unreachable(
    tmp_path,
    monkeypatch,
    stub_worker_pipeline,
):
    """Realistic prod scenario: Ollama / OpenRouter goes down or becomes
    unreachable mid-workflow. Worker agents can't get LLM responses, so
    they sit in PROCESSING with no last_activity bumps. After stale_threshold
    seconds every worker in the wave is stalled, and await_workers's
    all-stalled return path should fire → orchestrator finalizes the wave
    with whatever evidence is on disk → workflow finishes failed (not hung).

    We simulate this by overriding await_workers to return its all-stalled
    warning string (the production path that fires when every worker is
    stuck without progress) and verify the workflow still finalizes cleanly.
    No output files exist (LLM was down, no work was done), so each step
    flips to failed via the pass-but-no-output / no-verdict-no-output
    fallback. The DB row gets terminated.
    """
    from sessions.manager import SessionManager

    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    sid = fresh.create_session(title="orchestrator-llm-outage")

    wf_yaml = """---
name: llm_outage
description: LLM outage scenario
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: []
  - id: b
    type: instruction
    description: b
    output_file: b.md
    depends_on: [a]
---
"""
    _install_wf(tmp_path, monkeypatch, "llm_outage", wf_yaml)

    from core.extensions import orchestration as orch

    # Override await_workers to behave like the all-stalled production path:
    # it doesn't write any output files (because no LLM = no work) and
    # returns a stalled-warning string. The wave loop must still call
    # _finalize_step on each worker and proceed to mark them failed.
    def stalled_await(stale_threshold=120, worker_ids=None, min_done=0, suspend=False, _context=None):
        return f"Warning: all {len(worker_ids or [])} pending worker(s) appear stalled (idle > {stale_threshold}s)."

    monkeypatch.setattr(orch, "await_workers", stalled_await)

    # No reflect verdict either — simulates that reflect's LLM call also
    # couldn't reach the endpoint (verdict=unknown, recovery returns None).
    monkeypatch.setattr(orch, "_latest_reflect", lambda wid: {})
    monkeypatch.setattr(orch, "_recover_reflect_verdict", lambda wid, ctx: None)

    finish_calls: list = []
    monkeypatch.setattr(
        orch.db,
        "finish_workflow_run",
        lambda run_id, status, sp, sf, pc: finish_calls.append((run_id, status, sp, sf, pc)),
    )

    from core.extensions.orchestration import run_workflow

    result = run_workflow("llm_outage", "", _context={"session_id": sid})

    # The workflow MUST have called finish_workflow_run — otherwise the DB
    # row is orphaned and the user sees status='running' forever.
    assert finish_calls, "finish_workflow_run never called → DB row orphaned"
    _run_id, final_status, _sp, _sf, _pc = finish_calls[-1]
    assert final_status == "failed", (
        f"all-stalled wave with no output should finalize as failed; " f"got {final_status!r}"
    )

    # Manifest must show step a failed (no-verdict, no output) and step b
    # short-circuited as skipped — NOT pending or running.
    manifests = list((tmp_path / "ws" / "workflows").rglob("manifest.json"))
    manifest = json.loads(manifests[-1].read_text())
    assert manifest["steps"]["a"]["status"] == "failed", manifest["steps"]["a"]
    assert manifest["steps"]["b"]["status"] == "skipped", manifest["steps"]["b"]

    # Background ref balanced (released by run_workflow's finally).
    session = fresh.get(sid)
    assert session._background_refs == 0


def test_run_workflow_back_to_back_on_same_orchestrator(
    tmp_path,
    monkeypatch,
    stub_worker_pipeline,
):
    """Realistic prod scenario: a cron-driven session runs the same workflow
    twice in close succession (e.g. tick fired before previous run cleaned
    up, or the user manually re-triggers). Each invocation must:
      - get its own run_id (no clobbering),
      - take and release its own background_ref so the orchestrator session
        ends with refs balanced to zero,
      - finalize cleanly without state leak from the previous run.

    A leak would manifest as either (a) the second run inheriting wave state
    from the first, or (b) background_refs on the orchestrator session not
    decrementing back to zero, eventually pinning the session against the
    reaper indefinitely.
    """
    from sessions.manager import SessionManager

    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    sid = fresh.create_session(title="cron-style-orchestrator")
    session = fresh.get(sid)
    initial_refs = session._background_refs

    wf_yaml = """---
name: cron_rerun
description: simple two-step workflow rerun
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: []
---
"""
    _install_wf(tmp_path, monkeypatch, "cron_rerun", wf_yaml)

    from core.extensions.orchestration import run_workflow

    result1 = run_workflow("cron_rerun", "first-run", _context={"session_id": sid})
    refs_between = session._background_refs
    result2 = run_workflow("cron_rerun", "second-run", _context={"session_id": sid})
    refs_after = session._background_refs

    # Background ref accounting: must return to baseline after each run.
    assert refs_between == initial_refs, (
        f"first run leaked a background ref: started {initial_refs}, " f"between runs {refs_between}"
    )
    assert refs_after == initial_refs, f"second run leaked a background ref: ended {refs_after}"

    # Each run must produce its own manifest (distinct run_ids → different dirs).
    manifests = sorted((tmp_path / "ws" / "workflows").rglob("manifest.json"))
    assert len(manifests) == 2, (
        f"expected 2 distinct run dirs, got {len(manifests)}: " f"{[m.parent.name for m in manifests]}"
    )
    run_ids = {json.loads(m.read_text())["run_id"] for m in manifests}
    assert len(run_ids) == 2, f"run_ids collided: {run_ids}"

    # Both runs should report success in their summary.
    assert "complete" in result1.lower(), f"first run did not complete: {result1[:200]}"
    assert "complete" in result2.lower(), f"second run did not complete: {result2[:200]}"


def test_run_workflow_takes_and_releases_background_ref(
    tmp_path,
    monkeypatch,
    stub_worker_pipeline,
):
    """Regression for workflow run 1ec11d2b (2026-04-27 ai-tech-daily-brief):
    `run_workflow` blocks the agent thread for the entire wave loop without
    bumping last_activity_time. Without a background ref, the reaper unstuck
    the orchestrator session at 300s of idle, transitioning PROCESSING →
    IDLE_READY mid-flight; the workflow workers were orphaned and the agent
    could no longer report results to the user.

    Fix: take a background ref on entry, release in finally. Verifies both
    sides — the ref is taken (so the reaper would skip), AND the ref is
    released after run_workflow returns (so the session can be reaped
    normally once the work is done).
    """
    from sessions.manager import SessionManager

    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    sid = fresh.create_session(title="orchestrator-for-ref-test")

    wf_yaml = """---
name: ref_test
description: ref test
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: []
---
"""
    _install_wf(tmp_path, monkeypatch, "ref_test", wf_yaml)

    # Sanity: starts with no background refs
    session = fresh.get(sid)
    assert session.has_background_tasks is False
    initial_refs = session._background_refs

    # Track ref usage during execution by patching add/remove and recording
    # the high-water mark.
    high_water_mark = [initial_refs]
    real_add = type(session).add_background_ref
    real_remove = type(session).remove_background_ref

    def tracking_add(self):
        real_add(self)
        if self.session_id == sid:
            high_water_mark[0] = max(high_water_mark[0], self._background_refs)

    monkeypatch.setattr(type(session), "add_background_ref", tracking_add)

    from core.extensions.orchestration import run_workflow

    run_workflow("ref_test", "", _context={"session_id": sid})

    # Restore for cleanliness (monkeypatch unrolls automatically, but be explicit)
    monkeypatch.setattr(type(session), "add_background_ref", real_add)

    # During execution: ref must have been > 0 to protect from reaper
    assert high_water_mark[0] > initial_refs, (
        "run_workflow did not take a background ref — reaper would unstick "
        "the orchestrator at 300s of waiting on workers"
    )
    # After return: ref must be released back to baseline
    assert session._background_refs == initial_refs, (
        f"background ref leaked: started at {initial_refs}, " f"ended at {session._background_refs}"
    )
    assert session.has_background_tasks is False


def test_run_workflow_releases_background_ref_on_exception(
    tmp_path,
    monkeypatch,
    stub_worker_pipeline,
):
    """The background ref must also be released when the wave loop aborts —
    otherwise a single panic in run_workflow would mark the session
    permanently un-reapable.
    """
    from sessions.manager import SessionManager

    fresh = SessionManager()
    monkeypatch.setattr("sessions.manager._manager", fresh)
    sid = fresh.create_session(title="orchestrator-exception-ref")

    wf_yaml = """---
name: ref_exc
description: ref test exc
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: []
---
"""
    _install_wf(tmp_path, monkeypatch, "ref_exc", wf_yaml)

    # Force an exception by making the wave loop's _emit_workflow_event blow up.
    # The outer try/finally must still release the ref.
    from core.extensions import orchestration as orch

    original_emit = orch._emit_workflow_event
    call_count = [0]

    def angry_emit(event):
        call_count[0] += 1
        # Allow the workflow.started event through; blow up on wave_started
        if event.get("type") == "workflow.wave_started":
            raise RuntimeError("simulated unexpected exception")
        return original_emit(event)

    monkeypatch.setattr(orch, "_emit_workflow_event", angry_emit)

    session = fresh.get(sid)
    initial = session._background_refs
    from core.extensions.orchestration import run_workflow

    # Should not propagate — wave-loop except + outer finally handle it.
    run_workflow("ref_exc", "", _context={"session_id": sid})
    assert session._background_refs == initial, "background ref leaked on exception path"


def test_run_workflow_extends_orchestrator_session_budget(
    tmp_path,
    monkeypatch,
    stub_worker_pipeline,
):
    """run_workflow must grow the orchestrator session's LLM budget by
    (waves+1) × base_timeout so reflect-retry / post-flow rounds don't get
    cut off by the wall-clock cap that's only sized for a normal turn.
    """
    wf_yaml = """---
name: budget_ext
description: budget extension
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: []
  - id: b
    type: instruction
    description: b
    output_file: b.md
    depends_on: [a]
  - id: c
    type: instruction
    description: c
    output_file: c.md
    depends_on: [b]
---
"""
    _install_wf(tmp_path, monkeypatch, "budget_ext", wf_yaml)

    extend_calls: list[tuple] = []

    def fake_extend(sid, secs):
        extend_calls.append((sid, secs))
        return 1800.0 + secs

    import core.llm.client as _client_mod

    monkeypatch.setattr(_client_mod, "extend_session_budget", fake_extend)
    # Pin the base timeout so the math is deterministic regardless of env.
    monkeypatch.setattr("config.settings.llm_session_timeout", 1800)

    from core.extensions.orchestration import run_workflow

    run_workflow("budget_ext", "", _context={"session_id": "orch-session"})

    # 3 steps in 3 waves (a → b → c). Extension should be (3+1) × 1800 = 7200.
    assert extend_calls, "run_workflow must call extend_session_budget"
    sid, secs = extend_calls[0]
    assert sid == "orch-session"
    assert secs == 4 * 1800.0, f"expected 4 × base, got {secs}"


def test_run_workflow_extension_capped_at_24h_for_pathological_workflows(
    tmp_path,
    monkeypatch,
    stub_worker_pipeline,
):
    """Hard ceiling: never extend more than 24h, regardless of wave count.
    This is the runaway guard — a 200-wave workflow shouldn't grant infinite
    budget.
    """
    # Build a workflow with many sequential waves to push past the cap.
    # 60 waves × 1800s = 108000s > 86400s (24h cap).
    steps = []
    for i in range(60):
        deps = [f"s{i-1}"] if i > 0 else []
        steps.append(f"""  - id: s{i}
    type: instruction
    description: step {i}
    output_file: s{i}.md
    depends_on: {deps}""")
    wf_yaml = "---\nname: huge\ndescription: many waves\nsteps:\n" + "\n".join(steps) + "\n---\n"
    _install_wf(tmp_path, monkeypatch, "huge", wf_yaml)

    extend_calls: list[tuple] = []
    monkeypatch.setattr(
        "core.llm.client.extend_session_budget",
        lambda sid, secs: extend_calls.append((sid, secs)) or (1800.0 + secs),
    )
    monkeypatch.setattr("config.settings.llm_session_timeout", 1800)

    # Stop early — we just need to confirm the extension call was made and capped.
    # Make spawn_worker fail so the workflow exits quickly.
    from core.extensions import orchestration as orch

    monkeypatch.setattr(orch, "spawn_worker", lambda *a, **kw: "Error: stop here")

    from core.extensions.orchestration import run_workflow

    run_workflow("huge", "", _context={"session_id": "orch-huge"})

    assert extend_calls
    _sid, secs = extend_calls[0]
    assert secs == 24 * 3600.0, f"expected 24h cap, got {secs}"


def test_run_workflow_await_workers_timeout_does_not_orphan_db_row(
    tmp_path,
    monkeypatch,
    stub_worker_pipeline,
):
    """Regression for session 7b97cf7ef84a / run 9b43d85e: when await_workers
    raises empty-str TimeoutError, the wave-level try/except must catch it,
    mark the step failed (via the pass-but-no-output check), and ALWAYS call
    finish_workflow_run. Previously this orphaned the row at status='running'
    forever and returned literal "Error: " to the agent.
    """
    wf_yaml = """---
name: timeout
description: await raises
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: []
---
"""
    _install_wf(tmp_path, monkeypatch, "timeout", wf_yaml)

    from core.extensions import orchestration as orch

    finish_calls: list[tuple] = []
    monkeypatch.setattr(
        orch.db,
        "finish_workflow_run",
        lambda run_id, status, sp, sf, pc: finish_calls.append((run_id, status, sp, sf, pc)),
    )

    def boom(*a, **kw):
        # Empty-str TimeoutError mirrors what concurrent.futures.TimeoutError
        # produced in the original incident (str(e) == '').
        raise TimeoutError()

    monkeypatch.setattr(orch, "await_workers", boom)

    from core.extensions.orchestration import run_workflow

    result = run_workflow("timeout", "", _context={"session_id": "s1"})

    # The DB row MUST have been finalized despite the inner exception.
    assert finish_calls, "finish_workflow_run was never called — DB row is orphaned"
    _run_id, status, _sp, _sf, _pc = finish_calls[-1]
    assert status == "failed", f"expected status=failed, got {status}"

    # The agent-visible result must NOT be a bare "Error:" — it must name
    # the run and step counts so the caller knows what happened.
    assert not result.startswith("Error: "), f"agent-visible result is bare Error literal: {result!r}"
    assert "1 failed" in result, result

    # Manifest's pending step must have been flipped to failed by the
    # pass-but-no-output downgrade — not left at 'running'.
    manifest = json.loads(list((tmp_path / "ws" / "workflows").rglob("manifest.json"))[-1].read_text())
    assert manifest["steps"]["a"]["status"] == "failed", manifest["steps"]["a"]


def test_run_workflow_unexpected_exception_finalizes_with_aborted_prefix(
    tmp_path,
    monkeypatch,
    stub_worker_pipeline,
):
    """If an exception escapes the inner await_workers guard (anywhere else
    in the wave loop), the outer try/except still finalizes the run as
    failed AND prefixes the agent-visible summary with 'Aborted: ...' so the
    caller doesn't see a confusing empty error.
    """
    wf_yaml = """---
name: boom_in_emit
description: emit raises
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: []
---
"""
    _install_wf(tmp_path, monkeypatch, "boom_in_emit", wf_yaml)

    from core.extensions import orchestration as orch

    finish_calls: list[tuple] = []
    monkeypatch.setattr(
        orch.db,
        "finish_workflow_run",
        lambda run_id, status, sp, sf, pc: finish_calls.append((run_id, status, sp, sf, pc)),
    )

    # Make the wave-started emit raise, simulating an unexpected mid-loop
    # failure that the await_workers inner guard cannot catch.
    real_emit = orch._emit_workflow_event

    def flaky_emit(payload):
        if payload.get("type") == "workflow.wave_started":
            raise RuntimeError("simulated event-bus failure")
        return real_emit(payload)

    monkeypatch.setattr(orch, "_emit_workflow_event", flaky_emit)

    from core.extensions.orchestration import run_workflow

    result = run_workflow("boom_in_emit", "", _context={"session_id": "s1"})

    assert finish_calls, "finish_workflow_run was never called — DB row is orphaned"
    _run_id, status, _sp, _sf, _pc = finish_calls[-1]
    assert status == "failed", status
    assert "Aborted" in result and "RuntimeError" in result, f"outer guard didn't surface the abort reason: {result!r}"


def test_run_workflow_escalate_halts(tmp_path, monkeypatch, stub_worker_pipeline):
    """verdict=escalate on any step halts the workflow; later waves are marked skipped."""
    wf_yaml = """---
name: esc
description: escalate halts
steps:
  - id: a
    type: instruction
    description: a
    output_file: a.md
    depends_on: []
  - id: b
    type: instruction
    description: b
    output_file: b.md
    depends_on: [a]
---
"""
    _install_wf(tmp_path, monkeypatch, "esc", wf_yaml)
    stub_worker_pipeline["step_behaviour"]["a"] = {
        "verdict": "escalate",
        "failure_cause": "skill",
    }

    from core.extensions.orchestration import run_workflow

    run_workflow("esc", "", _context={"session_id": "s1"})
    manifest = json.loads(list((tmp_path / "ws" / "workflows").rglob("manifest.json"))[-1].read_text())

    assert manifest["steps"]["a"]["status"] == "escalated"
    assert manifest["steps"]["b"]["status"] == "skipped"
    # Only 'a' was spawned; b never ran due to halt-on-escalate
    assert len(stub_worker_pipeline["spawned"]) == 1
