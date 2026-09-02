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
        resp = await client.put(
            "/workspace/big.txt", json={"content": "x" * (251 * 1024 * 1024)}
        )  # 251MB > 250MB limit
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


async def test_workspace_serve_file_is_sandboxed(tmp_path, monkeypatch):
    """Served workspace files must not run as the app origin.

    An agent-saved page (or an uploaded one) that renders inline on the app
    origin can read the auth token out of localStorage. `sandbox` gives it an
    opaque origin with scripts disabled; `nosniff` keeps a .txt a .txt.
    """
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    (tmp_path / "report.html").write_text("<script>localStorage.getItem('pernix_auth_token')</script>")
    (tmp_path / "notes.txt").write_text("plain")
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for name in ("report.html", "notes.txt"):
            resp = await client.get(f"/workspace/{name}")
            assert resp.status_code == 200
            assert resp.headers["content-security-policy"] == "sandbox"
            assert resp.headers["x-content-type-options"] == "nosniff"
            assert resp.headers["referrer-policy"] == "no-referrer"


# ---------------------------------------------------------------------------
# POST /api/upload — optional destination folder
# ---------------------------------------------------------------------------


async def test_upload_defaults_to_the_workspace_root(tmp_path, monkeypatch):
    """No `path` field = the old behaviour, for every caller that has none."""
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/upload", files={"file": ("note.txt", b"hi", "text/plain")})
    assert resp.status_code == 200
    assert resp.json()["path"] == "note.txt"
    assert (tmp_path / "note.txt").read_bytes() == b"hi"


async def test_upload_lands_in_the_requested_folder(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    (tmp_path / "reports" / "q3").mkdir(parents=True)
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/upload",
            files={"file": ("note.txt", b"hi", "text/plain")},
            data={"path": "reports/q3"},
        )
    assert resp.status_code == 200
    assert resp.json()["path"] == "reports/q3/note.txt"
    assert (tmp_path / "reports" / "q3" / "note.txt").read_bytes() == b"hi"
    assert not (tmp_path / "note.txt").exists()


async def test_upload_creates_a_missing_folder(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/upload",
            files={"file": ("note.txt", b"hi", "text/plain")},
            data={"path": "fresh/folder"},
        )
    assert resp.status_code == 200
    assert (tmp_path / "fresh" / "folder" / "note.txt").exists()


async def test_upload_path_traversal_blocked(tmp_path, monkeypatch):
    """The new field takes the same traversal check as every other route."""
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/upload",
            files={"file": ("evil.txt", b"hi", "text/plain")},
            data={"path": "../outside"},
        )
    assert resp.status_code == 403
    assert not (tmp_path / "outside").exists()


async def test_upload_collision_renames_inside_the_target_folder(tmp_path, monkeypatch):
    """The collision loop rebuilt its candidate from the ROOT, not the folder."""
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "note.txt").write_bytes(b"first")
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/upload",
            files={"file": ("note.txt", b"second", "text/plain")},
            data={"path": "sub"},
        )
    assert resp.status_code == 200
    assert resp.json()["path"] == "sub/note_1.txt"
    assert (tmp_path / "sub" / "note_1.txt").read_bytes() == b"second"
    assert (tmp_path / "sub" / "note.txt").read_bytes() == b"first"
    assert not (tmp_path / "note_1.txt").exists()
