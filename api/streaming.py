"""Pernix — SSE streaming utilities.

event_stream — persistent listener (GET /sessions/{id}/events), survives across turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from starlette.responses import StreamingResponse

from core.events import queue_was_dropped
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


async def event_stream(session: AgentSession, last_event_id: int | None = None):
    """Persistent event stream for GET /sessions/{id}/events.

    Stays open across turns. Does NOT break on done/error.
    Sends heartbeat after 30s of silence.
    Supports Last-Event-ID reconnection.
    Exits cleanly on server shutdown via shutdown event.

    `last_event_id` is three-valued, and the three are NOT interchangeable:

      None  no replay requested. The caller holds no boundary it trusts, so
            it is asking to be joined to the live stream and nothing else.
      0     cursor zero: "I have seen nothing, send everything still
            retained". Answerable only while the buffer still holds the very
            first event — otherwise the reply is a partial history that looks
            complete, which is the failure this signalling exists to name.
      N     everything after N.

    Collapsing None and 0 into a single int (what the old signature did) is
    what left an initial connection with no way to ask for replay at all: the
    client had a boundary from /status, passed it, and the server read the
    absent-cursor default as "nothing to replay" and skipped the branch.

    Whenever a cursor IS supplied the stream opens with a `stream.resume`
    control frame stating what was actually replayed, so the client can tell
    a clean resume from a server restart (server_seq below the cursor) and
    from an expired buffer (complete=false) instead of assuming continuity it
    does not have.
    """
    queue = session.subscribe()
    shutdown = get_shutdown_event()

    try:
        # Replay buffered events if reconnecting.
        #
        # Snapshot first, then yield. `session.events` is a live deque the
        # agent appends to under `_event_lock`, and a yield inside the loop
        # suspends the generator: a reconnect during an active turn with
        # enough backlog to fill uvicorn's write buffer would hand control
        # back to the loop mid-iteration and the next step raised
        # "RuntimeError: deque mutated during iteration". The stream died,
        # EventSource retried, and the same reconnect failed again.
        if last_event_id is not None:
            with session._event_lock:
                oldest_retained = session.events[0].get("_seq", 0) if session.events else 0
                server_seq = session.event_seq
                backlog = [e for e in session.events if e.get("_seq", 0) > last_event_id]
            # Sequence numbers start at 1, so 0 here is the empty-ring
            # sentinel, not a real event: either nothing was ever emitted, or
            # the session was reaped from memory and rebuilt with a fresh
            # buffer. Only the first of those is a complete answer to a cursor.
            if oldest_retained == 0:
                complete = server_seq <= last_event_id
            else:
                # The ring still reaches back to (or past) the event right
                # after the cursor, so the backlog is the whole gap.
                complete = oldest_retained <= last_event_id + 1
            yield sse_event(
                "stream.resume",
                {
                    # Restated in the payload as well as the SSE event name so
                    # the backend↔frontend listener audit
                    # (tests/test_sse_event_sync.py) can see this emitter.
                    "type": "stream.resume",
                    "session_id": session.session_id,
                    "from_seq": last_event_id,
                    "replayed": len(backlog),
                    "oldest_retained": oldest_retained,
                    "server_seq": server_seq,
                    "complete": complete,
                },
            )
            for event in backlog:
                yield sse_event(event.get("type", "message"), _clean_event(event), event_id=event.get("_seq", 0))
            if backlog:
                logger.debug("Replayed %d events for session %s", len(backlog), session.session_id)

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
                if queue_was_dropped(queue):
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

            # Detached for being too slow: end the response so EventSource
            # reconnects and replays from Last-Event-ID, instead of holding
            # a live-looking connection that will never carry another event.
            if queue_was_dropped(queue):
                return

    except (asyncio.CancelledError, GeneratorExit):
        pass
    finally:
        session.unsubscribe(queue)


def _clean_event(event: dict) -> dict:
    """Remove internal fields before sending to client.

    Exposes _seq as 'seq' for client-side dedup on reconnection.
    """
    cleaned = {k: v for k, v in event.items() if not k.startswith("_")}
    if "_seq" in event:
        cleaned["seq"] = event["_seq"]
    return cleaned
