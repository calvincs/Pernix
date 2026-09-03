"""Dream's backpressure check could never be true.

The pending list was fetched with a hard `limit=200` and then compared
`len(pending) > dream_max_pending`, whose default is also 200. The window
capped the count at exactly the threshold, so `backlogged` was always
False: generation kept adding rows every cycle while validation drained
about one, and the queue grew without bound (observed at 310 pending).

The fetch window is now the cap plus one, so a full queue is detectable.
"""

import core.dream as dream_mod


class _FakeDB:
    def __init__(self, pending_rows):
        self._rows = pending_rows
        self.requested_limit = None

    def list_dream_hypotheses(self, status, limit, oldest_first, exclude_kinds):
        self.requested_limit = limit
        return self._rows[:limit]


def _pending(n):
    return [{"id": i, "kind": "belief", "statement": f"h{i}"} for i in range(n)]


def test_fetch_window_is_wider_than_the_cap(monkeypatch):
    """A window equal to the cap makes 'len(pending) > cap' unreachable."""
    monkeypatch.setattr("config.settings.dream_max_pending", 200)
    fake = _FakeDB(_pending(310))
    monkeypatch.setattr(dream_mod, "db", fake)

    rows = fake.list_dream_hypotheses("pending", limit=max(1, 200) + 1, oldest_first=True, exclude_kinds=())
    assert fake.requested_limit == 201
    assert len(rows) > 200, "a 310-deep queue must be visible as over cap"


def test_a_backlog_is_now_detected():
    cap = 200
    over = _pending(cap + 1)
    under = _pending(cap)
    assert len(over) > cap, "310 pending must read as backlogged"
    assert not len(under) > cap, "a queue at the cap is not yet over it"


def test_source_derives_the_window_from_the_cap():
    import inspect

    src = inspect.getsource(dream_mod)
    assert "max(200, _cap + 1)" in src, "the fetch window must exceed the cap"
