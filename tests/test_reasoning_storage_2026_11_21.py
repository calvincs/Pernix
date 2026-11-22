"""A reply's reasoning is stored beside it (2026-11-21, schema v46).

`messages.reasoning` holds the model's thinking for an assistant row. It is
not content: search, char_count and every reader of the answer stay
answer-only. Rows written before v46, or by a model that returned no
reasoning, carry NULL.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from core.llm.providers.openai import OpenAIProvider
from core.llm.types import ProviderConfig

SELF_HOSTED = "http://llm.example.internal:8000/v1"
MODEL = "inkling-2-mini"
WINDOW = 262_144


def _sse(*chunks: dict | str) -> bytes:
    return "".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks).encode()


def _provider(handler=None, *, base_url: str = SELF_HOSTED, key: str = "") -> OpenAIProvider:
    p = OpenAIProvider(ProviderConfig(name="openai", base_url=base_url, api_key=key))
    if handler is not None:
        p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        p._quick_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


# ---------------------------------------------------------------------------
# Persistence and migration
# ---------------------------------------------------------------------------


def test_reasoning_round_trips_apart_from_content():
    from db import models as db

    sid = db.create_session()
    mid = db.add_message(sid, "assistant", "the answer", reasoning="the thinking")
    plain = db.add_message(sid, "assistant", "no thinking here")
    row = db.get_message(mid)
    assert row["content"] == "the answer" and row["reasoning"] == "the thinking"
    assert row["char_count"] == len("the answer")
    assert db.get_message(plain)["reasoning"] is None
    # Search indexes the answer only.
    from db.database import connect_sessions

    with connect_sessions() as conn:
        hits = conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'thinking'").fetchall()
    assert [h[0] for h in hits] == [plain]


def test_migration_v46_adds_the_column_to_an_existing_database(tmp_path, monkeypatch):
    from db import database

    monkeypatch.setattr("config.settings.db_path", str(tmp_path / "v45.db"))
    monkeypatch.setattr(database, "MIGRATIONS", [m for m in database.MIGRATIONS if m[0] <= 45])
    database.init_sessions_db()
    from db import models as db

    sid = db.create_session()
    with database.connect_sessions() as conn:
        assert "reasoning" not in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', 'old row')", (sid,))

    monkeypatch.undo()
    monkeypatch.setattr("config.settings.db_path", str(tmp_path / "v45.db"))
    database.init_sessions_db()
    database.init_sessions_db()  # idempotent on the next boot
    with database.connect_sessions() as conn:
        assert "reasoning" in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        assert int(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]) >= 46
        old = conn.execute("SELECT content, reasoning FROM messages WHERE session_id=?", (sid,)).fetchone()
        assert tuple(old) == ("old row", None)


def test_v46_is_the_newest_migration_and_only_adds_a_column():
    from db.database import MIGRATIONS

    version, _, statements = next(m for m in MIGRATIONS if m[0] == 46)
    assert statements == ["ALTER TABLE messages ADD COLUMN reasoning TEXT"]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute(statements[0])  # plain SQLite, no extension needed


# ---------------------------------------------------------------------------
# Agent persistence helper and settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_answer_saves_reasoning_with_its_model(monkeypatch):
    from unittest.mock import AsyncMock

    from core import agent
    from core.llm.stream_ladder import StreamOutcome

    monkeypatch.setattr(
        agent,
        "stream_with_failover",
        AsyncMock(return_value=StreamOutcome(content="answer", reasoning="why", model=MODEL)),
    )
    monkeypatch.setattr(
        agent,
        "compile_context",
        lambda **kw: SimpleNamespace(messages=[], static_prefix_chars=0, effective_max_output=0),
    )
    monkeypatch.setattr(agent, "_acknowledge_delivery", AsyncMock())
    saved = AsyncMock()
    session = SimpleNamespace(emit_event=lambda e: None, touch=lambda: None, active_goal_id=None)
    await agent._stream_final_answer(
        session=session,
        session_id="s",
        client=SimpleNamespace(resolve_provider=lambda model: "test"),
        scout_text="",
        resource_status="",
        supports_vision=False,
        supports_audio=False,
        context_budget=1000,
        max_output=100,
        model=MODEL,
        turn_user_msg_id=None,
        save_turn_msg=saved,
        sched_created_at=0,
        sched_priority=0,
        tried_fallback=False,
        last_usage=None,
    )
    saved.assert_awaited_with("assistant", "answer", reasoning="why", reasoning_model=MODEL)
