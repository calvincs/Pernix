"""Live audit 2026-09-14 / L12: a session showed a "queued" chip for another
session's message.

`_injectedMessages` was one flat array for the whole app. `turn.complete`
stopped emptying it — deliberately, an unread correction can continue as
queued work after the turn it was aimed at — which left `session.cancelled`
as the only thing that cleared it, and `selectSession` never did. So a
correction injected in A followed by a switch to B before A's
`message.consumed` arrived left a DETACHED element in the array forever, and
B showed "Sent to the running turn · …" for a message that was not B's. It
could not be cleared either: the event that would have clears it by message
id, and it arrives on a stream B is not listening to. A token, a cancel or a
reload was the only way out.

The bookkeeping is a `Map<sessionId, element[]>` now; the chip is rebuilt from
the opened session's own entries on `selectSession`, and an element that is no
longer in the document is dropped on sight because nothing can ever clear it.
"""

from __future__ import annotations

import pytest

from tests.js_harness import requires_node, run_js

pytestmark = requires_node


SCENARIO = r"""
import { decls, fns, makeContext, makeDoc, run, deferred, settle, report,
         runScenario, guard, ck, callees, stubMissing } from './sandbox.mjs';

const dom = makeDoc();

// The real chip lives or dies on whether its bubble is still in the document,
// so the fixture needs a document to be in.
function element(tag) {
  const e = dom.element(tag);
  Object.defineProperty(e, 'isConnected', {
    // makeDoc's removeChild leaves the orphan's parentNode pointing back at
    // the parent it was taken out of, so "has a parentNode" is not enough:
    // the parent has to still be holding it.
    get() {
      let n = e;
      while (n.parentNode) {
        if (!n.parentNode.childNodes.includes(n)) return false;
        n = n.parentNode;
      }
      return n.__root === true;
    },
  });
  return e;
}

const CODE = [
  decls(['_injectedBySession', '_consumedInjectionIds', '_queuedChipShown', '_selectSeq',
         '_expandedKeys', '_pendingFiles', '_lastSeq', '_streamingEl', '_collected',
         '_toolGroup', '_toolGroupCount', '_toolGroupErrors', '_toolGroupLatency',
         '_reloadOwner', '_reloadBuffer', '_joinedMidTurn', '_sessionModelOverride',
         '_lastStreamModel']),
  fns(['_injectedFor', '_restoreQueuedChip', '_clearConsumedInjections', '_setQueuedChip',
       '_injectMessage', 'selectSession']),
].join('\n\n');

function build(stubbed) {
  const log = [];
  const chip = element('div'); chip.hidden = true;
  const messages = element('div'); messages.__root = true;
  const els = { 'queued-chip': chip, 'msg-input': element('textarea'), 'send-btn': element('button') };
  const state = { sid: 'A', streaming: false, sessions: [], spaces: [], model: 'm' };
  const gates = { inject: deferred() };
  const base = {
    console, setTimeout, clearTimeout, setInterval, clearInterval, Date, Set, Map, JSON, Object, String, Number,
    state,
    document: {
      getElementById: id => els[id] || null,
      querySelector: () => null, querySelectorAll: () => [],
      createElement: element, createTextNode: dom.textNode,
      addEventListener() {}, body: element('body'),
    },
    window: { dispatchEvent() {}, addEventListener() {} },
    clear: e => { while (e.firstChild) e.removeChild(e.firstChild); },
    text: v => dom.textNode(String(v)),
    appendMessage(role, txt) {
      const e = element('div');
      e.classList.add('message', role);
      messages.appendChild(e);
      log.push(`appendMessage(${role}) ${JSON.stringify(String(txt || '').slice(0, 40))}`);
      return e;
    },
    post(url, body) { log.push(`post(${url}, ${body.session_id})`); return gates.inject.promise; },
    get: () => Promise.resolve({ event_seq: 7, state: 'idle_ready', status: 'idle' }),
    announce: m => log.push(`announce ${JSON.stringify(m)}`),
    notify: (kind, m) => log.push(`notify(${kind}) ${JSON.stringify(String(m).slice(0, 60))}`),
    loadMessages: sid => { log.push(`loadMessages(${sid})`); return Promise.resolve(); },
    loadContextInfo: () => Promise.resolve(),
    loadPendingQuestions: () => Promise.resolve(),
    openRlmViewer: () => Promise.resolve(),
    connectSSE: () => {}, disconnectSSE: () => {},
    _knownSession: sid => ({ id: sid, session_type: 'chat', read_only: false }),
    isCompact: () => false,
    _recentlyFinished: { delete() {} },
    _offListSessions: { set() {} },
    renderFileChips() {},
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, CODE);
  stubMissing(ctx, callees(CODE));
  return {
    ctx, log, state, chip, messages, gates,
    peek: expr => run(ctx, `(${expr})`),
    chipText: () => (chip.hidden ? null : chip.textContent),
    // What a real transcript render does to the bubbles of the session
    // being left: they are thrown away.
    clearTranscript: () => { while (messages.firstChild) messages.removeChild(messages.firstChild); },
  };
}

const out = {};

// -- the filed case: inject in A, switch to B before the POST lands ----------
await runScenario(build, async h => {
  h.state.sid = 'A';
  guard(h, run(h.ctx, `_injectMessage('please also check the logs')`));
  await settle(); ck(h);
  out.case1_postedTo = h.log.filter(l => l.startsWith('post('));
  guard(h, run(h.ctx, `selectSession('B')`));
  await settle(10); ck(h);
  out.case1_chipInB_beforeAck = h.chipText();
  // A's acknowledgement arrives while B is on screen.
  h.gates.inject.resolve({ status: 'injected', message_id: 41 });
  await settle(10); ck(h);
  out.case1_chipInB = h.chipText();
  out.case1_bookkeeping = h.peek('[..._injectedBySession.keys()]');
  out.case1_aStillPending = h.peek(`_injectedFor('A').length`);
  // ...and back to A, where the message really is still queued.
  guard(h, run(h.ctx, `selectSession('A')`));
  await settle(10); ck(h);
  out.case1_chipBackInA = h.chipText();
});

// -- the chip belongs to the session it was raised in ------------------------
await runScenario(build, async h => {
  h.state.sid = 'A';
  guard(h, run(h.ctx, `_injectMessage('one')`));
  await settle(); ck(h);
  h.gates.inject.resolve({ status: 'injected', message_id: 7 });
  await settle(10); ck(h);
  out.case2_chipInA = h.chipText();
  out.case2_announced = h.log.filter(l => l.startsWith('announce'));
  guard(h, run(h.ctx, `selectSession('B')`));
  await settle(10); ck(h);
  out.case2_chipInB = h.chipText();
});

// -- a bubble the transcript threw away cannot hold a chip up ----------------
await runScenario(build, async h => {
  h.state.sid = 'A';
  guard(h, run(h.ctx, `_injectMessage('one')`));
  await settle(); ck(h);
  h.gates.inject.resolve({ status: 'injected', message_id: 7 });
  await settle(10); ck(h);
  // What loadMessages does on the way back into a session.
  h.clearTranscript();
  guard(h, run(h.ctx, `selectSession('A')`));
  await settle(10); ck(h);
  out.case3_chip = h.chipText();
  out.case3_keys = h.peek('[..._injectedBySession.keys()]');
});

// -- message.consumed clears the right session's entry, from anywhere --------
await runScenario(build, async h => {
  h.state.sid = 'A';
  guard(h, run(h.ctx, `_injectMessage('one')`));
  await settle(); ck(h);
  h.gates.inject.resolve({ status: 'injected', message_id: 7 });
  await settle(10); ck(h);
  guard(h, run(h.ctx, `selectSession('B')`));
  await settle(10); ck(h);
  // handleEvent's message.consumed branch, minus handleEvent.
  run(h.ctx, `_consumedInjectionIds.add('7'); _clearConsumedInjections();`);
  out.case4_keys = h.peek('[..._injectedBySession.keys()]');
  out.case4_chipInB = h.chipText();
  guard(h, run(h.ctx, `selectSession('A')`));
  await settle(10); ck(h);
  out.case4_chipBackInA = h.chipText();
});

// -- a refused inject leaves nothing behind ----------------------------------
await runScenario(build, async h => {
  h.state.sid = 'A';
  guard(h, run(h.ctx, `_injectMessage('one')`));
  await settle(); ck(h);
  h.gates.inject.reject(new Error('session is gone'));
  await settle(10); ck(h);
  out.case5_chip = h.chipText();
  out.case5_keys = h.peek('[..._injectedBySession.keys()]');
  out.case5_systemLine = h.log.filter(l => l.includes('Inject failed')).length;
});

// -- ...and says so in the session it was typed in, not the one on screen ----
await runScenario(build, async h => {
  h.state.sid = 'A';
  guard(h, run(h.ctx, `_injectMessage('one')`));
  await settle(); ck(h);
  guard(h, run(h.ctx, `selectSession('B')`));
  await settle(10); ck(h);
  h.log.length = 0;
  h.gates.inject.reject(new Error('session is gone'));
  await settle(10); ck(h);
  out.case6_transcriptWrites = h.log.filter(l => l.startsWith('appendMessage'));
  out.case6_chipInB = h.chipText();
  out.case6_notifies = h.log.filter(l => l.startsWith('notify'));
});

report(out);
"""


@pytest.fixture(scope="module")
def l12(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("l12"))


# ── the filed case ───────────────────────────────────────────────────────────


def test_a_correction_injected_in_one_session_does_not_chip_another(l12):
    """The headline: B showed "Sent to the running turn" for A's message, and
    nothing short of a token, a cancel or a reload could take it down."""
    assert l12["case1_postedTo"] == ["post(/api/chat/inject, A)"]
    assert l12["case1_chipInB_beforeAck"] is None
    assert l12["case1_chipInB"] is None


def test_the_message_is_still_recorded_against_its_own_session(l12):
    """Not "clear everything on switch": A's message really is queued, and A
    has to say so when it is opened again."""
    assert l12["case1_bookkeeping"] == ["A"]
    assert l12["case1_aStillPending"] == 1
    assert l12["case1_chipBackInA"] == "Sent to the running turn · “please also check the logs”"


def test_the_chip_is_raised_in_the_session_it_belongs_to(l12):
    assert l12["case2_chipInA"] == "Sent to the running turn · “one”"
    assert l12["case2_announced"] == ['announce "Sent to the running turn"']
    assert l12["case2_chipInB"] is None


# ── the entry that could never be cleared ────────────────────────────────────


def test_a_bubble_the_transcript_threw_away_is_dropped(l12):
    """A detached element cannot be cleared by message.consumed — the class it
    carries is on a node nobody can see — so it used to hold the chip up for
    the rest of the session. The pending queue is re-read from the server by
    _markPendingQueued once the transcript lands."""
    assert l12["case3_chip"] is None
    assert l12["case3_keys"] == []


def test_message_consumed_clears_the_right_session_from_anywhere(l12):
    """The acknowledgement arrives on A's stream, which may well be read while
    B is on screen."""
    assert l12["case4_keys"] == []
    assert l12["case4_chipInB"] is None
    assert l12["case4_chipBackInA"] is None


# ── failures ─────────────────────────────────────────────────────────────────


def test_a_refused_inject_leaves_no_entry_and_no_chip(l12):
    assert l12["case5_chip"] is None
    assert l12["case5_keys"] == []
    assert l12["case5_systemLine"] == 1


def test_a_refused_inject_does_not_report_into_someone_elses_transcript(l12):
    """The same rule the 3.2.2 send() fix established: a late failure belongs
    to the session it was typed in, not to whatever is on screen."""
    assert l12["case6_transcriptWrites"] == []
    assert l12["case6_chipInB"] is None
    assert l12["case6_notifies"], "the user still has to be told the message was not queued"
