"""Tests for api/routers/workspace.py."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _make_app():
    from api.routers import workspace

    app = FastAPI()
    app.include_router(workspace.router)
    return app


# ---------------------------------------------------------------------------
# GET /api/workspace (listing)
# ---------------------------------------------------------------------------


async def test_workspace_listing_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/workspace")
    assert resp.status_code == 200
    data = resp.json()
    assert "entries" in data


async def test_workspace_listing_with_files(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    (tmp_path / "readme.txt").write_text("hello")
    (tmp_path / "subdir").mkdir()
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/workspace")
    assert resp.status_code == 200
    data = resp.json()
    names = [e["name"] for e in data["entries"]]
    assert "readme.txt" in names


async def test_workspace_listing_subdir(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "file.py").write_text("code")
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/workspace?path=subdir")
    assert resp.status_code == 200
    data = resp.json()
    names = [e["name"] for e in data["entries"]]
    assert "file.py" in names


async def test_workspace_listing_traversal_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/workspace?path=../../etc")
    assert resp.status_code == 403


async def test_workspace_search(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    (tmp_path / "myproject.py").write_text("content")
    (tmp_path / "other.txt").write_text("content")
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/workspace?q=myproject")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /workspace/{path} (serve workspace file)
# ---------------------------------------------------------------------------


async def test_workspace_serve_file(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    (tmp_path / "test.txt").write_text("file content")
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/workspace/test.txt")
    assert resp.status_code == 200


async def test_workspace_serve_file_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/workspace/nonexistent.txt")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /workspace/{path} (save workspace file)
# ---------------------------------------------------------------------------


async def test_workspace_write_file(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.put("/workspace/new_file.txt", json={"content": "hello world"})
    assert resp.status_code in (200, 201)
    assert (tmp_path / "new_file.txt").exists()


async def test_workspace_write_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.put("/workspace/big.txt", json={"content": "x" * (11 * 1024 * 1024)})  # 11MB > 10MB limit
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# DELETE /workspace/{path}
# ---------------------------------------------------------------------------


async def test_workspace_delete_file(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "to_delete.txt"
    f.write_text("bye")
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/workspace/to_delete.txt")
    assert resp.status_code == 200
    assert not f.exists()


async def test_workspace_delete_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/workspace/nonexistent.txt")
    assert resp.status_code == 404
