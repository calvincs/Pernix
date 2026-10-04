"""Live audit 2026-09-14 / L02: a click at the centre of a session row pinned
it instead of opening it.

`ec5f2d7` (09-02) moved the row's hover controls into an absolutely positioned
`.session-actions` overlay, which is what let the title have the row's width
at rest. Then the overlay grew: seven 24px buttons — id badge, pin, rename,
move, archive, delete — is 184px of a 253px row, starting at x=57. On hover
the title collapsed to about four characters and the strip covered the rest,
so Playwright clicking the centre of six rows pinned six sessions
(`elementFromPoint` at the centre was `button.session-pin`), and Delete sat
under the cursor at the top right of whatever you were pointing at.

The strip is two controls now — the pin, and an overflow button onto the same
menu the touch action sheet uses. Nothing is lost: rename, move, archive,
copy id and delete are all in that menu, for the mouse and the keyboard both.

The geometry (the element at the row centre, the strip's width) needs a real
browser and is asserted in `tools/ui-gate/check.py` — "m2: the row's hover
strip is two controls" and "m2: hovering a row leaves the title under the
cursor". What is pinned here is the structure that produces it, driven
through the real `sidebar.js` sources.
"""

from __future__ import annotations

import re

import pytest

from tests.js_harness import STATIC_JS, requires_node, run_js

pytestmark = requires_node


SCENARIO = r"""
import { fns, makeContext, makeDoc, mod, run, report, runScenario, settle,
         callees, stubMissing } from './sandbox.mjs';

const SIDEBAR = mod('components/sidebar.js');
const RENDER = mod('render.js');

const dom = makeDoc();

// makeDoc's addEventListener is a no-op; the row under test is built entirely
// out of listeners, so they have to be recorded to be clicked.
function element(tag) {
  const e = dom.element(tag);
  e.__on = {};
  e.addEventListener = (t, fn) => { (e.__on[t] = e.__on[t] || []).push(fn); };
  return e;
}
const click = e => (e.__on.click || []).forEach(fn => fn({
  stopPropagation() {}, preventDefault() {}, currentTarget: e, target: e,
}));

const CODE = [
  fns(['el', 'text'], RENDER),
  fns(['_sessionActions', '_openSessionSheet', '_renderSessionItem'], SIDEBAR),
].join('\n\n');

function build(stubbed) {
  const sheets = [], patches = [];
  const base = {
    console, setTimeout, clearTimeout, Set, Map, Promise, JSON, Math, Object, String, Boolean, Date,
    document: {
      createElement: element,
      createTextNode: dom.textNode,
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener() {},
      body: element('body'),
    },
    window: { dispatchEvent() {}, addEventListener() {} },
    CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } },
    icon: (name) => { const s = element('span'); s.className = `pxi-${name}`; return s; },
    isTouch: () => false,
    _getTypeKey: () => 'chat',
    SESSION_TYPES: { chat: { cls: 'dot-chat', label: 'Chat' } },
    CHILD_TYPES: new Set(['worker', 'rlm']),
    _displayTitle: () => 'A perfectly ordinary session title',
    _relativeTime: () => '2m',
    _cleanPreview: () => '',
    _attentionBadge: () => null,
    _attentionOf: () => null,
    _isBusy: () => false,
    _activity: new Map(),
    _activateOnKey: () => {},
    _reducedMotion: () => true,
    _spaces: [{ id: 'sp1', label: 'Research lab' }],
    _select: () => {},
    patch: (url, body) => { patches.push({ url, body }); return Promise.resolve({}); },
    // The sheet the overflow button opens: record what it was offered, then
    // answer "cancel" so nothing runs.
    actionSheet: (opts) => { sheets.push(opts); return Promise.resolve(null); },
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, CODE);
  stubMissing(ctx, callees(CODE));
  return { ctx, container: element('div'), sheets, patches };
}

function render(h, session, isWorker = false) {
  h.ctx.__session = session;
  h.ctx.__container = h.container;
  h.ctx.__isWorker = isWorker;
  run(h.ctx, '_renderSessionItem(__session, __container, null, __isWorker)');
  return h.container.children[h.container.children.length - 1];
}

const strip = row => row.children.find(c => c.classList.contains('session-actions'));
const classesOf = s => (s ? s.children.map(b => b.className.split(/\s+/)[0]) : null);

const out = {};

// -- an ordinary chat row ----------------------------------------------------
await runScenario(build, async h => {
  const row = render(h, { id: 'A', title: 'x', updated_at: '2026-09-14T00:00:00Z', message_count: 3 });
  const s = strip(row);
  out.chat_controls = classesOf(s);
  out.chat_stripIsOverlayChild = !!s && s.parentNode === row;
  // Everything the old strip held, gone from the row itself.
  out.chat_rowClasses = row.querySelectorAll('button').map(b => b.className.split(/\s+/)[0]);
  out.chat_titleText = (row.querySelector('.session-title-text') || {}).textContent;

  // The pin still pins, from the strip.
  click(s.children[0]);
  await settle();
  out.chat_pinPatch = h.patches.slice();

  // ...and the overflow button opens the shared menu.
  click(s.children[1]);
  await settle();
  out.chat_sheetItems = (h.sheets[0] || {}).items?.map(i => i.id) || null;
  out.chat_sheetTitle = (h.sheets[0] || {}).title || null;
  out.chat_moreLabel = s.children[1].getAttribute('aria-label');
  out.chat_morePopup = s.children[1].getAttribute('aria-haspopup');
});

// -- a pinned row keeps its state in the line, not in the overlay ------------
await runScenario(build, async h => {
  const row = render(h, { id: 'A', pinned: 1, updated_at: '2026-09-14T00:00:00Z' });
  out.pinned_controls = classesOf(strip(row));
  out.pinned_mark = !!row.querySelector('.session-pinned-mark');
  out.pinned_pressed = strip(row).children[0].getAttribute('aria-pressed');
});

// -- a worker row: no pin of its own, so one control ------------------------
await runScenario(build, async h => {
  const row = render(h, { id: 'W', session_type: 'worker', updated_at: '2026-09-14T00:00:00Z' }, true);
  out.worker_controls = classesOf(strip(row));
});

// -- the menu a child session gets --------------------------------------------
await runScenario(build, async h => {
  const row = render(h, { id: 'W', session_type: 'worker', updated_at: '2026-09-14T00:00:00Z' }, true);
  click(strip(row).children[0]);
  await settle();
  out.worker_sheetItems = (h.sheets[0] || {}).items?.map(i => i.id) || null;
});

report(out);
"""


@pytest.fixture(scope="module")
def l02(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("l02"))


# ── the strip ────────────────────────────────────────────────────────────────


def test_the_hover_strip_is_two_controls(l02):
    """Seven was 184px of a 253px row. The contract is pin plus overflow."""
    assert l02["chat_controls"] == ["session-pin", "session-more"]
    assert l02["chat_stripIsOverlayChild"] is True


def test_delete_has_left_the_hover_strip(l02):
    """It was under the cursor at the top right of every row you pointed at."""
    assert l02["chat_rowClasses"] == ["session-pin", "session-more"]
    for gone in ("session-delete", "session-rename", "session-id-badge", "session-space-move", "session-archive"):
        assert gone not in l02["chat_rowClasses"]


def test_the_pin_still_pins_from_the_strip(l02):
    """The one action worth its own click keeps it — optimistically, as before."""
    assert l02["chat_pinPatch"] == [{"url": "/api/sessions/A", "body": {"pinned": True}}]


def test_a_pinned_row_still_says_so_in_the_line(l02):
    """The overlay is invisible at rest, so the state cannot live in it."""
    assert l02["pinned_controls"] == ["session-pin", "session-more"]
    assert l02["pinned_mark"] is True
    assert l02["pinned_pressed"] == "true"


def test_a_worker_row_has_no_pin_and_so_one_control(l02):
    assert l02["worker_controls"] == ["session-more"]


# ── nothing is lost ──────────────────────────────────────────────────────────


def test_the_overflow_button_opens_the_menu_the_touch_tier_uses(l02):
    """Every action that left the strip is in it, plus the id badge's copy."""
    assert l02["chat_sheetItems"] == ["pin", "rename", "move", "archive", "copy", "delete"]
    assert l02["chat_sheetTitle"] == "A perfectly ordinary session title"


def test_the_overflow_button_is_named_and_announces_a_menu(l02):
    """A keyboard user reaches every action through this one control, so it
    has to say what it is and that it opens something."""
    assert l02["chat_moreLabel"] == "Actions for A perfectly ordinary session title"
    assert l02["chat_morePopup"] == "dialog"


def test_a_child_session_gets_the_two_actions_it_owns(l02):
    """Workers and RLM runs belong to their parent: no pin, rename or space."""
    assert l02["worker_sheetItems"] == ["copy", "delete"]


# ── the width the gate measures ──────────────────────────────────────────────


def test_the_strips_own_box_holds_only_its_controls():
    """The 20px seam was `padding-left` on the strip, which put it inside the
    box the <=64px contract is measured on. It is a pseudo-element now."""
    css = (STATIC_JS.parent / "css" / "layout.css").read_text()
    block = re.search(r"\n\.session-actions \{(.*?)\n\}", css, re.S)
    assert block, ".session-actions block not found"
    assert "padding" not in block.group(1)
    assert "position: absolute" in block.group(1)
    assert ".session-actions::before {" in css


def test_the_row_rebuild_can_still_hand_focus_back_to_both_controls():
    """renderSessionList throws the list away several times a minute. A control
    missing from FOCUS_CONTROLS drops the user's focus onto <body> mid-Tab."""
    src = (STATIC_JS / "components" / "sidebar.js").read_text()
    block = re.search(r"const FOCUS_CONTROLS = \[(.*?)\];", src, re.S).group(1)
    assert "'session-pin'" in block
    assert "'session-more'" in block
