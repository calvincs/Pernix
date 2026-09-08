"""`{"arguments": "null"}` killed the turn instead of being refused.

JSON's root may be a scalar, a list or null; a tool argument schema accepts
none of those. Two places assumed otherwise, neither inside a try:

  * `_ToolCallGate._parse_and_validate` json-parsed the argument string and
    went straight into the required-parameter membership test, so `null` and
    `42` raised TypeError. `[]` reached `_summarize_args` and raised
    AttributeError on `.items()`. `"path"` and `["path"]` slipped through the
    membership test — substring and element containment both say yes — and
    died in `_validate_arg_types` instead.
  * Signal 12 of `StuckDetector.evaluate`, which runs BEFORE the gate, called
    `args.get("path")` on the same parsed value for file_read/grep/bash.

Either way the exception unwound past the tool-execution handler to
sessions/manager, and a repairable argument mistake ended the turn with a raw
Python error. Both now produce the ordinary admission rejection, which — since
the assistant row carries refused calls — reaches the model's next request.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.agent import StuckDetector, _ToolCallGate
from core.context.compiler import compile_context
from core.tools.registry import ToolRegistry
from db import models as db

# Every JSON root that is not an object, plus the object control.
NON_OBJECT_ARGUMENTS = ["null", "42", "true", '"path"', "[]", '["path"]']


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        "file_read",
        lambda path: path,
        "read a file",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "file path"}},
            "required": ["path"],
        },
    )
    return reg


def _gate(registry: ToolRegistry, save, events: list) -> _ToolCallGate:
    return _ToolCallGate(
        registry=registry,
        session=SimpleNamespace(emit_event=events.append),
        save_turn_msg=save,
        stuck=StuckDetector(),
        tool_failures={},
    )


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", NON_OBJECT_ARGUMENTS)
async def test_a_non_object_root_is_refused_not_raised(raw):
    saved: list[tuple[str, str]] = []
    events: list[dict] = []

    async def _save(role, content, **kwargs):
        saved.append((role, content))

    gate = _gate(_registry(), _save, events)
    parsed, _notes = await gate.admit([{"id": "c1", "name": "file_read", "arguments": raw}], ["file_read"])
    await gate.flush_rejections()

    assert parsed == [], "a non-object argument root must never reach the executor"
    assert any("must be a JSON object" in content for _role, content in saved)
    assert any(e.get("was_error") for e in events), "the UI is told too"


async def test_an_object_root_still_admits():
    """The control: the guard rejects shapes, not arguments."""
    saved: list[tuple[str, str]] = []

    async def _save(role, content, **kwargs):
        saved.append((role, content))

    gate = _gate(_registry(), _save, [])
    parsed, _notes = await gate.admit(
        [{"id": "c1", "name": "file_read", "arguments": json.dumps({"path": "a.py"})}], ["file_read"]
    )

    assert [item["parsed_args"] for item in parsed] == [{"path": "a.py"}]
    assert saved == []


@pytest.mark.parametrize("raw", NON_OBJECT_ARGUMENTS)
async def test_the_refusal_names_what_it_received(raw):
    """The model has to be able to see its own mistake to fix it."""
    saved: list[str] = []

    async def _save(role, content, **kwargs):
        saved.append(content)

    gate = _gate(_registry(), _save, [])
    await gate.admit([{"id": "c1", "name": "file_read", "arguments": raw}], ["file_read"])
    await gate.flush_rejections()

    assert any(raw in content for content in saved)


# ---------------------------------------------------------------------------
# Stuck detection, which runs first
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", NON_OBJECT_ARGUMENTS)
@pytest.mark.parametrize("name", ["file_read", "grep", "bash"])
def test_signal_12_survives_a_non_object_root(raw, name):
    registry = SimpleNamespace(exists=lambda n: True)
    score, repeats = StuckDetector().evaluate("", [{"name": name, "arguments": raw}], {}, registry)
    assert score >= 0.0 and repeats == 0


def test_signal_12_still_counts_real_reads():
    """The guard skips bad calls, not the counting it exists to do."""
    registry = SimpleNamespace(exists=lambda n: True)
    detector = StuckDetector()
    for _ in range(3):
        detector.evaluate("", [{"name": "file_read", "arguments": json.dumps({"path": "big.py"})}], {}, registry)
    assert detector.file_read_counts["big.py"] == 3


# ---------------------------------------------------------------------------
# End to end: the feedback lands in the next request (with F05)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", NON_OBJECT_ARGUMENTS)
async def test_the_feedback_reaches_the_next_compiled_request(raw):
    registry = _registry()
    session_id = db.create_session(title=f"non-object {raw}")
    user_msg_id = db.add_message(session_id, "user", "read the file")
    meta = json.dumps({"parent_user_msg_id": user_msg_id})

    async def _save(role, content, **kwargs):
        return db.add_message(session_id, role, content, metadata=meta, **kwargs)

    gate = _gate(registry, _save, [])
    parsed, _notes = await gate.admit([{"id": "c1", "name": "file_read", "arguments": raw}], ["file_read"])
    proposed = gate.proposed_calls
    db.add_message(session_id, "assistant", "", tool_calls=json.dumps(proposed), metadata=meta)
    await gate.flush_rejections()

    payload = compile_context(
        session_id=session_id,
        tool_schemas=registry.get_schemas(["file_read"]),
        turn_user_msg_id=user_msg_id,
    )
    history = "\n".join(str(m.get("content") or "") for m in payload.messages if m.get("role") != "system")
    assert "must be a JSON object" in history
    assert parsed == []
