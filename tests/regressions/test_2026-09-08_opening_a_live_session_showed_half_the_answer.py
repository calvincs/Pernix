"""Audit 3.2.2 / S02, 2026-09-08: opening a session that was already answering
showed a suffix of the answer — sometimes none of it — and nothing could ever
repair it.

`selectSession()` did three separate reads and treated them as one snapshot:
it loaded the transcript, then fetched `/status`, then subscribed. The
subscription carried no replay cursor at all, and the server only replayed for
a positive `last_event_id`, so `api/streaming.py` skipped the replay branch
entirely. Anything emitted between the transcript read and the status read
therefore existed in neither place: an authoritative "The answer is
forty-two." rendered as "forty-two.".

When the whole round completed inside that window it was worse. The assistant
row is persisted just before `stream.done`, so a round finishing after the
transcript read but before the status read left the answer out of the
transcript, and the `stream.done` that carried it was then swallowed by the
`seq <= _lastSeq` dedup — because `_lastSeq` had just been set *to* the
server's counter. That is also why it was permanently unrepairable:
`_reconcile()` and the 45 s interval both compare `event_seq` against
`_lastSeq`, and those two numbers had been made equal, so drift read as zero
forever. It survived until the user re-selected the session or reloaded.

This is a live-view defect only. `_save_turn_msg` persists the assistant's
content before `stream.done`, so the stored transcript was always intact.

The fix defines one contract, used by both `selectSession()` and
`_softReload()`: a view's content is snapshot(B) plus every event with
seq > B, where B is a boundary read BEFORE the transcript. The subscription is
opened with B as a replay cursor, so the window between the reads is covered
by the server's own replay buffer. Because a cursor cannot recover an
in-flight prefix that is in neither persisted history nor retained events, a
view that joins a turn mid-flight marks itself and re-reads the completed
answer from the database once — reconciliation by stable identity rather than
a fabricated completion.

The wire now distinguishes the four cases the audit asked for: no replay
requested, cursor zero, server restart, and an expired replay buffer.
"""

from __future__ import annotations

import asyncio
from collections import deque

import pytest

from api.streaming import event_stream
from sessions.state import AgentSession
from tests.js_harness import requires_node, run_js

# ===========================================================================
# The client half: one snapshot, one cursor, one repair
# ===========================================================================

SCENARIO = r"""
import { decls, fns, makeContext, makeDoc, run, deferred, settle, report, runScenario, guard, ck, callees, stubMissing } from './sandbox.mjs';

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
  const calls = [], store = {};
  const textarea = dom.element('textarea'); textarea.value = '';
  const els = { 'msg-input': textarea, 'send-btn': dom.element('button'),
                'status-info': dom.element('div'), 'status-notice': dom.element('div'),
                'file-chips': dom.element('div') };
  const inner = dom.element('div');
  const state = { sid: null, streaming: false, sessions: [], spaces: [], model: 'm' };
  let statusReply = { event_seq: 40, state: 'processing', status: 'processing' };
  const base = {
    console, setTimeout, clearTimeout, setInterval, clearInterval, Date, Set, Map, JSON, RegExp, Number, Math,
    state,
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; }, removeItem: k => { delete store[k]; } },
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
    get: url => { calls.push(`GET ${url}`); return Promise.resolve(statusReply); },
    post: () => Promise.resolve({}),
    loadMessages: (sid, o) => { calls.push(`loadMessages(${sid}${o && o.keepScroll ? ',keepScroll' : ''})`); return Promise.resolve(); },
    loadContextInfo: () => { calls.push('loadContextInfo'); return Promise.resolve(); },
    loadPendingQuestions: () => { calls.push('loadPendingQuestions'); return Promise.resolve(); },
    loadSessions: () => Promise.resolve(),
    openRlmViewer: () => Promise.resolve(),
    connectSSE: (sid, h, opts) => calls.push(`connectSSE(${sid}, cursor=${opts && opts.cursor === null ? 'null' : (opts || {}).cursor})`),
    disconnectSSE: () => calls.push('disconnectSSE'),
    _knownSession: sid => ({ id: sid, session_type: 'chat', read_only: false }),
    isCompact: () => false,
    renderFileChips() {},
    _showStopButton: () => calls.push('_showStopButton'),
    _showSendButton: () => calls.push('_showSendButton'),
    _recentlyFinished: { delete() {} },
    _offListSessions: { set() {} },
    scrollToBottom() {},
    addCopyButtons() {}, processFileRefs() {},
    announce() {}, updateStatus() {}, _showNotice: m => calls.push(`notice(${m})`),
    humanizeError: e => String((e && e.message) || e),
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, CODE);
  // Anything the extracted source calls that this fixture does not provide.
  // Without it a bare `catch {}` inside app.js eats the ReferenceError and
  // the scenario quietly measures the wrong branch.
  const autoStubbed = stubMissing(ctx, callees(CODE));
  return {
    ctx, calls, state, inner, els,
    setStatus: s => { statusReply = s; },
    peek: expr => run(ctx, `(${expr})`),
    fire: ev => run(ctx, `handleEvent(${JSON.stringify(ev)})`),
    bubbleText: () => run(ctx, `_streamingEl ? _streamingEl.querySelector('.content').textContent : null`),
  };
}

const out = {};

// -- the snapshot is taken in one order, and the cursor covers the window ---
await runScenario(build, async h => {
  h.setStatus({ event_seq: 40, state: 'processing', status: 'processing' });
  guard(h, run(h.ctx, `selectSession('A')`));
  await settle(15); ck(h);
  out.order = h.calls.filter(c => /^GET|^loadMessages|^loadPendingQuestions|^connectSSE/.test(c));
  out.lastSeq = h.peek('_lastSeq');
  out.joinedMidTurn = h.peek('_joinedMidTurn');
});

// -- cursor zero is a real request, not "no cursor" -------------------------
await runScenario(build, async h => {
  h.setStatus({ event_seq: 0, state: 'idle_ready', status: 'idle' });
  guard(h, run(h.ctx, `selectSession('FRESH')`));
  await settle(15); ck(h);
  out.freshConnect = h.calls.filter(c => c.startsWith('connectSSE'));
  out.freshJoined = h.peek('_joinedMidTurn');
});

// -- a session reaped from memory reports a null seq: no boundary to trust --
await runScenario(build, async h => {
  h.setStatus({ status: 'idle', in_memory: false, event_seq: null });
  guard(h, run(h.ctx, `selectSession('REAPED')`));
  await settle(15); ck(h);
  out.reapedConnect = h.calls.filter(c => c.startsWith('connectSSE'));
});

// -- an unreachable status endpoint must not invent a cursor ----------------
await runScenario(build, async h => {
  run(h.ctx, `get = () => Promise.reject(new Error('offline'))`);
  guard(h, run(h.ctx, `selectSession('DOWN')`));
  await settle(15); ck(h);
  out.downConnect = h.calls.filter(c => c.startsWith('connectSSE'));
  out.downLastSeq = h.peek('_lastSeq');
});

// -- joining mid-turn: the completed answer is re-read, not left a suffix ---
await runScenario(build, async h => {
  h.setStatus({ event_seq: 40, state: 'processing', status: 'processing' });
  guard(h, run(h.ctx, `selectSession('A')`));
  await settle(15); ck(h);
  h.calls.length = 0;
  // Only the tail of the answer is replayable — the prefix was emitted before
  // the boundary and never persisted.
  h.fire({ type: 'stream.token', seq: 41, session_id: 'A', content: 'forty-two.' });
  await settle(4); ck(h);
  out.midTurnVisible = h.bubbleText();
  h.fire({ type: 'stream.done', seq: 42, session_id: 'A', model: 'm' });
  await settle(15); ck(h);
  out.midTurnRepair = h.calls.filter(c => c.startsWith('loadMessages') || c.startsWith('GET'));
  out.midTurnFlagCleared = h.peek('_joinedMidTurn');
});

// -- a completed turn we did NOT join must not trigger a re-read ------------
await runScenario(build, async h => {
  h.setStatus({ event_seq: 40, state: 'idle_ready', status: 'idle' });
  guard(h, run(h.ctx, `selectSession('A')`));
  await settle(15); ck(h);
  h.calls.length = 0;
  h.fire({ type: 'stream.token', seq: 41, session_id: 'A', content: 'The answer is forty-two.' });
  await settle(4); ck(h);
  h.fire({ type: 'stream.done', seq: 42, session_id: 'A', model: 'm' });
  await settle(15); ck(h);
  out.cleanTurnVisible = h.inner.querySelectorAll('.assistant').map(e => e.textContent);
  out.cleanTurnReReads = h.calls.filter(c => c.startsWith('loadMessages'));
});

// -- stream.resume: expired replay buffer ------------------------------------
await runScenario(build, async h => {
  h.setStatus({ event_seq: 40, state: 'idle_ready', status: 'idle' });
  guard(h, run(h.ctx, `selectSession('A')`));
  await settle(15); ck(h);
  h.calls.length = 0;
  h.fire({ type: 'stream.resume', session_id: 'A', from_seq: 40, replayed: 5,
           oldest_retained: 300, server_seq: 900, complete: false });
  await settle(15); ck(h);
  out.expiredReload = h.calls.filter(c => c.startsWith('loadMessages'));
});

// -- stream.resume: the server restarted and its counter reset --------------
await runScenario(build, async h => {
  h.setStatus({ event_seq: 900, state: 'idle_ready', status: 'idle' });
  guard(h, run(h.ctx, `selectSession('A')`));
  await settle(15); ck(h);
  h.calls.length = 0;
  h.setStatus({ event_seq: 3, state: 'idle_ready', status: 'idle' });
  h.fire({ type: 'stream.resume', session_id: 'A', from_seq: 900, replayed: 0,
           oldest_retained: 1, server_seq: 3, complete: true });
  await settle(15); ck(h);
  out.restartReload = h.calls.filter(c => c.startsWith('loadMessages'));
  out.restartLastSeq = h.peek('_lastSeq');
});

// -- stream.resume: a clean resume changes nothing ---------------------------
await runScenario(build, async h => {
  h.setStatus({ event_seq: 40, state: 'idle_ready', status: 'idle' });
  guard(h, run(h.ctx, `selectSession('A')`));
  await settle(15); ck(h);
  h.calls.length = 0;
  h.fire({ type: 'stream.resume', session_id: 'A', from_seq: 40, replayed: 3,
           oldest_retained: 12, server_seq: 43, complete: true });
  await settle(15); ck(h);
  out.cleanResume = h.calls.slice();
});

report(out);
"""


@pytest.fixture(scope="module")
def s02(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("s02"))


@requires_node
def test_the_boundary_is_read_before_the_transcript_and_carried_as_a_cursor(s02):
    """The whole finding in one assertion. Status first establishes the
    boundary; the transcript read after it therefore contains everything
    persisted at that boundary; and the subscription asks the server to replay
    everything after it, which covers every await in between."""
    assert s02["order"] == [
        "GET /api/sessions/A/status",
        "loadMessages(A)",
        "loadPendingQuestions",
        "connectSSE(A, cursor=40)",
    ]
    assert s02["lastSeq"] == 40


@requires_node
def test_opening_a_generating_session_marks_itself_as_holding_a_suffix(s02):
    """A cursor cannot recover an in-flight prefix: it is in neither the
    persisted transcript nor the retained events. The view has to know that."""
    assert s02["joinedMidTurn"] is True
    assert s02["freshJoined"] is False


@requires_node
def test_a_fresh_session_asks_for_cursor_zero_not_for_no_cursor(s02):
    """Cursor zero means "I have seen nothing, send everything you retain".
    Sending no cursor means "just join me to the live stream". Collapsing them
    is what made the initial connection unable to ask for replay at all."""
    assert s02["freshConnect"] == ["connectSSE(FRESH, cursor=0)"]


@requires_node
def test_a_missing_or_unreadable_boundary_asks_for_no_replay(s02):
    """A reaped session reports a null `event_seq`, and an unreachable status
    endpoint reports nothing. Guessing a cursor here would replay a whole
    session's events over a transcript that already contains them."""
    assert s02["reapedConnect"] == ["connectSSE(REAPED, cursor=null)"]
    assert s02["downConnect"] == ["connectSSE(DOWN, cursor=null)"]
    assert s02["downLastSeq"] == 0


@requires_node
def test_a_mid_turn_join_re_reads_the_completed_answer_once(s02):
    """The repair for the prefix that cannot be replayed: when the turn ends,
    the database holds the whole answer, so read it back by identity instead of
    leaving a half-answer no drift check can see."""
    assert s02["midTurnVisible"] == "forty-two.", "the live view genuinely only has the suffix"
    assert any(c.startswith("loadMessages") for c in s02["midTurnRepair"])
    assert s02["midTurnFlagCleared"] is False


@requires_node
def test_a_turn_seen_from_its_start_is_not_re_read(s02):
    """The repair must be paid for only where it is needed."""
    assert s02["cleanTurnVisible"] == ["The answer is forty-two."]
    assert s02["cleanTurnReReads"] == []


@requires_node
def test_an_expired_replay_buffer_refreshes_instead_of_pretending(s02):
    """`complete: false` says the retained ring no longer reaches the cursor,
    so the gap cannot be filled from events at all."""
    assert s02["expiredReload"] == ["loadMessages(A,keepScroll)"]


@requires_node
def test_a_server_restart_is_told_apart_from_a_clean_resume(s02):
    """A counter that went backwards would otherwise make every future event
    fail the `seq <= _lastSeq` dedup, and the view would silently go dead."""
    assert s02["restartReload"] == ["loadMessages(A,keepScroll)"]
    assert s02["restartLastSeq"] == 3
    assert s02["cleanResume"] == [], "a complete resume is not an event"


# ===========================================================================
# The server half: what a cursor actually means on the wire
# ===========================================================================


@pytest.fixture
def fast_heartbeat(monkeypatch):
    import api.streaming as sm

    monkeypatch.setattr(sm, "_shutdown_event", None)
    monkeypatch.setattr(sm, "HEARTBEAT_INTERVAL", 0.05)
    return sm


async def _drain(session, last_event_id):
    """Everything the stream emits before it goes quiet."""
    chunks: list[str] = []
    gen = event_stream(session, last_event_id=last_event_id)
    try:
        async with asyncio.timeout(1.0):
            async for chunk in gen:
                if chunk.startswith(": heartbeat"):
                    break
                chunks.append(chunk)
    except (asyncio.TimeoutError, TimeoutError):
        pass
    finally:
        await gen.aclose()
    return chunks


def _resume_frame(chunks):
    import json

    for chunk in chunks:
        if "event: stream.resume" in chunk:
            return json.loads(chunk.split("data: ", 1)[1].strip())
    return None


def _session_with(n_events, maxlen=2000):
    session = AgentSession(session_id="s02")
    session.events = deque(maxlen=maxlen)
    for i in range(n_events):
        session.emit_event({"type": "stream.token", "content": f"tok{i}"})
    return session


async def test_no_cursor_means_no_replay_and_no_resume_frame(fast_heartbeat):
    """The default request is still "join me to the live stream"."""
    session = _session_with(4)
    chunks = await _drain(session, None)
    assert chunks == []


async def test_cursor_zero_replays_everything_retained(fast_heartbeat):
    """The case the old signature could not express: it defaulted a missing
    cursor to 0 and then refused to replay for it."""
    session = _session_with(4)
    chunks = await _drain(session, 0)
    frame = _resume_frame(chunks)
    assert frame is not None
    assert frame["replayed"] == 4
    assert frame["complete"] is True
    assert frame["from_seq"] == 0
    assert sum("event: stream.token" in c for c in chunks) == 4


async def test_a_positive_cursor_still_replays_only_what_follows_it(fast_heartbeat):
    """The pre-existing behaviour this must not regress."""
    session = _session_with(6)
    chunks = await _drain(session, 4)
    frame = _resume_frame(chunks)
    assert frame["replayed"] == 2
    assert frame["complete"] is True
    tokens = [c for c in chunks if "event: stream.token" in c]
    assert len(tokens) == 2
    assert "id: 5" in tokens[0] and "id: 6" in tokens[1]


async def test_an_evicted_buffer_reports_an_incomplete_replay(fast_heartbeat):
    """The failure that used to be invisible: the client got a partial history
    that looked exactly like a complete one."""
    session = _session_with(10, maxlen=5)
    chunks = await _drain(session, 2)
    frame = _resume_frame(chunks)
    assert frame["complete"] is False
    assert frame["oldest_retained"] == 6
    assert frame["server_seq"] == 10


async def test_a_restarted_counter_is_visible_in_the_resume_frame(fast_heartbeat):
    """`server_seq` below the cursor is the client's only way to notice that
    the in-memory counter reset under it."""
    session = _session_with(3)
    chunks = await _drain(session, 900)
    frame = _resume_frame(chunks)
    assert frame["server_seq"] == 3
    assert frame["from_seq"] == 900
    assert frame["replayed"] == 0


async def test_the_replay_snapshot_is_still_taken_before_the_first_yield(fast_heartbeat):
    """The deque-mutation guard is load-bearing (see the comment in
    api/streaming.py): `session.events` is a live deque the agent appends to,
    and a yield inside the iteration raised "deque mutated during iteration",
    killing the stream on exactly the reconnect that needed it."""
    session = _session_with(4)
    gen = event_stream(session, last_event_id=1)
    first = await gen.__anext__()
    assert "event: stream.resume" in first
    # Mutating mid-iteration is precisely what used to break it.
    for i in range(50):
        session.emit_event({"type": "stream.token", "content": f"late{i}"})
    replayed = []
    async with asyncio.timeout(1.0):
        for _ in range(3):
            replayed.append(await gen.__anext__())
    await gen.aclose()
    assert all("event: stream.token" in c for c in replayed)


async def test_the_resume_frame_carries_no_event_id(fast_heartbeat):
    """It is a control frame about the stream, not an event in it. Giving it an
    id would let a native EventSource reconnect adopt it as Last-Event-ID."""
    session = _session_with(2)
    chunks = await _drain(session, 0)
    resume = next(c for c in chunks if "event: stream.resume" in c)
    assert not resume.startswith("id:")
