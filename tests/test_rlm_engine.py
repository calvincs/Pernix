"""Tests for the RLM engine core (core/extensions/rlm/).

Scripted root/sub LLMs + REAL child subprocesses: every test that executes a
cell spawns an actual sandboxed child REPL, so the socket protocol, namespace
persistence, and kill discipline are exercised for real.
"""

import socket
import struct
import time

import pytest

from core.extensions.rlm import child_env as child_env_mod
from core.extensions.rlm import protocol
from core.extensions.rlm.broker import LLMBroker, SubcallLedger
from core.extensions.rlm.child_env import ChildREPL, stage_context
from core.extensions.rlm.engine import RLMEngine
from core.extensions.rlm.parsing import MAX_CELL_OUTPUT_CHARS, find_code_blocks, format_iteration
from core.extensions.rlm.prompts import BUDGET_NOTICE, NO_BLOCK_NUDGE
from core.extensions.rlm.types import CellResult, RLMBudgetExhausted, RLMCaps, RLMChildDied

# =============================================================================
# Helpers
# =============================================================================


def _echo_sub_chat(prompt, model, timeout):
    return f"echo:{prompt[:40]}"


class ScriptedRoot:
    """root_chat seam fake: returns scripted responses, records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def __call__(self, messages, timeout):
        self.calls.append([dict(m) for m in messages])
        if not self.responses:
            raise AssertionError("ScriptedRoot ran out of responses")
        return self.responses.pop(0)


def _make_engine(tmp_path, *, task="What is in the context?", text="hello world " * 50, **kw):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    staged = stage_context(run_dir, text=text)
    defaults = dict(
        run_dir=run_dir,
        task=task,
        staged=staged,
        sub_chat=_echo_sub_chat,
        caps=RLMCaps(max_iterations=5, timeout_seconds=60),
    )
    defaults.update(kw)
    return RLMEngine(**defaults)


@pytest.fixture
def child(tmp_path):
    c = ChildREPL(tmp_path)
    c.start()
    yield c
    c.cleanup()


def _cell(child, code, deadline_in=30.0, **kw):
    return child.execute_cell(code, deadline=time.monotonic() + deadline_in, **kw)


# =============================================================================
# protocol
# =============================================================================


def test_frame_roundtrip():
    a, b = socket.socketpair()
    payload = {"type": "exec", "code": "print('héllo')", "n": 3}
    protocol.send_frame(a, payload)
    assert protocol.recv_frame(b) == payload
    a.close()
    with pytest.raises(EOFError):
        protocol.recv_frame(b)


def test_frame_cap_send(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_FRAME_BYTES", 100)
    a, _b = socket.socketpair()
    with pytest.raises(protocol.FrameError):
        protocol.send_frame(a, {"data": "x" * 200})


def test_frame_cap_recv(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_FRAME_BYTES", 100)
    a, b = socket.socketpair()
    a.sendall(struct.pack(">I", 5000))  # hostile length prefix, no body needed
    with pytest.raises(protocol.FrameError):
        protocol.recv_frame(b)


# =============================================================================
# parsing
# =============================================================================


def test_find_code_blocks_multi():
    text = "plan\n```repl\nx = 1\n```\nmore\n```repl\nprint(x)\n```\n```python\nignored\n```"
    assert find_code_blocks(text) == ["x = 1", "print(x)"]


def test_format_iteration_truncates_and_shapes():
    cells = [CellResult(stdout="a" * (MAX_CELL_OUTPUT_CHARS + 500)), CellResult(stdout="tiny")]
    msgs = format_iteration("resp", cells)
    assert [m["role"] for m in msgs] == ["assistant", "user"]
    assert "chars truncated]" in msgs[1]["content"]
    assert "REPL output (block 2):" in msgs[1]["content"]
    assert format_iteration("resp", []) == [{"role": "assistant", "content": "resp"}]


# =============================================================================
# child REPL (real subprocess)
# =============================================================================


def test_child_exec_and_state_persistence(child):
    r1 = _cell(child, "x = 41\nprint('setup done')")
    assert "setup done" in r1.stdout and r1.stderr == ""
    r2 = _cell(child, "print(x + 1)")
    assert "42" in r2.stdout
    assert any(v.startswith("x:int") for v in r2.var_names)


def test_child_scaffold_restore_and_show_vars(child):
    staged = stage_context(child.run_dir, text="the context body")
    child.load_context(staged)
    _cell(child, "context = 'clobbered'\nllm_query = None")
    r = _cell(child, "print(context)\nprint(SHOW_VARS())")
    assert "the context body" in r.stdout  # scaffold restored from context_0
    assert "Available variables" in r.stdout


def test_child_answer_ready_including_rebound_dict(child):
    r = _cell(child, "answer['content'] = 'done'\nanswer['ready'] = True")
    assert r.final_answer == "done"
    # model rebinds `answer` to a plain dict — restore still captures it
    r2 = _cell(child, "answer = {'content': 'plain', 'ready': True}")
    assert r2.final_answer == "plain"


def test_child_error_traceback_is_cell_scoped(child):
    r = _cell(child, "def boom():\n    return 1 / 0\nboom()")
    assert "ZeroDivisionError" in r.stderr
    assert "<cell>" in r.stderr
    assert "child_runner" not in r.stderr
    # child survives the exception
    assert "ok" in _cell(child, "print('ok')").stdout


def test_child_blocked_builtins(child):
    r = _cell(child, "eval('1+1')")
    assert "TypeError" in r.stderr  # eval is None -> not callable


def test_child_env_is_scrubbed(child, monkeypatch):
    # parent has secrets; the child env is built from scratch and must not
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-leak-canary")
    r = _cell(child, "import os\nprint(sorted(os.environ.keys()))")
    assert "OPENROUTER_API_KEY" not in r.stdout
    assert "PATH" in r.stdout


def test_child_multi_file_context(tmp_path):
    (tmp_path / "a.txt").write_text("alpha doc")
    (tmp_path / "b.txt").write_text("beta doc")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    staged = stage_context(run_dir, files=[tmp_path / "a.txt", tmp_path / "b.txt"])
    assert staged.context_type == "list[str]" and staged.total_chars == 17
    c = ChildREPL(run_dir)
    c.start()
    try:
        c.load_context(staged)
        r = _cell(c, "print(len(context), context_files)\nprint(context[1])")
        assert "2 ['a.txt', 'b.txt']" in r.stdout
        assert "beta doc" in r.stdout
    finally:
        c.cleanup()


def test_child_soft_oom_survivable(tmp_path):
    c = ChildREPL(tmp_path, address_space_limit=1024 * 1024 * 1024)
    c.start()
    try:
        r = _cell(c, "x = 'a' * (4 * 1024 * 1024 * 1024)")
        assert "MemoryError" in r.stderr
        assert "ok" in _cell(c, "print('ok')").stdout  # namespace/process intact
    finally:
        c.cleanup()


def test_child_hard_death_detected(child):
    with pytest.raises(RLMChildDied):
        _cell(child, "import os\nos._exit(9)")


def test_child_runaway_cell_gets_sigint(child, monkeypatch):
    monkeypatch.setattr(child_env_mod, "CELL_QUIET_TIMEOUT", 1.0)
    r = _cell(child, "while True:\n    pass")
    assert "KeyboardInterrupt" in r.stderr
    # namespace preserved, child still serving
    assert "ok" in _cell(child, "print('ok')").stdout


# =============================================================================
# broker (real child calling through the unix socket)
# =============================================================================


@pytest.fixture
def brokered_child(tmp_path):
    ledger = SubcallLedger(limit=5)
    broker = LLMBroker(
        tmp_path / "llm.sock",
        sub_chat=_echo_sub_chat,
        caps=RLMCaps(max_subcalls=5, max_concurrent_subcalls=2),
        ledger=ledger,
    )
    broker.start()
    c = ChildREPL(tmp_path)
    c.start()
    yield c, broker, ledger
    c.cleanup()
    broker.stop()


def test_brokered_llm_query(brokered_child):
    c, _broker, ledger = brokered_child
    r = _cell(c, "print(llm_query('summarize this'))")
    assert "echo:summarize this" in r.stdout
    assert ledger.count == 1


def test_brokered_batched_order_preserved(brokered_child):
    c, _broker, ledger = brokered_child
    r = _cell(c, "print(llm_query_batched(['p0', 'p1', 'p2']))")
    assert "['echo:p0', 'echo:p1', 'echo:p2']" in r.stdout
    assert ledger.count == 3


def test_brokered_ledger_cap(brokered_child):
    c, _broker, ledger = brokered_child
    r = _cell(c, "for i in range(7):\n    print(llm_query(f'p{i}'))")
    assert "budget exhausted" in r.stdout
    assert ledger.count == 5


def test_brokered_model_allowlist(tmp_path):
    broker = LLMBroker(
        tmp_path / "llm.sock",
        sub_chat=_echo_sub_chat,
        caps=RLMCaps(),
        ledger=SubcallLedger(50),
        allowed_models={"allowed-model"},
    )
    broker.start()
    c = ChildREPL(tmp_path)
    c.start()
    try:
        r = _cell(c, "print(llm_query('x', model='gpt-expensive'))\nprint(llm_query('y', model='allowed-model'))")
        assert "not allowed for this run" in r.stdout
        assert "echo:y" in r.stdout
    finally:
        c.cleanup()
        broker.stop()


def test_rlm_query_falls_back_to_llm(brokered_child):
    c, _broker, _ledger = brokered_child
    r = _cell(c, "print(rlm_query('deep question'))")
    assert "echo:deep question" in r.stdout


# =============================================================================
# engine end-to-end (scripted root, real child)
# =============================================================================


def test_engine_completes(tmp_path):
    root = ScriptedRoot(
        [
            "Probing first.\n```repl\nprint(len(context))\nprint(context[:11])\n```",
            "Delegating.\n```repl\nsummary = llm_query('sum: ' + context[:20])\nprint(summary)\n```",
            "Done.\n```repl\nanswer['content'] = 'FINAL ' + summary\nanswer['ready'] = True\n```",
        ]
    )
    engine = _make_engine(tmp_path, root_chat=root)
    result = engine.run()
    assert result.status == "completed" and not result.partial
    assert result.answer == "FINAL echo:sum: hello world hello wo"
    assert result.iterations == 3 and result.subcalls == 1
    assert (engine.run_dir / "answer.txt").read_text() == result.answer
    trace = (engine.run_dir / "trace.jsonl").read_text()
    assert '"type": "end"' in trace and '"subcall"' in trace
    # turn prompts flowed through the seam
    assert "Turn 1/5" in root.calls[0][-1]["content"]


def test_engine_no_block_nudge(tmp_path):
    root = ScriptedRoot(
        [
            "I will now inspect the context (no code emitted).",
            "```repl\nanswer['content'] = 'ok'\nanswer['ready'] = True\n```",
        ]
    )
    result = _make_engine(tmp_path, root_chat=root).run()
    assert result.status == "completed"
    assert any(m["content"] == NO_BLOCK_NUDGE for m in root.calls[1])


def test_engine_iteration_cap_synthesis(tmp_path):
    caps = RLMCaps(max_iterations=2, timeout_seconds=60)
    root = ScriptedRoot(
        [
            "```repl\nprint('turn a')\n```",
            "```repl\nprint('turn b')\n```",
            "synthesized best-effort answer",  # the synthesis call
        ]
    )
    result = _make_engine(tmp_path, root_chat=root, caps=caps).run()
    assert result.status == "iteration_cap" and result.partial
    assert result.answer == "synthesized best-effort answer"
    assert "out of REPL turns" in root.calls[-1][-1]["content"]


def test_engine_timeout(tmp_path):
    caps = RLMCaps(max_iterations=5, timeout_seconds=0.01)
    result = _make_engine(tmp_path, root_chat=ScriptedRoot([]), caps=caps).run()
    assert result.status == "timeout" and result.partial


def test_engine_cancelled(tmp_path):
    result = _make_engine(tmp_path, root_chat=ScriptedRoot([]), cancel_check=lambda: True).run()
    assert result.status == "cancelled" and result.partial


def test_engine_root_failure_is_salvaged(tmp_path):
    def bad_root(messages, timeout):
        raise ConnectionError("provider down")

    result = _make_engine(tmp_path, root_chat=bad_root).run()
    assert result.status == "failed"
    assert "provider down" in result.error


def test_engine_budget_exhausted_notice(tmp_path):
    def broke_sub_chat(prompt, model, timeout):
        raise RLMBudgetExhausted("session LLM budget exhausted")

    root = ScriptedRoot(
        [
            "```repl\nprint(llm_query('x'))\n```",
            "```repl\nanswer['content'] = 'wrapped up'\nanswer['ready'] = True\n```",
        ]
    )
    result = _make_engine(tmp_path, root_chat=root, sub_chat=broke_sub_chat).run()
    assert result.status == "completed"
    assert any(m["content"] == BUDGET_NOTICE for m in root.calls[1])


def test_engine_rejects_event_loop():
    async def _on_loop():
        RLMEngine._assert_off_loop()

    import asyncio

    with pytest.raises(RuntimeError, match="event loop"):
        asyncio.run(_on_loop())


def test_trim_messages_keeps_head():
    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "T"}]
    for i in range(40):
        messages.append({"role": "assistant", "content": f"a{i}" + "x" * 20_000})
        messages.append({"role": "user", "content": f"u{i}" + "y" * 20_000})
    RLMEngine._trim_messages(messages)
    total = sum(len(m["content"]) for m in messages)
    assert total <= 400_000 + 20_500
    assert messages[0]["content"] == "S" and messages[1]["content"] == "T"
    assert "elided" in messages[2]["content"]
