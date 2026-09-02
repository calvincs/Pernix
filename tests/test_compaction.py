"""Tests for core/context/compaction.py: view pruning, orphan exclusion, LLM compaction."""

import json

import pytest

from core.context.compaction import (
    _serialize_messages,
    apply_view_pruning,
    compact_with_llm,
    exclude_orphans,
)

# ---------------------------------------------------------------------------
# apply_view_pruning
# ---------------------------------------------------------------------------


def test_view_pruning_short_list():
    """Lists shorter than keep_recent are returned as-is."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "content": "a" * 500},
    ]
    result = apply_view_pruning(messages, keep_recent=10)
    assert len(result) == 2
    assert result[1]["content"] == "a" * 500


def test_view_pruning_stubs_old_tools():
    """Old tool results above min_chars are stubbed (and marked)."""
    messages = []
    for i in range(15):
        messages.append({"role": "user", "content": f"msg {i}"})
        messages.append({"role": "tool", "content": f"output {'x' * 500}"})

    result = apply_view_pruning(messages, keep_recent=4, min_chars=300)
    # Old tool messages should be stubbed
    old_tool = result[1]  # Second message (first tool)
    assert "[pruned" in old_tool["content"]
    assert old_tool.get("_view_pruned") is True  # compiler counts these
    # Recent tool messages should be intact
    recent_tool = result[-1]
    assert "output" in recent_tool["content"]


def test_view_pruning_preserves_short_tools():
    """Old tool results at or below min_chars are kept intact — the raised
    default (2000) means routine tool results survive pruning now."""
    messages = []
    for i in range(15):
        messages.append({"role": "user", "content": f"msg {i}"})
        messages.append({"role": "tool", "content": "x" * 1500})

    result = apply_view_pruning(messages, keep_recent=4)
    assert all("[pruned" not in m.get("content", "") for m in result)


def test_view_pruning_non_tool_untouched():
    """Non-tool messages are never pruned."""
    messages = []
    for i in range(15):
        messages.append({"role": "user", "content": f"long user message {'x' * 500}"})
        messages.append({"role": "assistant", "content": f"long assistant {'x' * 500}"})

    result = apply_view_pruning(messages, keep_recent=4)
    for m in result:
        assert "[pruned" not in m.get("content", "")


# ---------------------------------------------------------------------------
# exclude_orphans
# ---------------------------------------------------------------------------


def test_exclude_orphans_keeps_valid():
    messages = [
        {"role": "assistant", "tool_calls": json.dumps([{"id": "tc1"}])},
        {"role": "tool", "tool_call_id": "tc1", "content": "result"},
        {"role": "user", "content": "hi"},
    ]
    result = exclude_orphans(messages)
    assert len(result) == 3


def test_exclude_orphans_removes_orphan():
    messages = [
        {"role": "tool", "tool_call_id": "nonexistent", "content": "orphan"},
        {"role": "user", "content": "hi"},
    ]
    result = exclude_orphans(messages)
    assert len(result) == 1
    assert result[0]["role"] == "user"


def test_exclude_orphans_tool_calls_as_list():
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "tc1"}, {"id": "tc2"}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "r1"},
        {"role": "tool", "tool_call_id": "tc2", "content": "r2"},
        {"role": "tool", "tool_call_id": "tc3", "content": "orphan"},
    ]
    result = exclude_orphans(messages)
    assert len(result) == 3  # assistant + 2 valid tools


def test_exclude_orphans_no_tool_calls():
    """Tool messages without tool_call_id are kept."""
    messages = [
        {"role": "tool", "content": "no id"},
        {"role": "user", "content": "hi"},
    ]
    result = exclude_orphans(messages)
    assert len(result) == 2  # both kept (no tool_call_id means not orphaned)


# ---------------------------------------------------------------------------
# _serialize_messages
# ---------------------------------------------------------------------------


def test_serialize_basic():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    result = _serialize_messages(messages)
    assert "[user] hello" in result
    assert "[assistant] world" in result


def test_serialize_truncates_long():
    messages = [
        {"role": "user", "content": "x" * 3000},
    ]
    result = _serialize_messages(messages)
    assert len(result) <= 2050  # 2000 char content + role prefix


def test_serialize_budget():
    messages = [{"role": "user", "content": "x" * 500} for _ in range(100)]
    result = _serialize_messages(messages, max_chars=2000)
    assert "truncated" in result
    assert len(result) <= 3000  # slightly over due to last line + marker


def test_serialize_list_content():
    """Handle list-format content (vision messages)."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        },
    ]
    result = _serialize_messages(messages)
    assert "describe this" in result


# ---------------------------------------------------------------------------
# compact_with_llm (async)
# ---------------------------------------------------------------------------


async def test_compact_with_llm_success(mock_llm_client):
    """LLM compaction writes a compaction marker."""
    from core.llm.types import ChatResponse, TokenUsage
    from db import models as db

    sid = db.create_session(title="Compact Test")
    for i in range(10):
        db.add_message(sid, "user", f"Message {i} with some content " * 10)
        db.add_message(sid, "assistant", f"Response {i} with analysis " * 10)

    # Configure fake LLM to return a good summary
    mock_llm_client.responses = [
        ChatResponse(
            content='```json\n{"goal": "testing", "progress": ["msg sent"]}\n```\nGood summary.',
            tool_calls=None,
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]

    messages = db.get_messages(sid)
    msg_dicts = [{"role": m["role"], "content": m["content"], "id": m["id"]} for m in messages]
    result = await compact_with_llm(sid, msg_dicts)
    assert result is True

    # Verify compaction marker was written
    all_msgs = db.get_messages(sid)
    compaction_msgs = [m for m in all_msgs if m["role"] == "compaction"]
    assert len(compaction_msgs) == 1


async def test_compact_with_llm_uses_session_sched_identity(mock_llm_client):
    """The agent loop awaits compaction mid-turn, so the LLM call must carry
    the session's own scheduling identity — the default background identity
    (created_at=inf) sorts last in the fair queue (priority inversion)."""
    from core.llm.semaphore import PRIORITY_ORCHESTRATOR
    from core.llm.types import ChatResponse, TokenUsage
    from db import models as db

    sid = db.create_session(title="Sched Identity Test")
    for i in range(10):
        db.add_message(sid, "user", f"Message {i} with some content " * 10)
        db.add_message(sid, "assistant", f"Response {i} with analysis " * 10)

    mock_llm_client.responses = [
        ChatResponse(
            content='```json\n{"goal": "testing", "progress": ["msg sent"]}\n```\nGood summary.',
            tool_calls=None,
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            model="test",
            provider="fake",
            finish_reason="stop",
        )
    ]
    messages = db.get_messages(sid)
    msg_dicts = [{"role": m["role"], "content": m["content"], "id": m["id"]} for m in messages]
    assert await compact_with_llm(sid, msg_dicts) is True

    call = mock_llm_client.calls[-1]
    assert call["session_id"] == sid
    assert call["session_created_at"] != float("inf")
    assert call["session_priority"] == PRIORITY_ORCHESTRATOR


async def test_compact_with_llm_too_few_messages(mock_llm_client):
    """Rejects compaction if fewer than 4 messages."""
    from db import models as db

    sid = db.create_session(title="Short")
    db.add_message(sid, "user", "hi")
    db.add_message(sid, "assistant", "hello")

    messages = db.get_messages(sid)
    msg_dicts = [{"role": m["role"], "content": m["content"], "id": m["id"]} for m in messages]
    result = await compact_with_llm(sid, msg_dicts)
    assert result is False


async def test_compact_with_llm_failure(mock_llm_client):
    """Handles LLM failure gracefully."""
    from db import models as db

    sid = db.create_session(title="Fail")
    for i in range(10):
        db.add_message(sid, "user", f"Message {i} " * 20)
        db.add_message(sid, "assistant", f"Response {i} " * 20)

    # Make LLM raise an error
    async def failing_chat(*args, **kwargs):
        raise ConnectionError("LLM down")

    mock_llm_client.chat = failing_chat

    messages = db.get_messages(sid)
    msg_dicts = [{"role": m["role"], "content": m["content"], "id": m["id"]} for m in messages]
    result = await compact_with_llm(sid, msg_dicts)
    assert result is False


def _summary_response():
    from core.llm.types import ChatResponse, TokenUsage

    return ChatResponse(
        content='```json\n{"goal": "t", "progress": ["p"]}\n```\nSummary prose.',
        tool_calls=None,
        usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        model="test",
        provider="fake",
        finish_reason="stop",
    )


async def test_compact_with_llm_ignores_missing_payload_ids(mock_llm_client, monkeypatch):
    """Regression (compaction loop): the real caller passes compiled messages
    that have been stripped of their DB ids (_strip_private_fields). Compaction
    must derive compacted_up_to from the DB, never from the payload. The old
    `to_summarize[-1].get("id", 0)` returned 0 on every real call, pinning the
    pointer at 0 so the active window never shrank and compaction re-fired
    forever (observed: 8 markers in ~7 min, all compacted_up_to=0)."""
    import json as _json

    from db import models as db

    monkeypatch.setattr("config.settings.compaction_keep_tokens", 120)

    sid = db.create_session(title="Strip Test")
    for i in range(12):
        db.add_message(sid, "user", f"User message number {i} " * 8)
        db.add_message(sid, "assistant", f"Assistant reply number {i} " * 8)

    mock_llm_client.responses = [_summary_response()]

    # Simulate the stripped payload: role + content only, NO id / _db_id.
    stripped = [{"role": m["role"], "content": m["content"]} for m in db.get_messages(sid)]
    assert await compact_with_llm(sid, stripped) is True

    all_msgs = db.get_messages(sid)
    markers = [m for m in all_msgs if m["role"] == "compaction"]
    assert len(markers) == 1
    ptr = _json.loads(markers[0]["metadata"])["compacted_up_to"]
    real_ids = [m["id"] for m in all_msgs if m["role"] in ("user", "assistant")]
    # The pointer must ADVANCE to a real message id — never the historical 0.
    assert ptr > 0
    assert ptr in real_ids


async def test_compact_with_llm_resumes_from_prior_marker(mock_llm_client, monkeypatch):
    """A second compaction summarizes only messages added since the prior
    marker and advances compacted_up_to (never rewinds/re-summarizes)."""
    import json as _json

    from db import models as db

    monkeypatch.setattr("config.settings.compaction_keep_tokens", 50)

    sid = db.create_session(title="Resume Test")
    for i in range(8):
        db.add_message(sid, "user", f"first batch {i} " * 8)
        db.add_message(sid, "assistant", f"first reply {i} " * 8)

    mock_llm_client.responses = [_summary_response()]
    stripped = [{"role": m["role"], "content": m["content"]} for m in db.get_messages(sid)]
    assert await compact_with_llm(sid, stripped) is True
    first_marker = [m for m in db.get_messages(sid) if m["role"] == "compaction"][-1]
    first_ptr = _json.loads(first_marker["metadata"])["compacted_up_to"]
    assert first_ptr > 0

    # New activity after the first compaction.
    for i in range(8):
        db.add_message(sid, "user", f"second batch {i} " * 8)
        db.add_message(sid, "assistant", f"second reply {i} " * 8)

    mock_llm_client.responses = [_summary_response()]
    stripped2 = [{"role": m["role"], "content": m["content"]} for m in db.get_messages(sid)]
    assert await compact_with_llm(sid, stripped2) is True

    markers = [m for m in db.get_messages(sid) if m["role"] == "compaction"]
    assert len(markers) == 2
    second_ptr = _json.loads(markers[-1]["metadata"])["compacted_up_to"]
    assert second_ptr > first_ptr


# ---------------------------------------------------------------------------
# Event-loop hygiene
# ---------------------------------------------------------------------------


def test_hot_path_db_calls_run_off_the_event_loop():
    """Post-hooks, compaction and the agent loop must not touch the DB inline.

    These run while other sessions are mid-stream, and several load the full
    transcript — with 100KB tool results an inline read freezes every
    session's SSE for its duration. Anything genuinely cheap and indexed
    (single-row lookups, MAX(created_at), question rows) is exempt.
    """
    offenders = [o for o in _find_on_loop_blocking_calls() if o.startswith(_DB_GUARDED_PATHS)]
    assert not offenders, "blocking DB calls on the event loop:\n  " + "\n  ".join(offenders)


# Modules whose db.* usage has been audited and moved off-loop. The repo-wide
# ratchet below covers everything else without demanding a big-bang refactor.
_DB_GUARDED_PATHS = ("sessions/hooks.py", "core/context/compaction.py", "core/agent.py")

# Known on-loop db.* calls outside the audited modules, as of 2026-07-25.
# These predate the memory-store work and are NOT fixed here — this is a
# ratchet, not an amnesty: the count may drop, never grow. Concentrated in
# sessions/manager.py (7, mostly notice/divider writes in _finalize_turn and
# transcript reads in _finalize_worker) and api/routers/chat.py (3).
_KNOWN_ON_LOOP_DB_CALLS = 11


def test_no_new_on_loop_db_calls_are_introduced():
    """Ratchet: the audited modules are clean (asserted above); everywhere
    else must not get worse. Lower this number when you fix some."""
    db_offenders = [
        o for o in _find_on_loop_blocking_calls() if " calls db." in o and not o.startswith(_DB_GUARDED_PATHS)
    ]
    assert (
        len(db_offenders) <= _KNOWN_ON_LOOP_DB_CALLS
    ), f"new on-loop db call(s): {len(db_offenders)} > {_KNOWN_ON_LOOP_DB_CALLS}\n  " + "\n  ".join(db_offenders)
    if len(db_offenders) < _KNOWN_ON_LOOP_DB_CALLS:
        raise AssertionError(
            f"on-loop db calls dropped to {len(db_offenders)} — lower "
            f"_KNOWN_ON_LOOP_DB_CALLS to match so the ratchet keeps holding."
        )


# Heavy synchronous surfaces that must never run on the event loop.
_HEAVY_DB = {"get_messages", "add_message", "add_token_usage", "add_compaction"}
# MemoryStore: every one of these hits SQLite and/or the filesystem, and
# add_entry/health_check additionally take a threading.Lock — acquiring that
# from the loop stalls every other session until it is released.
_HEAVY_STORE = {
    "search",
    "search_lessons",
    "list_files",
    "read_file",
    "health_check",
    "reindex",
    "add_entry",
    "update_entry",
    "archive_entry",
    "forget",
}


def _own_body(fn):
    """Nodes belonging to `fn` itself, excluding nested function bodies.

    ast.walk() descends into nested defs, so a SYNC helper defined inside an
    async function looks like it lives in the async scope. Those helpers are
    fine — the caller dispatches them via to_thread (scout's
    _gather_memory_baseline is exactly this shape). Counting them produced
    false positives that nearly led to "fixing" correct code.
    """
    import ast

    out = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            out.append(child)
            walk(child)

    walk(fn)
    return out


def _find_on_loop_blocking_calls() -> list[str]:
    """Repo-wide scan for heavy sync calls made directly from async code."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders: list[str] = []

    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".venv/", "tests/", "build/", "data/")):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in _own_body(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                attr = node.func.attr
                base = node.func.value
                base_name = base.id if isinstance(base, ast.Name) else ""
                # Passing the function OBJECT to to_thread is an ast.Attribute,
                # not an ast.Call, so correct usage never reaches here.
                if attr in _HEAVY_DB and base_name in ("db", "_db", "db_models"):
                    offenders.append(f"{rel}:{node.lineno} {fn.name}() calls db.{attr} inline")
                elif attr in _HEAVY_STORE and (base_name.endswith("store") or base_name in ("store", "_store")):
                    offenders.append(f"{rel}:{node.lineno} {fn.name}() calls {base_name}.{attr} inline")

    return offenders


def test_memory_store_is_never_called_from_the_event_loop():
    """MemoryStore is fully synchronous — SQLite queries, markdown file I/O,
    and a threading.Lock. Calling it from an async handler blocks the loop for
    the whole operation; health_check(fix=True) walks every markdown file and
    rebuilds the index, which on a real store (125 files / 3743 entries) is
    seconds of frozen SSE for every other session.

    Covers the whole repo, not a hand-listed set of files — the original
    version of this guard listed three modules and so never saw the 15
    MemoryStore call sites in snooze, the memory router, and maintenance.
    """
    offenders = [o for o in _find_on_loop_blocking_calls() if " calls db." not in o]
    assert not offenders, "MemoryStore called on the event loop:\n  " + "\n  ".join(offenders)


def test_guard_ignores_nested_sync_helpers():
    """The guard must not flag a sync helper nested in an async function —
    those are dispatched via to_thread by their caller."""
    import ast

    src = (
        "import asyncio\n"
        "async def outer():\n"
        "    def _helper():\n"
        "        return store.list_files()\n"
        "    return await asyncio.to_thread(_helper)\n"
    )
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))
    calls = [n for n in _own_body(fn) if isinstance(n, ast.Call)]
    names = {n.func.attr for n in calls if isinstance(n.func, ast.Attribute)}
    assert "list_files" not in names, "nested sync helper must not count as on-loop"


def test_sweeps_archive_markdown_and_index_atomically():
    """The markdown archive-tag and the FTS removal were two separate sync
    calls from async code. A cancel between them left the index serving
    entries whose markdown said archived. They are now one helper dispatched
    through to_thread, which cannot be cancelled mid-flight."""
    import ast
    import inspect
    import pathlib

    import core.memory.sweeps as sweeps_mod

    body = inspect.getsource(sweeps_mod.archive_entries)
    assert "_archive_entries_in_file" in body and "_remove_from_index" in body

    # No async caller may invoke either half on its own any more.
    halves = {"_archive_entries_in_file", "_remove_from_index"}
    stray = []
    for mod in (sweeps_mod, __import__("core.snooze", fromlist=["x"])):
        tree = ast.parse(pathlib.Path(mod.__file__).read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in _own_body(fn):
                called = ""
                if isinstance(node, ast.Call):
                    called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if called in halves:
                    stray.append(f"{mod.__name__}.{fn.name}():{node.lineno} calls {called} directly")
    assert not stray, "archive halves must be paired via archive_entries:\n  " + "\n  ".join(stray)
