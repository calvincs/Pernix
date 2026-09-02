"""The storage ledger: /api/storage, backup rotation, and the compact button."""

import os
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config import settings
from scripts import backup


def _app():
    from api.routers import storage

    app = FastAPI()
    app.include_router(storage.router)
    return app


async def _get(path="/api/storage"):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        return await client.get(path)


async def _post(path, body=None):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        return await client.post(path, json=body if body is not None else {})


@pytest.fixture
def seeded():
    """A handful of sessions across types, one pinned, one in a space."""
    from db import models as db

    space = db.create_space(label="Ledger", color="#c9a227", slug="ledger")["id"]
    normal = db.create_session(title="A plain chat")
    db.create_session(title="Another plain chat", space_id=space)
    db.create_session(title="A worker", session_type="worker")
    db.create_session(title="A cron", session_type="cron")
    db.set_session_meta(normal, pinned=True)
    return {"normal": normal, "space": space}


# ---------------------------------------------------------------------------
# GET /api/storage
# ---------------------------------------------------------------------------


async def test_ledger_counts_sessions_by_type(seeded):
    resp = await _get()
    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert sessions["total"] == 4
    assert sessions["by_type"]["normal"] == 2
    assert sessions["by_type"]["worker"] == 1
    assert sessions["by_type"]["cron"] == 1
    assert sessions["pinned"] == 1
    assert sessions["in_spaces"] == 1


async def test_archived_is_null_until_the_column_exists(seeded):
    """A schema that cannot archive reports null, not a misleading zero."""
    from db import models as db

    with db.connect_sessions() as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    archived = (await _get()).json()["sessions"]["archived"]
    if "archived_at" in columns:
        assert isinstance(archived, int)
    else:
        assert archived is None


async def test_ledger_reports_the_database_file(seeded):
    database = (await _get()).json()["database"]
    assert database["path"] == str(settings.db_path)
    assert database["bytes"] > 0
    assert database["page_size"] >= 512
    assert database["wal_bytes"] >= 0
    assert database["reclaimable_bytes"] >= 0
    assert database["reclaimable_bytes"] % database["page_size"] == 0


async def test_reclaimable_bytes_appear_after_a_delete(seeded):
    """Deleting rows frees pages inside the file without shrinking it."""
    from db import models as db
    from sessions.manager import get_manager

    for i in range(40):
        sid = db.create_session(title=f"Filler {i}")
        db.add_message(sid, "user", "x" * 20_000)

    before = (await _get()).json()["database"]
    manager = get_manager()
    for row in db.list_sessions(200):
        if str(row["title"]).startswith("Filler"):
            manager.delete_session(row["id"])

    after = (await _get()).json()["database"]
    assert after["reclaimable_bytes"] > before["reclaimable_bytes"]


async def test_backups_block_when_there_is_no_directory():
    backups = (await _get()).json()["backups"]
    assert backups["dir"].endswith("backups")
    assert backups["count"] == 0
    assert backups["bytes"] == 0
    assert backups["last_backup_at"] is None
    assert backups["beyond_keep"] == []


async def test_backups_block_lists_what_rotation_would_remove(monkeypatch):
    monkeypatch.setattr(settings, "backup_keep_count", 2)
    root = backup.backups_dir()
    root.mkdir(parents=True, exist_ok=True)
    names = [
        "sessions.2026-01-02T03:04:05+00:00.db",
        "sessions.db.20260201-030405",
        "sessions-20260301-030405.db",
    ]
    base = time.time() - 5000
    for i, name in enumerate(names):
        path = root / name
        path.write_bytes(b"x" * 500)
        os.utime(path, (base + i, base + i))
    (root / "settings-20260102.json").write_text("{}")

    backups = (await _get()).json()["backups"]
    assert backups["count"] == 3, "the settings json is not a snapshot"
    assert backups["keep"] == 2
    assert backups["last_backup_at"] is not None
    assert [f["name"] for f in backups["beyond_keep"]] == ["sessions.2026-01-02T03:04:05+00:00.db"]
    assert backups["beyond_keep"][0]["scheme"] == "iso"
    assert backups["beyond_keep"][0]["bytes"] == 500
    assert backups["bytes"] >= 1500


# ---------------------------------------------------------------------------
# The legacy directory: data/.backups
# ---------------------------------------------------------------------------
#
# Backups used to live in a dotted directory. The rename left a boot-time path
# behind that still writes a database copy and a settings copy into the old one
# on every container start, so it grows with the deploys and nothing has ever
# rotated it — on the box it is the larger of the two directories.

# The name shapes actually found in data/.backups, one per scheme.
LEGACY_NAMES = (
    "sessions.20260825T183703Z.db",  # compact ISO, the oldest era
    "sessions.db.20260824-103602",  # suffixed
    "sessions-20260826-024434.db",  # stamped, what the deploy writes today
)


@pytest.fixture
def legacy(monkeypatch):
    """A populated data/.backups, oldest first, beside an empty primary dir."""
    monkeypatch.setattr(settings, "backup_keep_count", 1)
    root = backup.legacy_backups_dir()
    root.mkdir(parents=True, exist_ok=True)
    base = time.time() - 5000
    for i, name in enumerate(LEGACY_NAMES):
        path = root / name
        path.write_bytes(b"x" * 400)
        os.utime(path, (base + i, base + i))
    # The non-snapshots that share the directory: a settings dump the deploy
    # writes alongside the database copy, and a memory corpus.
    (root / "settings-20260902-130414.json").write_text("{}")
    (root / "memories-20260902-160055").mkdir()
    (root / "memories-20260902-160055" / "notes.md").write_text("corpus")
    return root


async def test_legacy_backups_is_null_when_there_is_no_such_directory():
    """Present and null, not absent: a key that only appears on the boxes with
    the problem is a key no client remembers to look for."""
    payload = (await _get()).json()
    assert "legacy_backups" in payload
    assert payload["legacy_backups"] is None


async def test_legacy_backups_block_has_the_backups_shape(legacy):
    block = (await _get()).json()["legacy_backups"]
    assert block["dir"] == str(legacy)
    assert block["dir"].endswith(".backups")
    assert block["count"] == 3, "the settings json and the corpus are not snapshots"
    assert block["keep"] == 1
    assert block["last_backup_at"] is not None
    assert block["bytes"] >= 1200 + 6, "the whole directory, corpus and settings dump included"
    assert [f["name"] for f in block["beyond_keep"]] == [LEGACY_NAMES[1], LEGACY_NAMES[0]]
    assert {f["scheme"] for f in block["beyond_keep"]} == {"suffixed", "iso"}


async def test_the_two_directories_are_counted_separately(legacy):
    """The keep count is "the newest N in this directory", not a shared budget."""
    primary = backup.backups_dir()
    primary.mkdir(parents=True, exist_ok=True)
    (primary / "sessions-20260901-010101.db").write_bytes(b"x" * 100)

    payload = (await _get()).json()
    assert payload["backups"]["count"] == 1
    assert payload["backups"]["beyond_keep"] == [], "one snapshot, keep 1 — nothing to sweep here"
    assert payload["legacy_backups"]["count"] == 3
    assert len(payload["legacy_backups"]["beyond_keep"]) == 2


async def test_rotate_legacy_removes_only_the_legacy_snapshots(legacy):
    primary = backup.backups_dir()
    primary.mkdir(parents=True, exist_ok=True)
    (primary / "sessions-20260901-010101.db").write_bytes(b"x" * 100)
    (primary / "sessions-20260801-010101.db").write_bytes(b"x" * 100)

    plan = (await _post("/api/storage/backups/rotate", {"dry_run": True, "dir": "legacy"})).json()
    assert plan["dir"] == "legacy"
    assert plan["removed"] == [LEGACY_NAMES[1], LEGACY_NAMES[0]]
    assert plan["bytes_freed"] == 800
    assert plan["kept"] == 1
    assert len(backup.list_snapshots(legacy)) == 3, "a dry run deletes nothing"

    body = (await _post("/api/storage/backups/rotate", {"dry_run": False, "dir": "legacy"})).json()
    assert body["removed"] == plan["removed"]
    assert [s["path"].name for s in backup.list_snapshots(legacy)] == [LEGACY_NAMES[2]]
    assert (legacy / "settings-20260902-130414.json").exists(), "not a snapshot, not rotation's business"
    assert (legacy / "memories-20260902-160055").is_dir()
    assert len(backup.list_snapshots(primary)) == 2, "the primary directory is untouched by a legacy sweep"


async def test_rotate_defaults_to_the_primary_directory(legacy):
    """No `dir` means the directory the schedule writes, as it always did."""
    body = (await _post("/api/storage/backups/rotate")).json()
    assert body["dir"] == "primary"
    assert body["removed"] == []
    assert len(backup.list_snapshots(legacy)) == 3


async def test_rotate_legacy_is_404_when_the_directory_is_absent():
    resp = await _post("/api/storage/backups/rotate", {"dry_run": True, "dir": "legacy"})
    assert resp.status_code == 404


async def test_rotate_rejects_an_unknown_directory(legacy):
    """A typo must not fall back to deleting from the primary directory."""
    resp = await _post("/api/storage/backups/rotate", {"dry_run": False, "dir": "legacyy"})
    assert resp.status_code == 400
    assert len(backup.list_snapshots(legacy)) == 3


async def test_run_backup_never_writes_to_the_legacy_directory(legacy, monkeypatch):
    """The schedule owns one directory. Sweeping the other is an explicit act."""
    monkeypatch.setattr(settings, "memory_dir", str(legacy.parent / "memories"))
    result = backup.run_backup(keep=1)
    assert result["dir"] == str(backup.backups_dir())
    assert len(backup.list_snapshots(legacy)) == 3, "not written to, and not rotated"
    assert len(backup.list_snapshots(backup.backups_dir())) == 1


async def test_sweeps_reports_the_retention_counters():
    payload = (await _get()).json()
    # Omitted rather than faked when the snooze runner cannot be reached.
    if "sweeps" in payload:
        assert "last_cycle" in payload["sweeps"]
        assert all(k == "last_cycle" or k.endswith("_pruned") for k in payload["sweeps"])


# ---------------------------------------------------------------------------
# POST /api/storage/backups/rotate
# ---------------------------------------------------------------------------


@pytest.fixture
def three_schemes(monkeypatch):
    monkeypatch.setattr(settings, "backup_keep_count", 1)
    root = backup.backups_dir()
    root.mkdir(parents=True, exist_ok=True)
    names = ["sessions.2026-01-02T03:04:05.db", "sessions.db.20260201-030405", "sessions-20260301-030405.db"]
    base = time.time() - 5000
    for i, name in enumerate(names):
        path = root / name
        path.write_bytes(b"x" * 100)
        os.utime(path, (base + i, base + i))
    return root, names


async def test_rotate_defaults_to_a_dry_run(three_schemes):
    root, names = three_schemes
    resp = await _post("/api/storage/backups/rotate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["removed"] == [names[1], names[0]]
    assert body["bytes_freed"] == 200
    assert body["kept"] == 1
    assert len(backup.list_snapshots(root)) == 3, "a dry run deletes nothing"


async def test_rotate_for_real_removes_only_what_the_dry_run_named(three_schemes):
    root, names = three_schemes
    planned = (await _post("/api/storage/backups/rotate")).json()["removed"]
    body = (await _post("/api/storage/backups/rotate", {"dry_run": False})).json()
    assert body["dry_run"] is False
    assert body["removed"] == planned
    assert [s["path"].name for s in backup.list_snapshots(root)] == [names[2]]


# ---------------------------------------------------------------------------
# POST /api/storage/optimize
# ---------------------------------------------------------------------------


async def test_optimize_reports_the_file_size_either_side(seeded):
    resp = await _post("/api/storage/optimize")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bytes_before"] > 0
    assert body["bytes_after"] > 0


async def test_optimize_reclaims_the_free_pages(seeded):
    from db import models as db
    from sessions.manager import get_manager

    for i in range(40):
        sid = db.create_session(title=f"Filler {i}")
        db.add_message(sid, "user", "x" * 20_000)
    manager = get_manager()
    for row in db.list_sessions(200):
        if str(row["title"]).startswith("Filler"):
            manager.delete_session(row["id"])

    assert (await _get()).json()["database"]["reclaimable_bytes"] > 0
    body = (await _post("/api/storage/optimize")).json()
    assert body["bytes_after"] < body["bytes_before"]
    assert (await _get()).json()["database"]["reclaimable_bytes"] == 0


async def test_optimize_refuses_while_a_turn_is_running(monkeypatch):
    """VACUUM holds a write lock for the whole rebuild — long enough to cost
    a running agent its next write."""
    from sessions.manager import get_manager

    monkeypatch.setattr(type(get_manager()), "has_active_work", lambda self, strict=False: True)
    resp = await _post("/api/storage/optimize")
    assert resp.status_code == 409
    assert "turn is running" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Mounting
# ---------------------------------------------------------------------------


def test_router_is_mounted_on_the_app():
    import api.app

    paths = {route.path for route in api.app.app.routes}
    assert {"/api/storage", "/api/storage/backups/rotate", "/api/storage/optimize"} <= paths


def test_storage_is_not_a_public_path():
    """It reports the database path and the backup directory; it sits behind
    the same bearer check as /api/settings."""
    import api.app

    assert "/api/storage" not in api.app._PUBLIC_EXACT
    assert not any("/api/storage".startswith(p) for p in api.app._PUBLIC_PREFIXES)
