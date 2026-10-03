"""Canary producers go through core/notices.py (2026-10).

Before the registry these sites wrote bell rows directly, each picking its own
urgency. Suite auto-maintenance (park, probe retirement, suite health,
maintenance summaries, the stale nudge) was retired in 3.2 with every notice
it raised; what is pinned here is the producers that remain and their tiers.
"""

from __future__ import annotations

import pytest

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
        "canary.contaminated",
        # skill self-healing became suggestions only (3.2): no verify-block
        # sync, no automatic rollback, no auto-apply.
        "skills.verify_unsafe",
        "skills.auto_rolled_back",
        "skills.proposals_auto_applied",
    ):
        assert gone not in CATEGORIES, gone
