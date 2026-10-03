"""Canary and skill-verify producers go through core/notices.py (2026-10).

Before the registry these sites wrote bell rows directly, each picking its own
urgency. Suite auto-maintenance (park, probe retirement, suite health,
maintenance summaries, the stale nudge) was retired in 3.2 with every notice
it raised; what is pinned here is the producers that remain and their tiers.
"""

from __future__ import annotations

import pytest

from core.canary import contamination
from db import models as db


@pytest.fixture(autouse=True)
def _canaries_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.canaries_dir", str(tmp_path / "canaries"))
    monkeypatch.setattr("config.settings.skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr("config.settings.canary_enabled", True)


def _rows(category: str) -> list[dict]:
    return [n for n in db.list_notifications("log", limit=500) if n["category"] == category]


def _bell(category: str) -> list[dict]:
    return [n for n in db.get_notifications() if n["category"] == category]


# ---------------------------------------------------------------------------
# category + tier per producer
# ---------------------------------------------------------------------------


def test_a_contaminated_run_is_a_log_line_with_its_session():
    contamination.notify("leaky", "sess-abcdef123456", ["memory tool called: recall"])
    rows = _rows("canary.contaminated")
    assert len(rows) == 1 and rows[0]["tier"] == "log"
    assert rows[0]["subject"] == "leaky" and rows[0]["session_id"] == "sess-abcdef123456"
    assert _bell("canary.contaminated") == []


def test_an_unsafe_verify_block_is_a_bell_item_once_per_content():
    from core.canary.skill_verify import _notify_unsafe_once

    _notify_unsafe_once("my-skill", "d1", "gate command uses a pipe")
    _notify_unsafe_once("my-skill", "d1", "gate command uses a pipe")
    rows = _bell("skills.verify_unsafe")
    assert len(rows) == 1 and rows[0]["subject"] == "my-skill"
    _notify_unsafe_once("my-skill", "d2", "gate command uses a pipe")
    assert len(_rows("skills.verify_unsafe")) == 2


def test_retired_canary_categories_are_gone_from_the_registry():
    from core.notices import CATEGORIES

    for gone in (
        "canary.parked",
        "canary.probe_retired",
        "canary.suite_chronic",
        "canary.suite_unhealthy",
        "canary.maintenance",
        "canary.stale",
        "canary.auto_admitted",
    ):
        assert gone not in CATEGORIES, gone
