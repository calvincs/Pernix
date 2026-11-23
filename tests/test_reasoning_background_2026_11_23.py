"""Background work takes the background reasoning effort (2026-11-23).

With one model for agent turns and background work, reasoning_effort_background
had no effect: roles were told apart by model name only. A memory merge ran
at the model's default effort, spent all 24,384 output tokens on reasoning and
returned an empty reply, 12 times in one day.
"""

from __future__ import annotations

import asyncio

from config import settings
from core.llm import reasoning


def _one_model(monkeypatch, primary="high", background="low"):
    monkeypatch.setattr(settings, "llm_model", "served-model")
    monkeypatch.setattr(settings, "background_model", "")
    monkeypatch.setattr(settings, "reasoning_effort", primary)
    monkeypatch.setattr(settings, "reasoning_effort_background", background)


def test_agent_turn_keeps_the_primary_effort(monkeypatch):
    _one_model(monkeypatch)
    assert reasoning.effort_for("served-model") == "high"


def test_background_work_takes_the_background_effort(monkeypatch):
    _one_model(monkeypatch)
    with reasoning.background_work():
        assert reasoning.effort_for("served-model") == "low"
    assert reasoning.effort_for("served-model") == "high"


async def test_a_task_started_inside_inherits_the_mark(monkeypatch):
    _one_model(monkeypatch)

    async def call():
        return reasoning.effort_for("served-model")

    with reasoning.background_work():
        inside = await asyncio.wait_for(call(), timeout=5)
        in_thread = await asyncio.to_thread(reasoning.effort_for, "served-model")
    assert (inside, in_thread) == ("low", "low")
    assert await call() == "high"


async def test_snooze_activity_runs_as_background_work(monkeypatch):
    from core.snooze import SnoozeRunner

    _one_model(monkeypatch)
    seen = []

    async def activity():
        seen.append(reasoning.effort_for("served-model"))

    loop = SnoozeRunner.__new__(SnoozeRunner)
    loop._stats = {}
    await loop._rung("consolidate_files", activity())
    assert seen == ["low"]
    assert reasoning.effort_for("served-model") == "high"
