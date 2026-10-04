"""Audit 3.2.2 / S01, 2026-09-08: a message typed in one chat was persisted
into another, and a failure in one chat wiped out a second one's composer.

`send()` captured nothing. It read the module-level `state.sid` again at POST
time, after awaiting the attachment upload — so starting a send in session A
and clicking session B while the upload ran filed A's text *and* A's uploaded
filename under `{"session_id": "B"}`. `_sending` was no defence: it gates the
Send button, not navigation, and none of its four call sites is navigational.

The same missing capture ran the other way. A late rejection from A's
`/api/chat` removed B's live streaming bubble, forced `state.streaming` to
false and wrote A's text into B's draft; a failed upload appended its "N
upload(s) failed" line to whatever transcript was on screen; and
`clearPendingFiles()` deleted attachments the user had added to B. Navigation
during new-session creation was worse than merely losing a click: the response
reassigned `state.sid`, moving the composer, the SSE connection and the event
cursor to a session nobody had picked while the other transcript stayed on
screen — a silent session swap. And because `_sending` was one flag for the
whole app, a slow upload in A made B unsendable, with the button disabled and
nothing on screen to say why.

The fix gives every submission an owner — the destination session plus the
selection generation it was started in (`_viewOwner()` / `_ownsView()`). The
POST always names the captured destination; every post-await UI mutation,
upload error, draft restoration and `finally` cleanup asks first whether it
still holds the view; in-flight state is keyed per session; and the attachment
list is owned by the submission, detached from the composer on the way out.
"""

from __future__ import annotations

import pytest

from tests.js_harness import requires_node, run_js

pytestmark = requires_node


# The scenario source is one module. Each case builds a fresh vm context from
# the real app.js bytes, so nothing leaks between them.
SCENARIO = r"""
import { decls, fns, makeContext, makeDoc, run, deferred, settle, report, runScenario, guard, ck, callees, stubMissing } from './sandbox.mjs';

const dom = makeDoc();

const CODE = [
  decls(['_pendingFiles', '_sendingSids', '_selectSeq', '_streamingEl', '_collected', '_toolGroup',
         '_lastSeq', '_toolGroupCount', '_toolGroupErrors', '_toolGroupLatency',
         '_reloadOwner', '_reloadBuffer', '_joinedMidTurn',
         '_paintTimer', '_paintDirty', '_paintOwner',
         '_sessionModelOverride', '_histIdx', '_expandedKeys', '_lastStreamModel', '_DRAFT_PREFIX']),
  fns(['_viewOwner', '_ownsView', '_sendKey', '_setSendingState', '_syncSendEnabled',
       '_saveDraftFor', '_uploadWithProgress', 'send', 'selectSession']),
  `globalThis.__set = (name, v) => { globalThis[name] = v; };`,
].join('\n\n');

function build(stubbed) {
  const log = [], posts = [], xhrs = [];
  const store = {};
  const textarea = dom.element('textarea');
  textarea.value = ''; textarea.disabled = false;
  const sendBtn = dom.element('button'); sendBtn.disabled = false;
  const els = {
    'msg-input': textarea, 'send-btn': sendBtn,
    'status-info': dom.element('div'), 'file-chips': dom.element('div'),
  };
  const state = { sid: 'A', streaming: false, sessions: [], spaces: [], model: 'm' };
  const gates = { chat: deferred(), newSession: deferred() };
  let statusReply = { event_seq: 7, state: 'idle_ready', status: 'idle' };
  const base = {
    console, setTimeout, clearTimeout, setInterval, clearInterval, Date, Set, Map, JSON, RegExp, Number,
    Event: class { constructor(t) { this.type = t; } },
    FormData: class { append() {} },
    state, MAX_MESSAGE_CHARS: 100000, SLASH_COMMANDS: {},
    localStorage: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: k => { delete store[k]; },
    },
    document: {
      getElementById: id => els[id] || null,
      querySelector: () => null, querySelectorAll: () => [],
      createElement: t => dom.element(t), addEventListener() {},
      body: dom.element('body'),
    },
    appendMessage(role, txt) {
      const e = dom.element('div'); e.classList.add('message', role); e.text = txt;
      log.push(`appendMessage(${role}) ${JSON.stringify(String(txt || '').slice(0, 60))}`);
      return e;
    },
    notify(kind, msg) { log.push(`notify(${kind}) ${JSON.stringify(String(msg).slice(0, 60))}`); },
    humanizeError: e => String((e && e.message) || e),
    post(url, body) {
      posts.push({ url, body: JSON.parse(JSON.stringify(body)) });
      if (url === '/api/chat') return gates.chat.promise;
      if (url === '/api/sessions') return gates.newSession.promise;
      return Promise.resolve({});
    },
    get: () => Promise.resolve(statusReply),
    loadMessages: () => Promise.resolve(),
    loadContextInfo: () => Promise.resolve(),
    loadSessions: () => Promise.resolve(),
    loadPendingQuestions: () => Promise.resolve(),
    openRlmViewer: () => Promise.resolve(),
    connectSSE: (sid, h, opts) => log.push(`connectSSE(${sid}, cursor=${opts && opts.cursor})`),
    disconnectSSE: () => log.push('disconnectSSE()'),
    _knownSession: sid => ({ id: sid, session_type: 'chat', read_only: false }),
    isCompact: () => false,
    clearPendingFiles() { log.push('clearPendingFiles()'); run(ctx, '_pendingFiles = []'); },
    renderFileChips() {},
    _setChipProgress() {},
    getAuthToken: () => 't',
    XMLHttpRequest: class {
      constructor() { this.upload = {}; this.status = 200; this.responseText = ''; xhrs.push(this); }
      open() {} setRequestHeader() {} send() {}
    },
    _showStopButton: () => log.push('_showStopButton()'),
    _showSendButton: () => log.push('_showSendButton()'),
    _recentlyFinished: { delete() {} },
    _offListSessions: { set() {} },
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, CODE);
  // Anything the extracted source calls that this fixture does not provide.
  // Without it a bare `catch {}` inside app.js eats the ReferenceError and
  // the scenario quietly measures the wrong branch.
  const autoStubbed = stubMissing(ctx, callees(CODE));
  return {
    ctx, log, posts, xhrs, textarea, sendBtn, state, store, gates, els,
    setStatus: s => { statusReply = s; },
    peek: expr => run(ctx, `(${expr})`),
  };
}

const out = {};

// -- case 1: upload for A in flight, user selects B, upload then resolves ----
await runScenario(build, async h => {
  h.state.sid = 'A';
  h.textarea.value = 'secret note for A';
  run(h.ctx, `_pendingFiles = [{ name: 'a.txt', file: {}, uploaded: false, serverName: null }]`);
  guard(h, run(h.ctx, 'send()'));
  await settle(); ck(h);
  if (h.xhrs.length !== 1) throw new Error('upload never started');
  out.case1_sendDisabledInA = h.peek(`(_syncSendEnabled(), document.getElementById('send-btn').disabled)`);
  guard(h, run(h.ctx, `selectSession('B')`));
  await settle(); ck(h);
  h.textarea.value = '';                       // B's composer, freshly empty
  run(h.ctx, `_pendingFiles = [{ name: 'b-new.txt', file: {}, uploaded: false }]`);
  out.case1_sendDisabledInB = h.peek(`(_syncSendEnabled(), document.getElementById('send-btn').disabled)`);
  const x = h.xhrs[0];
  x.status = 200; x.responseText = JSON.stringify({ filename: 'a.txt' });
  x.onload();
  await settle(10); ck(h);
  const chat = h.posts.find(p => p.url === '/api/chat');
  out.case1_postedTo = chat ? chat.body.session_id : null;
  out.case1_postedText = chat ? chat.body.message : null;
  out.case1_bComposer = h.textarea.value;
  out.case1_bAttachments = h.peek('_pendingFiles.map(f => f.name)');
  out.case1_visibleSid = h.state.sid;
});

// -- case 2: A's POST rejects while B is streaming ---------------------------
await runScenario(build, async h => {
  h.state.sid = 'A';
  h.textarea.value = 'note for A';
  run(h.ctx, '_pendingFiles = []');
  guard(h, run(h.ctx, 'send()'));
  await settle(); ck(h);
  h.setStatus({ event_seq: 7, state: 'processing', status: 'processing' });
  guard(h, run(h.ctx, `selectSession('B')`));
  await settle(10); ck(h);
  const bBubble = h.peek('_streamingEl');
  h.textarea.value = '';
  h.log.length = 0;
  h.gates.chat.reject(new Error('session A is gone'));
  await settle(10); ck(h);
  out.case2_bBubbleRemoved = !!(bBubble && bBubble.removed);
  out.case2_bStillStreaming = h.state.streaming;
  out.case2_bHasStreamingEl = h.peek('!!_streamingEl');
  out.case2_bComposer = h.textarea.value;
  out.case2_aDraftSaved = h.store['pernix:draft:A'] || null;
  out.case2_bDraftSaved = h.store['pernix:draft:B'] || null;
  out.case2_log = h.log.slice();
});

// -- case 3: navigation during new-session creation --------------------------
await runScenario(build, async h => {
  h.state.sid = null;
  h.textarea.value = 'first message';
  run(h.ctx, '_pendingFiles = []');
  guard(h, run(h.ctx, 'send()'));
  await settle(); ck(h);
  guard(h, run(h.ctx, `selectSession('B')`));
  await settle(10); ck(h);
  h.log.length = 0;
  h.gates.newSession.resolve({ session_id: 'NEW' });
  await settle(10); ck(h);
  out.case3_visibleSid = h.state.sid;
  const chat = h.posts.find(p => p.url === '/api/chat');
  out.case3_postedTo = chat ? chat.body.session_id : null;
  out.case3_connectLog = h.log.filter(l => l.startsWith('connectSSE'));
  out.case3_lastSeq = h.peek('_lastSeq');
});

// -- case 4: A -> B -> A; the FIRST A's submission owns nothing --------------
await runScenario(build, async h => {
  h.state.sid = 'A';
  h.textarea.value = 'from the first visit to A';
  run(h.ctx, `_pendingFiles = [{ name: 'a.txt', file: {}, uploaded: false }]`);
  guard(h, run(h.ctx, 'send()'));
  await settle(); ck(h);
  guard(h, run(h.ctx, `selectSession('B')`));
  await settle(10); ck(h);
  guard(h, run(h.ctx, `selectSession('A')`));
  await settle(10); ck(h);
  h.textarea.value = 'typed on the SECOND visit to A';
  h.log.length = 0;
  const x = h.xhrs[0];
  x.status = 500; x.responseText = '{"detail":"nope"}';
  x.onload();
  await settle(10); ck(h);
  out.case4_composer = h.textarea.value;
  out.case4_systemLines = h.log.filter(l => l.startsWith('appendMessage'));
  out.case4_posts = h.posts.map(p => p.url);
});

// -- case 5: partial upload failure, user still in A -------------------------
await runScenario(build, async h => {
  h.state.sid = 'A';
  h.textarea.value = 'message with two files';
  run(h.ctx, `_pendingFiles = [{ name: 'a.txt', file: {}, uploaded: false }, { name: 'b.txt', file: {}, uploaded: false }]`);
  guard(h, run(h.ctx, 'send()'));
  await settle(); ck(h);
  const x1 = h.xhrs[0];
  x1.status = 500; x1.responseText = '{"detail":"disk full"}';
  x1.onload();
  await settle(10); ck(h);
  const x2 = h.xhrs[1];
  x2.status = 200; x2.responseText = JSON.stringify({ filename: 'b.txt' });
  x2.onload();
  await settle(10); ck(h);
  out.case5_posted = h.posts.filter(p => p.url === '/api/chat').length;
  out.case5_composer = h.textarea.value;
  out.case5_sendReleased = h.peek(`(_syncSendEnabled(), !document.getElementById('send-btn').disabled)`);
});

// -- case 6: partial upload failure AFTER the user has moved to B ------------
await runScenario(build, async h => {
  h.state.sid = 'A';
  h.textarea.value = 'message for A';
  run(h.ctx, `_pendingFiles = [{ name: 'a.txt', file: {}, uploaded: false }]`);
  guard(h, run(h.ctx, 'send()'));
  await settle(); ck(h);
  guard(h, run(h.ctx, `selectSession('B')`));
  await settle(10); ck(h);
  h.textarea.value = 'half-typed message in B';
  run(h.ctx, `_pendingFiles = [{ name: 'b-new.txt', file: {}, uploaded: false }]`);
  h.log.length = 0;
  const x = h.xhrs[0];
  x.status = 500; x.responseText = '{"detail":"disk full"}';
  x.onload();
  await settle(10); ck(h);
  out.case6_bComposer = h.textarea.value;
  out.case6_bAttachments = h.peek('_pendingFiles.map(f => f.name)');
  out.case6_aDraft = h.store['pernix:draft:A'] || null;
  out.case6_transcriptWrites = h.log.filter(l => l.startsWith('appendMessage'));
  out.case6_notifies = h.log.filter(l => l.startsWith('notify'));
});

report(out);
"""


@pytest.fixture(scope="module")
def s01(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("s01"))


# ── the filed cases ──────────────────────────────────────────────────────────


def test_a_delayed_upload_still_posts_to_the_session_it_was_typed_in(s01):
    """The headline failure: A's text and A's uploaded filename were POSTed as
    `{"session_id": "B"}` because the destination was read after the await."""
    assert s01["case1_postedTo"] == "A"
    assert "secret note for A" in s01["case1_postedText"]
    assert "[attached: a.txt]" in s01["case1_postedText"]


def test_a_delayed_upload_leaves_the_new_sessions_composer_alone(s01):
    """`clearPendingFiles()` used to run against whatever list was installed by
    the time the upload finished, deleting attachments added to B."""
    assert s01["case1_bComposer"] == ""
    assert s01["case1_bAttachments"] == ["b-new.txt"]
    assert s01["case1_visibleSid"] == "B"


def test_a_slow_upload_in_one_session_does_not_disable_send_in_another(s01):
    """`_sending` was a single module flag, so B's Send button was disabled by
    A's upload with nothing on screen to explain it."""
    assert s01["case1_sendDisabledInA"] is True
    assert s01["case1_sendDisabledInB"] is False


def test_a_late_rejection_does_not_touch_the_session_now_on_screen(s01):
    """Verified case 2: A's rejection removed B's live streaming bubble, forced
    `state.streaming` false, and wrote A's text into B's draft."""
    assert s01["case2_bBubbleRemoved"] is False
    assert s01["case2_bStillStreaming"] is True
    assert s01["case2_bHasStreamingEl"] is True
    assert s01["case2_bComposer"] == ""
    assert s01["case2_bDraftSaved"] is None


def test_a_late_rejection_preserves_the_originating_sessions_draft(s01):
    """ "Do not silently redirect" cuts both ways: the text has to survive, in
    the session it was typed in."""
    assert s01["case2_aDraftSaved"] == "note for A"
    assert any("notify(error)" in line for line in s01["case2_log"])


def test_navigating_during_session_creation_does_not_swap_the_session(s01):
    """Verified case 3, worse than filed: the response reassigned `state.sid`,
    so the composer, `connectSSE` and `_lastSeq = 0` all moved to a session the
    user never picked while B's transcript stayed on screen."""
    assert s01["case3_visibleSid"] == "B"
    assert s01["case3_connectLog"] == []
    assert s01["case3_postedTo"] == "NEW"


# ── the acceptance criteria the audit adds ───────────────────────────────────


def test_a_then_b_then_a_does_not_let_the_first_visit_apply_itself(s01):
    """Session-id equality alone would pass here — the id is 'A' both times.
    Only the selection generation can tell the two visits apart."""
    assert s01["case4_composer"] == "typed on the SECOND visit to A"
    assert s01["case4_systemLines"] == []
    assert "/api/chat" not in s01["case4_posts"]


def test_a_partial_upload_failure_holds_the_message_and_the_text(s01):
    """Unchanged behaviour, re-pinned: a message must not be sent without its
    attachments, and the text goes back into the composer."""
    assert s01["case5_posted"] == 0
    assert s01["case5_composer"] == "message with two files"
    assert s01["case5_sendReleased"] is True


def test_a_partial_upload_failure_after_navigation_reports_out_of_band(s01):
    """The failure notice and the restored text both belong to the session the
    message was typed in. Dropped into whatever is on screen they append a
    failure line to someone else's transcript and overwrite their composer."""
    assert s01["case6_bComposer"] == "half-typed message in B"
    assert s01["case6_bAttachments"] == ["b-new.txt"]
    assert s01["case6_aDraft"] == "message for A"
    assert s01["case6_transcriptWrites"] == []
    assert s01["case6_notifies"], "the user still has to be told the send failed"
