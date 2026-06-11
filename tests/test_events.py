"""Tests for core/events.py: JobEventBus."""

import asyncio

import pytest

from core.events import JobEventBus, get_event_bus


def test_emit_adds_seq_and_timestamp():
    bus = JobEventBus()
    event = {"type": "test"}
    bus.emit(event)
    assert event["_seq"] == 1
    assert "timestamp" in event


def test_emit_preserves_existing_timestamp():
    bus = JobEventBus()
    event = {"type": "test", "timestamp": 12345}
    bus.emit(event)
    assert event["timestamp"] == 12345


def test_subscribe_receives_events():
    bus = JobEventBus()
    q = bus.subscribe()
    bus.emit({"type": "hello"})
    assert not q.empty()
    event = q.get_nowait()
    assert event["type"] == "hello"


def test_unsubscribe():
    bus = JobEventBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.emit({"type": "after_unsub"})
    assert q.empty()


def test_unsubscribe_idempotent():
    bus = JobEventBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.unsubscribe(q)  # Should not raise


def test_dead_subscriber_removed():
    bus = JobEventBus()
    q = bus.subscribe()
    # Fill the queue to capacity
    for i in range(500):
        bus.emit({"type": "fill", "i": i})
    # Queue is full. Next emit should remove the dead subscriber.
    bus.emit({"type": "overflow"})
    assert q not in bus._subscribers


def test_multiple_subscribers():
    bus = JobEventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    bus.emit({"type": "broadcast"})
    assert not q1.empty()
    assert not q2.empty()


def test_seq_increments():
    bus = JobEventBus()
    events = [{"type": "1"}, {"type": "2"}, {"type": "3"}]
    for e in events:
        bus.emit(e)
    assert [e["_seq"] for e in events] == [1, 2, 3]


def test_get_event_bus_singleton(monkeypatch):
    import core.events

    monkeypatch.setattr(core.events, "_bus", None)
    bus1 = get_event_bus()
    bus2 = get_event_bus()
    assert bus1 is bus2
