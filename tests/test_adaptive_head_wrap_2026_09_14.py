"""Live audit 2026-09-14 / L04: the Self-checks toolbar overlapped its own
buttons in the desktop Explorer.

`.adaptive-head` is `display:flex` with no wrap. Wrapping was added for the
narrow panel — but gated on `body[data-compact]` and `body[data-touch]`,
because that is where a 360px Explorer was assumed to happen. The DOCKED
desktop Explorer is the same 360px and sets neither attribute, so on a plain
desktop browser the heartbeat chip and the four buttons were squeezed into one
line that could not wrap, and overlapped. The rule is unconditional now.

Whether two boxes overlap is a question for a layout engine, so the real
check is a new ui-gate pass, `adaptive_head_wrap` — it opens the docked
Explorer at 1280px on the Self-checks, Learning and Goals tabs and asserts
every `.adaptive-head` computes `flex-wrap: wrap`, that no two children
intersect, and that none of them runs past the head's right edge. What is
pinned here is the shape of the rule that produces it: a stylesheet that grows
a `body[data-compact]`-only wrap again would pass the gate on the tier it was
written for and fail the desktop exactly as before.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CSS = REPO_ROOT / "static" / "css" / "file-panel.css"
MODALS = REPO_ROOT / "static" / "js" / "components" / "modals"


def _block(css: str, selector: str) -> str:
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{(.*?)\n\}", css, re.S)
    assert m, f"{selector} block not found"
    return m.group(1)


def test_the_head_wraps_on_every_tier():
    """The one rule, ungated. `flex-wrap` is what stops the overlap; the
    `gap`/`row-gap` pair is what keeps a wrapped line readable."""
    css = CSS.read_text()
    head = _block(css, ".adaptive-head")
    assert "display: flex" in head
    assert "flex-wrap: wrap" in head
    assert "row-gap" in head


def test_the_children_keep_their_own_size():
    """Without this the row does not overlap, it crushes: flex shrinks every
    button until its label is unreadable."""
    css = CSS.read_text()
    kids = _block(css, ".adaptive-head > *")
    assert "flex: 0 0 auto" in kids
    assert "max-width: 100%" in kids


def test_no_tier_gated_copy_of_the_rule_is_left():
    """The bug was not a missing rule, it was a rule attached to two of the
    three tiers that have a 360px panel."""
    css = CSS.read_text()
    for gate in ("body[data-compact] .adaptive-head", "body[data-touch] .adaptive-head"):
        assert gate not in css, f"{gate} is redundant now and hides the unconditional rule"


def test_every_self_tuning_head_uses_the_class():
    """Learning, Self-checks, Goals and Trust all build their toolbar this
    way, so one rule fixes and one rule breaks all four."""
    used = {p.name for p in MODALS.glob("*.js") if "class: 'adaptive-head'" in p.read_text()}
    assert used == {"adaptive.js", "canary.js", "telos.js", "trust.js"}
