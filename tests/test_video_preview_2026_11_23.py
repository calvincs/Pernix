"""A browser-playable copy of a workspace video (2026-11-23).

A phone clip in HEVC played in the Explorer with sound and a black picture:
Chrome on Linux has no HEVC decoder. The player now asks the server for an
H.264 copy.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

URL = "/api/workspace/video-preview"


def _app(tmp_path, monkeypatch):
    from api.routers import workspace

    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    monkeypatch.setattr("config.settings.workspace_dir", str(ws))
    workspace._preview_jobs.clear()
    workspace._preview_failed.clear()
    monkeypatch.setattr(workspace, "_preview_slot", None)
    app = FastAPI()
    app.include_router(workspace.router)
    return app, ws


def _clip(path, size="640x360"):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=duration=1:size={size}:rate=10",
         "-c:v", "mpeg4", str(path)],
        check=True,
    )  # fmt: skip


async def _wait_ready(client, path):
    for _ in range(100):
        state = (await client.get(URL, params={"path": path, "probe": 1})).json()
        if state["status"] != "converting":
            return state
        await asyncio.sleep(0.1)
    raise AssertionError("conversion did not finish")


async def test_first_request_starts_a_conversion_and_the_copy_is_h264(tmp_path, monkeypatch):
    app, ws = _app(tmp_path, monkeypatch)
    _clip(ws / "clip.mov", size="2560x1440")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(URL, params={"path": "clip.mov", "probe": 1})
        assert first.json() == {"status": "converting"}
        assert (await _wait_ready(client, "clip.mov")) == {"status": "ready"}
        got = await client.get(URL, params={"path": "clip.mov"})
    assert got.status_code == 200 and got.headers["content-type"] == "video/mp4"
    out = tmp_path / "out.mp4"
    out.write_bytes(got.content)
    info = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
             "-of", "json", str(out)]
        )  # fmt: skip
    )["streams"][0]
    assert info["codec_name"] == "h264"
    assert (info["width"], info["height"]) == (1920, 1080)
    # The original is untouched and the copy is outside the workspace.
    assert sorted(p.name for p in ws.iterdir()) == ["clip.mov"]


async def test_a_file_that_is_not_a_video_reports_failed(tmp_path, monkeypatch):
    app, ws = _app(tmp_path, monkeypatch)
    (ws / "broken.mov").write_bytes(b"not a video")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.get(URL, params={"path": "broken.mov", "probe": 1})
        state = await _wait_ready(client, "broken.mov")
    assert state["status"] == "failed" and state["detail"]


async def test_other_files_and_paths_outside_the_workspace_are_refused(tmp_path, monkeypatch):
    app, ws = _app(tmp_path, monkeypatch)
    (ws / "notes.txt").write_text("x")
    (tmp_path / "outside.mov").write_bytes(b"x")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(URL, params={"path": "notes.txt"})).status_code == 400
        assert (await client.get(URL, params={"path": "../outside.mov"})).status_code == 403
        assert (await client.get(URL, params={"path": "missing.mov"})).status_code == 404
