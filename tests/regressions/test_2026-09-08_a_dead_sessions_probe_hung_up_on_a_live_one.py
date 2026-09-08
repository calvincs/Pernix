"""Audit 3.2.2 / S03, 2026-09-08: a recovery request issued for one session
tore down a completely different one, and left it with no way back.

`sse.js` had no notion of which connection a late result belonged to.
`_probeSessionExists()` captured the session id it was probing for, but then
called the *global* `disconnectSSE()` on a 404 — so a probe started for A,
resolving after the user had selected B, closed B's stream. The
`sse.session_gone` notice that followed was stamped with A's id, which
`handleEvent`'s session guard then dropped: B was left with `_sessionId = null`,
no source, no health timer, no message, and nothing to reconnect it.

The stale-stream watchdog had the mirror defect. It re-read the module-level
`_sessionId` *after* its await, so a 404 for A announced that B had been
deleted; and any probe failure set `behind = true`, whose only guard —
`if (!_source) return;` — is satisfied by the freshly-installed source, so a
network error while probing A tore down B and rebuilt the connection against
it, flipping a healthy stream to "reconnecting".

And the rebuild itself installed a bare `onerror` that neither counted
consecutive failures nor probed for a deleted session. After ANY
watchdog-driven reconnect, deleted-session detection was gone for the life of
the connection: twelve consecutive errors produced zero probes and the health
dot spun on "reconnecting" forever — exactly the failure the probe exists to
end.

The fix stamps every source with a monotonic connection generation, re-checks
the generation AND the specific source after every await, aborts obsolete
recovery fetches, and installs one shared set of handlers so a rebuilt
connection recovers the same way a fresh one does. Session-id equality alone
cannot do this: A -> B -> A puts the same id back on screen behind a different
connection.
"""

from __future__ import annotations

import pytest

from tests.js_harness import requires_node, run_js

pytestmark = requires_node


SCENARIO = r"""
import { SSE, arrayConst, decls, fns, makeContext, makeDoc, run, deferred, settle, report, runScenario, guard, ck, callees, stubMissing } from './sandbox.mjs';

const dom = makeDoc();

const CODE = [
  decls(['_source', '_onEvent', '_sessionId', '_lastEventTime', '_healthTimer', '_lastSeq',
         '_connGen', '_recoveryAbort', '_connectionState', '_consecutiveErrors',
         '_staleProbeInFlight', '_reconnectBannerTimer',
         'HEALTH_CHECK_INTERVAL', 'STALE_THRESHOLD', 'RECONNECT_BANNER_DELAY'], SSE),
  arrayConst('EVENT_TYPES', SSE),
  fns(['_attachListeners', '_cursorQuery', '_installHandlers', 'connectSSE', '_probeSessionExists',
       'disconnectSSE', '_startHealthCheck', '_checkStale', '_armReconnectBanner',
       '_clearReconnectBanner', '_updateHealthIndicator'], SSE),
].join('\n\n');

function build(stubbed) {
  const log = [], events = [], sources = [], fetches = [];
  const base = {
    console, setTimeout, clearTimeout, setInterval, clearInterval, Date, Set, Map, JSON, Number,
    AbortController,
    isOnline: () => true,
    authHeaders: () => ({}),
    document: {
      getElementById: () => null, createElement: t => dom.element(t),
      addEventListener() {}, body: dom.element('body'),
    },
    window: { addEventListener() {} },
    EventSource: class {
      constructor(url) {
        this.url = url; this.closed = false; this.listeners = {};
        sources.push(this);
        log.push(`new EventSource(${url})`);
      }
      addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
      close() { this.closed = true; log.push(`close(${this.url})`); }
    },
    fetch: (url, opts) => {
      const d = deferred();
      fetches.push({ url, opts, ...d });
      log.push(`fetch(${url})`);
      return d.promise;
    },
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, CODE);
  // Anything the extracted source calls that this fixture does not provide.
  // Without it a bare `catch {}` inside app.js eats the ReferenceError and
  // the scenario quietly measures the wrong branch.
  const autoStubbed = stubMissing(ctx, callees(CODE));
  run(ctx, `globalThis.__handler = e => { globalThis.__events.push(e); };`);
  run(ctx, `globalThis.__events = [];`);
  const h = {
    ctx, log, sources, fetches,
    peek: expr => run(ctx, `(${expr})`),
    events: () => run(ctx, '__events.map(e => JSON.stringify(e))').map(JSON.parse),
    connect: (sid, opts) => {
      run(ctx, `connectSSE(${JSON.stringify(sid)}, __handler, ${JSON.stringify(opts || {})})`);
      // A real EventSource opens; without simulating it the connection state
      // never leaves 'disconnected' and the assertions below prove nothing.
      const s = sources[sources.length - 1];
      if (s && s.onopen) s.onopen();
      return s;
    },
    // Three consecutive transport errors is what arms the deleted-session probe.
    errors: (source, n) => { for (let i = 0; i < n; i++) source.onerror(); },
    goStale: () => run(ctx, '_lastEventTime = 0'),
  };
  return h;
}

const out = {};
const json = r => ({ status: r.status, ok: r.status < 400, json: () => Promise.resolve(r.body || {}) });

// -- cursor shapes on the wire ----------------------------------------------
await runScenario(build, async h => {
  h.connect('A', { cursor: 12 });
  h.connect('B', { cursor: 0 });
  h.connect('C', {});
  out.urls = h.sources.map(s => s.url);
});

// -- case 1: A's 404 arrives after B is connected ---------------------------
await runScenario(build, async h => {
  const aSource = h.connect('A', { cursor: 5 });
  h.errors(aSource, 3);
  ck(h);
  out.case1_probeFired = h.fetches.length === 1;
  const bSource = h.connect('B', { cursor: 9 });
  h.fetches[0].resolve(json({ status: 404 }));
  await settle(10); ck(h);
  out.case1_bSourceClosed = bSource.closed;
  out.case1_bStillInstalled = h.peek('_source && _source.url') === bSource.url;
  out.case1_sessionId = h.peek('_sessionId');
  out.case1_events = h.events().map(e => e.type);
});

// -- case 2: the watchdog's 404 for A arrives after B is connected ----------
await runScenario(build, async h => {
  h.connect('A', { cursor: 5 });
  h.goStale();
  guard(h, run(h.ctx, '_checkStale()'));
  await settle(2); ck(h);
  out.case2_probeFired = h.fetches.length === 1;
  const bSource = h.connect('B', { cursor: 9 });
  h.fetches[0].resolve(json({ status: 404 }));
  await settle(10); ck(h);
  out.case2_bSourceClosed = bSource.closed;
  out.case2_events = h.events().map(e => e.type);
  out.case2_staleFlag = h.peek('_staleProbeInFlight');
});

// -- case 3: a network-error probe for A must not rebuild B ----------------
await runScenario(build, async h => {
  h.connect('A', { cursor: 5 });
  h.goStale();
  guard(h, run(h.ctx, '_checkStale()'));
  await settle(2); ck(h);
  const bSource = h.connect('B', { cursor: 9 });
  const before = h.sources.length;
  h.fetches[0].reject(new Error('network down'));
  await settle(10); ck(h);
  out.case3_bSourceClosed = bSource.closed;
  out.case3_newSources = h.sources.length - before;
  out.case3_connectionState = h.peek('_connectionState');
});

// -- case 4 (control): a genuine current-session 404 must still work --------
await runScenario(build, async h => {
  const aSource = h.connect('A', { cursor: 5 });
  h.errors(aSource, 3);
  ck(h);
  h.fetches[0].resolve(json({ status: 404 }));
  await settle(10); ck(h);
  out.case4_aSourceClosed = aSource.closed;
  out.case4_sourceCleared = h.peek('_source') === null;
  out.case4_events = h.events();
});

// -- case 5: a delayed SUCCESS for A must also leave B alone ----------------
await runScenario(build, async h => {
  h.connect('A', { cursor: 5 });
  h.goStale();
  guard(h, run(h.ctx, '_checkStale()'));
  await settle(2); ck(h);
  const bSource = h.connect('B', { cursor: 9 });
  const before = h.sources.length;
  h.fetches[0].resolve(json({ status: 200, body: { event_seq: 9999 } }));
  await settle(10); ck(h);
  out.case5_bSourceClosed = bSource.closed;
  out.case5_newSources = h.sources.length - before;
  out.case5_events = h.events().map(e => e.type);
});

// -- case 6: deleted-session detection survives a watchdog rebuild ----------
await runScenario(build, async h => {
  h.connect('A', { cursor: 5 });
  h.goStale();
  guard(h, run(h.ctx, '_checkStale()'));
  await settle(2); ck(h);
  h.fetches[0].resolve(json({ status: 200, body: { event_seq: 500 } }));   // server moved ahead
  await settle(10); ck(h);
  out.case6_rebuilt = h.sources.length === 2;
  out.case6_rebuildUrl = h.sources[1] ? h.sources[1].url : null;
  const rebuilt = h.sources[1];
  const fetchesBefore = h.fetches.length;
  h.errors(rebuilt, 12);
  await settle(2); ck(h);
  out.case6_probesAfterRebuild = h.fetches.length - fetchesBefore;
  out.case6_reconnectAnnounced = (rebuilt.onopen(), h.events().map(e => e.type));
});

// -- case 7: A -> B -> A; a probe from the FIRST A owns nothing -------------
await runScenario(build, async h => {
  h.connectFirst = h.connect('A', { cursor: 5 });
  h.errors(h.connectFirst, 3);
  ck(h);
  h.connect('B', { cursor: 9 });
  const secondA = h.connect('A', { cursor: 11 });
  h.fetches[0].resolve(json({ status: 404 }));
  await settle(10); ck(h);
  out.case7_secondAClosed = secondA.closed;
  out.case7_events = h.events().map(e => e.type);
  out.case7_sessionId = h.peek('_sessionId');
});

report(out);
"""


@pytest.fixture(scope="module")
def s03(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("s03"))


# ── the cursor is now three-valued on the wire ───────────────────────────────


def test_the_replay_cursor_distinguishes_absent_from_zero(s03):
    """ "No boundary I trust" and "cursor zero" are different requests, and the
    old client could express only one of them."""
    assert s03["urls"] == [
        "/api/sessions/A/events?last_event_id=12",
        "/api/sessions/B/events?last_event_id=0",
        "/api/sessions/C/events",
    ]


# ── the filed cases ──────────────────────────────────────────────────────────


def test_a_late_404_for_a_left_session_does_not_close_the_live_one(s03):
    """Verified case 1, worse than filed: the probe called global
    `disconnectSSE()`, and its `sse.session_gone` carried the OLD session id so
    the app dropped it — B lost its stream with no message at all."""
    assert s03["case1_probeFired"] is True
    assert s03["case1_bSourceClosed"] is False
    assert s03["case1_bStillInstalled"] is True
    assert s03["case1_sessionId"] == "B"
    assert "sse.session_gone" not in s03["case1_events"]


def test_the_watchdog_does_not_announce_the_wrong_sessions_deletion(s03):
    """Verified case 2: the watchdog re-read `_sessionId` after its await, so
    A's 404 told B that *it* had been deleted."""
    assert s03["case2_probeFired"] is True
    assert s03["case2_bSourceClosed"] is False
    assert "sse.session_gone" not in s03["case2_events"]
    assert s03["case2_staleFlag"] is False, "the in-flight flag must be released either way"


def test_a_network_error_probing_one_session_does_not_rebuild_another(s03):
    """Verified case 3: any probe failure set `behind = true`, and the only
    guard (`if (!_source) return`) was satisfied by the freshly-installed
    source — so B was torn down, rebuilt, and flipped to "reconnecting"."""
    assert s03["case3_bSourceClosed"] is False
    assert s03["case3_newSources"] == 0
    assert s03["case3_connectionState"] == "connected"


def test_a_genuine_current_session_404_still_stops_and_reports(s03):
    """Case 4 is the control. Ownership checks must not cost the behaviour
    they are protecting."""
    assert s03["case4_aSourceClosed"] is True
    assert s03["case4_sourceCleared"] is True
    gone = [e for e in s03["case4_events"] if e["type"] == "sse.session_gone"]
    assert gone and gone[0]["session_id"] == "A"


def test_a_delayed_success_for_a_left_session_stays_harmless(s03):
    """The audit asked for this case; it was already safe, because reconnecting
    zeroes the watchdog's cursor so `behind` is false. Pinned so it stays that
    way rather than defended with machinery it does not need."""
    assert s03["case5_bSourceClosed"] is False
    assert s03["case5_newSources"] == 0
    assert s03["case5_events"] == []


def test_deleted_session_detection_survives_a_watchdog_rebuild(s03):
    """The one the audit missed. The rebuild installed a bare `onerror`, so
    after any watchdog reconnect twelve consecutive errors produced ZERO probes
    and the dot span on "reconnecting" for the life of the connection."""
    assert s03["case6_rebuilt"] is True
    assert s03["case6_rebuildUrl"] == "/api/sessions/A/events?last_event_id=5"
    assert s03["case6_probesAfterRebuild"] == 1
    assert "sse.reconnected" in s03["case6_reconnectAnnounced"]


def test_a_then_b_then_a_is_not_answered_by_session_id_equality(s03):
    """The id is 'A' both times, so only the connection generation can tell the
    first visit's probe from the second visit's connection."""
    assert s03["case7_secondAClosed"] is False
    assert "sse.session_gone" not in s03["case7_events"]
    assert s03["case7_sessionId"] == "A"
