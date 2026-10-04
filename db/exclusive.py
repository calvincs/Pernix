"""Pernix — the admission protocol for database-exclusive maintenance.

`VACUUM` rebuilds the whole database file under the writer lock. Nothing in
the codebase used to say so out loud: `/api/storage/optimize` asked the
session manager whether a turn was in flight, and then — several `await`s
later, on a thread it could no longer see — took the lock. Between the
question and the answer there was no state anywhere recording that a rebuild
was about to happen, so a second `/optimize` was admitted, a cancelled HTTP
request left a vacuum running with nothing marking it, and every writer that
is not an agent session (this module's own callers, the WAL checkpoint, the
backup snapshot, the retention prunes, 237 `connect_sessions()` sites) was
invisible to the check and to each other.

This is the missing state. One process-wide gate with two roles:

  * :meth:`_DatabaseGate.writing` — a background writer announcing itself for
    the duration of its work. Cheap, re-entrant across threads, and bounded:
    a writer that arrives while an exclusive operation holds the gate waits
    only as long as it said it would and then raises, so a duty defers rather
    than piling up behind a rebuild.

  * :meth:`_DatabaseGate.claim` / :meth:`drain` / :meth:`release` — the
    exclusive phase, split into three calls on purpose. `claim` is
    synchronous and never waits, so an async caller can take the slot on the
    event loop *before* handing the actual work to a thread; `drain` blocks
    and therefore belongs on that thread; `release` is the thread's job in a
    `finally`. Cancelling the request that started it all cannot release a
    gate the request no longer holds a reference to.

WHAT THIS IS NOT

It is advisory, and deliberately so. Agent turns do not participate: making a
user's next message wait on a rebuild would trade a measurable stall for an
unbounded one. What protects a running turn is SQLite's own `busy_timeout`
(5000 ms, db/database.py), and the measurement says that is enough at the
sizes Pernix actually runs at — at 178 MB a full `VACUUM` holds the writer
lock 0.70 s and a concurrent writer simply waits it out; the timeout is not
reached until the file is past 1.2 GB. The gate exists for the writers that
CAN run long — a `VACUUM INTO` of the whole file, a checkpoint of a large WAL,
an incremental vacuum — and for the one invariant the old code had no way to
state at all: only one exclusive operation at a time, held until the work
itself is done rather than until its caller stops listening.
"""

from __future__ import annotations

import itertools
import threading
import time
from contextlib import contextmanager

# How long a background writer waits for an exclusive phase to end before it
# gives up and defers its own work. Short on purpose: the caller is a
# maintenance duty with a next attempt an hour away, not a user request.
DEFAULT_WRITER_WAIT = 5.0

# How long an exclusive operation waits for announced writers to finish
# before it abandons the attempt and hands the slot back. Long enough for a
# WAL checkpoint or a snapshot of a large file, short enough that an operator
# pressing a button gets an answer.
DEFAULT_DRAIN_TIMEOUT = 60.0


class MaintenanceBusy(RuntimeError):
    """The gate could not be had: someone else holds it, or writers stayed.

    Carries the names rather than a bare message because both callers render
    it to a person — the API as a 409 detail, the daily tier as the reason a
    duty deferred — and "something else is running" is not an answer anyone
    can act on.
    """

    def __init__(self, reason: str, *, holder: str | None = None, writers: tuple[str, ...] = ()):
        self.reason = reason
        self.holder = holder
        self.writers = writers
        super().__init__(reason)


class _DatabaseGate:
    """Process-wide admission state for the sessions database.

    One condition variable guards both roles. `_holder` is set for the whole
    exclusive phase — from the synchronous claim through the drain and the
    work itself — and while it is set no new writer is admitted.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._holder: str | None = None
        self._holder_since: float = 0.0
        self._writers: dict[int, str] = {}
        self._tokens = itertools.count(1)

    # -- background writers ------------------------------------------------

    @contextmanager
    def writing(self, label: str, wait: float = DEFAULT_WRITER_WAIT):
        """Announce a background write for the length of the `with` body.

        Raises :class:`MaintenanceBusy` if an exclusive operation is holding
        the gate and has not finished within `wait` seconds. Callers treat
        that as "not now" — the work stays eligible and is attempted again —
        never as a failure of the work itself.
        """
        token = self._enter_write(label, wait)
        try:
            yield
        finally:
            self._leave_write(token)

    def _enter_write(self, label: str, wait: float) -> int:
        deadline = time.monotonic() + max(0.0, wait)
        with self._cv:
            while self._holder is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MaintenanceBusy(
                        f"{label} deferred: {self._holder} holds the database",
                        holder=self._holder,
                    )
                self._cv.wait(remaining)
            token = next(self._tokens)
            self._writers[token] = label
            return token

    def _leave_write(self, token: int) -> None:
        with self._cv:
            self._writers.pop(token, None)
            self._cv.notify_all()

    # -- the exclusive phase -----------------------------------------------

    def claim(self, label: str) -> None:
        """Take the exclusive slot, or raise. Never blocks.

        Split out of :meth:`exclusive` so an async caller can claim on the
        event loop and only then hand the blocking part to a thread. The slot
        is held from this moment: two `/optimize` requests arriving together
        cannot both get past it, and the second one gets a 409 naming the
        first rather than a second `VACUUM`.
        """
        with self._cv:
            if self._holder is not None:
                raise MaintenanceBusy(
                    f"{self._holder} is already running",
                    holder=self._holder,
                )
            self._holder = label
            self._holder_since = time.monotonic()

    def drain(self, timeout: float = DEFAULT_DRAIN_TIMEOUT) -> None:
        """Wait for announced writers to finish. Blocks — call off the loop.

        Raising here leaves the slot held: whoever claimed it is still the
        one who must release it, and a `drain` that tidied up on its own
        behalf could hand the gate back to a different claimant a moment
        before the first one's `finally` released it again.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            while self._writers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stuck = tuple(sorted(set(self._writers.values())))
                    raise MaintenanceBusy(
                        f"{self._holder} deferred: background writes still running ({', '.join(stuck)})",
                        holder=self._holder,
                        writers=stuck,
                    )
                self._cv.wait(remaining)

    def release(self) -> None:
        """Hand the slot back. Idempotent — a double release is not an error."""
        with self._cv:
            self._holder = None
            self._holder_since = 0.0
            self._cv.notify_all()

    @contextmanager
    def exclusive(self, label: str, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT):
        """claim + drain + release, for a caller already on a thread."""
        self.claim(label)
        try:
            self.drain(drain_timeout)
            yield
        finally:
            self.release()

    def wait_until_free(self, timeout: float = DEFAULT_DRAIN_TIMEOUT) -> bool:
        """Block until no exclusive operation holds the gate. Blocks — off-loop.

        For a caller that wants to know when the rebuild the request it
        cancelled is still running has actually finished. Returns False on
        timeout rather than raising: "still busy" is an answer, not a fault.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            while self._holder is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
            return True

    # -- observability -----------------------------------------------------

    def status(self) -> dict:
        """Who holds the gate and who is writing, for /api/health and 409s."""
        with self._cv:
            return {
                "exclusive": self._holder,
                "exclusive_for_seconds": (round(time.monotonic() - self._holder_since, 3) if self._holder else None),
                "writers": sorted(self._writers.values()),
            }


_gate = _DatabaseGate()


def get_gate() -> _DatabaseGate:
    """The process-wide gate. One database, one gate."""
    return _gate


def writing(label: str, wait: float = DEFAULT_WRITER_WAIT):
    """Module-level shorthand for ``get_gate().writing(...)``."""
    return _gate.writing(label, wait)


def exclusive(label: str, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT):
    """Module-level shorthand for ``get_gate().exclusive(...)``."""
    return _gate.exclusive(label, drain_timeout)


def status() -> dict:
    """Module-level shorthand for ``get_gate().status()``."""
    return _gate.status()
