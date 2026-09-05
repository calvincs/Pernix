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


# ---------------------------------------------------------------------------
# Generated fixtures — parser acceptance
# ---------------------------------------------------------------------------

_GEN_MD = """---
name: {name}
generated: true
timeout: 120
tags: [sentinel, generated, holdout]
---
notes
"""

_TRIVIAL_GEN = """
def generate(seed):
    return {
        "prompt": f"write {seed} to answer.txt",
        "files": {"seed.txt": str(seed)},
        "gates": [{"name": "g", "command": f"grep -qx '{seed}' answer.txt", "watch_paths": ["answer.txt"]}],
    }
"""


def _write_generated_canary(base: Path, name: str, *, with_py: bool = True, flag: bool = True) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    fm = _GEN_MD.format(name=name)
    if not flag:
        fm = fm.replace("generated: true\n", "")
    (d / "CANARY.md").write_text(fm)
    if with_py:
        (d / "generate.py").write_text(_TRIVIAL_GEN)
    return d / "CANARY.md"


def test_parser_accepts_a_canary_whose_prompt_and_gates_come_from_generate_py(tmp_path):
    from core.canary.parser import parse_canary_md

    c = parse_canary_md(_write_generated_canary(tmp_path, "gen-sample"))
    assert c.generated is True
    assert c.generator_path is not None and c.generator_path.name == "generate.py"
    # The fields the file still owns survive.
    assert c.name == "gen-sample" and c.timeout == 120
    assert c.tags == ["sentinel", "generated", "holdout"]
    assert c.flaky is False
    # The fields the generator owns are simply absent until a run.
    assert c.prompt == "" and c.gates == [] and c.files == {}


def test_a_sibling_generate_py_is_enough_without_the_frontmatter_flag(tmp_path):
    from core.canary.parser import parse_canary_md

    c = parse_canary_md(_write_generated_canary(tmp_path, "gen-implicit", flag=False))
    assert c.generated is True


def test_the_flag_alone_parses_so_maintenance_rewrites_survive(tmp_path):
    """core/canary/maintain.py revalidates a rewritten CANARY.md in a bare
    temp directory. Without the frontmatter flag, every generated canary
    would be frozen out of park/flaky/probe maintenance by a parse error."""
    from core.canary.parser import parse_canary_md

    c = parse_canary_md(_write_generated_canary(tmp_path, "gen-flagonly", with_py=False))
    assert c.generated is True
    assert c.generator_path is None  # parseable, not runnable


def test_a_plain_canary_still_needs_a_prompt_and_gates(tmp_path):
    from core.canary.parser import CanaryParseError, parse_canary_md

    d = tmp_path / "plain"
    d.mkdir()
    (d / "CANARY.md").write_text("---\nname: plain\nprompt: do it\n---\n")
    with pytest.raises(CanaryParseError):
        parse_canary_md(d / "CANARY.md")

    (d / "CANARY.md").write_text("---\nname: plain\ngates:\n  - name: g\n    command: 'true'\n---\n")
    with pytest.raises(CanaryParseError):
        parse_canary_md(d / "CANARY.md")


def test_scan_canaries_picks_up_generated_directories(tmp_path):
    from core.canary.parser import scan_canaries

    _write_generated_canary(tmp_path, "gen-one")
    _write_generated_canary(tmp_path, "gen-two")
    found = {c.name: c for c in scan_canaries(tmp_path)}
    assert set(found) == {"gen-one", "gen-two"}
    assert all(c.generated for c in found.values())


# ---------------------------------------------------------------------------
# Generated fixtures — the generate() contract
# ---------------------------------------------------------------------------


def test_generate_variant_keeps_what_the_file_owns_and_replaces_the_rest(tmp_path):
    from core.canary.fixtures import generate_variant
    from core.canary.parser import parse_canary_md

    c = parse_canary_md(_write_generated_canary(tmp_path, "gen-sample"))
    v = generate_variant(c, 4242)
    assert v.name == "gen-sample" and v.timeout == 120 and v.tags == c.tags
    assert v.prompt == "write 4242 to answer.txt"
    assert v.files == {"seed.txt": "4242"}
    assert v.gates == [{"name": "g", "command": "grep -qx '4242' answer.txt", "watch_paths": ["answer.txt"]}]


def test_generate_variant_refuses_a_flag_without_a_generator(tmp_path):
    from core.canary.fixtures import FixtureError, generate_variant
    from core.canary.parser import parse_canary_md

    c = parse_canary_md(_write_generated_canary(tmp_path, "gen-flagonly", with_py=False))
    with pytest.raises(FixtureError):
        generate_variant(c, 1)


@pytest.mark.parametrize(
    "body,fragment",
    [
        ("def generate(seed):\n    return []\n", "must return a dict"),
        ("def generate(seed):\n    return {'gates': [{'name': 'g', 'command': 'true'}]}\n", "no prompt"),
        ("def generate(seed):\n    return {'prompt': 'p', 'gates': []}\n", "non-empty gates"),
        ("def generate(seed):\n    return {'prompt': 'p', 'gates': [{'name': 'g'}]}\n", "'name' and 'command'"),
        (
            "def generate(seed):\n    return {'prompt': 'p', 'gates': [{'name': 'g', 'command': 'true'}],"
            " 'files': {'/etc/passwd': 'x'}}\n",
            "workspace-relative",
        ),
        ("def nope(seed):\n    return {}\n", "no callable generate"),
        ("raise RuntimeError('boom')\n", "import failed"),
    ],
)
def test_a_broken_generator_is_rejected_not_silently_run(tmp_path, body, fragment):
    from core.canary.fixtures import FixtureError, generate_variant
    from core.canary.parser import parse_canary_md

    md = _write_generated_canary(tmp_path, "gen-broken")
    (md.parent / "generate.py").write_text(body)
    with pytest.raises(FixtureError) as e:
        generate_variant(parse_canary_md(md), 1)
    assert fragment in str(e.value)


def test_pick_seed_is_not_constant():
    from core.canary.fixtures import SEED_MAX, pick_seed

    seeds = {pick_seed() for _ in range(50)}
    assert len(seeds) > 40
    assert all(0 < s < SEED_MAX for s in seeds)


# ---------------------------------------------------------------------------
# The three shipped generators — determinism and correctness across seeds
# ---------------------------------------------------------------------------

SEEDS = list(range(1, 21))
GENERATED = ("gen-file-create", "gen-grep-count", "gen-json-transform")


def _real_canary(name: str):
    from core.canary.parser import parse_canary_md

    return parse_canary_md(Path("data/canaries") / name / "CANARY.md")


def _variant(name: str, seed: int):
    from core.canary.fixtures import generate_variant

    return generate_variant(_real_canary(name), seed)


def _run_gate(spec, workspace: Path) -> int:
    import subprocess

    proc = subprocess.run(spec.gates[0]["command"], shell=True, cwd=workspace, capture_output=True)
    return proc.returncode


def _materialise(spec, workspace: Path) -> None:
    for rel, content in spec.files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


# --- what a correct agent would produce, derived from prompt + files only ---


def _solve_file_create(spec) -> tuple[str, str]:
    import re

    m = re.search(r"Create a file named (\S+) in the workspace root", spec.prompt)
    assert m, spec.prompt
    line = spec.prompt.split("\n\n")[1].strip()
    return m.group(1), line


def _solve_grep_count(spec) -> int:
    return sum(1 for content in spec.files.values() for line in content.splitlines() if "ERROR" in line)


def _solve_json_transform(spec) -> dict:
    import json

    records = json.loads(spec.files["data.json"])
    return {
        "shipped_total": sum(r["amount"] for r in records if r["status"] == "shipped"),
        "customers": sorted({r["customer"] for r in records}),
    }


@pytest.mark.parametrize("name", GENERATED)
def test_shipped_generators_are_deterministic_per_seed(name):
    for seed in (1, 7, 12345):
        a, b = _variant(name, seed), _variant(name, seed)
        assert a.prompt == b.prompt and a.files == b.files and a.gates == b.gates


@pytest.mark.parametrize("name", GENERATED)
def test_shipped_generators_actually_vary_with_the_seed(name):
    """A 'generated' fixture that returns the same task every run is just a
    static canary with extra steps."""
    gates = {_variant(name, s).gates[0]["command"] for s in SEEDS}
    files = {tuple(sorted((k, v) for k, v in _variant(name, s).files.items())) for s in SEEDS}
    # The fixture itself is never repeated across 20 seeds.
    assert len(files) == len(SEEDS)
    # The expected value moves too. gen-grep-count's answer is a small count,
    # so its gate command collides by arithmetic, not by design — a spread is
    # all that can be asserted there.
    floor = 4 if name == "gen-grep-count" else len(SEEDS) - 2
    assert len(gates) >= floor, gates


@pytest.mark.parametrize("seed", SEEDS)
def test_gen_file_create_gate_agrees_with_its_own_prompt(tmp_path, seed):
    spec = _variant("gen-file-create", seed)
    _materialise(spec, tmp_path)
    filename, line = _solve_file_create(spec)

    (tmp_path / filename).write_text(line + "\n", encoding="utf-8")
    assert _run_gate(spec, tmp_path) == 0, spec.gates[0]["command"]

    # Off-by-one: the decoy in the workspace is never the right answer.
    (tmp_path / filename).write_text(spec.files["reference/sample.txt"], encoding="utf-8")
    assert _run_gate(spec, tmp_path) != 0


@pytest.mark.parametrize("seed", SEEDS)
def test_gen_grep_count_expected_value_matches_the_fixture(tmp_path, seed):
    spec = _variant("gen-grep-count", seed)
    _materialise(spec, tmp_path)
    expected = _solve_grep_count(spec)
    assert expected > 0

    (tmp_path / "answer.txt").write_text(f"{expected}\n", encoding="utf-8")
    assert _run_gate(spec, tmp_path) == 0, spec.gates[0]["command"]

    (tmp_path / "answer.txt").write_text(f"{expected + 1}\n", encoding="utf-8")
    assert _run_gate(spec, tmp_path) != 0


@pytest.mark.parametrize("seed", SEEDS)
def test_gen_grep_count_keeps_both_traps(seed):
    """Case-sensitivity and substring semantics are the whole point of the
    task; a fixture without them is scoring nothing."""
    spec = _variant("gen-grep-count", seed)
    lines = [ln for content in spec.files.values() for ln in content.splitlines()]
    assert any("error" in ln and "ERROR" not in ln for ln in lines), "lowercase trap missing"
    assert any("ERRORS-SUMMARY" in ln for ln in lines), "substring trap missing"
    assert 2 <= len(spec.files) <= 4
    assert all(p.startswith("logs/") for p in spec.files)


@pytest.mark.parametrize("seed", SEEDS)
def test_gen_json_transform_expected_values_match_the_fixture(tmp_path, seed):
    import json

    spec = _variant("gen-json-transform", seed)
    _materialise(spec, tmp_path)
    answer = _solve_json_transform(spec)
    assert answer["shipped_total"] > 0 and len(answer["customers"]) >= 3

    (tmp_path / "output.json").write_text(json.dumps(answer), encoding="utf-8")
    assert _run_gate(spec, tmp_path) == 0, spec.gates[0]["command"]

    off_by_one = dict(answer, shipped_total=answer["shipped_total"] + 1)
    (tmp_path / "output.json").write_text(json.dumps(off_by_one), encoding="utf-8")
    assert _run_gate(spec, tmp_path) != 0

    # The trap: the shipped subset never spans every customer.
    shipped_only = dict(
        answer,
        customers=sorted({r["customer"] for r in json.loads(spec.files["data.json"]) if r["status"] == "shipped"}),
    )
    (tmp_path / "output.json").write_text(json.dumps(shipped_only), encoding="utf-8")
    assert _run_gate(spec, tmp_path) != 0


@pytest.mark.parametrize("name", GENERATED)
def test_shipped_generated_canaries_are_tagged_sentinel_generated_holdout(name):
    c = _real_canary(name)
    assert c.generated is True and c.generator_path is not None
    assert set(c.tags) == {"sentinel", "generated", "holdout"}
    assert c.flaky is False


def test_no_expected_value_is_written_into_the_canary_directory():
    """The whole design: the answer exists only inside the gate command,
    which is built at run time and never persisted to disk."""
    for name in GENERATED:
        for f in sorted((Path("data/canaries") / name).iterdir()):
            assert f.name in ("CANARY.md", "generate.py", "__pycache__"), f
        md = (Path("data/canaries") / name / "CANARY.md").read_text(encoding="utf-8")
        assert "prompt:" not in md and "gates:" not in md and "files:" not in md


# ---------------------------------------------------------------------------
# Generated fixtures — the runner
# ---------------------------------------------------------------------------


def _fake_manager(monkeypatch, solve):
    """Minimal stand-in for sessions.manager, modelled on the one in
    tests/test_canary.py: prompt() leaves a real task handle behind, because
    the runner waits on that handle and not on the session state."""
    import asyncio
    from types import SimpleNamespace

    from sessions import state_v2 as sv2
    from sessions.state import TurnState

    sessions: dict = {}

    def create_session(title="", session_type="normal", **kw):
        sid = db.create_session(title=title, session_type=session_type)
        sessions[sid] = SimpleNamespace(
            session_id=sid,
            session_type=session_type,
            workspace_override=None,
            model_override=None,
            tool_allowlist=None,
            turn=TurnState(reflect_count=0),
            cancel_requested=False,
            _parked=False,
            task=None,
        )
        return sid

    async def prompt(sid, message):
        s = sessions[sid]

        async def _turn():
            solve(Path(s.workspace_override), message)
            s._parked = True

        s.task = asyncio.create_task(_turn())

    mgr = SimpleNamespace(create_session=create_session, get=lambda sid: sessions.get(sid), prompt=prompt)
    monkeypatch.setattr("sessions.manager.get_manager", lambda: mgr)
    monkeypatch.setattr(
        sv2,
        "_current_state",
        lambda s: sv2.SessionStateV2.IDLE_READY if getattr(s, "_parked", False) else sv2.SessionStateV2.PROCESSING,
    )
    return mgr


async def test_a_generated_canary_runs_scores_and_records_its_seed(monkeypatch):
    import json as _json

    from core.canary import fixtures
    from core.canary.runner import run_canary

    seen: dict = {}

    def solve(ws: Path, _message: str):
        seen["workspace"] = sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file())
        count = 0
        for log in sorted((ws / "logs").glob("*.log")):
            count += sum(1 for line in log.read_text().splitlines() if "ERROR" in line)
        (ws / "answer.txt").write_text(f"{count}\n")

    _fake_manager(monkeypatch, solve)
    monkeypatch.setattr(fixtures, "pick_seed", lambda: 909090)

    result = await run_canary(_real_canary("gen-grep-count"), trigger="manual")

    assert result.passed is True, result.error
    assert result.seed == 909090

    # The seed rides in gate_results_json, next to the gate payloads.
    record = [g for g in result.gate_results if g.get("generated")]
    assert record and record[0]["seed"] == 909090
    row = db.list_canary_runs(task="gen-grep-count")[0]
    stored = _json.loads(row["gate_results_json"])
    assert {"seed": 909090, "generated": True}.items() <= [g for g in stored if g.get("generated")][0].items()
    assert any(g.get("kind") == "gate" and g["passed"] for g in stored)

    # The workspace the agent saw held the generated INPUT files and nothing
    # else — no answer key, no expectation file.
    expected_inputs = sorted(fixtures.generate_variant(_real_canary("gen-grep-count"), 909090).files)
    assert seen["workspace"] == expected_inputs


async def test_each_run_of_a_generated_canary_draws_a_fresh_seed(monkeypatch):
    from core.canary import fixtures
    from core.canary.runner import run_canary

    seeds = iter([11, 22, 33])
    monkeypatch.setattr(fixtures, "pick_seed", lambda: next(seeds))
    _fake_manager(monkeypatch, lambda ws, msg: None)  # agent does nothing; gates fail

    c = _real_canary("gen-grep-count")
    first = await run_canary(c, trigger="manual")
    second = await run_canary(c, trigger="manual")
    assert (first.seed, second.seed) == (11, 22)
    assert first.passed is False and second.passed is False


async def test_a_broken_generator_fails_the_run_instead_of_the_sweep(monkeypatch, tmp_path):
    from core.canary.parser import parse_canary_md
    from core.canary.runner import run_canary

    md = _write_generated_canary(tmp_path, "gen-broken")
    (md.parent / "generate.py").write_text("def generate(seed):\n    return {'prompt': ''}\n")
    _fake_manager(monkeypatch, lambda ws, msg: None)

    result = await run_canary(parse_canary_md(md), trigger="manual")
    assert result.passed is False
    assert "generate()" in result.error
    assert result.outcome == "error"


def test_a_static_canary_records_no_seed(monkeypatch):
    from core.canary.runner import CanaryRunResult

    r = CanaryRunResult(task="file-create", passed=True, trigger="manual")
    assert r.seed is None and r.to_dict()["seed"] is None


# ---------------------------------------------------------------------------
# Post-run contamination scan
# ---------------------------------------------------------------------------

WS = "/tmp/canary-demo-1234"


def _msg(content="", tool_calls=None):
    import json as _json

    row = {"role": "assistant", "content": content}
    if tool_calls is not None:
        row["tool_calls"] = _json.dumps(
            [
                {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": a}}
                for i, (n, a) in enumerate(tool_calls)
            ]
        )
    return row


def _scan(messages, name="gen-grep-count", known=("file-create", "grep-count", "json-transform")):
    from core.canary.contamination import scan_session

    return scan_session("sid", WS, name, known_canaries=list(known), messages=messages)


def test_a_well_behaved_canary_run_scans_clean():
    assert (
        _scan(
            [
                _msg("Counting ERROR lines.", [("bash", '{"command": "grep -c ERROR logs/app.log"}')]),
                _msg("", [("file_write", '{"path": "answer.txt", "content": "7"}')]),
                {"role": "tool", "content": "7"},
            ]
        )
        == []
    )


def test_scan_catches_a_memory_tool_call():
    findings = _scan([_msg("", [("recall", '{"query": "gen-grep-count answer"}')])])
    assert findings and "memory tool called: recall" in findings[0]


def test_scan_catches_every_memory_verb():
    from core.canary.contamination import MEMORY_TOOL_NAMES

    for verb in sorted(MEMORY_TOOL_NAMES):
        assert _scan([_msg("", [(verb, "{}")])]), verb


def test_scan_catches_a_read_outside_the_workspace():
    findings = _scan([_msg("", [("file_read", '{"path": "/home/pernix/data/memories/user.profile"}')])])
    assert any("outside the workspace" in f for f in findings)
    assert any("/home/pernix/data/memories/user.profile" in f for f in findings)


def test_scan_catches_a_bash_escape_from_the_workspace():
    findings = _scan([_msg("", [("bash", '{"command": "cat /home/calvin/Pernix/data/adaptive/ADAPTIVE.md"}')])])
    assert any("outside the workspace" in f for f in findings)


def test_scan_accepts_the_workspace_toolchain_and_relative_paths():
    assert (
        _scan(
            [
                _msg("", [("file_read", '{"path": "' + WS + '/logs/app.log"}')]),
                _msg("", [("bash", '{"command": "/usr/bin/env python3 -c \\"print(1/2)\\""}')]),
                _msg("", [("bash", '{"command": "wc -l logs/*.log > answer.txt"}')]),
                _msg("", [("bash", '{"command": "grep -c ERROR logs/app.log 2>/dev/null"}')]),
            ]
        )
        == []
    )


def test_scan_catches_a_reference_to_the_suite_directory():
    findings = _scan([{"role": "assistant", "content": "I will look in data/canaries for the expected answer."}])
    assert any("data/canaries" in f for f in findings)


def test_scan_catches_another_canarys_name_in_the_transcript():
    findings = _scan([{"role": "assistant", "content": "This is the same task as grep-count, whose answer is 8."}])
    assert any("names other canaries" in f for f in findings)


def test_a_canarys_own_name_is_never_a_finding():
    """gen-grep-count contains 'grep-count' as a substring; matching on bare
    substrings would make every generated sentinel permanently contaminated."""
    assert (
        _scan(
            [
                {"role": "user", "content": "gen-grep-count: count the ERROR lines"},
                {"role": "assistant", "content": "Done — gen-grep-count answered."},
            ]
        )
        == []
    )


def test_scan_never_raises_on_junk():
    assert _scan([{"role": "assistant", "tool_calls": "not json"}, {}, {"role": "tool", "content": None}]) == []


def test_contamination_outranks_every_other_outcome():
    from core.canary.runner import CanaryRunResult

    r = CanaryRunResult(task="t", passed=True, trigger="manual", tokens=10, duration_s=5)
    assert r.outcome == "pass"
    r.contamination = ["memory tool called: recall"]
    assert r.outcome == "contaminated"
    r.passed = False
    r.error = "timeout after 300s"
    assert r.outcome == "contaminated"


async def test_a_contaminated_run_is_recorded_and_notified(monkeypatch):
    import json as _json

    from core.canary.parser import CanaryDef
    from core.canary.runner import run_canary

    def solve(ws: Path, _message: str):
        (ws / "hello.txt").write_text("hi\n")
        sid = [s for s in db.list_sessions(limit=10) if s["session_type"] == "canary"][0]["id"]
        db.add_message(
            sid,
            "assistant",
            "Let me check what the answer used to be.",
            tool_calls=_json.dumps(
                [{"id": "c0", "type": "function", "function": {"name": "recall", "arguments": '{"query": "answer"}'}}]
            ),
        )

    _fake_manager(monkeypatch, solve)
    c = CanaryDef(
        name="leaky",
        prompt="write hello.txt",
        gates=[{"name": "exists", "command": "grep -qx hi hello.txt", "watch_paths": []}],
        timeout=60,
    )
    result = await run_canary(c, trigger="manual")

    # The gates are honoured exactly as scored; only the verdict's standing changes.
    assert result.passed is True
    assert result.outcome == "contaminated"
    assert result.contamination and "recall" in result.contamination[0]

    row = db.list_canary_runs(task="leaky")[0]
    assert row["passed"] == 1 and row["outcome"] == "contaminated"
    stored = _json.loads(row["gate_results_json"])
    assert any(g.get("kind") == "contamination" and not g["passed"] for g in stored)

    notes = db.get_notifications()
    assert len([n for n in notes if "contaminated" in (n.get("title") or "")]) == 1


async def test_a_clean_run_is_not_flagged(monkeypatch):
    from core.canary.parser import CanaryDef
    from core.canary.runner import run_canary

    _fake_manager(monkeypatch, lambda ws, msg: (ws / "hello.txt").write_text("hi\n"))
    c = CanaryDef(
        name="tidy",
        prompt="write hello.txt",
        gates=[{"name": "exists", "command": "grep -qx hi hello.txt", "watch_paths": []}],
        timeout=60,
    )
    result = await run_canary(c, trigger="manual")
    assert result.contamination == [] and result.outcome == "pass"
    assert db.list_canary_runs(task="tidy")[0]["outcome"] == "pass"
    assert not [n for n in db.get_notifications() if "contaminated" in (n.get("title") or "")]


# ---------------------------------------------------------------------------
# The tripwire ignores contaminated rows
# ---------------------------------------------------------------------------


def _seed_history(batch_id: str, *, baseline_outcomes: list[str], post_outcomes: list[str]) -> None:
    from db.database import connect_sessions

    for outcome in baseline_outcomes:
        db.add_canary_run("t1", "scheduled", None, "[]", outcome in ("pass", "contaminated"), outcome=outcome)
    with connect_sessions() as conn:
        conn.execute("UPDATE canary_runs SET created_at = '2026-01-01T00:00:00+00:00' WHERE trigger = 'scheduled'")
    db.adaptive_create_batch(batch_id, "refine", "[]", status="applied")
    for outcome in post_outcomes:
        db.add_canary_run("t1", "post_batch", None, "[]", outcome == "pass", batch_id=batch_id, outcome=outcome)


def test_contaminated_post_batch_rows_cannot_convict_a_batch(monkeypatch):
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    _seed_history(
        "ab-dirty",
        baseline_outcomes=["pass", "pass", "pass"],
        post_outcomes=["contaminated", "contaminated"],
    )
    actions = evaluate_tripwire()
    assert not [a for a in actions if a["batch_id"] == "ab-dirty"]
    assert db.adaptive_get_batch("ab-dirty")["status"] == "applied"


def test_the_same_rows_would_convict_if_they_were_clean(monkeypatch):
    """The control for the test above: without the exclusion these two
    gate_fails are a confirmed regression."""
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    _seed_history(
        "ab-real",
        baseline_outcomes=["pass", "pass", "pass"],
        post_outcomes=["gate_fail", "gate_fail"],
    )
    actions = evaluate_tripwire()
    assert any(a["action"] == "flagged" and a["batch_id"] == "ab-real" for a in actions)


def test_contaminated_rows_cannot_stand_in_a_green_baseline(monkeypatch):
    """A contaminated run passed its gates, but it cannot vouch for the task:
    without three clean green runs before the apply, nothing testifies."""
    from core.adaptive.tripwire import evaluate_tripwire

    monkeypatch.setattr("config.settings.adaptive_enabled", True)
    monkeypatch.setattr("core.canary.scan_canaries", lambda *a, **k: [])
    _seed_history(
        "ab-thin",
        baseline_outcomes=["pass", "pass", "contaminated"],
        post_outcomes=["gate_fail", "gate_fail"],
    )
    actions = evaluate_tripwire()
    assert not [a for a in actions if a["batch_id"] == "ab-thin"]


# ---------------------------------------------------------------------------
# The holdout rule
# ---------------------------------------------------------------------------


def _holdout_dir(tmp_path: Path, prompt: str) -> Path:
    d = tmp_path / "held-out-task"
    d.mkdir(parents=True, exist_ok=True)
    (d / "CANARY.md").write_text(
        "---\nname: held-out-task\nprompt: |\n  "
        + prompt.replace("\n", "\n  ")
        + "\ngates:\n  - name: g\n    command: 'true'\ntags: [sentinel, holdout]\n---\nnotes\n"
    )
    return tmp_path


def test_holdout_is_a_tag_not_a_convention():
    from core.canary.parser import HOLDOUT_TAG

    assert HOLDOUT_TAG == "holdout"
    for name in GENERATED:
        assert _real_canary(name).holdout is True
    assert _real_canary("file-create").holdout is False


def test_prompt_safe_canaries_drops_the_holdouts():
    from core.canary import prompt_safe_canaries, scan_canaries

    everything = {c.name for c in scan_canaries(Path("data/canaries"))}
    safe = {c.name for c in prompt_safe_canaries(Path("data/canaries"))}
    assert set(GENERATED) <= everything
    assert not (set(GENERATED) & safe)
    assert safe == {c.name for c in scan_canaries(Path("data/canaries")) if "holdout" not in c.tags}


def test_producer_prompts_name_no_holdout_canary():
    """Nothing quotes canary names into a refine or dream prompt today. This
    fails the moment something starts — use core.canary.prompt_safe_canaries."""
    from core.canary.propose import CANARY_PROPOSALS_PROMPT
    from core.refine import REFINE_PROMPT

    prompts = [REFINE_PROMPT, CANARY_PROPOSALS_PROMPT]
    try:
        from core.dream import hypothesize

        prompts += [v for k, v in vars(hypothesize).items() if k.isupper() and isinstance(v, str)]
    except ImportError:  # pragma: no cover — dream is optional
        pass
    blob = "\n".join(prompts)
    for name in GENERATED:
        assert name not in blob, name


def test_holdout_prompts_materialise_generated_holdouts():
    from core.canary.propose import holdout_prompts

    prompts = holdout_prompts(Path("data/canaries"))
    assert set(GENERATED) <= set(prompts)
    assert all(p.strip() for p in prompts.values())
    # The reference prompt is a comparison artefact, never a run.
    assert "answer.txt" in prompts["gen-grep-count"]


def test_a_proposal_that_re_describes_a_holdout_is_refused(tmp_path):
    from core.canary.propose import resembles_holdout

    base = _holdout_dir(
        tmp_path,
        "The logs/ directory in the workspace contains application log files. Count\n"
        'how many lines across ALL files in logs/ contain the substring "ERROR".',
    )
    spec = {
        "name": "count-errors",
        "prompt": (
            "the logs directory in the workspace contains application log files count "
            "how many lines across all files in logs contain the substring ERROR!"
        ),
        "gates": [{"name": "g", "command": "true"}],
    }
    assert resembles_holdout(spec, base) == "held-out-task"


def test_a_holdout_copied_into_the_seed_files_is_also_refused(tmp_path):
    from core.canary.propose import resembles_holdout

    base = _holdout_dir(tmp_path, "Produce output.json holding the sum of amount over shipped records only.")
    spec = {
        "name": "sum-orders",
        "prompt": "Do the task described in task.md.",
        "files": {"task.md": "produce output json holding the sum of amount over shipped records only"},
        "gates": [{"name": "g", "command": "true"}],
    }
    assert resembles_holdout(spec, base) == "held-out-task"


def test_an_unrelated_proposal_is_not_refused(tmp_path):
    from core.canary.propose import resembles_holdout

    base = _holdout_dir(tmp_path, "Count how many lines across all files in logs/ contain the substring ERROR.")
    spec = {
        "name": "tar-roundtrip",
        "prompt": "Create a tar archive of src/ and verify it extracts without overwriting anything.",
        "gates": [{"name": "g", "command": "true"}],
    }
    assert resembles_holdout(spec, base) is None


def test_queue_canary_proposals_drops_a_holdout_lookalike(monkeypatch, tmp_path):
    from core.canary import propose

    monkeypatch.setattr("config.settings.canaries_dir", str(_holdout_dir(tmp_path, "Count the ERROR lines in logs/.")))
    monkeypatch.setattr("config.settings.canary_enabled", True)
    monkeypatch.setattr("config.settings.canary_auto_admit", False)

    lookalike = {
        "name": "error-count",
        "prompt": "count the error lines in logs",
        "gates": [{"name": "g", "command": "grep -qx '4' answer.txt"}],
        "rationale": "r",
    }
    assert propose.queue_canary_proposals([lookalike], producer="refine") == 0
    assert db.adaptive_list_proposals() == []


def test_materialize_canary_refuses_a_holdout_lookalike(monkeypatch, tmp_path):
    from core.canary.propose import materialize_canary

    base = _holdout_dir(tmp_path, "Count the ERROR lines in logs/ and write the number to answer.txt.")
    name, err = materialize_canary(
        {
            "name": "error-count",
            "prompt": "count the error lines in logs and write the number to answer txt",
            "gates": [{"name": "g", "command": "true"}],
        },
        base=base,
    )
    assert name is None and "holdout" in err
    assert not (base / "error-count").exists()


def test_a_genuinely_new_proposal_still_materialises(monkeypatch, tmp_path):
    from core.canary.propose import materialize_canary

    base = _holdout_dir(tmp_path, "Count the ERROR lines in logs/.")
    name, err = materialize_canary(
        {
            "name": "tar-roundtrip",
            "prompt": "Create a tar archive of src/ and verify it extracts without overwriting anything.",
            "gates": [{"name": "g", "command": "test -f out.tar"}],
        },
        base=base,
    )
    assert err == "" and name == "tar-roundtrip"
