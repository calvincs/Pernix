"""Notification settings (2026-10): validation of the tier overrides and kill
switch on POST /api/settings, the registry data GET /api/settings publishes for
the Settings rows, and a saved override reaching resolve_tier()."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config import settings


def _client():
    from api.routers import health

    app = FastAPI()
    app.include_router(health.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _clean_notify(monkeypatch):
    monkeypatch.setattr(settings, "notify_tier_overrides", {})
    monkeypatch.setattr(settings, "notify_tiers_enabled", True)


async def test_valid_overrides_saved_and_default_entries_dropped():
    async with _client() as client:
        resp = await client.post(
            "/api/settings",
            json={
                "notify_tier_overrides": {
                    "canary": "drop",
                    "jobs.test_failed": "interrupt",
                    "dream": "default",
                    "spaces": "",
                    "system": None,
                }
            },
        )
    assert resp.status_code == 200
    assert "notify_tier_overrides" in resp.json()["updated"]
    assert settings.notify_tier_overrides == {"canary": "drop", "jobs.test_failed": "interrupt"}


async def test_unknown_key_rejected_and_nothing_applied():
    async with _client() as client:
        resp = await client.post(
            "/api/settings",
            json={"notify_tier_overrides": {"canary": "drop", "nosuch": "bell"}, "notify_tiers_enabled": False},
        )
    assert resp.status_code == 400
    assert "nosuch" in resp.json()["detail"]
    assert settings.notify_tier_overrides == {}
    assert settings.notify_tiers_enabled is True


async def test_overrides_for_a_retired_area_are_dropped_not_rejected():
    """The adaptive layer (and its notice area) went away in 3.2. A cached
    client may still post its old override; that must not 400 the save."""
    async with _client() as client:
        resp = await client.post(
            "/api/settings",
            json={
                "notify_tier_overrides": {
                    "canary": "drop",
                    "adaptive": "log",
                    "adaptive.tripwire_suspect": "interrupt",
                }
            },
        )
    assert resp.status_code == 200
    assert settings.notify_tier_overrides == {"canary": "drop"}


def test_the_adaptive_area_is_retired_and_has_no_categories():
    from core.notices import CATEGORIES, RETIRED_AREAS, area_of

    assert "adaptive" in RETIRED_AREAS
    assert not [c for c in CATEGORIES if area_of(c) in RETIRED_AREAS]


@pytest.mark.parametrize("bad", ["loud", "INTERRUPT", 3, ["bell"]])
async def test_bad_tier_rejected(bad):
    async with _client() as client:
        resp = await client.post("/api/settings", json={"notify_tier_overrides": {"canary": bad}})
    assert resp.status_code == 400
    assert "tier must be one of" in resp.json()["detail"]
    assert settings.notify_tier_overrides == {}


async def test_non_dict_overrides_rejected():
    async with _client() as client:
        resp = await client.post("/api/settings", json={"notify_tier_overrides": ["canary"]})
    assert resp.status_code == 400


@pytest.mark.parametrize("bad", ["false", 0, 1, None])
async def test_non_bool_kill_switch_rejected(bad):
    async with _client() as client:
        resp = await client.post("/api/settings", json={"notify_tiers_enabled": bad})
    assert resp.status_code == 400
    assert settings.notify_tiers_enabled is True


async def test_bool_kill_switch_saved():
    async with _client() as client:
        resp = await client.post("/api/settings", json={"notify_tiers_enabled": False})
    assert resp.status_code == 200
    assert "notify_tiers_enabled" in resp.json()["updated"]
    assert settings.notify_tiers_enabled is False


async def test_settings_get_exposes_areas_from_registry():
    from core.notices import CATEGORIES, TIERS, area_of

    async with _client() as client:
        data = (await client.get("/api/settings")).json()
    for key in ("notify_tiers_enabled", "notify_tier_overrides", "push_urgency_floor", "notification_retention_days"):
        assert key in data
    areas = data["notify_areas"]
    assert set(areas) == {area_of(c) for c in CATEGORIES}
    assert len(areas) == 10  # adaptive retired in 3.2
    for area, info in areas.items():
        cats = [c for c in CATEGORIES if area_of(c) == area]
        assert info["categories"] == cats
        assert info["default_tiers"] == [t for t in TIERS if any(CATEGORIES[c].tier == t for c in cats)]
    assert areas["canary"]["default_tiers"] == ["bell", "log"]


async def test_saved_override_reaches_resolve_tier():
    from core.notices import resolve_tier

    assert resolve_tier("canary.parked") == "bell"
    assert resolve_tier("jobs.test_failed") == "bell"
    async with _client() as client:
        resp = await client.post(
            "/api/settings",
            json={"notify_tier_overrides": {"canary": "drop", "jobs": "log", "jobs.test_failed": "interrupt"}},
        )
    assert resp.status_code == 200
    assert resolve_tier("canary.parked") == "drop"
    assert resolve_tier("jobs.failed") == "log"
    # A category override beats its area's.
    assert resolve_tier("jobs.test_failed") == "interrupt"

    # Removing an entry with "default" restores the registry tier.
    async with _client() as client:
        await client.post(
            "/api/settings", json={"notify_tier_overrides": {"canary": "default", "jobs.test_failed": "interrupt"}}
        )
    assert resolve_tier("canary.parked") == "bell"
    assert resolve_tier("jobs.failed") == "interrupt"
