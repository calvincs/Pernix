"""L05 — the consolidation prompt overflowed the model context every cycle.

`build_llm_merge_prompt` concatenated 1 024 chars of EVERY entry of EVERY
file in the cluster with no total cap. The two largest clusters on the live
box (a twelve-plus research/macro family and the curiosity-drive family)
produced prompts vLLM refused with

    This model's maximum context length is 196608 tokens ... the messages
    resulted in at least 194609 input tokens

and because `consolidate_files` processed `clusters[0]` and nothing else,
the same cluster was re-sent and re-refused every cycle: the identical 400
appears on 09-11 and 09-12, and no cluster behind it ever consolidated.

The fix budgets the prompt against the real window, shrinks it in the order
that costs the least information, falls back to a PAIR merge when even the
floor does not fit, and quarantines a cluster the provider still refuses so
one bad cluster cannot block every other consolidation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.memory.consolidate import (
    _MIN_ENTRIES_PER_FILE,
    _estimate_tokens,
    build_budgeted_merge_prompt,
    build_signatures,
    largest_overlap_pair,
    merge_prompt_token_budget,
)
from core.memory.sweeps import (
    CONSOLIDATION_SKIP_DAYS,
    _cluster_key,
    _consolidation_skipped,
    _is_context_overflow,
    _mark_consolidation_skip,
    consolidate_files,
)

_CONTEXT_400 = (
    "Error code: 400 - {'object': 'error', 'message': \"This model's maximum context length is "
    "196608 tokens. However, you requested 196609 tokens (194609 in the messages, 2000 in the "
    "completion). Please reduce the length of the messages or completion.\", 'type': "
    "'BadRequestError'}"
)


def _seeded_store(tmp_path, names: list[str], entries: int = 8, chars: int = 1400):
    from core.memory.store import MemoryStore

    store = MemoryStore(str(tmp_path / "memories"))
    for name in names:
        for i in range(entries):
            body = f"{name} fact {i}: " + "consolidation payload " * (chars // 22)
            store.add_entry(body, file_name=name, skip_dedup=True)
    return store


_CLUSTER = [
    "research.macro.alpha",
    "research.macro.beta",
    "research.macro.gamma",
    "research.macro.delta",
]


# ---------------------------------------------------------------------------
# The prompt itself
# ---------------------------------------------------------------------------


def test_an_oversized_cluster_is_shrunk_under_budget_with_every_file_present(tmp_path):
    store = _seeded_store(tmp_path, _CLUSTER)

    unbudgeted, _ = build_budgeted_merge_prompt(_CLUSTER, store, budget_tokens=10_000_000)
    assert _estimate_tokens(unbudgeted) > 4_000, "test fixture is too small to exercise the budget"

    prompt, fits = build_budgeted_merge_prompt(_CLUSTER, store, budget_tokens=4_000)
    assert fits
    assert _estimate_tokens(prompt) <= 4_000
    # Every file in the cluster is still described — a merge decision that
    # never saw a file would rewrite it blind.
    for name in _CLUSTER:
        assert f'"{name}"' in prompt
    assert len(prompt) < len(unbudgeted)


def test_the_floor_keeps_the_newest_entries_of_every_file(tmp_path):
    store = _seeded_store(tmp_path, _CLUSTER)
    prompt, fits = build_budgeted_merge_prompt(_CLUSTER, store, budget_tokens=2_000)
    assert fits
    for name in _CLUSTER:
        assert prompt.count(f'"{name}"') >= _MIN_ENTRIES_PER_FILE
    # Oldest dropped first: entry 0 of at least one file is gone while the
    # newest entry of every file survives.
    for name in _CLUSTER:
        assert f"{name} fact 7:" in prompt


def test_a_budget_below_the_floor_reports_that_it_does_not_fit(tmp_path):
    store = _seeded_store(tmp_path, _CLUSTER, entries=4)
    prompt, fits = build_budgeted_merge_prompt(_CLUSTER, store, budget_tokens=400)
    assert fits is False
    assert prompt  # still returned, the caller decides what to do about it


def test_the_budget_derives_from_the_window_minus_the_output_reservation(monkeypatch):
    monkeypatch.setattr("config.settings.context_auto", False)
    monkeypatch.setattr("config.settings.context_budget", 196_608)
    # window*0.9 − the 2000 output tokens the merge call asks for.
    assert merge_prompt_token_budget("any-model") == int(196_608 * 0.9) - 2_000


def test_largest_overlap_pair_picks_the_two_most_similar_files(tmp_path):
    names = ["research.macro.alpha", "research.macro.beta", "kitchen.recipes.stew"]
    store = _seeded_store(tmp_path, names, entries=3)
    sig_map = {s.name: s for s in build_signatures(store)}
    pair = largest_overlap_pair(names, sig_map)
    assert sorted(pair) == ["research.macro.alpha", "research.macro.beta"]


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Captures merge prompts; optionally raises the provider's 400 first."""

    def __init__(self, raise_times: int = 0):
        self.prompts: list[str] = []
        self.raise_times = raise_times

    async def chat(self, messages, model="", max_tokens=None, **kw):
        self.prompts.append(messages[-1]["content"])
        if self.raise_times > 0:
            self.raise_times -= 1
            raise RuntimeError(_CONTEXT_400)

        from core.llm.types import ChatResponse, TokenUsage

        return ChatResponse(
            content="not json",
            tool_calls=None,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model=model or "test-model",
            provider="fake",
            finish_reason="stop",
        )


def _install(monkeypatch, client, clusters):
    monkeypatch.setattr("core.llm.client.get_llm_client", lambda: client)
    monkeypatch.setattr("core.memory.consolidate.find_clusters", lambda *a, **k: [list(c) for c in clusters])
    monkeypatch.setattr("core.memory.consolidate.plan_trivial_merge", lambda *a, **k: None)


async def _run(store, db):
    return await consolidate_files(
        store,
        db,
        lambda: False,
        did_llm_already=False,
        llm_ready=lambda: True,
        interval_hours=0,
    )


async def test_a_cluster_that_cannot_fit_is_merged_as_its_largest_overlap_pair(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.context_auto", False)
    monkeypatch.setattr("config.settings.context_budget", 3_000)  # → a 1 000-token prompt budget
    names = [f"research.macro.{s}" for s in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta")]
    store = _seeded_store(tmp_path, names)
    from db import models as db

    client = _RecordingClient()
    _install(monkeypatch, client, [names])
    await _run(store, db)

    assert len(client.prompts) == 1
    described = [n for n in names if f'"{n}"' in client.prompts[0]]
    assert len(described) == 2, f"expected a pair merge, got {described}"


async def test_a_provider_context_400_quarantines_the_cluster_and_moves_on(tmp_path, monkeypatch):
    first = ["research.macro.alpha", "research.macro.beta"]
    second = ["curiosity.drive.one", "curiosity.drive.two"]
    store = _seeded_store(tmp_path, first + second, entries=3, chars=200)
    from db import models as db

    client = _RecordingClient(raise_times=1)
    _install(monkeypatch, client, [first, second])
    await _run(store, db)

    # It did not stop at the refused cluster.
    assert len(client.prompts) == 2
    assert _consolidation_skipped(db, first) is True
    assert _consolidation_skipped(db, second) is False

    # Next cycle the quarantined cluster is not offered to the provider again.
    client2 = _RecordingClient()
    _install(monkeypatch, client2, [first, second])
    await _run(store, db)
    assert len(client2.prompts) == 1
    assert '"curiosity.drive.one"' in client2.prompts[0]

    # And the marker lapses: snooze_state has no TTL, so the value is the expiry.
    db.set_snooze_state(
        f"consolidation_skip:{_cluster_key(first)}",
        (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    assert _consolidation_skipped(db, first) is False
    client3 = _RecordingClient()
    _install(monkeypatch, client3, [first, second])
    await _run(store, db)
    assert '"research.macro.alpha"' in client3.prompts[0]


def test_the_skip_marker_expiry_is_seven_days(tmp_path, monkeypatch):
    from db import models as db

    cluster = ["a.one", "a.two"]
    _mark_consolidation_skip(db, cluster)
    raw = db.get_snooze_state(f"consolidation_skip:{_cluster_key(cluster)}")
    expires = datetime.fromisoformat(raw)
    assert timedelta(days=CONSOLIDATION_SKIP_DAYS) - (expires - datetime.now(timezone.utc)) < timedelta(minutes=1)
    # Order-independent key: the same cluster listed differently is one cluster.
    assert _consolidation_skipped(db, list(reversed(cluster))) is True


def test_only_a_context_length_refusal_triggers_the_quarantine():
    assert _is_context_overflow(RuntimeError(_CONTEXT_400)) is True
    assert _is_context_overflow(RuntimeError("Connection reset by peer")) is False
