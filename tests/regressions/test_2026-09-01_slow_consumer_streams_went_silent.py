"""A dropped slow subscriber kept a live-looking, permanently silent stream.

When a subscriber queue filled (backgrounded phone mid-turn), the producer
removed it from the subscriber list — but the SSE generator was still
awaiting that detached queue and still emitting 30s heartbeats, so the
browser saw a healthy connection carrying nothing. The session stream had
a client-side staleness watchdog; the notification bell and the jobs
indicator did not, so they stayed dead until a reload.

The generator now ends the response, which lets EventSource reconnect and
replay.
"""

import asyncio

from api.streaming import event_stream
from core.events import mark_queue_dropped, queue_was_dropped
from sessions.state import AgentSession


def test_marking_a_queue_is_visible_to_the_reader():
    q: asyncio.Queue = asyncio.Queue()
    assert not queue_was_dropped(q)
    mark_queue_dropped(q)
    assert queue_was_dropped(q)


async def test_full_subscriber_queue_is_marked_when_dropped():
    session = AgentSession(session_id="s-slow")
    q = session.subscribe()
    for i in range(q.maxsize):
        q.put_nowait({"type": "filler", "_seq": i})

    session._deliver_to_subscribers({"type": "stream.token", "_seq": 999})

    assert q not in session.subscribers, "a full queue is detached from the producer"
    assert queue_was_dropped(q), "and flagged so its reader can stop"


async def test_stream_ends_instead_of_heartbeating_into_the_void():
    session = AgentSession(session_id="s-end")
    gen = event_stream(session)

    # event_stream subscribes on its first advance, so prime it with an
    # event it will deliver, then take the queue it registered.
    first = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)
    queue = session.subscribers[0]
    queue.put_nowait({"type": "stream.token", "content": "last one", "_seq": 7})
    out = await first
    assert "last one" in out, "whatever was already buffered still reaches the client"

    # Once flagged, the very next resume ends the response rather than
    # settling into a 30s heartbeat on a queue nothing writes to.
    mark_queue_dropped(queue)
    queue.put_nowait({"type": "stream.token", "content": "after drop", "_seq": 8})
    ended = False
    try:
        await asyncio.wait_for(gen.__anext__(), timeout=1)
    except StopAsyncIteration:
        ended = True
    except asyncio.TimeoutError:
        ended = False
    await gen.aclose()
    assert ended, "the generator must return so EventSource reconnects"
