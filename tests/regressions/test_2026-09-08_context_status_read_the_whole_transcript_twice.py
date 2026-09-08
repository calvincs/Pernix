"""The context status bar read the whole transcript twice, on the loop.

`/api/context/{id}` is what the status-bar indicator polls: on every session
selection, and again after every compaction. It called `compile_context()`
straight from the coroutine — a full history read plus tokenization of the
surviving suffix — and then called `db.get_messages()` a SECOND time, in
full, to produce two integers: how many messages there are and how many of
them are compaction markers.

Measured on a 871-session / 15,991-message fixture whose target session
holds 1,201 messages and 2,461,000 bytes of content, compacted at 80%:
two full-history reads, 4,922,000 bytes materialized per call, and a
heartbeat scheduled every 5 ms delivered ZERO of the 98 ticks due across
the 492 ms the route ran. The second read alone was 7.00 ms of a 47 ms
warm route; the aggregate that replaces it measures 0.18 ms, because
`idx_messages_session_role` covers `COUNT(*)` and `SUM(role='compaction')`
without touching the content column at all.

The audit stopped there. Two more things were wrong on the same two routes:

`/api/context/{id}/payload` — the transparency view, the thing a user opens
to see what the agent is actually sending — called the compiler with the
session id and the tool schemas and NOTHING else. No budget, no output
reservation, no model name, no vision flag. It then reported
`settings.context_budget` as "budget" regardless. On a session with a
32,000-token override the agent compiles history into a 4,000-token floor
and drops 226 messages; `/payload` showed budget 192,000 and
messages_trimmed 0, and its system prompt was not byte-identical to the
agent's. A transparency endpoint that reports a payload nobody sends is
worse than a slow one.

And neither route 404'd on an unknown id. `context_breakdown("no-such-id")`
answered HTTP 200 with `message_count: 0, total_tokens: 4601, status:
"healthy"` — a full system prompt and the whole tool catalog, compiled and
billed against a session that does not exist.

Pinned here: one full-history read per call, counts from SQL, both routes
off the loop with a heartbeat that keeps ticking, `/payload` compiled from
the same inputs as the agent, and 404 for an id with no session behind it.
"""

import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from db import models as db
from db.database import connect_sessions

HEARTBEAT_S = 0.005


def _client() -> AsyncClient:
    from api.routers import context

    app = FastAPI()
    app.include_router(context.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _fat_transcript(rounds: int = 120, chunk: int = 2000) -> str:
    """A transcript with real bulk in it — old tool output included."""
    sid = db.create_session(title="the long one")
    with connect_sessions() as conn:
        rows = []
        for i in range(rounds):
            rows.append((sid, "user", f"round {i} " + "q" * chunk))
            rows.append((sid, "assistant", f"round {i} " + "a" * chunk))
            rows.append((sid, "tool", f"round {i} " + "t" * chunk))
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, char_count, created_at)"
            " VALUES (?, ?, ?, length(?), '2026-09-08T00:00:00+00:00')",
            [(a, b, c, c) for (a, b, c) in rows],
        )
    return sid


def _compact_at(sid: str, fraction: float = 0.8) -> None:
    with connect_sessions() as conn:
        ids = [int(r["id"]) for r in conn.execute("SELECT id FROM messages WHERE session_id=? ORDER BY id", (sid,))]
        boundary = ids[int(len(ids) * fraction)]
        conn.execute(
            "INSERT INTO messages (session_id, role, content, char_count, metadata, created_at)"
            " VALUES (?, 'compaction', ?, 0, ?, '2026-09-08T00:00:00+00:00')",
            (sid, "SUMMARY OF EARLIER WORK. " * 20, json.dumps({"compacted_up_to": boundary})),
        )


class _ReadMeter:
    """Counts full-history reads and the content bytes they materialize."""

    def __init__(self, monkeypatch):
        import core.context.compiler as compiler

        self.reads = 0
        self.content_bytes = 0
        real = db.get_messages

        def counting(session_id, *a, **kw):
            rows = real(session_id, *a, **kw)
            self.reads += 1
            self.content_bytes += sum(len(r["content"] or "") for r in rows)
            return rows

        monkeypatch.setattr(db, "get_messages", counting)
        monkeypatch.setattr(compiler.db, "get_messages", counting)


class _Heartbeat:
    """A 5 ms tick that remembers the longest gap it actually saw.

    A window in which the loop never got control leaves the running gap
    open, so `report()` closes it against the clock rather than reporting
    a reassuring zero for a tick that never happened.
    """

    async def __aenter__(self):
        self.ticks = 0
        self.max_gap = 0.0
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(0.05)
        self.ticks = 0
        self.max_gap = 0.0
        self._last = time.perf_counter()
        return self

    async def _run(self):
        self._last = time.perf_counter()
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            now = time.perf_counter()
            self.max_gap = max(self.max_gap, now - self._last)
            self._last = now
            self.ticks += 1

    async def __aexit__(self, *a):
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return False

    @property
    def gap(self) -> float:
        return max(self.max_gap, time.perf_counter() - self._last)


# ---------------------------------------------------------------------------
# (a) one read, not two
# ---------------------------------------------------------------------------


async def test_status_reads_the_history_once_and_counts_in_sql(monkeypatch):
    sid = _fat_transcript()
    _compact_at(sid)
    meter = _ReadMeter(monkeypatch)

    async with _client() as c:
        resp = await c.get(f"/api/context/{sid}")

    assert resp.status_code == 200, resp.text
    assert meter.reads == 1, f"the transcript was materialized {meter.reads} times to answer one status poll"


async def test_the_counts_are_still_right_without_the_second_read():
    sid = _fat_transcript(rounds=4)
    _compact_at(sid)
    _compact_at(sid, fraction=0.9)

    async with _client() as c:
        body = (await c.get(f"/api/context/{sid}")).json()

    with connect_sessions() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM messages WHERE session_id=?", (sid,)).fetchone()["c"]
    assert body["message_count"] == total
    assert body["compaction_count"] == 2


def test_the_aggregate_never_touches_the_content_column():
    """The point of the SQL replacement — a covering index, not a cheaper loop."""
    sid = _fat_transcript(rounds=3)
    with connect_sessions() as conn:
        plan = " ".join(
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*), SUM(role = 'compaction') FROM messages WHERE session_id = ?",
                (sid,),
            )
        )
    assert "idx_messages_session_role" in plan, plan
    assert "SCAN messages" not in plan, plan
    assert db.count_messages_and_compactions(sid) == (9, 0)


def test_an_unknown_session_counts_as_nothing_rather_than_failing():
    assert db.count_messages_and_compactions("no-such-session-id") == (0, 0)


# ---------------------------------------------------------------------------
# (b) the loop keeps ticking
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_a_heartbeat_survives_a_slow_compile(monkeypatch):
    """Compilation is the load-bearing half: it must run off the loop.

    The gap is asserted against a generous multiple of the tick, not the
    tick itself — a shared CI core will miss a 5 ms deadline for reasons
    that have nothing to do with this route. Blocking the loop for the
    whole compile misses it by two orders of magnitude.
    """
    from core.context import compiler

    sid = _fat_transcript(rounds=6)
    real = compiler.compile_context

    def slow(*a, **kw):
        time.sleep(0.4)
        return real(*a, **kw)

    monkeypatch.setattr(compiler, "compile_context", slow)

    async with _Heartbeat() as hb:
        async with _client() as c:
            resp = await c.get(f"/api/context/{sid}")
        gap = hb.gap
        ticks = hb.ticks

    assert resp.status_code == 200, resp.text
    assert ticks > 20, f"only {ticks} heartbeats fired while the route compiled"
    assert gap < 0.2, f"the loop was held for {gap*1000:.0f} ms by a route that should be off it"


# ---------------------------------------------------------------------------
# (c) /payload tells the truth
# ---------------------------------------------------------------------------


async def _agent_view(sid: str, budget: int):
    """What core/agent.py would compile for this session, with the same knobs."""
    from config import settings
    from core.context.compiler import compile_context
    from core.llm.budget import derive_max_output
    from core.tools.registry import get_registry

    registry = get_registry()
    return compile_context(
        session_id=sid,
        tool_schemas=registry.get_schemas([t.name for t in registry.enabled_tools()]),
        context_budget=budget,
        max_output_tokens=derive_max_output(settings.llm_model),
        model_name=settings.llm_model,
        supports_vision=False,
        supports_audio=False,
    )


async def test_payload_compiles_against_the_budget_the_agent_actually_uses():
    from sessions.manager import get_manager

    sid = _fat_transcript(rounds=60)
    get_manager().get_or_create(sid).context_budget_override = 32000

    async with _client() as c:
        body = (await c.get(f"/api/context/{sid}/payload")).json()
    agent = await _agent_view(sid, 32000)

    assert body["token_breakdown"]["budget"] == 32000, "the transparency view reported a budget nobody uses"
    assert body["token_breakdown"]["history_budget"] == agent.history_budget
    assert body["messages_trimmed"] == agent.metadata.messages_trimmed
    assert body["messages_trimmed"] > 0, "fixture too small to trim — the divergence would not show"
    assert body["system_prompt"] == agent.messages[0]["content"], "the system prompt is not the agent's"


async def test_status_and_payload_agree_with_each_other():
    from sessions.manager import get_manager

    sid = _fat_transcript(rounds=40)
    get_manager().get_or_create(sid).context_budget_override = 40000

    async with _client() as c:
        status = (await c.get(f"/api/context/{sid}")).json()
        payload = (await c.get(f"/api/context/{sid}/payload")).json()

    assert status["budget"] == payload["token_breakdown"]["budget"] == 40000
    assert status["system_tokens"] == payload["token_breakdown"]["system"]
    assert status["history_tokens"] == payload["token_breakdown"]["history"]
    assert status["total_tokens"] == payload["token_breakdown"]["total"]
    assert status["messages_trimmed"] == payload["messages_trimmed"]


async def test_payload_runs_off_the_loop_too(monkeypatch):
    from core.context import compiler

    sid = _fat_transcript(rounds=4)
    real = compiler.compile_context

    def slow(*a, **kw):
        time.sleep(0.3)
        return real(*a, **kw)

    monkeypatch.setattr(compiler, "compile_context", slow)

    async with _Heartbeat() as hb:
        async with _client() as c:
            resp = await c.get(f"/api/context/{sid}/payload")
        ticks = hb.ticks
        gap = hb.gap

    assert resp.status_code == 200
    assert ticks > 15 and gap < 0.2, f"{ticks} ticks, worst gap {gap*1000:.0f} ms"


# ---------------------------------------------------------------------------
# (d) a session that does not exist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/context/{sid}", "/api/context/{sid}/payload"])
async def test_an_unknown_session_is_a_404_not_a_billed_system_prompt(path):
    async with _client() as c:
        resp = await c.get(path.format(sid="no-such-session-id"))

    assert resp.status_code == 404, resp.text


async def test_a_real_but_empty_session_is_still_a_200():
    """404 is "no such session", not "nothing said yet"."""
    sid = db.create_session(title="brand new")

    async with _client() as c:
        resp = await c.get(f"/api/context/{sid}")

    assert resp.status_code == 200
    assert resp.json()["message_count"] == 0
