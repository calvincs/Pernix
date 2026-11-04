"""2026-11-04: typing lagged because the sidebar rebuilt 408 archived rows.

Attached to the user's own Chrome, the session list held 595 rows and 11k
elements; 408 of the rows were the Archived group, which — unlike the time and
space groups — had no row cap and built its rows even while folded. Every
rebuild (each ten-second poll whose payload changed, i.e. whenever any session
was working) blocked the main thread for 158-185 ms, and a keyup in the
composer measured 222 ms of input delay behind one.

The Archived group now builds nothing while folded and, when open, shows the
first TIME_GROUP_ROW_CAP rows behind the same "Show all N" button the time
groups use.
"""

from __future__ import annotations

import re

from tests.js_harness import STATIC_JS

SRC = (STATIC_JS / "components" / "sidebar.js").read_text()


def _archived_group() -> str:
    m = re.search(r"function _renderArchivedGroup\(.*?\n}\n", SRC, re.S)
    assert m, "_renderArchivedGroup not found"
    return m.group(0)


def test_a_folded_archive_builds_no_rows():
    body = _archived_group()
    ret = body.index("if (collapsed) return;")
    assert ret < body.index("_renderSessionItem(")
    # Opening it must repaint, since the folded body is empty.
    assert "if (!isCollapsed) _repaint();" in body


def test_an_open_archive_is_capped_like_a_time_group():
    body = _archived_group()
    assert "i < TIME_GROUP_ROW_CAP" in body
    assert "s.id === activeSid" in body, "the open session keeps its row"
    assert "_saveTimeShowAll(ARCHIVED_GROUP" in body
    # No uncapped loop left behind.
    assert "for (const s of _archived) {\n        _renderSessionItem" not in body
