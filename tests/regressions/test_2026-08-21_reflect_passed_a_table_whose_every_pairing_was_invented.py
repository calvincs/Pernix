"""Regression — 2026-08-21, box session dce9a6de7f81.

Asked what proposals #189-#193 were, the agent built a 5-row table mapping
each id to a policy name. Every token in it was real (the ids came from the
user's notification, the names from ADAPTIVE.md) and every pairing was
invented — the ids were memory corrections the agent never fetched. Reflect
graded it pass at 0.95: it had the tool results and the answer side by side
and nothing asked it to check that any result showed a row's two halves
together.

Pinned here: the evidence blob ends with a mechanical GROUNDING CHECK that
lists identifiers no tool result contains and table rows no single tool
result supports; the flags ride on the ReflectResult, into the post-mortem,
and into the next attempt's retry context.
"""

from core.reflect import ReflectResult, _build_evidence, build_retry_context
from db import models as db

ADAPTIVE_MD = (
    "### hard-stop-after-write\n- id: `hard-stop-after-write` · v1 · risk=high · source=refine\n"
    "### produce-every-planned-deliverable\n- id: `produce-every-planned-deliverable` · v1\n"
)
NOTIFICATION = "5 proposal(s) past the 24h veto window applied at idle (#189, #190). What were these?"


def _session(final: str, extra_tool: str | None = None) -> str:
    sid = db.create_session(title="grounding")
    db.add_message(sid, "user", NOTIFICATION)
    db.add_message(sid, "scout", '{"type": "scout.done"}')
    db.add_message(sid, "assistant", "Reading the mirror.")
    db.add_message(sid, "tool", ADAPTIVE_MD)
    if extra_tool:
        db.add_message(sid, "tool", extra_tool)
    db.add_message(sid, "assistant", final)
    return sid


def test_reconstructed_id_to_name_table_is_flagged_row_by_row():
    final = (
        "Here is the mapping:\n\n"
        "| ID | Policy |\n|---|---|\n"
        "| #189 | `hard-stop-after-write` |\n"
        "| #190 | `produce-every-planned-deliverable` |\n\n"
        "Batch `ab-000000000000` applied them; see also `made-up-entry-name`."
    )
    out: dict = {}
    _, evidence = _build_evidence(_session(final), attempt=1, grounding_out=out)

    assert "GROUNDING CHECK (mechanical, advisory — a flag, not a verdict):" in evidence
    # Every id and both policy names are real tokens — only the pairings are not.
    assert out["unverified_rows"] == ["#189 ↔ hard-stop-after-write", "#190 ↔ produce-every-planned-deliverable"]
    # Tokens that exist nowhere at all are listed separately.
    assert out["ungrounded"] == ["ab-000000000000", "made-up-entry-name"]
    assert "2 table row(s) pair identifiers that no single tool result shows together" in evidence
    assert "#189 ↔ hard-stop-after-write" in evidence
    assert "ab-000000000000, made-up-entry-name" in evidence
    # The section comes after the final response, as a reviewer would read it.
    assert evidence.index("AGENT FINAL RESPONSE:") < evidence.index("GROUNDING CHECK")


def test_a_row_supported_by_one_tool_result_is_not_flagged():
    row_source = "proposal 189 auto_approved — payload: create policy hard-stop-after-write"
    final = "| ID | Policy |\n|---|---|\n| #189 | `hard-stop-after-write` |\n"
    out: dict = {}
    _, evidence = _build_evidence(_session(final, extra_tool=row_source), attempt=1, grounding_out=out)
    assert out == {"checked": 2, "ungrounded": [], "unverified_rows": []}
    assert "all 2 cited identifiers appear in a tool result or the user's message" in evidence


def test_user_supplied_ids_count_as_grounded_but_numbers_need_word_boundaries():
    # #190 is in the user's message; #1900 is not, and "190" inside "1900"
    # must not ground it.
    final = "You asked about #190; I also looked at #1900."
    out: dict = {}
    _build_evidence(_session(final), attempt=1, grounding_out=out)
    assert out["ungrounded"] == ["#1900"] and out["unverified_rows"] == []


def test_flags_ride_into_the_retry_context_and_the_post_mortem():
    from core.reflect import _write_post_mortem

    result = ReflectResult(
        verdict="retry",
        reasoning="table reconstructed",
        grounding={
            "checked": 4,
            "ungrounded": ["ab-000000000000"],
            "unverified_rows": ["#189 ↔ hard-stop-after-write"],
        },
    )
    ctx = build_retry_context(result, attempt=2, max_attempts=3)
    assert "GROUNDING FLAGS FROM PRIOR ATTEMPT" in ctx
    assert "ab-000000000000" in ctx and "#189 ↔ hard-stop-after-write" in ctx
    assert "'not retrieved — would need <call>'" in ctx

    sid = db.create_session(title="pm")
    _write_post_mortem(sid, 1, result, None, {})
    pm = db.list_post_mortems(session_id=sid)[0]
    import json

    payload = json.loads(pm["payload_json"])
    assert payload["grounding"]["unverified_rows"] == ["#189 ↔ hard-stop-after-write"]


def test_clean_answers_carry_no_flags_into_the_post_mortem():
    result = ReflectResult(verdict="pass", grounding={"checked": 3, "ungrounded": [], "unverified_rows": []})
    assert "GROUNDING FLAGS" not in build_retry_context(result, attempt=2, max_attempts=3)
