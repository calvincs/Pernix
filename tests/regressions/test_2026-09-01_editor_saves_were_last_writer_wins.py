"""Regression: the Explorer editor's saves were last-writer-wins.

`PUT /workspace/{path}` and `PUT /api/skills/{name}` wrote whatever the browser
sent, unconditionally. Open a file in the editor, let the agent (or a shell, or
a second tab) rewrite it while you type, press save — and the other writer's
work was gone with no error, no warning, and nothing in the UI to suggest
anything had happened. The two writers in this app are the *user* and the
*agent*, and they routinely touch the same workspace file in the same minute,
so this was not a theoretical race.

The fix is opt-in optimistic concurrency:

* the file read stamps `X-File-Mtime` on the response (and `GET
  /api/skills/{name}` returns `mtime`),
* the editor hands that value back as `base_mtime` on save,
* a PUT whose `base_mtime` no longer matches the file on disk returns
  **409** with `{"detail": "changed_on_disk", "mtime": <current>}` and writes
  nothing.

The opt-in half is load-bearing: every other caller — agent file tools, curl,
an older cached client — omits `base_mtime` and keeps the old behaviour
exactly. Comparison carries a 0.5s tolerance so coarse filesystem timestamps
and the float round-trip through JSON cannot manufacture a phantom conflict.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.anyio


def _workspace_app() -> FastAPI:
    from api.routers import workspace

    app = FastAPI()
    app.include_router(workspace.router)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# The read side hands out the mtime the write side will check
# ---------------------------------------------------------------------------


async def test_workspace_read_exposes_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "notes.txt"
    f.write_text("one")

    async with _client(_workspace_app()) as client:
        resp = await client.get("/workspace/notes.txt")

    assert resp.status_code == 200
    assert float(resp.headers["x-file-mtime"]) == pytest.approx(f.stat().st_mtime, abs=0.001)


# ---------------------------------------------------------------------------
# Absent base_mtime = the old behaviour, byte for byte
# ---------------------------------------------------------------------------


async def test_put_without_base_mtime_still_overwrites(tmp_path, monkeypatch):
    """The agent's own file tools never send one — they must not start failing."""
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "notes.txt"
    f.write_text("one")
    os.utime(f, (1_000_000, 1_000_000))  # obviously stale mtime

    async with _client(_workspace_app()) as client:
        resp = await client.put("/workspace/notes.txt", json={"content": "two"})

    assert resp.status_code == 200
    assert f.read_text() == "two"


async def test_put_creates_new_file_with_base_mtime(tmp_path, monkeypatch):
    """A base_mtime for a file that does not exist yet is not a conflict."""
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))

    async with _client(_workspace_app()) as client:
        resp = await client.put("/workspace/fresh.txt", json={"content": "hello", "base_mtime": 12345.0})

    assert resp.status_code == 200
    assert (tmp_path / "fresh.txt").read_text() == "hello"


# ---------------------------------------------------------------------------
# Matching base_mtime = 200 plus the new mtime to keep editing against
# ---------------------------------------------------------------------------


async def test_put_with_matching_base_mtime_saves_and_returns_new_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "notes.txt"
    f.write_text("one")
    base = f.stat().st_mtime

    async with _client(_workspace_app()) as client:
        resp = await client.put("/workspace/notes.txt", json={"content": "two", "base_mtime": base})

    assert resp.status_code == 200
    body = resp.json()
    assert f.read_text() == "two"
    assert body["mtime"] == pytest.approx(f.stat().st_mtime, abs=0.001)
    # The returned mtime is usable as the next base_mtime.
    async with _client(_workspace_app()) as client:
        again = await client.put("/workspace/notes.txt", json={"content": "three", "base_mtime": body["mtime"]})
    assert again.status_code == 200
    assert f.read_text() == "three"


async def test_sub_second_drift_is_not_a_conflict(tmp_path, monkeypatch):
    """Coarse filesystem timestamps must not read as a second writer."""
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "notes.txt"
    f.write_text("one")
    base = f.stat().st_mtime - 0.4

    async with _client(_workspace_app()) as client:
        resp = await client.put("/workspace/notes.txt", json={"content": "two", "base_mtime": base})

    assert resp.status_code == 200
    assert f.read_text() == "two"


# ---------------------------------------------------------------------------
# Stale base_mtime = 409, and the file on disk is untouched
# ---------------------------------------------------------------------------


async def test_stale_base_mtime_is_409_and_leaves_the_file_alone(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "notes.txt"
    f.write_text("written by the agent")
    stale = f.stat().st_mtime - 60  # what the editor read a minute ago

    async with _client(_workspace_app()) as client:
        resp = await client.put(
            "/workspace/notes.txt",
            json={"content": "typed by the user", "base_mtime": stale},
        )

    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == "changed_on_disk"
    assert body["mtime"] == pytest.approx(f.stat().st_mtime, abs=0.001)
    # The whole point: the other writer's content survives.
    assert f.read_text() == "written by the agent"


async def test_overwrite_after_conflict_drops_base_mtime(tmp_path, monkeypatch):
    """The UI's Overwrite button resends without base_mtime — that must win."""
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "notes.txt"
    f.write_text("written by the agent")
    stale = f.stat().st_mtime - 60

    async with _client(_workspace_app()) as client:
        conflict = await client.put("/workspace/notes.txt", json={"content": "mine", "base_mtime": stale})
        forced = await client.put("/workspace/notes.txt", json={"content": "mine"})

    assert conflict.status_code == 409
    assert forced.status_code == 200
    assert f.read_text() == "mine"


async def test_garbage_base_mtime_is_ignored_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.workspace_dir", str(tmp_path))
    f = tmp_path / "notes.txt"
    f.write_text("one")

    async with _client(_workspace_app()) as client:
        resp = await client.put("/workspace/notes.txt", json={"content": "two", "base_mtime": "not-a-number"})

    assert resp.status_code == 200
    assert f.read_text() == "two"


# ---------------------------------------------------------------------------
# The SKILL.md editor is the same editor, so it fails the same way
# ---------------------------------------------------------------------------


def _skills_app() -> FastAPI:
    from api.routers import skills

    app = FastAPI()
    app.include_router(skills.router)
    return app


@pytest.fixture
def skill_dir(tmp_path, monkeypatch):
    """One installed skill the registry can actually see."""
    skills_root = tmp_path / "skills"
    (skills_root / "demo").mkdir(parents=True)
    (skills_root / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: a demo skill\nversion: 1.0.0\n---\nBody.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("config.settings.skills_dir", str(skills_root))

    from core.skills import registry as registry_mod

    monkeypatch.setattr(registry_mod, "_registry", None, raising=False)
    reg = registry_mod.get_skill_registry()
    reg.rescan(skills_root)
    if not reg.get("demo"):
        pytest.skip("skill registry did not pick up the fixture skill")
    yield skills_root / "demo" / "SKILL.md"
    monkeypatch.setattr(registry_mod, "_registry", None, raising=False)


def _skill_md(body: str) -> str:
    """A SKILL.md the registry will still recognise after the write.

    update_skill rescans; a body without frontmatter would drop the skill and
    turn the next request into a 404, which is a different bug from the one
    under test.
    """
    return f"---\nname: demo\ndescription: a demo skill\nversion: 1.0.0\n---\n{body}\n"


async def test_skill_get_exposes_mtime_and_put_honours_it(skill_dir):
    md = skill_dir
    async with _client(_skills_app()) as client:
        got = await client.get("/api/skills/demo")
        assert got.status_code == 200
        base = got.json()["mtime"]
        assert base == pytest.approx(md.stat().st_mtime, abs=0.001)

        ok = await client.put("/api/skills/demo", json={"content": _skill_md("new body"), "base_mtime": base})
        assert ok.status_code == 200
        assert md.read_text(encoding="utf-8") == _skill_md("new body")

        stale = await client.put("/api/skills/demo", json={"content": _skill_md("clobber"), "base_mtime": base - 60})
        assert stale.status_code == 409
        assert stale.json()["detail"] == "changed_on_disk"
        assert md.read_text(encoding="utf-8") == _skill_md("new body")

        # No base_mtime = old behaviour.
        forced = await client.put("/api/skills/demo", json={"content": _skill_md("clobber")})
        assert forced.status_code == 200
        assert md.read_text(encoding="utf-8") == _skill_md("clobber")
