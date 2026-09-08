"""Audit 3.2.2 / S12 + S13, 2026-09-08: a fast answer painted nothing at all
while it streamed, and a long code block was re-parsed from scratch on every
paint that did happen.

S12. The markdown scheduler was a TRAILING-edge debounce — every token cleared
the pending 100 ms timer and set a new one — so a stream with no 100 ms gap in
it never rendered. Measured: 500 tokens 20 ms apart produced ZERO incremental
paints in ten seconds; 2,000 at 8 ms, zero in sixteen; 200 at 90 ms, zero in
17.9. At exactly 100 ms gaps it painted every token, so it is a cliff, not a
gradient, and it is inverted: the faster the model answers, the longer the
transcript sits empty.

Two things kept that from reading as a hang. `.message.assistant .content:empty`
paints a pulsing "..." so it looks like thinking, and `_activityTimer` — a
separate 500 ms timer that was never reset per token — kept updating the
SIDEBAR preview, so the sidebar showed text the transcript did not.

S12 also caused silent text loss, which is the part the filing missed.
`stream.error` and `stream.budget_exhausted` did no final render at all, and
`_dropEmptyStreamingBubble()` declines to remove a bubble whose `_collected` is
non-empty — so a turn that broke after 1,500 streamed characters left an empty
assistant card above an error line, with `_rawContent` unset so copy-message
copied nothing either.

S13. `_renderStreamIncremental` answered "are we inside a code block?" with
`prefix.match(/```/g).length % 2`, rescanning the entire stable prefix on every
boundary advance — 232 million characters scanned for a 113 KB answer with no
code in it at all. Three wrong answers froze into the transcript, because the
stable boundary only ever moves forward: `~~~` fences were not counted, a ```
mentioned inline in prose flipped the parity, and a six-backtick fence counted
as two.

The honest scale of the parsing cost: ~304 ms of marked CPU spread over 4,000
paints on a desktop, worst single paint 1.5 ms — never a dropped frame, and
today it costs nothing at all because S12 means the paints never happen. So
this is fixed for correctness, and because fixing S12 would otherwise start
paying for it. No latency claim is made here that was not measured.
"""

from __future__ import annotations

import pytest

from tests.js_harness import requires_node, run_js

pytestmark = requires_node


SCENARIO = r"""
import { decls, fns, makeContext, makeDoc, run, settle, until, wait, report, runScenario,
         guard, ck, callees, stubMissing } from './sandbox.mjs';

const dom = makeDoc();

const CODE = [
  decls(['_pendingFiles', '_sendingSids', '_selectSeq', '_streamingEl', '_collected', '_toolGroup',
         '_lastSeq', '_toolGroupCount', '_toolGroupErrors', '_toolGroupLatency', '_toolGroupRunning',
         '_reloadOwner', '_reloadBuffer', '_joinedMidTurn', '_reconcileTimer',
         '_paintTimer', '_paintDirty', '_paintOwner', 'PAINT_INTERVAL_MS',
         '_sessionModelOverride', '_histIdx', '_expandedKeys', '_lastStreamModel',
         '_activityTimer', '_injectedMessages', '_DRAFT_PREFIX']),
  fns(['_viewOwner', '_ownsView', '_advanceFenceScan', '_resetOpenFence', '_renderOpenFence',
       '_renderStreamIncremental', '_schedulePaint', '_paintTick', '_paintStreamNow',
       '_cancelStreamPaint', '_finalizeStreamingBubble', '_dropEmptyStreamingBubble',
       'handleEvent', '_softReload', '_isRlmView', '_lastMessageIsUnanswered', 'selectSession']),
].join('\n\n');

// Counters wrapped around the two things the audit measured: characters of
// markdown handed to the parser, and characters of buffer scanned for fences.
const METERS = `
var __paints = 0, __mdChars = 0, __mdCalls = 0, __scanned = 0;
(() => {
  const realRender = _renderStreamIncremental;
  _renderStreamIncremental = function (c) { __paints++; return realRender(c); };
  const realScan = _advanceFenceScan;
  _advanceFenceScan = function (c, b) {
    const before = c._scanLen || 0;
    const r = realScan(c, b);
    __scanned += Math.max(0, (c._scanLen || 0) - before);
    return r;
  };
})();
globalThis.__meters = () => ({ paints: __paints, mdChars: __mdChars, mdCalls: __mdCalls, scanned: __scanned });
globalThis.__resetMeters = () => { __paints = 0; __mdChars = 0; __mdCalls = 0; __scanned = 0; };
`;

function build(stubbed) {
  const calls = [], mdInputs = [];
  const els = { 'msg-input': dom.element('textarea'), 'send-btn': dom.element('button'),
                'status-info': dom.element('div'), 'status-notice': dom.element('div'),
                'file-chips': dom.element('div') };
  const inner = dom.element('div');
  const state = { sid: 'A', streaming: false, sessions: [], spaces: [], model: 'm' };
  const base = {
    console, setTimeout, clearTimeout, setInterval, clearInterval, Date, Set, Map, JSON, RegExp, Number, Math,
    state,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { getElementById: id => els[id] || null, querySelector: () => null,
                querySelectorAll: () => [], createElement: t => dom.element(t),
                addEventListener() {}, body: dom.element('body') },
    Event: class { constructor(t) { this.type = t; } },
    el: (tag, attrs = {}, children = []) => {
      const e = dom.element(tag);
      for (const [k, v] of Object.entries(attrs)) { if (k === 'class') e.className = v; else e.setAttribute(k, v); }
      for (const c of children) if (c) e.appendChild(typeof c === 'string' ? dom.textNode(c) : c);
      return e;
    },
    text: v => dom.textNode(String(v)),
    clear: e => { while (e.firstChild) e.removeChild(e.firstChild); },
    renderMarkdown: md => {
      // Stands in for marked + DOMPurify. Only the VOLUME handed to it and the
      // fact that it is the single markup-producing path are under test here.
      mdInputs.push(md);
      run(ctx, `__mdChars += ${md.length}; __mdCalls++;`);
      const d = dom.element('div'); d.className = 'md';
      d.appendChild(dom.textNode(md));
      return d;
    },
    _messagesInner: () => inner,
    _messagesScroll: () => ({ scrollHeight: 0, scrollTop: 0 }),
    appendMessage(role, txt) {
      const e = dom.element('div'); e.classList.add('message', role);
      const c = dom.element('div'); c.className = 'content'; e.appendChild(c);
      inner.appendChild(e); calls.push(`appendMessage(${role})`);
      if (txt) c.appendChild(dom.textNode(txt));
      return e;
    },
    get: () => Promise.resolve({ event_seq: 0, state: 'idle_ready', status: 'idle' }),
    post: () => Promise.resolve({}),
    loadMessages: () => Promise.resolve(),
    loadContextInfo: () => Promise.resolve(),
    loadPendingQuestions: () => Promise.resolve(),
    loadSessions: () => Promise.resolve(),
    openRlmViewer: () => Promise.resolve(),
    connectSSE() {}, disconnectSSE() {},
    _knownSession: sid => ({ id: sid, session_type: 'chat', read_only: false }),
    isCompact: () => false, renderFileChips() {},
    _showStopButton() {}, _showSendButton() {},
    _recentlyFinished: { delete() {} }, _offListSessions: { set() {} },
    scrollToBottom() {}, addCopyButtons() {}, processFileRefs() {},
    announce() {}, updateStatus() {}, _showNotice() {},
    humanizeError: e => String((e && e.message) || e),
    closeToolGroup() {},
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, CODE);
  const autoStubbed = stubMissing(ctx, callees(CODE));
  run(ctx, METERS);
  const h = {
    ctx, calls, inner, state, mdInputs, autoStubbed,
    peek: expr => run(ctx, `(${expr})`),
    fire: ev => run(ctx, `handleEvent(${JSON.stringify(ev)})`),
    meters: () => run(ctx, 'JSON.stringify(__meters())').valueOf(),
    // Open a bubble and drive the incremental renderer directly, once per
    // tick — the audit's own measurement, isolated from the paint cadence.
    ticks: (chunks) => {
      run(ctx, `_streamingEl = appendMessage('assistant', ''); _collected = ''; __resetMeters();`);
      for (const c of chunks) {
        run(ctx, `_collected += ${JSON.stringify(c)}; _renderStreamIncremental(_streamingEl.querySelector('.content'));`);
      }
      return JSON.parse(run(ctx, 'JSON.stringify(__meters())'));
    },
    // Built with JSON.stringify rather than a template literal: a template
    // literal would turn the \n in a fence fixture into a real newline and
    // emit a JS string containing one.
    append: (s) => run(ctx, '_collected += ' + JSON.stringify(s)
                       + '; _renderStreamIncremental(_streamingEl.querySelector(".content"));'),
    contentEl: () => run(ctx, `_streamingEl && _streamingEl.querySelector('.content')`),
    bubbleText: () => run(ctx, `_streamingEl ? _streamingEl.querySelector('.content').textContent : null`),
  };
  return h;
}

const out = {};
const rep = (n, s) => new Array(n).fill(s);

// ===========================================================================
// S12 — bounded cadence
// ===========================================================================

// -- a steady 20 ms stream paints on a cadence instead of never -------------
await runScenario(build, async h => {
  run(h.ctx, '__resetMeters()');
  const t0 = Date.now();
  for (let i = 0; i < 60; i++) {
    h.fire({ type: 'stream.token', seq: 100 + i, session_id: 'A', content: `tok${i} ` });
    await wait(20);
  }
  await wait(160);
  const m = JSON.parse(run(h.ctx, 'JSON.stringify(__meters())'));
  out.steady_paints = m.paints;
  out.steady_elapsedMs = Date.now() - t0;
  out.steady_visible = h.bubbleText();
  out.steady_collectedLen = h.peek('_collected.length');
});

// -- sparse tokens paint immediately (leading edge) --------------------------
await runScenario(build, async h => {
  run(h.ctx, '__resetMeters()');
  for (let i = 0; i < 4; i++) {
    h.fire({ type: 'stream.token', seq: 200 + i, session_id: 'A', content: `s${i} ` });
    await wait(150);
  }
  out.sparse_paints = JSON.parse(run(h.ctx, 'JSON.stringify(__meters())')).paints;
});

// -- completion before the next tick still flushes ---------------------------
await runScenario(build, async h => {
  run(h.ctx, '__resetMeters()');
  for (let i = 0; i < 5; i++) h.fire({ type: 'stream.token', seq: 300 + i, session_id: 'A', content: `q${i} ` });
  h.fire({ type: 'stream.done', seq: 400, session_id: 'A', model: 'm' });
  out.done_visible = h.inner.querySelectorAll('.assistant').map(e => e.textContent);
  out.done_pendingTimer = h.peek('_paintTimer !== null');
  await wait(160);
  out.done_visibleAfterTick = h.inner.querySelectorAll('.assistant').map(e => e.textContent);
});

// -- a delayed paint must never touch another session's bubble --------------
await runScenario(build, async h => {
  h.fire({ type: 'stream.token', seq: 500, session_id: 'A', content: "A's answer" });
  const aBubble = h.peek('_streamingEl');
  h.fire({ type: 'stream.token', seq: 501, session_id: 'A', content: ' continues' });
  out.crossSession_pending = h.peek('_paintTimer !== null');
  guard(h, run(h.ctx, `selectSession('B')`));
  await settle(20); ck(h);
  await wait(200);
  out.crossSession_aText = aBubble.querySelector('.content').textContent;
  out.crossSession_paintOwner = h.peek('_paintOwner') === null;
  out.crossSession_streamingEl = h.peek('!!_streamingEl');
});

// -- a broken turn must not throw away what it already streamed -------------
await runScenario(build, async h => {
  const chunk = 'x'.repeat(300);
  for (let i = 0; i < 5; i++) h.fire({ type: 'stream.token', seq: 600 + i, session_id: 'A', content: chunk });
  const bubble = h.peek('_streamingEl');
  h.fire({ type: 'stream.error', seq: 700, session_id: 'A', error: 'provider exploded' });
  out.error_bubbleText = bubble.querySelector('.content').textContent.length;
  out.error_rawContent = (bubble._rawContent || '').length;
  out.error_bubbleRemoved = bubble.removed;
});

await runScenario(build, async h => {
  const chunk = 'y'.repeat(300);
  for (let i = 0; i < 5; i++) h.fire({ type: 'stream.token', seq: 800 + i, session_id: 'A', content: chunk });
  const bubble = h.peek('_streamingEl');
  h.fire({ type: 'stream.budget_exhausted', seq: 900, session_id: 'A', message: 'no further retries' });
  out.budget_bubbleText = bubble.querySelector('.content').textContent.length;
  out.budget_rawContent = (bubble._rawContent || '').length;
});

// -- a turn that streamed nothing still drops its empty card ----------------
await runScenario(build, async h => {
  run(h.ctx, `_streamingEl = appendMessage('assistant', ''); _collected = ''; state.streaming = true;`);
  const bubble = h.peek('_streamingEl');
  h.fire({ type: 'stream.error', seq: 1000, session_id: 'A', error: 'nothing arrived' });
  out.emptyTurn_removed = bubble.removed;
});

// ===========================================================================
// S13 — incremental fence tracking
// ===========================================================================

// -- scaling: parser input and scanned characters, per doubling -------------
await runScenario(build, async h => {
  const line = 'const value = someFunction(argument, another);\n';
  const mk = n => ['Here is the code:\n\n```js\n', ...rep(n, line)];
  const a = h.ticks(mk(500));
  const b = h.ticks(mk(1000));
  const c = h.ticks(mk(2000));
  out.scale = { a, b, c, finalLen: h.peek('_collected.length') };
});

// -- the rescan was unconditional: prose with no code at all ----------------
await runScenario(build, async h => {
  const para = 'Some ordinary prose that contains no code whatsoever.\n\n';
  const m = h.ticks(rep(2000, para));
  out.prose = { scanned: m.scanned, len: h.peek('_collected.length'), mdCalls: m.mdCalls };
});

// -- fence variant 1: ~~~ tilde fences are real GFM fences ------------------
await runScenario(build, async h => {
  const doc = 'intro para\n\n~~~python\nprint(1)\n\nprint(2)\n\n';
  h.ticks([doc]);
  const c = h.contentEl();
  out.tilde_open = { fenceChar: c._fenceChar, stableLen: c._stableLen, fenceAt: c._fenceAt };
  out.tilde_frozeMidFence = c._stableLen > doc.indexOf('~~~');
  h.append('~~~\n\nafter para\n\n');
  out.tilde_closed = { fenceChar: c._fenceChar, advanced: c._stableLen > doc.indexOf('~~~') };
});

// -- fence variant 2: a ``` MENTIONED inline is not a fence ----------------
await runScenario(build, async h => {
  const doc = 'Type ```js to open a block, then close it.\n\npara two\n\npara three\n\n';
  h.ticks([doc]);
  const c = h.contentEl();
  out.inline = { fenceChar: c._fenceChar, stableLen: c._stableLen, len: doc.length };
});

// -- fence variant 3: a six-backtick fence is one fence, not two -----------
await runScenario(build, async h => {
  const doc = 'before\n\n``````\ntext with ``` inside it\n\nstill inside\n\n';
  h.ticks([doc]);
  const c = h.contentEl();
  out.six_open = { fenceChar: c._fenceChar, fenceLen: c._fenceLen, stableLen: c._stableLen,
                   fenceAt: doc.indexOf('``````') };
  h.append('``````\n\nafter\n\n');
  out.six_closed = { fenceChar: c._fenceChar, advanced: c._stableLen > doc.indexOf('``````') };
});

// -- an open fence renders as text nodes, never as markup -------------------
await runScenario(build, async h => {
  h.ticks(['intro\n\n```html\n<img src=x onerror="alert(1)">\n<script>evil()</script>\n']);
  const tail = h.contentEl().querySelector('.stream-tail');
  const pre = tail.querySelector('pre');
  const code = pre.querySelector('code');
  out.openFence = {
    hasPre: !!pre,
    lang: code.className,
    allText: code.childNodes.every(n => n.nodeType === 3),
    text: code.textContent,
    mdSawTheFence: h.mdInputs.some(m => m.includes('onerror')),
  };
});

// -- a language label with junk in it cannot become anything but a class ----
await runScenario(build, async h => {
  h.ticks(['x\n\n```js" onload="alert(1)\nbody\n']);
  const code = h.contentEl().querySelector('.stream-tail').querySelector('code');
  out.langLabel = code.className;
});

// -- the completed render is authoritative and unchanged -------------------
await runScenario(build, async h => {
  const answer = 'para one\n\n```js\nlet a = 1;\n```\n\npara two with ~~~ and ``` in it\n';
  run(h.ctx, `_streamingEl = appendMessage('assistant', ''); _collected = ''; __resetMeters();`);
  for (const ch of answer.split('\n')) h.append(ch + '\n');
  const before = h.mdInputs.length;
  run(h.ctx, '_finalizeStreamingBubble()');
  out.finalize = {
    lastMdInput: h.mdInputs[h.mdInputs.length - 1],
    calledOnce: h.mdInputs.length === before + 1,
    rawContent: h.peek('_streamingEl._rawContent'),
    cursorsReset: h.peek(`_streamingEl.querySelector('.content')._scanLen === null`),
  };
});

report(out);
"""


@pytest.fixture(scope="module")
def s12(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("s12"))


# ── S12: the cadence ─────────────────────────────────────────────────────────


def test_a_steady_stream_paints_on_a_bounded_cadence(s12):
    """The headline measurement, at 1/8 the audit's scale so it runs in a
    suite: 60 tokens 20 ms apart. The trailing-edge debounce painted zero of
    them until the stream stopped; a bounded cadence paints roughly once per
    interval, and the transcript shows the answer while it is being written."""
    elapsed = s12["steady_elapsedMs"]
    lower = max(3, elapsed // 200)
    assert s12["steady_paints"] >= lower, f"{s12['steady_paints']} paints in {elapsed}ms"
    assert s12["steady_paints"] <= (elapsed // 100) + 4, "and no more often than the cadence allows"
    assert len(s12["steady_visible"]) > 0
    assert s12["steady_collectedLen"] > 0


def test_sparse_tokens_paint_immediately(s12):
    """Leading edge: with no paint pending there is nothing to wait for, so a
    slow stream is not made slower by the throttle."""
    assert s12["sparse_paints"] == 4


def test_a_completion_before_the_next_tick_still_flushes(s12):
    """`stream.done` renders the whole answer itself and cancels the pending
    paint, so nothing is left for a timer that will never usefully fire."""
    assert s12["done_visible"] == ["q0 q1 q2 q3 q4 "]
    assert s12["done_pendingTimer"] is False
    assert s12["done_visibleAfterTick"] == s12["done_visible"]


def test_a_pending_paint_never_lands_in_another_sessions_bubble(s12):
    """The audit's binding constraint on the cadence. A scheduled paint carries
    the view it was scheduled for and stands down if that view is gone."""
    assert s12["crossSession_pending"] is True
    assert s12["crossSession_aText"] == "A's answer"
    assert s12["crossSession_paintOwner"] is True
    assert s12["crossSession_streamingEl"] is False


# ── S12: the silent text loss the filing missed ──────────────────────────────


def test_a_stream_error_paints_what_already_arrived(s12):
    """Measured before the fix: 1,500 characters received, ZERO painted. The
    bubble was kept (because `_collected` was non-empty) but empty, and
    `_rawContent` was never set so copy-message copied nothing."""
    assert s12["error_bubbleText"] == 1500
    assert s12["error_rawContent"] == 1500
    assert s12["error_bubbleRemoved"] is False


def test_an_exhausted_budget_paints_what_already_arrived(s12):
    """Same shape, same fix — the other terminal handler with no final render."""
    assert s12["budget_bubbleText"] == 1500
    assert s12["budget_rawContent"] == 1500


def test_a_turn_that_streamed_nothing_still_drops_its_empty_card(s12):
    """The finalize must not resurrect the empty-bubble problem it sits next
    to: with nothing collected there is nothing to paint and the card goes."""
    assert s12["emptyTurn_removed"] is True


# ── S13: the scaling ─────────────────────────────────────────────────────────


def test_an_open_code_fence_no_longer_retains_quadratic_parser_work(s12):
    """Reproduced at 500/1000/2000 ticks. The old renderer fed marked the whole
    accumulated block on every tick — 4.00x per doubling, dead-on quadratic.
    Open-fence content now goes through text-safe append, so the parser sees a
    bounded amount regardless of how long the block gets."""
    a, b, c = s12["scale"]["a"], s12["scale"]["b"], s12["scale"]["c"]
    assert a["paints"] == 501 and c["paints"] == 2001, "one paint per tick, as measured"
    # Quadratic would be 4.00x per doubling. Anything at or below linear passes.
    assert c["mdChars"] <= 2.2 * b["mdChars"] + 1000
    assert b["mdChars"] <= 2.2 * a["mdChars"] + 1000
    assert c["mdChars"] < s12["scale"]["finalLen"], "the block is never re-parsed while open"


def test_the_fence_scan_reads_every_character_exactly_once(s12):
    """`prefix.match(/```/g)` rescanned the whole stable prefix on every
    boundary advance — for ordinary prose that is every tick: 232 million
    characters scanned for a 113 KB answer containing no code at all."""
    prose = s12["prose"]
    assert prose["scanned"] <= prose["len"], "the scan cursor only ever moves forward"
    assert prose["scanned"] >= prose["len"] * 0.9, "and it does cover the text"


# ── S13: the three fence variants that froze into the transcript ─────────────


def test_a_tilde_fence_is_a_fence(s12):
    """`~~~` is valid GFM and the vendored marked v15 supports it, but
    `/```/g` could not see it — so the boundary advanced straight through the
    middle of the block and froze a half-fence into the stable prefix."""
    assert s12["tilde_open"]["fenceChar"] == "~"
    assert s12["tilde_frozeMidFence"] is False
    assert s12["tilde_closed"]["fenceChar"] == ""
    assert s12["tilde_closed"]["advanced"] is True


def test_a_fence_marker_mentioned_in_prose_is_not_a_fence(s12):
    """The regex was unanchored, so prose that merely mentions ``` once flipped
    the parity and froze the prefix for the rest of the stream. Scanning by
    line agrees with how marked itself decides."""
    assert s12["inline"]["fenceChar"] == ""
    assert s12["inline"]["stableLen"] > 0, "the boundary keeps advancing through the prose"


def test_a_six_backtick_fence_counts_as_one_fence(s12):
    """`/```/g` found two markers in `` `````` `` and called the block closed.
    A closer has to use the same character and be at least as long, so the
    inner ``` does not close it and the trailing `` `````` `` does."""
    assert s12["six_open"]["fenceChar"] == "`"
    assert s12["six_open"]["fenceLen"] == 6
    assert s12["six_open"]["stableLen"] <= s12["six_open"]["fenceAt"]
    assert s12["six_closed"]["fenceChar"] == ""
    assert s12["six_closed"]["advanced"] is True


# ── S13: not weakening sanitization to save the work ─────────────────────────


def test_open_fence_content_is_built_from_text_nodes_only(s12):
    """ "Render open-code content through text-safe append/update." No markup is
    produced, so there is nothing for a sanitizer to miss — rather than a
    hand-rolled parser deciding what is safe."""
    of = s12["openFence"]
    assert of["hasPre"] is True
    assert of["allText"] is True
    assert "<img src=x onerror=" in of["text"]
    assert "<script>evil()</script>" in of["text"]
    assert of["mdSawTheFence"] is False


def test_a_language_label_can_only_ever_become_a_class_name(s12):
    """The info string is model output. It reaches the DOM as a class and
    nothing else, and non-identifier characters are dropped on the way."""
    assert s12["langLabel"] == "language-js"


def test_the_completed_render_is_still_one_full_markdown_pass(s12):
    """The incremental path is an optimisation for what is on screen mid-turn.
    The finalize pass re-parses the complete text through the app's single
    markdown chokepoint, and stays authoritative."""
    fin = s12["finalize"]
    assert fin["calledOnce"] is True
    assert fin["lastMdInput"] == fin["rawContent"]
    assert "```js" in fin["lastMdInput"] and "~~~" in fin["lastMdInput"]
    assert fin["cursorsReset"] is True
