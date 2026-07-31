"""Full-application lifespan smoke test.

The real startup path had zero coverage until a function-local import
shadowing bug (asyncio in api.app lifespan) crashed only on a real boot.
This boots the ACTUAL app — extensions, registries, maintenance, journal
listener — serves one request, and shuts down cleanly. Slow-ish by unit
standards but cheap insurance for every future lifespan edit.
"""

from starlette.testclient import TestClient


def test_full_lifespan_boots_serves_and_shuts_down():
    from api.app import app

    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"
    # Reaching here means shutdown completed without hanging.
