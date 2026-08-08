"""Regression: the history-budget floor handed out headroom that didn't exist.

Shipped defect (architecture review 2026-08-07, Appendix C §3):

    history_budget = max(budget - max_output - system - tools - margin, 4000)

On any model where the real remainder is negative — entirely reachable, e.g.
ollama_num_ctx_cap 65,536 -> budget 58,982, minus max_tokens 32,000, minus a
~6k system prompt, minus tool schemas — the compiler handed back a 4,000-token
history budget for a context with no room at all, trimmed to it, and
dispatched a request that overflowed anyway.

Fix: the floor is bought out of the output reservation first (real headroom,
reported back as ContextPayload.effective_max_output) and, when even a
minimal completion no longer fits, compile_context raises ContextBudgetError
rather than assembling a request guaranteed to fail.

Also pinned here: the view-pruning pressure gate resolved its budget with
`context_budget or settings.context_budget` while the history budget used
`is not None`, so the two disagreed for an explicit context_budget=0.
"""

import pytest

from core.context.compiler import ContextBudgetError, compile_context


def _session_with_history(title: str) -> str:
    from db import models as db

    sid = db.create_session(title=title)
    big = "alpha beta gamma delta " * 400
    db.add_message(sid, "user", "first turn " + big)
    db.add_message(sid, "assistant", "first answer " + big)
    return sid


def test_genuine_headroom_is_untouched():
    """The common case must be bit-identical: budget minus the reservations."""
    sid = _session_with_history("Headroom")

    payload = compile_context(sid, context_budget=200_000, max_output_tokens=8_000)

    expected = 200_000 - 8_000 - payload.metadata.system_tokens - payload.metadata.tool_schema_tokens - 2_000
    assert payload.history_budget == expected
    assert payload.effective_max_output == 8_000


def test_tight_budget_shrinks_output_instead_of_inventing_history():
    """A budget that cannot hold the requested output still runs — but the
    history floor comes out of the output reservation, not out of thin air."""
    sid = _session_with_history("Tight")

    payload = compile_context(sid, context_budget=20_000, max_output_tokens=32_000)

    assert payload.effective_max_output < 32_000
    assert payload.effective_max_output >= 1_024
    # Everything the request will actually send must fit inside the budget.
    accounted = (
        payload.history_budget
        + payload.effective_max_output
        + payload.metadata.system_tokens
        + payload.metadata.tool_schema_tokens
        + 2_000
    )
    assert accounted <= 20_000


def test_impossible_budget_raises_instead_of_dispatching():
    sid = _session_with_history("Impossible")

    with pytest.raises(ContextBudgetError):
        compile_context(sid, context_budget=4_500, max_output_tokens=32_000)


def test_zero_budget_is_zero_for_both_guards():
    """`or` treated an explicit context_budget=0 as "use the global default",
    so the view-pruning pressure gate silently ran at 192k while the history
    budget ran at 0."""
    sid = _session_with_history("ZeroBudget")

    with pytest.raises(ContextBudgetError):
        compile_context(sid, context_budget=0)
