"""A reconnect replay iterated the live event deque across yields.

`event_stream` looped over `session.events` — the deque the agent appends
to while a turn runs — and yielded from inside the loop. Once the backlog
was big enough to fill uvicorn's write buffer the yield suspended the
generator, the agent appended, and the next iteration raised
"RuntimeError: deque mutated during iteration". The stream died mid-replay,
EventSource retried, and the same reconnect failed again.

The existing test only passed last_event_id=0, which skips the branch.
"""

from api.streaming import event_stream
from sessions.state import AgentSession


def _seed(session: AgentSession, n: int) -> None:
    for i in range(1, n + 1):
        session.events.append({"type": "stream.token", "content": f"tok{i}", "_seq": i})


async def test_replay_survives_an_append_between_yields():
    session = AgentSession(session_id="s-replay")
    _seed(session, 50)

    gen = event_stream(session, last_event_id=10)
    first = await gen.__anext__()
    assert "tok11" in first

    # The agent emits while the client is still draining the backlog.
    for i in range(51, 71):
        session.events.append({"type": "stream.token", "content": f"live{i}", "_seq": i})

    seen = [first]
    for _ in range(39):
        seen.append(await gen.__anext__())
    await gen.aclose()

    # The whole pre-reconnect backlog replayed, in order, with no crash.
    assert len(seen) == 40
    assert "tok50" in seen[-1]


async def test_replay_sends_only_events_after_the_last_seen_id():
    session = AgentSession(session_id="s-window")
    _seed(session, 5)
    gen = event_stream(session, last_event_id=3)
    out = [await gen.__anext__() for _ in range(2)]
    await gen.aclose()
    assert "tok4" in out[0] and "tok5" in out[1]
