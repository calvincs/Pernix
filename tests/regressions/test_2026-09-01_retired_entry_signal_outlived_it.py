"""A re-created adaptive entry inherited its predecessor's failure record.

The outcome signal is keyed by entry id and survived delete_entry's soft
delete. A producer that re-minted the same slug (same title -> same id)
therefore started life already failure-dominated, and the usage sweep's
failure branch has no age or epoch gate by design — so the fresh entry was
retired again on the very next cycle with zero new observations:
apply -> canary sweep -> retire -> re-mint, each turn spending a batch and
two notifications.
"""

from core.adaptive.engine import create_entry, delete_entry
from db import models as db


def _signal(entry_id):
    rows = db.get_signals_by_subjects([("adaptive_entry", entry_id)])
    return rows[0] if rows else None


def _seed_failures(entry_id, wins=1, losses=5):
    db.upsert_signal(
        "adaptive_entry",
        entry_id,
        delta_successes=wins,
        delta_failures=losses,
        delta_reinforcements=wins + losses,
    )


def test_deleting_an_entry_clears_its_outcome_signal():
    entry = create_entry(kind="prompt_note", title="Prefer ripgrep", content="use rg", actor="test")
    _seed_failures(entry["entry_id"])
    assert _signal(entry["entry_id"]) is not None

    delete_entry(entry["entry_id"], actor="usage_sweep", reason="failure-dominated")
    assert _signal(entry["entry_id"]) is None, "the record must not outlive the entry it judged"


def test_a_batch_re_mint_starts_clean():
    """create_entry refuses a used id outright, but the batch apply path
    only rejects an ACTIVE one — which is how a producer re-mints a slug
    the usage sweep just retired."""
    from core.adaptive.engine import _apply_one

    first = create_entry(kind="prompt_note", title="Prefer ripgrep", content="use rg", actor="test")
    _seed_failures(first["entry_id"])
    delete_entry(first["entry_id"], actor="usage_sweep", reason="failure-dominated")

    err = _apply_one(
        {"action": "create", "kind": "prompt_note", "title": "Prefer ripgrep", "content": "use rg"},
        producer="refine",
        actor="drain",
        batch_id="b1",
        proposal_id=None,
    )
    assert err is None, f"the batch path re-mints a retired slug: {err}"
    sig = _signal(first["entry_id"])
    assert sig is None or int(sig.get("failures") or 0) == 0, "it must be judged on its own outcomes"
