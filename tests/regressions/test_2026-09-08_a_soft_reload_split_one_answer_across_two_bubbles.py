"""Audit 3.2.2 / S04, 2026-09-08: a transcript refresh could split one answer
across two bubbles in reverse order, replace it with an empty string, or write
it into a completely different session.

`_softReload()` held its guard for exactly one of its two awaits. `_reloading`
was cleared in a `finally` around `loadMessages()` — before the `/status`
request that follows it — so there were two windows, not one. A token landing
in the second window built a bubble and filled `_collected`, and the status
branch then opened a SECOND bubble and assigned the (still empty)
`_bufferedDuringReload` over the text. Text arriving in both windows came out
as "CCC DDD | AAA BBB EEE" for an authoritative "AAA BBB CCC DDD EEE".

The buffer held token strings only, so `tool.call` and `stream.done` arriving
inside the window ran their normal branches against a DOM `loadMessages` was
about to discard, gluing two rounds into one bubble. The two newest handlers,
`turn.round_renewal` and `context.trim_floor`, shared that defect by
construction: neither checked the guard either.

And it had no session token at all. It re-read `state.sid` after its awaits,
and `selectSession()` reset neither `_reloading` nor `_bufferedDuringReload` —
so a reload of A that straddled a session switch installed B's `_lastSeq`,
opened a bubble in B, and rendered A's buffered text into B's transcript. That
makes S04 the same bug family as S03, not a separate one.

The fix makes the whole reload one generation-scoped transaction: the guard is
never released between awaits, ORDERED events are buffered (not just token
strings), the boundary is read before the transcript so nothing falls between
them, the buffered events are replayed through the normal handler afterwards
so `handleEvent`'s own dedup decides what the snapshot already represents, and
every step is scoped to the view that started it.
"""

from __future__ import annotations

import pytest

from tests.js_harness import requires_node, run_js

pytestmark = requires_node


SCENARIO = r"""
import { decls, fns, makeContext, makeDoc, run, deferred, settle, until, wait, report, runScenario, guard, ck, callees, stubMissing } from './sandbox.mjs';

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

function build(stubbed) {
  const calls = [], gets = [], loads = [], store = {};
  const textarea = dom.element('textarea'); textarea.value = '';
  const els = { 'msg-input': textarea, 'send-btn': dom.element('button'),
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
    renderMarkdown: md => { const d = dom.element('div'); d.className = 'md'; d.appendChild(dom.textNode(md)); return d; },
    _messagesInner: () => inner,
    _messagesScroll: () => ({ scrollHeight: 0, scrollTop: 0 }),
    appendMessage(role, txt) {
      const e = dom.element('div'); e.classList.add('message', role);
      const c = dom.element('div'); c.className = 'content'; e.appendChild(c);
      inner.appendChild(e);
      calls.push(`appendMessage(${role})`);
      if (txt) c.appendChild(dom.textNode(txt));
      return e;
    },
    get: url => { const d = deferred(); gets.push({ url, ...d }); calls.push(`GET ${url}`); return d.promise; },
    post: () => Promise.resolve({}),
    loadMessages: (sid, o) => {
      const d = deferred();
      loads.push({ sid, keepScroll: !!(o && o.keepScroll), ...d });
      calls.push(`loadMessages(${sid})`);
      return d.promise;
    },
    loadContextInfo: () => Promise.resolve(),
    loadPendingQuestions: () => Promise.resolve(),
    loadSessions: () => Promise.resolve(),
    openRlmViewer: () => Promise.resolve(),
    connectSSE: (sid, h, opts) => calls.push(`connectSSE(${sid}, cursor=${opts && opts.cursor === null ? 'null' : (opts || {}).cursor})`),
    disconnectSSE() {},
    _knownSession: sid => ({ id: sid, session_type: 'chat', read_only: false }),
    isCompact: () => false,
    renderFileChips() {},
    _showStopButton: () => calls.push('_showStopButton'),
    _showSendButton: () => calls.push('_showSendButton'),
    _recentlyFinished: { delete() {} },
    _offListSessions: { set() {} },
    scrollToBottom() {},
    addCopyButtons() {}, processFileRefs() {},
    announce() {}, updateStatus: m => { if (m) calls.push(`updateStatus(${m})`); },
    _showNotice: m => calls.push(`notice(${m})`),
    humanizeError: e => String((e && e.message) || e),
    closeToolGroup: () => calls.push('closeToolGroup'),
    _renderToolCall: () => calls.push('renderToolCall'),
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, CODE);
  // Anything the extracted source calls that this fixture does not provide.
  // Without it a bare `catch {}` inside app.js eats the ReferenceError and
  // the scenario quietly measures the wrong branch.
  const autoStubbed = stubMissing(ctx, callees(CODE));
  return {
    ctx, calls, gets, loads, state, inner, els,
    peek: expr => run(ctx, `(${expr})`),
    fire: ev => run(ctx, `handleEvent(${JSON.stringify(ev)})`),
    assistants: () => inner.querySelectorAll('.assistant').filter(e => !e.removed).map(e => e.textContent),
  };
}

// One paint interval plus slack (PAINT_INTERVAL_MS is 100).
const PAINT_WAIT = 160;

const IDLE = { event_seq: 10, state: 'idle_ready', status: 'idle', pending_messages: 0 };
const BUSY = { event_seq: 10, state: 'processing', status: 'processing', pending_messages: 0 };
const out = {};

// -- case 1: text in BOTH old windows lands in one bubble, in order ---------
await runScenario(build, async h => {
  guard(h, run(h.ctx, '_softReload()'));
  await settle(2); ck(h);
  // window one: the boundary request is pending
  h.fire({ type: 'stream.token', seq: 11, session_id: 'A', content: 'AAA BBB ' });
  h.gets[0].resolve(BUSY);
  await settle(2); ck(h);
  // window two: the transcript request is pending — the old code had already
  // dropped its guard by here
  h.fire({ type: 'stream.token', seq: 12, session_id: 'A', content: 'CCC DDD ' });
  h.loads[0].resolve();
  await settle(4); ck(h);
  h.fire({ type: 'stream.token', seq: 13, session_id: 'A', content: 'EEE' });
  // The paint cadence is bounded, not instantaneous: give it one interval so
  // the DOM catches up with the buffer it was handed.
  await wait(PAINT_WAIT); await settle(6); ck(h);
  out.case1_bubbles = h.assistants();
  out.case1_collected = h.peek('_collected');
  out.case1_keepScroll = h.loads[0].keepScroll;
});

// -- case 2: tool and completion boundaries inside the window ---------------
await runScenario(build, async h => {
  guard(h, run(h.ctx, '_softReload()'));
  await settle(2); ck(h);
  h.fire({ type: 'stream.token', seq: 11, session_id: 'A', content: 'first round' });
  h.fire({ type: 'tool.call', seq: 12, session_id: 'A', name: 'bash', args: {}, result: 'ok' });
  h.fire({ type: 'stream.token', seq: 13, session_id: 'A', content: 'second round' });
  h.gets[0].resolve(BUSY);
  await settle(2); ck(h);
  h.loads[0].resolve();
  await settle(8); await wait(PAINT_WAIT); await settle(6); ck(h);
  out.case2_bubbles = h.assistants();
  out.case2_orderedReplay = h.calls.filter(c => c === 'closeToolGroup' || c.startsWith('appendMessage(assistant)'));
});

// -- case 3: completion inside the window, then a whole new round ------------
await runScenario(build, async h => {
  guard(h, run(h.ctx, '_softReload()'));
  await settle(2); ck(h);
  h.fire({ type: 'stream.token', seq: 11, session_id: 'A', content: 'round one answer' });
  h.fire({ type: 'stream.done', seq: 12, session_id: 'A', model: 'm' });
  h.gets[0].resolve(IDLE);
  await settle(2); ck(h);
  h.loads[0].resolve();
  await settle(8); ck(h);
  h.fire({ type: 'stream.token', seq: 13, session_id: 'A', content: 'round two answer' });
  h.fire({ type: 'stream.done', seq: 14, session_id: 'A', model: 'm' });
  await settle(8); ck(h);
  out.case3_bubbles = h.assistants();
});

// -- case 4: events at or before the boundary are already in the transcript --
await runScenario(build, async h => {
  guard(h, run(h.ctx, '_softReload()'));
  await settle(2); ck(h);
  h.fire({ type: 'stream.token', seq: 8, session_id: 'A', content: 'stale, already persisted' });
  h.fire({ type: 'stream.token', seq: 11, session_id: 'A', content: 'genuinely new' });
  h.gets[0].resolve(BUSY);
  await settle(2); ck(h);
  h.loads[0].resolve();
  await settle(8); await wait(PAINT_WAIT); await settle(6); ck(h);
  out.case4_bubbles = h.assistants();
});

// -- case 5: navigation during a reload -------------------------------------
await runScenario(build, async h => {
  guard(h, run(h.ctx, '_softReload()'));
  await settle(2); ck(h);
  h.fire({ type: 'stream.token', seq: 11, session_id: 'A', content: "A's private text" });
  guard(h, run(h.ctx, `selectSession('B')`));
  await settle(2); ck(h);
  // B's own snapshot: boundary, then transcript.
  // A's reload is still parked on its boundary request, so B's own snapshot
  // is the first transcript read to happen — index by session, not by order.
  const bStatus = h.gets.find(g => g.url.includes('/B/'));
  bStatus.resolve({ event_seq: 500, state: 'idle_ready', status: 'idle', pending_messages: 0 });
  await until(() => h.loads.find(l => l.sid === 'B')); ck(h);
  h.loads.find(l => l.sid === 'B').resolve();
  await settle(8); ck(h);
  // Now A's reload finally comes back, into a view that has moved on.
  h.gets.find(g => g.url.includes('/A/')).resolve(BUSY);
  await settle(8); ck(h);
  const aLoad = h.loads.find(l => l.sid === 'A');
  if (aLoad) aLoad.resolve();
  await settle(10); ck(h);
  out.case5_aTranscriptReRead = !!aLoad;
  out.case5_visibleSid = h.state.sid;
  out.case5_bubbles = h.assistants();
  out.case5_lastSeq = h.peek('_lastSeq');
  out.case5_reloadOwner = h.peek('_reloadOwner') === null;
  out.case5_streamingEl = h.peek('!!_streamingEl');
});

// -- case 6: a failed snapshot fetch cleans up without applying stale state --
await runScenario(build, async h => {
  guard(h, run(h.ctx, '_softReload()'));
  await settle(2); ck(h);
  h.fire({ type: 'stream.token', seq: 11, session_id: 'A', content: 'buffered' });
  out.case6_buffered = h.peek('_reloadBuffer.length');
  h.gets[0].reject(new Error('server down'));
  await settle(8); ck(h);
  out.case6_reloadOwner = h.peek('_reloadOwner') === null;
  out.case6_bufferDropped = h.peek('_reloadBuffer.length');
  out.case6_lastSeq = h.peek('_lastSeq');
  out.case6_bubbles = h.assistants();
  // A later reload must not be blocked by the failed one's guard.
  guard(h, run(h.ctx, '_softReload()'));
  await settle(2); ck(h);
  out.case6_secondReloadStarted = h.gets.length === 2;
});

// -- case 7: the notice handlers the audit flagged are buffered too ---------
await runScenario(build, async h => {
  guard(h, run(h.ctx, '_softReload()'));
  await settle(2); ck(h);
  h.fire({ type: 'turn.round_renewal', seq: 11, session_id: 'A', granted: 2, authorized: 3, rounds: 20 });
  h.fire({ type: 'context.trim_floor', seq: 12, session_id: 'A', utilization: 0.9, trimmed: 4 });
  out.case7_duringWindow = h.inner.children.length;
  h.gets[0].resolve(IDLE);
  await settle(2); ck(h);
  h.loads[0].resolve();
  await settle(8); ck(h);
  out.case7_afterInstall = h.calls.filter(c => c.startsWith('appendMessage(system)') || c.startsWith('updateStatus')).length;
});

report(out);
"""


@pytest.fixture(scope="module")
def s04(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("s04"))


# ── the filed cases ──────────────────────────────────────────────────────────


def test_text_from_both_reload_windows_lands_in_one_bubble_in_order(s04):
    """Verified case 1: the two windows produced two bubbles holding
    "CCC DDD " and "AAA BBB EEE" — the answer, reversed."""
    assert s04["case1_bubbles"] == ["AAA BBB CCC DDD EEE"]
    assert s04["case1_collected"] == "AAA BBB CCC DDD EEE"


def test_a_token_in_the_second_window_is_not_replaced_by_an_empty_buffer(s04):
    """Verified case 3, word for word with the filed evidence: a token in the
    unguarded window set `_collected` to the real answer, and the status branch
    then assigned `_bufferedDuringReload` — still "" — over it."""
    assert s04["case1_collected"] != ""


def test_a_reload_keeps_the_readers_place(s04):
    """A recovery must not throw a reader who is not pinned to the bottom back
    down to the end of the transcript."""
    assert s04["case1_keepScroll"] is True


def test_tool_boundaries_inside_the_window_are_replayed_in_order(s04):
    """Verified case 2: the old buffer held token strings only, so `tool.call`
    ran its normal branch against a DOM about to be discarded and the two
    rounds were glued into one bubble."""
    assert s04["case2_bubbles"] == ["first round", "second round"]


def test_a_completion_inside_the_window_does_not_swallow_the_next_round(s04):
    assert s04["case3_bubbles"] == ["round one answer", "round two answer"]


def test_events_at_or_before_the_boundary_are_discarded_as_already_shown(s04):
    """The contract's other half: the boundary is read before the transcript,
    so anything at or before it is represented by what was just loaded."""
    assert s04["case4_bubbles"] == ["genuinely new"]


# ── the case the audit added ─────────────────────────────────────────────────


def test_a_reload_that_straddles_a_session_switch_leaks_nothing(s04):
    """Verified case 4 in the audit: `_softReload` had no session token at all,
    so a reload of A installed B's cursor, opened a bubble in B, and rendered
    A's buffered text into B's transcript."""
    assert s04["case5_visibleSid"] == "B"
    assert s04["case5_bubbles"] == []
    assert s04["case5_streamingEl"] is False
    assert s04["case5_lastSeq"] == 500, "B's own boundary, not the one A's reload read"
    assert s04["case5_reloadOwner"] is True


def test_a_failed_snapshot_releases_the_guard_and_drops_the_buffer(s04):
    """Buffered events must not be replayed into a transcript that was never
    refreshed, and the guard must not wedge every later recovery."""
    assert s04["case6_buffered"] == 1
    assert s04["case6_reloadOwner"] is True
    assert s04["case6_bufferDropped"] == 0
    assert s04["case6_lastSeq"] == 0, "no boundary was installed, so none is claimed"
    assert s04["case6_bubbles"] == []
    assert s04["case6_secondReloadStarted"] is True


def test_the_newest_notice_handlers_are_buffered_like_everything_else(s04):
    """`turn.round_renewal` and `context.trim_floor` shared the case-2 defect
    class by construction — neither checked the guard. Buffering at the top of
    `handleEvent` fixes the whole class rather than two more handlers."""
    assert s04["case7_duringWindow"] == 0
    assert s04["case7_afterInstall"] >= 2
