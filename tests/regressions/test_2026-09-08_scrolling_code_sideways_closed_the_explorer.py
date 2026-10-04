"""Audit 3.2.2 / S22, 2026-09-08: scrolling a code block sideways on a phone
closed the Explorer out from under it.

`_insideScrollableCode` was consulted in exactly one place — the
`preventDefault()` call inside touchmove. `tracking` and `direction` were never
told, so the gesture that had just been let through as a native scroll still
arrived at touchend, was classified as a rightward swipe past 60 px, and
clicked `#files-btn`. The user got their horizontal scroll *and* a panel toggle.

Measured on the real handlers, Explorer open, a 200 px rightward swipe:

  over a scrollable <pre>          native scroll allowed, panel toggled  ✗
  over a .code-block-wrap child    native scroll allowed, panel toggled  ✗
  over a wide <table>              scroll BLOCKED, panel toggled         ✗✗
  over ordinary prose              panel toggled                         ✓ (wanted)

The table case was the double failure: touch.css gives
`.message.assistant .content table` `display:block; overflow-x:auto`, but the
guard only matched `.code-block-wrap` and `PRE`, so `preventDefault()` fired —
the table could not scroll — and the panel toggled anyway.

Four more things the filing did not name, all reproduced:

  * the OPENING direction has it too — Explorer closed, a leftward scroll
    starting inside the 25 px right-edge zone opens the panel on top of the
    code being read;
  * the sidebar drawer is the same forty lines with the same bug, and on a
    phone the code block's own left edge sits within a few pixels of the
    drawer's edge zone, so scrolling code back to the left — the common
    direction — is arguably reached more often than the Explorer case;
  * `_setupFilePanelSwipe` gates on `isTouch()`, not `isCompact()`, so at
    1180 px an iPad user swiping in the transcript *beside* a docked Explorer
    closes it;
  * neither gesture registered `touchcancel`, so an OS-interrupted swipe left
    `tracking` true and the next touch inherited it.

The fix decides once, at touchstart, and disarms the whole gesture rather than
one call inside it: `_insideHorizontalScroller` walks up from the actual touch
target and `_scrollsHorizontally` asks both whether the element really
overflows and whether its computed `overflow-x` lets it scroll — the second
half matters because a container that sets only `overflow-y: auto` computes an
`auto` overflow-x too, and #messages and .fp-tree both do. A scroller stays
exempt even at the end of its travel: a finger that came down on code is not
asking for the panel. Both handlers now share one implementation, so they
cannot drift apart again, and it clears tracking on touchcancel and on a
second finger.

check.sh runs no JavaScript and the UI gate has no swipe primitive (Playwright
exposes only `touchscreen.tap()`), so this pytest shells out to node, extracts
the real handlers from static/js/mobile.js by content anchor, and drives them
against a stub DOM. Physical iOS/Android verification is still outstanding and
is not claimed here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed — the browser-side harness cannot run")


HARNESS = r"""
import fs from 'node:fs';
import path from 'node:path';

const root = process.argv[2];
const SRC = fs.readFileSync(path.join(root, 'static/js/mobile.js'), 'utf8');

function must(i, what) {
    if (i < 0) throw new Error(`anchor not found in mobile.js: ${what}`);
    return i;
}

// A whole declaration, brace-matched. The signature anchor must include the
// opening brace of the BODY, so a destructured parameter list cannot be
// mistaken for it.
function decl(sig) {
    if (!sig.endsWith('{')) throw new Error(`signature anchor must end with {: ${sig}`);
    const i = must(SRC.indexOf(sig), sig);
    let depth = 0;
    for (let k = i + sig.length - 1; k < SRC.length; k++) {
        if (SRC[k] === '{') depth++;
        else if (SRC[k] === '}' && --depth === 0) return SRC.slice(i, k + 1);
    }
    throw new Error(`unbalanced braces after ${sig}`);
}

function line(prefix) {
    const i = must(SRC.indexOf(prefix), prefix);
    return SRC.slice(i, SRC.indexOf('\n', i));
}

const PIECES = [
    line('const HSCROLL_SLOP'),
    decl('function _scrollsHorizontally(el) {'),
    decl('function _insideHorizontalScroller(node) {'),
    decl('function _installSwipeGesture({ enabled, arm, complete }) {'),
    decl('function _setupSwipeGesture() {'),
    decl('function _setupFilePanelSwipe() {'),
    decl('function _within(el, x, y) {'),
];

for (const needle of ['touchcancel', 'touches.length > 1', '_insideHorizontalScroller(e.target)']) {
    if (!PIECES.join('\n').includes(needle)) {
        throw new Error(`extracted source is missing the guard for ${needle}`);
    }
}

const FACTORY = new Function('deps', `
"use strict";
const { document, window, isTouch, isCompact, openSidebar, closeSidebar } = deps;
${PIECES.join('\n\n')}
return { _setupSwipeGesture, _setupFilePanelSwipe, _scrollsHorizontally, _insideHorizontalScroller };
`);

// ── the smallest DOM these handlers actually touch ──────────────────────────

function makeScene({
    compact = true, touch = true, innerWidth = 390,
    panelOpen = false, drawerOpen = false, computedStyle = true,
} = {}) {
    const counts = { filesBtn: 0, openSidebar: 0, closeSidebar: 0, prevented: 0 };
    const byId = new Map();

    function node(tag, opts = {}) {
        let classes = opts.classes || [];
        const el = {
            nodeType: opts.nodeType ?? 1,
            tagName: tag.toUpperCase(),
            id: opts.id || null,
            parentElement: null,
            scrollWidth: opts.scrollWidth ?? 0,
            clientWidth: opts.clientWidth ?? 0,
            overflowX: opts.overflowX,
            rect: opts.rect || null,
            classList: {
                contains: (c) => classes.includes(c),
                add: (c) => { if (!classes.includes(c)) classes = classes.concat(c); },
                remove: (c) => { classes = classes.filter((x) => x !== c); },
            },
            getBoundingClientRect: () => el.rect || { left: 0, right: 0, top: 0, bottom: 0 },
            click: () => { counts.filesBtn += 1; },
        };
        if (opts.parent) { el.parentElement = opts.parent; }
        if (el.id) byId.set(el.id, el);
        return el;
    }

    const body = node('body');
    const main = node('div', { parent: body });
    // A vertical scroller: overflow-y:auto promotes overflow-x to `auto` in the
    // computed style even though nothing overflows sideways.
    const messages = node('div', {
        id: 'messages', parent: main, overflowX: 'auto',
        scrollWidth: innerWidth, clientWidth: innerWidth,
    });
    const prose = node('p', { parent: messages, overflowX: 'visible' });

    const wrap = node('div', {
        classes: ['code-block-wrap'], parent: messages, overflowX: 'visible',
    });
    const pre = node('pre', { parent: wrap, overflowX: 'auto', scrollWidth: 980, clientWidth: 350 });
    const code = node('code', { parent: pre, overflowX: 'visible' });
    const codeSpan = node('span', { parent: code, overflowX: 'visible' });

    const table = node('table', { parent: messages, overflowX: 'auto', scrollWidth: 1200, clientWidth: 350 });
    const cell = node('td', { parent: table, overflowX: 'visible' });

    const narrowTable = node('table', { parent: messages, overflowX: 'auto', scrollWidth: 340, clientWidth: 350 });
    const narrowCell = node('td', { parent: narrowTable, overflowX: 'visible' });

    const sidebar = node('nav', { id: 'sidebar', parent: body, classes: drawerOpen ? ['mobile-open'] : [] });
    const panelRect = compact
        ? { left: 0, right: innerWidth, top: 0, bottom: 800 }
        : { left: innerWidth - 400, right: innerWidth, top: 0, bottom: 800 };
    const filePanel = node('aside', {
        id: 'file-panel', parent: body, classes: panelOpen ? ['open'] : [], rect: panelRect,
    });
    const panelBody = node('div', { parent: filePanel, overflowX: 'visible' });
    node('button', { id: 'files-btn', parent: body });

    const listeners = { touchstart: [], touchmove: [], touchend: [], touchcancel: [] };
    const documentStub = {
        body,
        getElementById: (id) => byId.get(id) || null,
        addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    };
    const windowStub = {
        innerWidth,
        getComputedStyle: computedStyle ? ((el) => ({ overflowX: el.overflowX })) : undefined,
    };

    const api = FACTORY({
        document: documentStub,
        window: windowStub,
        isTouch: () => touch,
        isCompact: () => compact,
        openSidebar: () => { counts.openSidebar += 1; },
        closeSidebar: () => { counts.closeSidebar += 1; },
    });
    api._setupSwipeGesture();
    api._setupFilePanelSwipe();

    function fire(type, { x, y, target, fingers = 1 }) {
        const touches = [];
        for (let i = 0; i < fingers; i++) touches.push({ clientX: x, clientY: y });
        const ev = {
            type, target,
            touches: type === 'touchend' || type === 'touchcancel' ? [] : touches,
            changedTouches: [{ clientX: x, clientY: y }],
            preventDefault: () => { counts.prevented += 1; },
        };
        for (const fn of listeners[type] || []) fn(ev);
    }

    function swipe({ from, to, y = 300, target, steps = 6, fingers = 1, cancelAfter = null }) {
        fire('touchstart', { x: from, y, target, fingers });
        const step = (to - from) / steps;
        for (let i = 1; i <= steps; i++) {
            fire('touchmove', { x: from + step * i, y, target, fingers });
            if (cancelAfter === i) fire('touchcancel', { x: from + step * i, y, target });
        }
        fire('touchend', { x: to, y, target });
        return { ...counts };
    }

    function drag(opts) {
        const before = { ...counts };
        const after = swipe(opts);
        return {
            filesBtn: after.filesBtn - before.filesBtn,
            openSidebar: after.openSidebar - before.openSidebar,
            closeSidebar: after.closeSidebar - before.closeSidebar,
            prevented: after.prevented - before.prevented,
        };
    }

    return { drag, targets: { prose, pre, code, codeSpan, wrap, table, cell, narrowTable, narrowCell, messages, panelBody }, api };
}

const out = {};

// ── Explorer open, phone: a rightward swipe is the close gesture ────────────
{
    const s = makeScene({ panelOpen: true });
    const right = (target) => s.drag({ from: 120, to: 320, target });
    out.explorer_open = {
        over_pre: right(s.targets.codeSpan),
        over_wrap_child: right(s.targets.code),
        over_table_cell: right(s.targets.cell),
        over_prose: right(s.targets.prose),
        over_table_that_fits: right(s.targets.narrowCell),
        over_the_transcript_scroller: right(s.targets.messages),
        vertical_over_prose: s.drag({ from: 200, to: 205, y: 300, target: s.targets.prose }),
        too_short_over_prose: s.drag({ from: 120, to: 160, target: s.targets.prose }),
    };

    // A gesture the OS took away mid-swipe must not leave tracking armed.
    out.explorer_open.cancelled = s.drag({ from: 120, to: 320, target: s.targets.prose, cancelAfter: 3 });
    out.explorer_open.after_cancel = right(s.targets.prose);

    // Two fingers is a pinch, not a panel swipe.
    out.explorer_open.two_fingers = s.drag({ from: 120, to: 320, target: s.targets.prose, fingers: 2 });
    out.explorer_open.after_two_fingers = right(s.targets.prose);
}

// ── Explorer closed: the OPENING direction has the same bug ────────────────
{
    const s = makeScene({ panelOpen: false });
    out.explorer_closed = {
        edge_over_pre: s.drag({ from: 380, to: 180, target: s.targets.codeSpan }),
        edge_over_prose: s.drag({ from: 380, to: 180, target: s.targets.prose }),
        middle_over_prose: s.drag({ from: 200, to: 60, target: s.targets.prose }),
    };
}

// ── The sidebar drawer — the same forty lines, never mentioned in the audit ─
{
    const open = makeScene({ drawerOpen: true });
    const closed = makeScene({ drawerOpen: false });
    out.drawer = {
        close_over_pre: open.drag({ from: 320, to: 120, target: open.targets.codeSpan }),
        close_over_table: open.drag({ from: 320, to: 120, target: open.targets.cell }),
        close_over_prose: open.drag({ from: 320, to: 120, target: open.targets.prose }),
        close_cancelled: open.drag({ from: 320, to: 120, target: open.targets.prose, cancelAfter: 2 }),
        // The phone case: a code block's left edge is a few pixels from the
        // drawer's edge zone, and scrolling code back to the left is common.
        open_over_pre: closed.drag({ from: 10, to: 210, target: closed.targets.codeSpan }),
        open_over_prose: closed.drag({ from: 10, to: 210, target: closed.targets.prose }),
    };
}

// ── Wide tablet: the Explorer is a column beside a live transcript ──────────
{
    const s = makeScene({ compact: false, touch: true, innerWidth: 1180, panelOpen: true });
    out.tablet_open = {
        transcript_code: s.drag({ from: 200, to: 400, target: s.targets.codeSpan }),
        transcript_prose: s.drag({ from: 200, to: 400, target: s.targets.prose }),
        inside_the_panel: s.drag({ from: 900, to: 1100, target: s.targets.panelBody }),
    };
    const closed = makeScene({ compact: false, touch: true, innerWidth: 1180, panelOpen: false });
    out.tablet_closed = {
        from_right_edge: closed.drag({ from: 1170, to: 970, target: closed.targets.prose }),
        from_right_edge_over_code: closed.drag({ from: 1170, to: 970, target: closed.targets.codeSpan }),
    };
}

// ── Under a mouse nothing is armed at all ──────────────────────────────────
{
    const s = makeScene({ compact: false, touch: false, innerWidth: 1400, panelOpen: true });
    out.desktop = { swipe_over_prose: s.drag({ from: 200, to: 400, target: s.targets.prose }) };
}

// ── The tag/class fallback, for when no computed style is available ─────────
{
    const s = makeScene({ panelOpen: true, computedStyle: false });
    out.no_computed_style = {
        over_pre: s.drag({ from: 120, to: 320, target: s.targets.codeSpan }),
        over_table_cell: s.drag({ from: 120, to: 320, target: s.targets.cell }),
        over_prose: s.drag({ from: 120, to: 320, target: s.targets.prose }),
    };
}

// ── The predicate on its own ───────────────────────────────────────────────
{
    const s = makeScene({ panelOpen: true });
    const t = s.targets;
    out.predicate = {
        pre: s.api._scrollsHorizontally(t.pre),
        wide_table: s.api._scrollsHorizontally(t.table),
        narrow_table: s.api._scrollsHorizontally(t.narrowTable),
        transcript_scroller: s.api._scrollsHorizontally(t.messages),
        prose: s.api._scrollsHorizontally(t.prose),
        nested_span_walks_up: s.api._insideHorizontalScroller(t.codeSpan),
        nested_cell_walks_up: s.api._insideHorizontalScroller(t.cell),
        prose_walks_up_to_nothing: s.api._insideHorizontalScroller(t.prose),
        null_target: s.api._insideHorizontalScroller(null),
    };
}

process.stdout.write(JSON.stringify(out, null, 2));
"""


@pytest.fixture(scope="module")
def r(tmp_path_factory):
    """Run the whole gesture matrix once; every test below reads a slice."""
    harness = tmp_path_factory.mktemp("s22") / "swipe-gestures.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(harness), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout)


# ── the filed bug: Explorer open, horizontal scroll ──────────────────────────


def test_scrolling_a_code_block_sideways_leaves_the_explorer_open(r):
    """The measured case: preventDefault=0, native scroll allowed, and
    files-btn clicked anyway."""
    s = r["explorer_open"]["over_pre"]
    assert s["filesBtn"] == 0, "the panel still toggles while code is being scrolled"
    assert s["prevented"] == 0, "the code must still scroll natively"


def test_a_nested_node_inside_the_code_wrapper_counts_too(r):
    """The finger lands on a <span> of syntax highlighting, never on the
    scroller itself."""
    assert r["explorer_open"]["over_wrap_child"]["filesBtn"] == 0


def test_a_wide_table_both_scrolls_and_keeps_the_panel_open(r):
    """The double failure. touch.css makes the table a scroller; the old guard
    matched only .code-block-wrap and PRE, so preventDefault fired — the table
    could not scroll — and the panel toggled regardless."""
    s = r["explorer_open"]["over_table_cell"]
    assert s["filesBtn"] == 0
    assert s["prevented"] == 0, "the table is a scroller and must not be prevented"


def test_the_genuine_close_gesture_still_closes(r):
    """The whole point of the guard is that it is narrow. A swipe over prose is
    a real panel gesture and must survive."""
    s = r["explorer_open"]["over_prose"]
    assert s["filesBtn"] == 1
    assert s["prevented"] > 0, "an intentional panel swipe still owns the event"


def test_a_table_that_fits_is_not_a_scroller(r):
    """`overflow-x: auto` on its own is not evidence: the content has to be
    wider than the box for the finger to have anywhere to go."""
    assert r["explorer_open"]["over_table_that_fits"]["filesBtn"] == 1


def test_the_transcripts_own_vertical_scroller_does_not_disarm_the_gesture(r):
    """#messages sets overflow-y:auto, which makes its computed overflow-x
    `auto` as well. Taking that for a horizontal scroller would disable the
    close gesture over the entire transcript."""
    assert r["explorer_open"]["over_the_transcript_scroller"]["filesBtn"] == 1


def test_direction_and_distance_thresholds_are_unchanged(r):
    s = r["explorer_open"]
    assert s["vertical_over_prose"]["filesBtn"] == 0, "a vertical drag is the page scrolling"
    assert s["vertical_over_prose"]["prevented"] == 0
    assert s["too_short_over_prose"]["filesBtn"] == 0, "40px is under the 60px threshold"


# ── interruption ─────────────────────────────────────────────────────────────


def test_touchcancel_ends_the_gesture_and_does_not_poison_the_next_one(r):
    """Neither handler registered touchcancel, so an OS-interrupted swipe left
    `tracking` true into the following touch."""
    s = r["explorer_open"]
    assert s["cancelled"]["filesBtn"] == 0
    assert s["after_cancel"]["filesBtn"] == 1, "the next real gesture must still work"


def test_a_second_finger_cancels_the_gesture(r):
    s = r["explorer_open"]
    assert s["two_fingers"]["filesBtn"] == 0
    assert s["after_two_fingers"]["filesBtn"] == 1


# ── the opening direction, which the filing described only as closing ────────


def test_scrolling_code_near_the_right_edge_does_not_open_the_explorer(r):
    """Explorer closed, a leftward scroll starting inside the 25 px edge zone:
    the panel opened on top of the code being read."""
    s = r["explorer_closed"]
    assert s["edge_over_pre"]["filesBtn"] == 0
    assert s["edge_over_prose"]["filesBtn"] == 1, "the edge-open gesture must survive"
    assert s["middle_over_prose"]["filesBtn"] == 0, "opening is still edge-only"


# ── the sidebar drawer, which the audit did not mention at all ───────────────


def test_the_drawer_has_the_same_guard(r):
    s = r["drawer"]
    assert s["close_over_pre"]["closeSidebar"] == 0
    assert s["close_over_table"]["closeSidebar"] == 0
    assert s["close_over_prose"]["closeSidebar"] == 1, "the genuine drawer-close gesture must survive"
    assert s["close_cancelled"]["closeSidebar"] == 0


def test_scrolling_code_back_to_the_left_does_not_open_the_drawer(r):
    """On a phone the code block's own left edge is within a few pixels of the
    drawer's 25 px zone, and scrolling code leftward is the common direction —
    arguably reached more often than the Explorer case."""
    s = r["drawer"]
    assert s["open_over_pre"]["openSidebar"] == 0
    assert s["open_over_prose"]["openSidebar"] == 1


# ── the wide-tablet tier ─────────────────────────────────────────────────────


def test_a_tablet_swipe_in_the_transcript_leaves_the_docked_explorer_alone(r):
    """`_setupFilePanelSwipe` gates on isTouch(), not isCompact(), so with the
    Explorer docked as a column the handler still tracked a swipe that began in
    the chat beside it. Above 900px the close gesture belongs to the panel's own
    region."""
    s = r["tablet_open"]
    assert s["transcript_code"]["filesBtn"] == 0
    assert s["transcript_prose"]["filesBtn"] == 0
    assert s["inside_the_panel"]["filesBtn"] == 1, "the panel's own region still closes it"


def test_the_tablet_edge_open_gesture_is_unchanged(r):
    s = r["tablet_closed"]
    assert s["from_right_edge"]["filesBtn"] == 1
    assert s["from_right_edge_over_code"]["filesBtn"] == 0


def test_nothing_is_armed_under_a_mouse(r):
    assert r["desktop"]["swipe_over_prose"] == {
        "filesBtn": 0,
        "openSidebar": 0,
        "closeSidebar": 0,
        "prevented": 0,
    }


# ── the predicate itself ─────────────────────────────────────────────────────


def test_the_predicate_falls_back_to_shapes_when_no_style_is_computable(r):
    """A detached node or a browser that hands back nothing must still not turn
    a code block into a panel gesture."""
    s = r["no_computed_style"]
    assert s["over_pre"]["filesBtn"] == 0
    assert s["over_table_cell"]["filesBtn"] == 0
    assert s["over_prose"]["filesBtn"] == 1


def test_the_predicate_answers_each_element_correctly(r):
    p = r["predicate"]
    assert p["pre"] is True
    assert p["wide_table"] is True
    assert p["narrow_table"] is False
    assert p["transcript_scroller"] is False
    assert p["prose"] is False
    assert p["nested_span_walks_up"] is True
    assert p["nested_cell_walks_up"] is True
    assert p["prose_walks_up_to_nothing"] is False
    assert p["null_target"] is False
