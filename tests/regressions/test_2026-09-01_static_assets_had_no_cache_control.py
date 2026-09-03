"""Static assets shipped without Cache-Control, enabling deploy skew.

With no Cache-Control the browser picks a heuristic freshness lifetime from
Last-Modified. After a deploy, the new service worker's precache could then
be satisfied from the HTTP cache and stamp an OLD module beside a NEW one
into the same versioned cache — a half-updated app whose "tap to refresh"
reloads into the same broken state.

no-cache means "revalidate before reuse", which is one conditional request
answering 304, not "never cache".
"""

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def client_app():
    from api.app import app

    return app


async def test_static_assets_must_be_revalidated(client_app):
    async with AsyncClient(transport=ASGITransport(app=client_app), base_url="http://t") as c:
        resp = await c.get("/static/js/sse.js")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"


async def test_the_service_worker_itself_is_still_no_cache(client_app):
    async with AsyncClient(transport=ASGITransport(app=client_app), base_url="http://t") as c:
        resp = await c.get("/sw.js")
    assert resp.status_code == 200
    assert "no-cache" in resp.headers.get("cache-control", "")
