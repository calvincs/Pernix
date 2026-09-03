"""Space suggestions (v35) through the API: the click that makes one real.

The accept path is the only place in the feature that writes anything, so it
is pinned end to end here: the space appears, the directive file the sheet
sent lands in the space's agent dir, the members move without their recency
changing, and everything else — a second accept, a taken slug, a vanished
space, a decline — comes back as a status the sheet can render rather than a
half-applied change.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core import spaces as spaces_lib
from db import models as db
from db.database import connect_sessions


@pytest.fixture(autouse=True)
def _fresh_space_cache():
    spaces_lib.invalidate_space_cache()
    yield
    spaces_lib.invalidate_space_cache()


@pytest.fixture()
def agent_env(tmp_path, monkeypatch):
    """A chdir'd sandbox with default directives — space agent dirs and the
    default files both resolve relative to the working directory."""
    monkeypatch.chdir(tmp_path)
    agent = tmp_path / "data" / "agent"
    agent.mkdir(parents=True)
    (agent / "SOUL.md").write_text("DEFAULT SOUL")
    (agent / "RULES.md").write_text("DEFAULT RULES")
    (agent / "SESSIONS.md").write_text("DEFAULT SESSIONS")
    return agent


def _client() -> AsyncClient:
    from api.routers import spaces

    app = FastAPI()
    app.include_router(spaces.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _sessions(n: int, *, space_id=None) -> list[str]:
    return [db.create_session(title=f"Claim {i}", space_id=space_id) for i in range(n)]


def _suggest(kind="new", *, members, directives=None, existing_space_id=None, label="Fact Checking") -> dict:
    return db.add_space_suggestion(
        kind,
        "fact-checking",
        label,
        "#4db6ac",
        "You keep verifying claims against sources.",
        members,
        existing_space_id=existing_space_id,
        directives=directives,
    )


_RULES_DRAFT = {
    "RULES": {"addition": "## Sourcing\nCite every claim.", "rationale": "This work lives or dies on sources."}
}


# ---------------------------------------------------------------------------
# Listing and detail
# ---------------------------------------------------------------------------


async def test_the_list_defaults_to_pending_and_resolves_its_members():
    sp = db.create_space("Pernix", "#7c9cff", "pernix")
    members = _sessions(3)
    row = _suggest("existing", members=members, existing_space_id=sp["id"])
    gone = _suggest(members=["deleted-id"], label="Ghosts")
    db.set_space_suggestion_status(gone["id"], "rejected")

    async with _client() as c:
        body = (await c.get("/api/space-suggestions")).json()

    assert body["status"] == "pending"
    assert [s["id"] for s in body["suggestions"]] == [row["id"]]
    listed = body["suggestions"][0]
    assert [s["id"] for s in listed["sessions"]] == members
    assert listed["sessions"][0]["title"] == "Claim 0"
    assert listed["existing_space"] == {"id": sp["id"], "label": "Pernix", "color": "#7c9cff"}
    assert listed["session_ids"] == members


async def test_ids_of_deleted_sessions_drop_out_of_the_member_list():
    members = _sessions(3)
    _suggest(members=members + ["never-existed"])
    async with _client() as c:
        listed = (await c.get("/api/space-suggestions")).json()["suggestions"][0]
    assert [s["id"] for s in listed["sessions"]] == members
    assert listed["session_ids"] == members + ["never-existed"]


async def test_the_list_filters_by_status_and_refuses_an_unknown_one():
    kept = _suggest(members=_sessions(2))
    declined = _suggest(members=_sessions(2), label="Weather")
    db.set_space_suggestion_status(declined["id"], "rejected")

    async with _client() as c:
        assert [s["id"] for s in (await c.get("/api/space-suggestions?status=rejected")).json()["suggestions"]] == [
            declined["id"]
        ]
        assert len((await c.get("/api/space-suggestions?status=all")).json()["suggestions"]) == 2
        bad = await c.get("/api/space-suggestions?status=maybe")
        assert bad.status_code == 400
        assert "pending" in bad.json()["detail"] and "rejected" in bad.json()["detail"]
        assert [s["id"] for s in (await c.get("/api/space-suggestions")).json()["suggestions"]] == [kept["id"]]


async def test_detail_shows_each_draft_next_to_the_default_it_appends_to(agent_env):
    row = _suggest(members=_sessions(2), directives=_RULES_DRAFT)
    async with _client() as c:
        got = await c.get(f"/api/space-suggestions/{row['id']}")
        missing = await c.get("/api/space-suggestions/nope")

    assert missing.status_code == 404
    body = got.json()
    assert body["directives"]["RULES"]["addition"].startswith("## Sourcing")
    assert body["directives"]["RULES"]["rationale"]
    assert body["directives"]["RULES"]["default"] == "DEFAULT RULES"


async def test_detail_reports_no_drafts_as_null(agent_env):
    row = _suggest(members=_sessions(2))
    async with _client() as c:
        assert (await c.get(f"/api/space-suggestions/{row['id']}")).json()["directives"] is None


# ---------------------------------------------------------------------------
# Accept
# ---------------------------------------------------------------------------


async def test_accepting_a_new_suggestion_creates_the_space_and_files_the_chats(agent_env):
    members = _sessions(5)
    before = {sid: db.get_session(sid)["updated_at"] for sid in members}
    row = _suggest(members=members, directives=_RULES_DRAFT)

    async with _client() as c:
        r = await c.post(
            f"/api/space-suggestions/{row['id']}/accept",
            json={
                "label": "Fact Checking",
                "color": "#ff8a65",
                "session_ids": members,
                "directives": {"RULES": "DEFAULT RULES\n\n## Sourcing\nCite every claim."},
            },
        )

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted" and body["moved"] == 5 and body["failed"] == []

    space = db.get_space_by_slug("fact-checking")
    assert space and space["label"] == "Fact Checking" and space["color"] == "#ff8a65"
    assert body["space"]["id"] == space["id"]

    # The directive the sheet sent is on disk under the space's agent dir.
    written = spaces_lib.space_agent_dir(space) / "RULES.md"
    assert written.read_text().endswith("## Sourcing\nCite every claim.")
    assert written.read_text().startswith("DEFAULT RULES")
    # The workspace home exists before anything writes into it.
    assert spaces_lib.space_workspace_home(space).exists()

    for sid in members:
        session = db.get_session(sid)
        assert session["space_id"] == space["id"]
        # Filing a chat is not activity: recency must be untouched.
        assert session["updated_at"] == before[sid]

    stored = db.get_space_suggestion(row["id"])
    assert stored["status"] == "accepted" and stored["space_id"] == space["id"] and stored["resolved_at"]


async def test_a_second_accept_is_a_conflict(agent_env):
    row = _suggest(members=_sessions(2))
    async with _client() as c:
        assert (await c.post(f"/api/space-suggestions/{row['id']}/accept", json={})).status_code == 200
        again = await c.post(f"/api/space-suggestions/{row['id']}/accept", json={})
    assert again.status_code == 409
    assert "accepted" in again.json()["detail"]


async def test_a_taken_slug_comes_back_as_a_conflict_that_names_it(agent_env):
    db.create_space("Fact Checking", "#7c9cff", "fact-checking")
    row = _suggest(members=_sessions(2))
    async with _client() as c:
        r = await c.post(f"/api/space-suggestions/{row['id']}/accept", json={"label": "Fact Checking"})
    assert r.status_code == 409
    assert "fact-checking" in r.json()["detail"]
    # The suggestion is untouched: the sheet stays open and asks for a name.
    assert db.get_space_suggestion(row["id"])["status"] == "pending"


async def test_a_renamed_label_is_what_the_space_is_called(agent_env):
    row = _suggest(members=_sessions(2))
    async with _client() as c:
        r = await c.post(f"/api/space-suggestions/{row['id']}/accept", json={"label": "Claim Review"})
    assert r.status_code == 200
    assert db.get_space_by_slug("claim-review")["label"] == "Claim Review"
    assert db.get_space_by_slug("fact-checking") is None


async def test_accept_moves_only_the_members_it_was_offered(agent_env):
    members = _sessions(3)
    outsider = db.create_session(title="unrelated")
    row = _suggest(members=members)

    async with _client() as c:
        r = await c.post(
            f"/api/space-suggestions/{row['id']}/accept",
            json={"session_ids": [members[0], outsider]},
        )

    assert r.json()["moved"] == 1
    space = db.get_space_by_slug("fact-checking")
    assert db.get_session(members[0])["space_id"] == space["id"]
    assert db.get_session(members[1])["space_id"] is None
    assert db.get_session(outsider)["space_id"] is None


async def test_a_member_deleted_since_the_scan_is_reported_not_fatal(agent_env):
    members = _sessions(2)
    row = _suggest(members=members + ["vanished"])
    async with _client() as c:
        body = (await c.post(f"/api/space-suggestions/{row['id']}/accept", json={})).json()
    assert body["moved"] == 2 and body["failed"] == ["vanished"]


async def test_accepting_an_existing_kind_moves_without_creating_a_space(agent_env):
    sp = db.create_space("Pernix", "#7c9cff", "pernix")
    members = _sessions(3)
    row = _suggest("existing", members=members, existing_space_id=sp["id"], directives=_RULES_DRAFT)

    async with _client() as c:
        body = (await c.post(f"/api/space-suggestions/{row['id']}/accept", json={})).json()

    assert body["moved"] == 3 and body["space"]["id"] == sp["id"]
    assert len(db.list_spaces()) == 1
    assert all(db.get_session(sid)["space_id"] == sp["id"] for sid in members)
    # An existing space may already carry hand-edited overrides: an accept
    # never silently replaces them.
    assert not (spaces_lib.space_agent_dir(sp) / "RULES.md").exists()


async def test_an_existing_kind_whose_space_vanished_expires_itself(agent_env):
    sp = db.create_space("Pernix", "#7c9cff", "pernix")
    row = _suggest("existing", members=_sessions(2), existing_space_id=sp["id"])
    db.delete_space(sp["id"])

    async with _client() as c:
        r = await c.post(f"/api/space-suggestions/{row['id']}/accept", json={})

    assert r.status_code == 409
    assert db.get_space_suggestion(row["id"])["status"] == "expired"


async def test_a_directive_body_gets_put_directives_validation(agent_env):
    row = _suggest(members=_sessions(2))
    async with _client() as c:
        empty = await c.post(f"/api/space-suggestions/{row['id']}/accept", json={"directives": {"RULES": "   "}})
        unknown = await c.post(f"/api/space-suggestions/{row['id']}/accept", json={"directives": {"EVIL": "x"}})
        huge = await c.post(
            f"/api/space-suggestions/{row['id']}/accept",
            json={"directives": {"RULES": "x" * 70_000}},
        )
    assert empty.status_code == 400 and unknown.status_code == 400 and huge.status_code == 400
    # Nothing was created by a rejected body.
    assert db.list_spaces() == []
    assert db.get_space_suggestion(row["id"])["status"] == "pending"


# ---------------------------------------------------------------------------
# Reject and clear
# ---------------------------------------------------------------------------


async def test_declining_is_terminal_and_blocks_a_later_accept():
    row = _suggest(members=_sessions(2))
    async with _client() as c:
        assert (await c.post(f"/api/space-suggestions/{row['id']}/reject")).json() == {"status": "rejected"}
        assert (await c.post(f"/api/space-suggestions/{row['id']}/reject")).status_code == 409
        accept = await c.post(f"/api/space-suggestions/{row['id']}/accept", json={})
    assert accept.status_code == 409
    stored = db.get_space_suggestion(row["id"])
    assert stored["status"] == "rejected" and stored["resolved_at"]
    assert db.list_spaces() == []


async def test_rejecting_something_that_is_not_there_is_a_404():
    async with _client() as c:
        assert (await c.post("/api/space-suggestions/nope/reject")).status_code == 404


async def test_clearing_one_row_and_then_every_declined_one():
    first = _suggest(members=_sessions(2))
    second = _suggest(members=_sessions(2), label="Weather")
    third = _suggest(members=_sessions(2), label="Invoices")
    for row in (second, third):
        db.set_space_suggestion_status(row["id"], "rejected")

    async with _client() as c:
        assert (await c.delete(f"/api/space-suggestions/{first['id']}")).json() == {"cleared": 1}
        assert (await c.delete(f"/api/space-suggestions/{first['id']}")).status_code == 404
        bulk = await c.delete("/api/space-suggestions?status=rejected")

    assert bulk.json()["cleared"] == 2
    assert db.list_space_suggestions() == []


async def test_the_bulk_clear_refuses_pending_and_unknown_statuses():
    _suggest(members=_sessions(2))
    async with _client() as c:
        pending = await c.delete("/api/space-suggestions?status=pending")
        unknown = await c.delete("/api/space-suggestions?status=maybe")
    assert pending.status_code == 400 and "accepting or declining" in pending.json()["detail"]
    assert unknown.status_code == 400
    assert len(db.list_space_suggestions("pending")) == 1


# ---------------------------------------------------------------------------
# Scan now
# ---------------------------------------------------------------------------


async def test_the_scan_endpoint_returns_the_scan_result(monkeypatch):
    from core import space_suggest as ss

    seen: dict = {}

    async def _fake_scan(*, force=False, dry_run=False):
        seen["force"] = force
        seen["dry_run"] = dry_run
        return {"scanned": 7, "proposed": [], "kept": [], "dry_run": dry_run}

    monkeypatch.setattr(ss, "scan", _fake_scan)

    async with _client() as c:
        default = await c.post("/api/space-suggestions/scan", json={})
        assert default.json()["dry_run"] is True
        assert seen == {"force": True, "dry_run": True}

        wet = await c.post("/api/space-suggestions/scan", json={"dry_run": False})

    assert wet.json() == {"scanned": 7, "proposed": [], "kept": [], "dry_run": False}
    assert seen["dry_run"] is False


async def test_the_scan_endpoint_is_a_conflict_while_the_lock_is_held():
    from core import space_suggest as ss

    async with ss._scan_lock:
        async with _client() as c:
            r = await c.post("/api/space-suggestions/scan", json={})
    assert r.status_code == 409
    assert "already running" in r.json()["detail"]


# ---------------------------------------------------------------------------
# The whole arc, once
# ---------------------------------------------------------------------------


async def test_a_scan_becomes_a_space_end_to_end(agent_env, monkeypatch):
    """Seed, scan, review, accept — the path a user actually walks."""
    from core import space_suggest as ss

    members = []
    for i in range(6):
        sid = db.create_session(title=f"Verify claim {i}")
        with connect_sessions() as conn:
            conn.execute("UPDATE sessions SET subtitle = 'fact checking' WHERE id = ?", (sid,))
        db.add_message(sid, "user", "check this claim")
        db.add_message(sid, "assistant", "checked")
        db.add_message(sid, "user", "and this one")
        db.add_message(sid, "assistant", "checked")
        db.add_message(sid, "scout", json.dumps({"task_type": "research"}))
        members.append(sid)
    # Spread across days so the "one busy afternoon" rule is satisfied.
    with connect_sessions() as conn:
        for offset, sid in enumerate(members):
            conn.execute(
                "UPDATE sessions SET created_at = datetime('now', ?) WHERE id = ?",
                (f"-{offset % 4} days", sid),
            )

    class _Resp:
        content = json.dumps(
            {
                "clusters": [
                    {
                        "kind": "new",
                        "topic_key": "fact-checking",
                        "label": "Fact Checking",
                        "why": "You keep verifying claims against sources.",
                        "session_ids": members,
                        "directives": {
                            "RULES": {"addition": "## Sourcing\nCite every claim.", "rationale": "sources matter"}
                        },
                    }
                ]
            }
        )

    async def _fake(client, **kw):
        return _Resp()

    monkeypatch.setattr(ss, "chat_with_backup", _fake)
    monkeypatch.setattr(ss, "get_llm_client", lambda: object())
    monkeypatch.setattr("config.settings.background_model", "fake-bg-model")

    async with _client() as c:
        scanned = (await c.post("/api/space-suggestions/scan", json={"dry_run": False})).json()
        assert len(scanned["kept"]) == 1

        pending = (await c.get("/api/space-suggestions")).json()["suggestions"]
        assert len(pending) == 1
        detail = (await c.get(f"/api/space-suggestions/{pending[0]['id']}")).json()
        assert detail["directives"]["RULES"]["default"] == "DEFAULT RULES"

        accepted = await c.post(
            f"/api/space-suggestions/{detail['id']}/accept",
            json={
                "session_ids": [s["id"] for s in detail["sessions"]],
                "directives": {
                    "RULES": detail["directives"]["RULES"]["default"] + "\n\n## Sourcing\nCite every claim."
                },
            },
        )

    assert accepted.json()["moved"] == 6
    space = db.get_space_by_slug("fact-checking")
    assert (spaces_lib.space_agent_dir(space) / "RULES.md").read_text().startswith("DEFAULT RULES")
    assert len(db.list_space_session_ids(space["id"])) == 6
    async with _client() as c:
        assert (await c.get("/api/space-suggestions")).json()["suggestions"] == []
