"""Regression: skill-improvement proposals survived the workflow removal.

The proposals feature (reflect/refine notice a skill under-performing, a human
reviews the suggested SKILL.md edit) was served from `api/routers/workflows.py`
and applied by `core/workflows/apply.py`. Neither had anything to do with
workflows beyond where post-run reflect happened to live — proposals target
SKILL.md files, and `core/refine.py` writes them on ordinary sessions with no
workflow anywhere in the picture.

Deleting the workflow engine would therefore have silently taken a live feature
with it. The endpoints moved to `/api/skills/proposals*` and the apply logic to
`core/skills/proposals.py`.

Two hazards pinned here:

1. **Route ordering.** Starlette matches routes in DECLARATION order, not by
   specificity. `GET /api/skills/{name}` was already declared, so registering
   the proposals routes after it made `GET /api/skills/proposals` bind
   name="proposals" and 404 as a missing skill. It has to be declared first —
   and the failure is invisible until someone opens the Skills tab.

2. **The legacy columns.** `skill_improvement_proposals` still has
   `workflow_name`/`run_id` (migrations are forward-only, so the columns outlive
   the feature). They must be written NULL, not dropped and not repurposed.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _app() -> FastAPI:
    from api.routers import skills as skills_router

    app = FastAPI()
    app.include_router(skills_router.router)
    return app


@pytest.mark.asyncio
async def test_proposals_endpoints_live_under_skills():
    from db import models as db

    sid = db.create_session(title="proposal host")
    pid = db.add_skill_proposal(
        skill_name="alpha",
        section="Usage",
        problem="p",
        proposed_change="c",
        confidence=0.8,
        source_origin="refine",
        session_id=sid,
    )

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        resp = await c.get("/api/skills/proposals")
        assert resp.status_code == 200, (
            f"GET /api/skills/proposals returned {resp.status_code} — the literal "
            "route is being shadowed by /api/skills/{name}; declare it first"
        )
        assert pid in [p["id"] for p in resp.json()["proposals"]]

        resp = await c.get("/api/skills/proposals?source_origin=refine")
        assert resp.status_code == 200 and len(resp.json()["proposals"]) == 1

        resp = await c.post(f"/api/skills/proposals/{pid}/reject")
        assert resp.status_code == 200 and resp.json()["status"] == "rejected"

        resp = await c.post("/api/skills/proposals/does-not-exist/approve")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_named_skill_route_is_not_shadowed():
    """The fix for hazard 1 must not break the route it was ordered ahead of."""
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        resp = await c.get("/api/skills/definitely-not-a-real-skill")
        assert resp.status_code == 404
        assert "not found" in resp.text.lower()


def test_legacy_workflow_columns_are_written_null():
    from db import models as db

    pid = db.add_skill_proposal(
        skill_name="beta",
        section="Notes",
        problem="p",
        proposed_change="c",
        confidence=0.5,
    )
    row = db.get_skill_proposal(pid)
    assert row["workflow_name"] is None
    assert row["run_id"] is None
    # Default origin is the session path now that nothing workflow-shaped writes.
    assert row["source_origin"] == "session"


def test_apply_module_lives_under_skills():
    """The apply logic moved out of core/workflows/ intact."""
    from core.skills.proposals import ProposalApplyError, apply_proposal

    assert callable(apply_proposal)
    assert issubclass(ProposalApplyError, Exception)
