"""Document ingest drove the shared LLM client from a second event loop.

ingest_document_sync runs on a tool thread and called asyncio.run(), which
builds its own event loop. ingest_document then drove the process-wide
httpx client and the LLM scheduler — objects created on the MAIN loop,
whose futures and connection pools are not loop-portable. A contended
acquire could park a future on a loop that never resolves it (a 120s
hang), and a reused keep-alive socket raises "attached to a different
loop". Every other tool marshals back to the main loop; this one did not.
"""

import asyncio
import inspect

import pytest

import core.events as events
from core.memory import ingest as ingest_mod


def test_it_prefers_the_main_loop():
    src = inspect.getsource(ingest_mod.ingest_document_sync)
    assert "run_coroutine_threadsafe" in src
    assert "_main_loop" in src


def test_a_tool_thread_reaches_the_main_loop(monkeypatch):
    """The call is made from a worker thread, as the tool does."""
    seen = {}

    async def fake_ingest(text, source_name, min_section_length, use_llm):
        seen["loop"] = asyncio.get_running_loop()
        return {"ok": True}

    monkeypatch.setattr(ingest_mod, "ingest_document", fake_ingest)

    async def main():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(events, "_main_loop", loop)
        result = await asyncio.to_thread(ingest_mod.ingest_document_sync, "some text")
        return loop, result

    main_loop, result = asyncio.run(main())
    assert result == {"ok": True}
    assert seen["loop"] is main_loop, "the shared client must be driven from the loop that owns it"


def test_it_still_works_with_no_main_loop(monkeypatch):
    """Tests and CLI use have no recorded loop and no shared client."""
    monkeypatch.setattr(events, "_main_loop", None)

    async def fake_ingest(text, source_name, min_section_length, use_llm):
        return {"sections": 1}

    monkeypatch.setattr(ingest_mod, "ingest_document", fake_ingest)
    assert ingest_mod.ingest_document_sync("text") == {"sections": 1}


def test_a_closed_main_loop_falls_back(monkeypatch):
    dead = asyncio.new_event_loop()
    dead.close()
    monkeypatch.setattr(events, "_main_loop", dead)

    async def fake_ingest(text, source_name, min_section_length, use_llm):
        return {"sections": 2}

    monkeypatch.setattr(ingest_mod, "ingest_document", fake_ingest)
    assert ingest_mod.ingest_document_sync("text") == {"sections": 2}
