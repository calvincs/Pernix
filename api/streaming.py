"""Pernix — SSE streaming utilities.

Two stream functions:
1. event_stream — persistent listener (GET /sessions/{id}/events), survives across turns
2. event_stream_from_queue — request-scoped (POST /chat), breaks on done/error
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from starlette.responses import StreamingResponse

from sessions.state import AgentSession

logger = logging.getLogger("pernix.api.streaming")

HEARTBEAT_INTERVAL = 30  # seconds

# Shutdown signal — set during lifespan shutdown to break all SSE loops
_shutdown_event: asyncio.Event | None = None
_shutdown_init_lock = threading.Lock()


def get_shutdown_event() -> asyncio.Event:
    """Get or create the shutdown event (must be called from async context)."""
    global _shutdown_event
    with _shutdown_init_lock:
        if _shutdown_event is None:
            _shutdown_event = asyncio.Event()
        return _shutdown_event


def signal_shutdown() -> None:
    """Signal all SSE generators to stop. Called from lifespan shutdown."""
    if _shutdown_event is not None:
        _shutdown_event.set()


def sse_event(event_type: str, data: dict | str, event_id: int | None = None) -> str:
    """Format a single SSE event with optional id for reconnection."""
    parts = []
    if event_id is not None:
        parts.append(f"id: {event_id}")
    parts.append(f"event: {event_type}")
    if isinstance(data, dict):
        data = json.dumps(data)
    parts.append(f"data: {data}")
    return "\n".join(parts) + "\n\n"


def sse_response(generator) -> StreamingResponse:
    """Wrap an async generator as an SSE StreamingResponse."""
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def event_stream(session: AgentSession, last_event_id: int = 0):
    """Persistent event stream for GET /sessions/{id}/events.

    Stays open across turns. Does NOT break on done/error.
    Sends heartbeat after 30s of silence.
    Supports Last-Event-ID reconnection.
    Exits cleanly on server shutdown via shutdown event.
    """
    queue = session.subscribe()
    shutdown = get_shutdown_event()

    try:
        # Replay buffered events if reconnecting
        if last_event_id > 0:
            replayed = 0
            for event in session.events:
                seq = event.get("_seq", 0)
                if seq > last_event_id:
                    event_type = event.get("type", "message")
                    yield sse_event(event_type, _clean_event(event), event_id=seq)
                    replayed += 1
            if replayed:
                logger.debug("Replayed %d events for session %s", replayed, session.session_id)

        # Stream live events. Use asyncio.timeout() instead of wait_for() —
        # the latter wraps queue.get() in an inner Task that wasn't reliably
        # cancelled when the outer generator was closed by the client (e.g.
        # browser disconnect on /api/sessions/{id}/events). The orphaned
        # Task showed up as "Task was destroyed but it is pending!" GC noise
        # in the log — frequent enough to be annoying though never fatal.
        # asyncio.timeout() (PEP 661 / Py3.11+) propagates cancellation
        # through the context cleanly so no inner task leaks on disconnect.
        while not shutdown.is_set():
            try:
                async with asyncio.timeout(HEARTBEAT_INTERVAL):
                    event = await queue.get()
            except asyncio.TimeoutError:
                if shutdown.is_set():
                    return
                yield ": heartbeat\n\n"
                continue

            event_type = event.get("type", "message")
            if event_type == "_heartbeat":
                continue
            if event_type == "_shutdown":
                return

            seq = event.get("_seq")
            yield sse_event(event_type, _clean_event(event), event_id=seq)

    except (asyncio.CancelledError, GeneratorExit):
        pass
    finally:
        session.unsubscribe(queue)


async def event_stream_from_queue(
    queue: asyncio.Queue,
    session: AgentSession | None = None,
):
    """Request-scoped event stream for POST /chat and POST /retry.

    Breaks on 'stream.done' or 'stream.error' events.
    Unsubscribes the queue from the session on completion.
    Exits cleanly on server shutdown.
    """
    shutdown = get_shutdown_event()
    try:
        # See event_stream_for_session for why asyncio.timeout() over wait_for().
        while not shutdown.is_set():
            try:
                async with asyncio.timeout(HEARTBEAT_INTERVAL):
                    event = await queue.get()
            except asyncio.TimeoutError:
                if shutdown.is_set():
                    return
                yield ": heartbeat\n\n"
                continue

            event_type = event.get("type", "message")
            if event_type == "_heartbeat":
                continue
            if event_type == "_shutdown":
                return

            seq = event.get("_seq")
            yield sse_event(event_type, _clean_event(event), event_id=seq)

            # Break on terminal events
            if event_type in ("stream.done", "stream.error"):
                return

    except (asyncio.CancelledError, GeneratorExit):
        pass
    finally:
        if session:
            session.unsubscribe(queue)


def _clean_event(event: dict) -> dict:
    """Remove internal fields before sending to client.

    Exposes _seq as 'seq' for client-side dedup on reconnection.
    """
    cleaned = {k: v for k, v in event.items() if not k.startswith("_")}
    if "_seq" in event:
        cleaned["seq"] = event["_seq"]
    return cleaned
