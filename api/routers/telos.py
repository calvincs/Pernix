"""Pernix — TELOS layer endpoints (read + bounded control).

Read surfaces mirror the store; the only writes exposed are the manual
slow-loop trigger and alarm acknowledgement. There is deliberately NO
endpoint that writes ledgers/trace/ or re-expresses g_root — the trace is
append-only via the engine, and root re-expression is an operator file edit
with co-sign semantics (spec §4.1, §5.4).
"""

from __future__ import annotations

import asyncio as _asyncio

from fastapi import APIRouter, HTTPException

from config import settings

router = APIRouter(tags=["telos"])


def _store():
    from core.telos.store import TelosStore

    return TelosStore.open()


def _obj_dict(o) -> dict:
    return {"id": o.id, **o.meta, "body": o.body[:2000]}


@router.get("/api/telos")
async def telos_overview():
    """Layer status: counts, band mix, coherence series, root."""
    if not settings.telos_enabled:
        return {"enabled": False}

    def build():
        store = _store()
        store.ensure_root()
        questions = store.list_questions()
        hyps = store.list_hypotheses()
        goals = store.list_goals()
        state = store.get_state()

        def by(items, key):
            out: dict = {}
            for i in items:
                k = str(i.get(key))
                out[k] = out.get(k, 0) + 1
            return out

        root = store.read("goal", "g_root")
        return {
            "enabled": True,
            "schedule": settings.telos_schedule,
            "root": _obj_dict(root) if root else None,
            "questions": by(questions, "state"),
            "serendipity_open": sum(
                1 for q in questions if q.get("origin") == "serendipity" and q.get("state") == "open"
            ),
            "hypotheses": by(hyps, "status"),
            "goals": by(goals, "kind"),
            "goals_suspended": sum(1 for g in goals if g.get("state") == "suspended"),
            "claims": len(store.list("claim")),
            "alarms_open": [_obj_dict(a) for a in store.list_alarms(open_only=True)[:10]],
            "band_mix": store.band_mix(),
            "serendipity_budget": store.serendipity_budget(),
            "vapor_classes": state.get("vapor_classes") or [],
            "coherence_series": (state.get("coherence_series") or [])[-12:],
        }

    return await _asyncio.to_thread(build)


@router.get("/api/telos/questions")
async def telos_questions(state: str = "", limit: int = 100):
    if not settings.telos_enabled:
        return {"questions": []}

    def build():
        store = _store()
        qs = store.list_questions(state=state or None)
        qs.sort(key=lambda q: str(q.get("created_at", "")), reverse=True)
        return {"questions": [_obj_dict(q) for q in qs[: max(1, min(limit, 500))]]}

    return await _asyncio.to_thread(build)


@router.get("/api/telos/hypotheses")
async def telos_hypotheses(status: str = "", limit: int = 100):
    if not settings.telos_enabled:
        return {"hypotheses": []}

    def build():
        store = _store()
        hs = store.list_hypotheses(status=status or None)
        hs.sort(key=lambda h: str(h.get("updated_at", "")), reverse=True)
        return {"hypotheses": [_obj_dict(h) for h in hs[: max(1, min(limit, 500))]]}

    return await _asyncio.to_thread(build)


@router.get("/api/telos/goals")
async def telos_goals():
    if not settings.telos_enabled:
        return {"goals": []}

    def build():
        store = _store()
        store.ensure_root()
        return {"goals": [_obj_dict(g) for g in store.list_goals()]}

    return await _asyncio.to_thread(build)


@router.get("/api/telos/claims")
async def telos_claims(limit: int = 100):
    if not settings.telos_enabled:
        return {"claims": []}

    def build():
        store = _store()
        cs = store.list("claim")
        cs.sort(key=lambda c: str(c.get("created_at", "")), reverse=True)
        return {"claims": [_obj_dict(c) for c in cs[: max(1, min(limit, 500))]]}

    return await _asyncio.to_thread(build)


@router.get("/api/telos/trace")
async def telos_trace(days: int = 2, type: str = "", limit: int = 200):
    """Read-only window into the trace ledger (operator-held record)."""
    if not settings.telos_enabled:
        return {"events": []}

    def build():
        store = _store()
        events = store.trace_events(days=max(1, min(days, 30)), types={type} if type else None)
        return {"events": events[-max(1, min(limit, 1000)) :]}

    return await _asyncio.to_thread(build)


@router.post("/api/telos/run")
async def telos_run(body: dict = {}):
    """Manually queue the daily slow-loop pass (optionally forcing the
    weekly block). The fast loop runs only at idle via snooze."""
    if not settings.telos_enabled:
        raise HTTPException(400, detail="telos_enabled is off")
    from core.extensions.scheduling import enqueue_manual_telos

    if not enqueue_manual_telos(force_weekly=bool(body.get("force_weekly"))):
        raise HTTPException(503, detail="scheduler unavailable")
    return {"queued": True, "force_weekly": bool(body.get("force_weekly"))}


@router.post("/api/telos/alarms/{alarm_id}/ack")
async def telos_ack_alarm(alarm_id: str):
    """Operator acknowledgement — marks the alarm reviewed. It reopens on
    its own if the signature still holds at the next monitor pass."""
    if not settings.telos_enabled:
        raise HTTPException(400, detail="telos_enabled is off")

    def ack():
        store = _store()
        a = store.read("alarm", alarm_id)
        if a is None:
            return None
        store.update(a, state="acknowledged")
        store.trace_append("alarm_acknowledged", {"id": alarm_id})
        return _obj_dict(a)

    result = await _asyncio.to_thread(ack)
    if result is None:
        raise HTTPException(404, detail=f"no alarm {alarm_id}")
    return result
