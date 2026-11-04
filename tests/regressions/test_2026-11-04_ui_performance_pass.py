"""2026-11-04 performance pass, measured in a real browser on a 214-session install.

- The list endpoint shipped each session's system prompt: 250 KB of a 425 KB
  page that every tab re-fetched every ten seconds, never read by the UI.
- Opening a 198-message session spent 244 ms in scrollToBottom: every
  replayed message read scrollHeight, a full layout of the growing transcript.
- The sidebar rebuilt in full inside the click that opened a session, and
  every rebuild emptied the list first, so restoring its scroll position
  forced another layout (55 ms).
- Each timestamp built a fresh Intl.DateTimeFormat (30 ms on that open).
"""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient

from db import models as db
from tests.js_harness import STATIC_JS

APP = (STATIC_JS / "app.js").read_text()
SIDEBAR = (STATIC_JS / "components" / "sidebar.js").read_text()


def _fn(src: str, head: str) -> str:
    m = re.search(re.escape(head) + r".*?\n}\n", src, re.S)
    assert m, head
    return m.group(0)


@pytest.mark.asyncio
async def test_the_list_leaves_out_the_system_prompt():
    from fastapi import FastAPI

    from api.routers import sessions

    app = FastAPI()
    app.include_router(sessions.router)
    sid = db.create_session(title="t", system_prompt="X" * 5000)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        page = (await c.get("/api/sessions?limit=50")).json()
        row = next(s for s in page["items"] if s["id"] == sid)
        assert "system_prompt" not in row
        # The single-session read still has it.
        one = (await c.get(f"/api/sessions/{sid}")).json()
        assert "X" * 100 in str(one)


def test_scroll_to_bottom_does_nothing_during_a_replay():
    body = _fn(APP, "function scrollToBottom(")
    first = body.index("if (_replayingTranscript) return;")
    assert first < body.index("scroll.scrollTop = scroll.scrollHeight")


def test_timestamps_reuse_one_formatter():
    body = _fn(APP, "function _msgTimestamp(")
    assert "toLocale" not in body
    assert "_MSG_TIME_FMT.format" in body
    assert "toLocaleDateString(undefined, { month: 'short', day: 'numeric' })" not in _fn(
        SIDEBAR, "function _relativeTime("
    )


def test_a_session_switch_moves_the_highlight_and_defers_the_rebuild():
    body = _fn(SIDEBAR, "export function renderSessionList(")
    assert "base === _lastBase && !_switchRebuild" in body
    assert "if (_switchRebuild !== token) return;" in body, "a stale deferred rebuild must stand down"


def test_every_forced_repaint_clears_both_fingerprints():
    # A reset of _lastJson alone would let the fast path skip a rebuild that
    # a collapse, Show all or search exit asked for.
    for line in SIDEBAR.splitlines():
        if "_lastJson = '';" in line and not line.startswith("let "):
            assert "_lastBase = '';" in line, line


def test_the_list_is_swapped_in_whole_rather_than_emptied_first():
    body = _fn(SIDEBAR, "export function renderSessionList(")
    assert "document.createDocumentFragment()" in body
    assert "liveList.replaceChildren(list);" in body
    assert "clear(list)" not in body
    assert "list.scrollTop = scrollTop" not in body
