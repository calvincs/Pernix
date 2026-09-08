"""Every admission rejection was deleted from the request that needed it.

The gate answers a refused tool call with a tool-role row keyed to the call id
— "Tool 'x' does not exist", "missing required parameters: path", "(already
executed in round 2 …)". The assistant row persisted right after it listed only
the calls that survived admission. So the rejection had no parent, and
`exclude_orphans` (which every `compile_context` runs) dropped it on the way to
the next request.

The result, live since v2.1.0: the model was corrected in the database and in
the UI and never in the one place that could change its next move. It re-made
the same invalid call, was refused again, and burned the round budget being
told nothing. Dedup stubs vanished the same way — a suppressed call simply
disappeared, so "use the previous result" never arrived either.

The assistant row now carries every proposed call, refused ones included, with
their arguments normalised to a JSON object so each provider adapter accepts
them; the rejections are written after it, so the pair survives compilation.
`exclude_orphans` is untouched — it is the right filter, it was just being fed
a transcript that lied about what the model had said.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.agent import StuckDetector, _rejected_call_args, _ToolCallGate
from core.context.compiler import compile_context
from core.llm.types import is_rejected_call
from core.tools.registry import ToolRegistry
from db import models as db

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "text to echo"}},
    "required": ["text"],
}
CALL_MODEL_SCHEMA = {
    "type": "object",
    "properties": {"model": {"type": "string"}, "prompt": {"type": "string"}},
    "required": ["model", "prompt"],
}


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register("echo", lambda text: text, "echo text", ECHO_SCHEMA)
    reg.register("call_model", lambda model, prompt: prompt, "call a model", CALL_MODEL_SCHEMA)
    return reg


def _echo(call_id: str, text: str) -> dict:
    return {"id": call_id, "name": "echo", "arguments": json.dumps({"text": text})}


class _Round:
    """One turn's persistence, mirroring run_agent exactly.

    admit → assistant row with `gate.proposed_calls` → flush_rejections →
    one tool row per admitted call. Anything less faithful would test the
    test, not the loop.
    """

    def __init__(self, registry: ToolRegistry, title: str):
        self.registry = registry
        self.session_id = db.create_session(title=title)
        self.user_msg_id = db.add_message(self.session_id, "user", "do the thing")
        self._meta = json.dumps({"parent_user_msg_id": self.user_msg_id})
        self.events: list[dict] = []
        self.gate = _ToolCallGate(
            registry=registry,
            session=SimpleNamespace(emit_event=self.events.append),
            save_turn_msg=self._save,
            stuck=StuckDetector(),
            tool_failures={},
        )
        self.round_num = 0

    async def _save(self, role: str, content: str, **kwargs):
        kwargs.pop("latency_ms", None)
        kwargs.pop("metadata", None)
        return await __import__("asyncio").to_thread(
            db.add_message, self.session_id, role, content, metadata=self._meta, **kwargs
        )

    async def run(self, calls: list[dict], active: list[str] | None = None) -> list[dict]:
        self.round_num += 1
        parsed, _notes = await self.gate.admit(calls, active or [t.name for t in self.registry.all_tools()])
        proposed = self.gate.proposed_calls
        db.add_message(
            self.session_id,
            "assistant",
            "",
            tool_calls=json.dumps(proposed) if proposed else None,
            metadata=self._meta,
        )
        await self.gate.flush_rejections()
        for item in parsed:
            result = f"echoed {item['parsed_args'].get('text', '')}"
            db.add_message(self.session_id, "tool", result, tool_call_id=item["tc"]["id"], metadata=self._meta)
            self.gate.remember_success(item["tc"]["name"], item["tc"]["arguments"], self.round_num, result)
        return proposed

    def compiled(self) -> list[dict]:
        payload = compile_context(
            session_id=self.session_id,
            tool_schemas=self.registry.get_schemas([t.name for t in self.registry.all_tools()]),
            turn_user_msg_id=self.user_msg_id,
        )
        return payload.messages


def _history_text(messages: list[dict]) -> str:
    """The conversation the model is shown, system prompt excluded."""
    return "\n".join(str(m.get("content") or "") for m in messages if m.get("role") != "system")


def _assert_every_tool_result_is_paired(messages: list[dict]) -> None:
    """The invariant `exclude_orphans` exists to protect, checked on the output."""
    issued: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                issued.add(tc.get("id", ""))
        elif m.get("role") == "tool":
            assert m.get("tool_call_id") in issued, f"orphan tool result {m.get('tool_call_id')}"


# ---------------------------------------------------------------------------
# The corrective text reaches the next request
# ---------------------------------------------------------------------------


async def test_unknown_tool_rejection_reaches_the_model():
    r = _Round(_registry(), "unknown tool")
    await r.run([{"id": "c1", "name": "nonexistent_tool", "arguments": "{}"}])

    messages = r.compiled()
    assert "does not exist" in _history_text(messages)
    _assert_every_tool_result_is_paired(messages)


async def test_missing_required_parameter_rejection_reaches_the_model():
    r = _Round(_registry(), "missing param")
    await r.run([{"id": "c1", "name": "echo", "arguments": "{}"}])

    messages = r.compiled()
    assert "missing required parameters: text" in _history_text(messages)
    _assert_every_tool_result_is_paired(messages)


async def test_unparsable_arguments_rejection_reaches_the_model():
    """The refused call still has to be a legal assistant tool_call.

    Its arguments are not JSON at all, so they travel as an object carrying
    the raw text — Ollama json.loads() that field and OpenAI-format backends
    render it into the chat template.
    """
    r = _Round(_registry(), "bad json")
    proposed = await r.run([{"id": "c1", "name": "echo", "arguments": "{not json"}])

    assert json.loads(proposed[0]["arguments"]) == {"_raw_arguments": "{not json"}
    messages = r.compiled()
    assert "Could not parse arguments" in _history_text(messages)
    _assert_every_tool_result_is_paired(messages)


async def test_intra_round_duplicate_stub_reaches_the_model():
    r = _Round(_registry(), "intra-round dup")
    await r.run([_echo("c1", "same"), _echo("c2", "same")])

    messages = r.compiled()
    assert "(duplicate call — see previous result)" in _history_text(messages)
    _assert_every_tool_result_is_paired(messages)


async def test_cross_round_duplicate_stub_reaches_the_model():
    r = _Round(_registry(), "cross-round dup")
    await r.run([_echo("c1", "cached")])
    await r.run([_echo("c2", "cached")])

    messages = r.compiled()
    assert "already executed in round 1 with identical arguments" in _history_text(messages)
    _assert_every_tool_result_is_paired(messages)


async def test_semantic_duplicate_stub_reaches_the_model():
    """Same model, same question, different capitalisation and spacing."""
    r = _Round(_registry(), "semantic dup")
    await r.run(
        [
            {"id": "c1", "name": "call_model", "arguments": json.dumps({"model": "m", "prompt": "describe this"})},
            {"id": "c2", "name": "call_model", "arguments": json.dumps({"model": "m", "prompt": "Describe   THIS"})},
        ]
    )

    messages = r.compiled()
    assert "(near-duplicate call — see previous result)" in _history_text(messages)
    _assert_every_tool_result_is_paired(messages)


# ---------------------------------------------------------------------------
# Shape of the transcript the fix writes
# ---------------------------------------------------------------------------


async def test_a_round_where_everything_is_refused_still_speaks_for_itself():
    """No empty assistant turn, and both refusals arrive."""
    r = _Round(_registry(), "all refused")
    await r.run(
        [
            {"id": "c1", "name": "ghost_tool", "arguments": "{}"},
            {"id": "c2", "name": "echo", "arguments": "{}"},
        ]
    )

    messages = r.compiled()
    text = _history_text(messages)
    assert "does not exist" in text and "missing required parameters: text" in text
    empty = [
        m
        for m in messages
        if m.get("role") == "assistant" and not (m.get("content") or "").strip() and not m.get("tool_calls")
    ]
    assert empty == [], "an assistant turn with no content and no calls is not a valid message"
    _assert_every_tool_result_is_paired(messages)


async def test_refused_calls_are_marked_and_admitted_ones_are_not():
    r = _Round(_registry(), "markers")
    proposed = await r.run([_echo("ok", "hello"), {"id": "bad", "name": "echo", "arguments": "{}"}])

    by_id = {tc["id"]: tc for tc in proposed}
    assert is_rejected_call(by_id["bad"]) is True
    assert is_rejected_call(by_id["ok"]) is False


async def test_a_refused_call_is_never_counted_as_a_tool_the_turn_used():
    """The monotonic allowlist widens on tools that ran, not on tools that failed
    admission — otherwise one hallucinated argument keeps a tool in the schema."""
    from core.agent import _prior_turn_tool_names

    r = _Round(_registry(), "allowlist")
    await r.run([_echo("ok", "hello"), {"id": "bad", "name": "call_model", "arguments": "{}"}])

    assert _prior_turn_tool_names(r.session_id) == {"echo"}


async def test_the_rejection_row_is_written_after_its_assistant_row():
    """Order is load-bearing: normalize_for_openrouter drops a tool message it
    has not yet seen the matching assistant tool_call for."""
    r = _Round(_registry(), "ordering")
    await r.run([{"id": "c1", "name": "echo", "arguments": "{}"}])

    roles = [m["role"] for m in db.get_messages(r.session_id)]
    assert roles == ["user", "assistant", "tool"]


# ---------------------------------------------------------------------------
# Argument normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"path": "a.py"}', {"path": "a.py"}),
        ("", {}),
        ("   ", {}),
        ("null", {"_raw_arguments": "null"}),
        ("42", {"_raw_arguments": "42"}),
        ("[]", {"_raw_arguments": "[]"}),
        ('"path"', {"_raw_arguments": '"path"'}),
        ("{not json", {"_raw_arguments": "{not json"}),
        ({"path": "a.py"}, {"path": "a.py"}),
    ],
)
def test_rejected_arguments_are_always_a_json_object(raw, expected):
    assert json.loads(_rejected_call_args(raw)) == expected


def test_a_huge_malformed_argument_body_is_capped():
    assert len(json.loads(_rejected_call_args("x" * 50_000))["_raw_arguments"]) == 1000
