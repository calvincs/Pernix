"""Tests for pre-execution argument type validation (spec Feature 3, stage 1).

_validate_arg_types runs inside the tool-call gate BEFORE dispatch: benign
mismatches (numeric strings, "true") are coerced with a note, unknown
parameters are dropped with a note, and uncoercible mismatches reject the
call with the expected type spelled out — all without burning a tool round.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.agent import _coerce_json_type, _validate_arg_types


def _tool(properties: dict, required: list | None = None, additional: bool | None = None):
    params: dict = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    if additional is not None:
        params["additionalProperties"] = additional
    return SimpleNamespace(parameters=params)


def _run(tool, args):
    notes: list[str] = []
    err = _validate_arg_types(tool, args, notes=notes.append)
    return err, notes, args


def test_correct_types_pass_untouched():
    tool = _tool({"path": {"type": "string"}, "limit": {"type": "integer"}, "deep": {"type": "boolean"}})
    err, notes, args = _run(tool, {"path": "a.py", "limit": 5, "deep": True})
    assert err is None
    assert not notes
    assert args == {"path": "a.py", "limit": 5, "deep": True}


def test_numeric_string_coerced_to_integer():
    tool = _tool({"limit": {"type": "integer"}})
    err, notes, args = _run(tool, {"limit": "25"})
    assert err is None
    assert args["limit"] == 25
    assert notes and "coerced" in notes[0]


def test_string_bool_coerced():
    tool = _tool({"deep": {"type": "boolean"}})
    err, _notes, args = _run(tool, {"deep": "true"})
    assert err is None
    assert args["deep"] is True


def test_number_accepts_int_and_coerces_string():
    tool = _tool({"score": {"type": "number"}})
    err, _n, args = _run(tool, {"score": 3})
    assert err is None and args["score"] == 3
    err, _n, args = _run(tool, {"score": "3.5"})
    assert err is None and args["score"] == 3.5


def test_scalar_coerced_to_string():
    tool = _tool({"query": {"type": "string"}})
    err, _n, args = _run(tool, {"query": 42})
    assert err is None
    assert args["query"] == "42"


def test_uncoercible_rejects_with_expected_type():
    tool = _tool({"limit": {"type": "integer"}})
    err, _n, _a = _run(tool, {"limit": "twenty"})
    assert err is not None
    assert "limit" in err and "integer" in err


def test_list_for_string_rejects():
    tool = _tool({"path": {"type": "string"}})
    err, _n, _a = _run(tool, {"path": ["a.py", "b.py"]})
    assert err is not None


def test_bool_not_accepted_as_integer():
    # bool subclasses int; True must not slip through an integer parameter.
    tool = _tool({"limit": {"type": "integer"}})
    err, _n, _a = _run(tool, {"limit": True})
    assert err is not None


def test_unknown_params_dropped_with_note():
    tool = _tool({"path": {"type": "string"}})
    err, notes, args = _run(tool, {"path": "a.py", "recursive": True})
    assert err is None
    assert "recursive" not in args
    assert any("unknown parameter" in n for n in notes)


def test_additional_properties_true_keeps_unknowns():
    tool = _tool({"path": {"type": "string"}}, additional=True)
    err, notes, args = _run(tool, {"path": "a.py", "extra": 1})
    assert err is None
    assert args["extra"] == 1
    assert not notes


def test_union_and_typeless_properties_skipped():
    tool = _tool({"maybe": {"type": ["string", "null"]}, "free": {}})
    err, notes, args = _run(tool, {"maybe": 5, "free": [1, 2]})
    assert err is None
    assert not notes
    assert args == {"maybe": 5, "free": [1, 2]}


def test_none_value_passes_through():
    tool = _tool({"limit": {"type": "integer"}})
    err, _n, args = _run(tool, {"limit": None})
    assert err is None
    assert args["limit"] is None


def test_enum_membership_enforced():
    tool = _tool({"mode": {"type": "string", "enum": ["fast", "slow"]}})
    err, _n, _a = _run(tool, {"mode": "fast"})
    assert err is None
    err, _n, _a = _run(tool, {"mode": "medium"})
    assert err is not None
    assert "fast" in err and "slow" in err


def test_enum_checked_after_coercion():
    # "5" coerces to 5, which IS an enum member — must pass.
    tool = _tool({"level": {"type": "integer", "enum": [1, 5, 9]}})
    err, _n, args = _run(tool, {"level": "5"})
    assert err is None
    assert args["level"] == 5


def test_array_item_types_validated():
    tool = _tool({"ids": {"type": "array", "items": {"type": "integer"}}})
    err, _n, args = _run(tool, {"ids": [1, 2, 3]})
    assert err is None
    err, notes, args = _run(tool, {"ids": [1, "2", 3]})
    assert err is None
    assert args["ids"] == [1, 2, 3]
    assert any("items" in n for n in notes)
    err, _n, _a = _run(tool, {"ids": [1, "two"]})
    assert err is not None
    assert "element 1" in err


def test_array_without_item_schema_passes():
    tool = _tool({"stuff": {"type": "array"}})
    err, notes, args = _run(tool, {"stuff": [1, "mixed", {"a": 1}]})
    assert err is None
    assert not notes


def test_coerce_helper_edges():
    assert _coerce_json_type("  7 ", "integer") == (True, 7)
    assert _coerce_json_type("-3", "integer") == (True, -3)
    assert _coerce_json_type(3.0, "integer") == (True, 3)
    assert _coerce_json_type("false", "boolean") == (True, False)
    assert _coerce_json_type(True, "string") == (True, "true")
    ok, _ = _coerce_json_type("3.5", "integer")
    assert not ok
    ok, _ = _coerce_json_type({"a": 1}, "string")
    assert not ok
