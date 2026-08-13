"""Invariant tests for the backend↔frontend SSE event-name seam.

The browser's EventSource API dispatches an event *only* to a listener
registered under its exact `event:` name. There is no wildcard listener, so
a client cannot fail open: any event type the server emits but the client
never registers is dropped before JS sees it. Worse, `seq` rides on the
event payload, so a dropped event also stalls the client's `_lastSeq` —
the next event that *is* subscribed looks like a sequence gap and triggers
a spurious soft reload of the whole transcript.

That makes the listener lists a hand-maintained mirror of the emitter set,
with no runtime safety net. These tests are the safety net: they grep the
source tree the way tests/test_state_machine_invariants.py does, and fail
when the two sides drift.

Three client streams consume server events, each with its own listener list:
  static/js/sse.js                          → /api/sessions/{id}/events
  static/js/components/jobs-indicator.js    → /api/jobs/events
  static/js/notifications.js                → /api/notifications/events

An event is considered handled if *any* of the three registers it, because
several types (snooze.*, dialog.*) are deliberately fanned out
to more than one stream by manager.broadcast().
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "static"

# Only dotted names ("stream.token", "rlm.done") are scanned. Undotted "type"
# keys are overwhelmingly JSON-schema fragments, tool-call envelopes and
# RLM trace records ("string", "object", "file", "cell", "subcall"), none of
# which ever reach an SSE stream. Every event type the UI subscribes to is
# dotted, so requiring the dot keeps this scan precise without an allowlist
# hundreds of entries long.
_TYPE_LITERAL = re.compile(r'"type":\s*"([a-z][a-z0-9_]*\.[a-z0-9_.]+)"')

# Packages that can reach a client SSE stream.
_SCANNED_PACKAGES = ("core", "sessions", "api")

# Dotted "type" literals that are NOT SSE events. Keep this list short and
# justified — every entry is a place the scan would otherwise false-positive.
_NOT_SSE_EVENTS = {
    # ASGI protocol messages in the request-logging middleware, not app events.
    "http.response.start",
    "http.response.body",
}

# Types the client registers that no Python emitter produces. Each needs a
# reason; an unjustified entry here is a dead listener.
_CLIENT_ONLY_EVENTS = {
    # Synthesised inside sse.js and handed to the app's event handler so the
    # transport can report its own state through the same code path.
    "sse.reconnected",
    "sse.session_gone",
    # Legacy alias kept alongside dialog.question. Harmless to keep listening
    # for (an unused listener costs nothing) and cheap insurance against an
    # older server on the other end of a remote/LAN client.
    "user_question",
}


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


def _emitted_event_types() -> dict[str, set[str]]:
    """Map dotted event type → set of files that emit it."""
    found: dict[str, set[str]] = {}
    for package in _SCANNED_PACKAGES:
        for path in (REPO_ROOT / package).rglob("*.py"):
            for match in _TYPE_LITERAL.finditer(path.read_text(errors="ignore")):
                name = match.group(1)
                if name in _NOT_SSE_EVENTS:
                    continue
                found.setdefault(name, set()).add(str(path.relative_to(REPO_ROOT)))
    return found


def _js_string_list(source: str, marker: str) -> set[str]:
    """Extract the single-quoted strings from the array literal following
    `marker`, ignoring // comments (the lists are heavily annotated)."""
    body = source.split(marker, 1)[1].split("]", 1)[0]
    body = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r"'([^'\n]+)'", body))


def _session_stream_listeners() -> set[str]:
    source = (STATIC / "js" / "sse.js").read_text()
    return _js_string_list(source, "const EVENT_TYPES = [")


def _jobs_stream_listeners() -> set[str]:
    source = (STATIC / "js" / "components" / "jobs-indicator.js").read_text()
    return _js_string_list(source, "for (const type of [")


def _notification_stream_listeners() -> set[str]:
    source = (STATIC / "js" / "notifications.js").read_text()
    return set(re.findall(r"_globalSource\.addEventListener\('([^']+)'", source))


def _all_client_listeners() -> set[str]:
    return _session_stream_listeners() | _jobs_stream_listeners() | _notification_stream_listeners()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_finds_the_known_emitters():
    """Guard the guard: if the regex or the layout changes so that nothing
    matches, every other test here would pass vacuously."""
    emitted = _emitted_event_types()
    assert len(emitted) > 50, f"event scan collapsed to {len(emitted)} types"
    for sentinel in ("stream.token", "tool.start", "session.state_changed"):
        assert sentinel in emitted, f"{sentinel} not found — scan is broken"


def test_every_emitted_event_has_a_client_listener():
    """The load-bearing assertion. A server event with no listener is dropped
    by EventSource *and* stalls the client's seq tracking, which shows up as
    a spurious 'SSE gap detected' soft reload rather than as a missing
    feature — expensive to diagnose from the symptom.

    If this fails: add the event name to EVENT_TYPES in static/js/sse.js
    (per-session stream), or to the listener list in jobs-indicator.js /
    notifications.js if it belongs to one of the global streams.
    """
    emitted = _emitted_event_types()
    listeners = _all_client_listeners()
    missing = {name: sorted(files) for name, files in emitted.items() if name not in listeners}
    assert not missing, "server emits event types no client listens for:\n" + "\n".join(
        f"  {name}  (emitted in {', '.join(files)})" for name, files in sorted(missing.items())
    )


def test_no_dead_client_listeners():
    """The reverse drift: a listener for an event nothing emits any more.
    Harmless at runtime but it rots the list into noise, which is how the
    real gaps get missed."""
    emitted = set(_emitted_event_types())
    dead = _all_client_listeners() - emitted - _CLIENT_ONLY_EVENTS
    assert not dead, (
        "client registers listeners for event types no Python emitter produces: "
        f"{sorted(dead)} — remove them, or justify them in _CLIENT_ONLY_EVENTS"
    )


def test_client_only_events_are_still_client_only():
    """Keeps _CLIENT_ONLY_EVENTS honest: if a name in it starts being emitted
    by the server, the exemption is stale and must be removed."""
    emitted = set(_emitted_event_types())
    now_emitted = _CLIENT_ONLY_EVENTS & emitted
    assert not now_emitted, f"{sorted(now_emitted)} are now emitted by Python — drop them from _CLIENT_ONLY_EVENTS"


def test_not_sse_allowlist_entries_still_exist():
    """If an allowlisted non-SSE literal disappears, the exemption should go
    with it rather than silently masking a future event of the same name."""
    all_literals: set[str] = set()
    for package in _SCANNED_PACKAGES:
        for path in (REPO_ROOT / package).rglob("*.py"):
            all_literals.update(_TYPE_LITERAL.findall(path.read_text(errors="ignore")))
    stale = _NOT_SSE_EVENTS - all_literals
    assert not stale, f"_NOT_SSE_EVENTS entries no longer present in the source: {sorted(stale)}"


def test_session_stream_list_has_no_duplicates():
    """Duplicates mean the list was edited by copy-paste and the copy may have
    been meant to be a different name."""
    source = (STATIC / "js" / "sse.js").read_text()
    body = source.split("const EVENT_TYPES = [", 1)[1].split("]", 1)[0]
    body = re.sub(r"//[^\n]*", "", body)
    names = re.findall(r"'([^'\n]+)'", body)
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate entries in EVENT_TYPES: {sorted(dupes)}"
