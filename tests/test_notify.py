"""Tests for core/notify.py: NotificationDispatcher."""

import asyncio

import pytest

from core.events import JobEventBus
from core.notify import NotificationDispatcher, get_dispatcher


def _make_dispatcher() -> NotificationDispatcher:
    return NotificationDispatcher()


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


async def test_start_stop():
    disp = _make_dispatcher()
    disp.start()
    assert disp._task is not None
    assert not disp._task.done()
    await disp.stop()
    assert disp._task.done()


async def test_start_creates_task_and_queue():
    """Starting creates both a task and a queue."""
    disp = _make_dispatcher()
    disp.start()
    assert disp._task is not None
    assert disp._queue is not None
    await disp.stop()


# ---------------------------------------------------------------------------
# _process_events: event filtering
# ---------------------------------------------------------------------------


async def test_process_events_dispatches_dialog_question():
    disp = _make_dispatcher()
    received = []

    async def handler(event):
        received.append(event)

    disp.register_handler(handler)
    disp.start()

    # Inject event directly into queue
    await disp._queue.put({"type": "dialog.question", "question": "Are you ready?"})
    await asyncio.sleep(0.05)  # Let the loop process
    await disp.stop()

    assert len(received) == 1
    assert received[0]["type"] == "dialog.question"


async def test_process_events_dispatches_dialog_notification():
    disp = _make_dispatcher()
    received = []

    async def handler(event):
        received.append(event)

    disp.register_handler(handler)
    disp.start()

    await disp._queue.put({"type": "dialog.notification", "title": "Alert", "body": "done"})
    await asyncio.sleep(0.05)
    await disp.stop()

    assert len(received) == 1


async def test_process_events_ignores_other_events():
    disp = _make_dispatcher()
    received = []

    async def handler(event):
        received.append(event)

    disp.register_handler(handler)
    disp.start()

    await disp._queue.put({"type": "session.idle", "session_id": "abc"})
    await asyncio.sleep(0.05)
    await disp.stop()

    assert len(received) == 0


async def test_handler_exception_does_not_crash():
    """Handler exceptions should be logged, not crash the dispatcher."""
    disp = _make_dispatcher()

    async def bad_handler(event):
        raise RuntimeError("handler blew up")

    disp.register_handler(bad_handler)
    disp.start()

    await disp._queue.put({"type": "dialog.question", "question": "test"})
    await asyncio.sleep(0.05)
    await disp.stop()

    # Dispatcher should still be done (stopped), not crashed
    assert disp._task.done()


# ---------------------------------------------------------------------------
# get_dispatcher singleton
# ---------------------------------------------------------------------------


def test_get_dispatcher_singleton(monkeypatch):
    import core.notify

    monkeypatch.setattr(core.notify, "_dispatcher", None)
    d1 = get_dispatcher()
    d2 = get_dispatcher()
    assert d1 is d2
