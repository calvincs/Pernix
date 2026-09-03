"""Regression: one circular symlink made a workspace folder undeletable.

`DELETE /workspace/{path}` walks a directory before removing it and refuses
any symlink whose target sits outside the workspace. The check was
`entry.resolve()`, which raises rather than returns for a link that cannot be
resolved. A self-referential link — `foo -> /abs/path/to/foo`, which archive
extractors and disk-image unpackers create routinely — raises
`OSError: [Errno 40] Too many levels of symbolic links` straight out of the
handler, so the request came back 500 and the folder could never be removed
from the Explorer. Retrying did the same thing every time.

An unresolvable link has no real target to escape to, and `rmtree` unlinks the
link itself without ever following it, so it is safe to pass over. The guard
against links that resolve to a genuine path outside the workspace is
unchanged — that is the case it was written for.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.anyio


def _client() -> AsyncClient:
    from api.routers import workspace

    app = FastAPI()
    app.include_router(workspace.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_self_referential_symlink_does_not_block_delete(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr("config.settings.workspace_dir", str(ws))

    victim = ws / "bundle"
    victim.mkdir()
    (victim / "readme.txt").write_text("keep me until the delete")
    loop = victim / "Applications"
    loop.symlink_to(loop)  # points at itself — resolve() raises ELOOP

    async with _client() as client:
        resp = await client.delete("/workspace/bundle")

    assert resp.status_code == 200, resp.text
    assert not victim.exists()


async def test_dangling_symlink_does_not_block_delete(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr("config.settings.workspace_dir", str(ws))

    victim = ws / "bundle"
    victim.mkdir()
    (victim / "broken").symlink_to(ws / "never_existed")

    async with _client() as client:
        resp = await client.delete("/workspace/bundle")

    assert resp.status_code == 200, resp.text
    assert not victim.exists()


async def test_symlink_escaping_the_workspace_is_still_refused(tmp_path, monkeypatch):
    """The guard must keep doing its job — this is why the walk exists."""
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secrets.txt").write_text("do not follow me")
    monkeypatch.setattr("config.settings.workspace_dir", str(ws))

    victim = ws / "bundle"
    victim.mkdir()
    (victim / "escape").symlink_to(outside)

    async with _client() as client:
        resp = await client.delete("/workspace/bundle")

    assert resp.status_code == 400
    assert "external symlink" in resp.json()["detail"]
    assert victim.exists()
    assert (outside / "secrets.txt").exists()
