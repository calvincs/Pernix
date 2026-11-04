"""2026-11-04: the web UI "loading incredibly slow" on the box.

A Chrome performance trace of one reload: navigation started at 0.07 s and
its first byte arrived at 60.05 s — then the whole app booted in 1.2 s. The
box answered every request it saw in under 0.7 s. The minute was spent in the
browser, waiting for a connection: uvicorn is HTTP/1.1, Chrome allows six
connections per host, and the box showed exactly six from that machine. Every
Pernix tab opens three EventSources; since 2026-09-14 a hidden tab closes the
two shared ones (tests/test_hidden_tab_streams_2026_09_14.py) but kept the
session stream, and the service worker's navigation fetch had no deadline.

Three changes, each driven here against the real source bytes:

1. `sw.js` answers a navigation from the cached shell once the network has
   not answered for NAV_NETWORK_TIMEOUT_MS, and still refreshes the cache.
2. `sse.js` closes the session stream in a hidden tab too — only while the
   session is idle — and reopens it from the last event on return.
3. `app.js` funnels every session-list reload through a guard: the same
   browser asked for `/api/sessions?limit=500` ~58 times a second, twice,
   for reasons that could not be reproduced. Calls inside a burst share one
   trailing fetch, and a burst logs who asked.
"""

from __future__ import annotations

import pytest

from tests.js_harness import REPO_ROOT, STATIC_JS, requires_node, run_js

pytestmark = requires_node


SCENARIO = r"""
import { SSE, APP, decls, fns, makeContext, mod, run, report, runScenario, settle, wait,
         callees, stubMissing } from './sandbox.mjs';

const out = {};

// ── 1. the service worker's navigation deadline ─────────────────────────────

const SW = mod('../sw.js');
const SW_CODE = [
  decls(['CACHE_VERSION', 'NAV_NETWORK_TIMEOUT_MS'], SW),
  fns(['_navigate'], SW),
].join('\n\n');

function buildSw({ networkMs, networkFails = false, cached = true }) {
  return (stubbed) => {
    const puts = [], waits = [];
    const shell = { tag: 'cached-shell' };
    const net = { tag: 'network', ok: true, clone() { return { tag: 'network-copy' }; } };
    const base = {
      console, setTimeout, clearTimeout, Promise,
      fetch: () => new Promise((res, rej) => setTimeout(
        () => (networkFails ? rej(new TypeError('Failed to fetch')) : res(net)), networkMs)),
      caches: {
        match: async () => (cached ? shell : undefined),
        open: async () => ({ put: async (k, v) => { puts.push([k, v.tag]); } }),
      },
    };
    for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
    const { ctx } = makeContext(base);
    run(ctx, SW_CODE);
    stubMissing(ctx, callees(SW_CODE));
    // The shipped deadline is 3 s; the scenario uses 40 ms and the Python
    // side pins the real number.
    run(ctx, 'NAV_NETWORK_TIMEOUT_MS = 40');
    const event = { request: { url: 'https://box/?token=x', mode: 'navigate' },
                    waitUntil: p => waits.push(p) };
    return { ctx, puts, waits, event, nav: () => run(ctx, '_navigate')(event) };
  };
}

async function navCase(key, opts) {
  await runScenario(buildSw(opts), async h => {
    const t0 = Date.now();
    const resp = await h.nav();
    out[key + '_answer'] = resp && resp.tag;
    out[key + '_ms'] = Date.now() - t0;
    await Promise.all(h.waits);
    await wait(opts.networkMs + 20);
    await Promise.all(h.waits);
    out[key + '_puts'] = h.puts;
    out[key + '_waitUntilCalled'] = h.waits.length;
  });
}

await navCase('fast', { networkMs: 5 });
await navCase('slow', { networkMs: 200 });
await navCase('slowNoCache', { networkMs: 120, cached: false });
await navCase('down', { networkMs: 5, networkFails: true });

// ── 2. the session stream in a hidden tab ───────────────────────────────────

const SSE_CODE = [
  decls(['_source', '_onEvent', '_sessionId', '_lastEventTime', '_healthTimer', '_lastSeq',
         '_connGen', '_recoveryAbort', '_connectionState', '_consecutiveErrors',
         '_staleProbeInFlight', '_reconnectBannerTimer', '_hiddenTimer', '_closedForHidden',
         '_busyCheck',
         'HEALTH_CHECK_INTERVAL', 'STALE_THRESHOLD', 'RECONNECT_BANNER_DELAY'], SSE),
  fns(['_cursorQuery', '_installHandlers', '_attachListeners', 'connectSSE', 'disconnectSSE',
       '_startHealthCheck', '_resumeStream', 'setStreamBusyCheck', '_armHiddenClose',
       '_onSessionVisibility', '_updateHealthIndicator', '_armReconnectBanner',
       '_clearReconnectBanner'], SSE),
].join('\n\n');

function buildSse(stubbed) {
  const sources = [], events = [];
  const doc = { visibilityState: 'visible', getElementById: () => null, addEventListener() {} };
  let online = true;
  const base = {
    console, setTimeout, clearTimeout, setInterval, clearInterval, Date, JSON, Number,
    AbortController,
    isOnline: () => online,
    authHeaders: () => ({}),
    document: doc,
    window: { addEventListener() {} },
    EVENT_TYPES: [],
    // An export, so not sliceable by decls(); the real 30 s is pinned below.
    SESSION_HIDDEN_GRACE_MS: 20,
    _checkStale: () => { events.push('checkStale'); },
    EventSource: class {
      constructor(url) { this.url = url; this.closed = false; sources.push(this); }
      addEventListener() {}
      close() { this.closed = true; }
    },
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, SSE_CODE);
  stubMissing(ctx, callees(SSE_CODE));
  run(ctx, 'globalThis.__busy = false; globalThis.__events = [];');
  run(ctx, 'setStreamBusyCheck(() => __busy)');
  run(ctx, 'globalThis.__handler = e => __events.push(e.type)');
  return {
    ctx, sources, events, doc,
    setOnline: v => { online = v; },
    peek: expr => run(ctx, `(${expr})`),
    busy: v => run(ctx, `__busy = ${v}`),
    handlerEvents: () => run(ctx, '__events.slice()'),
    connect: () => {
      run(ctx, "connectSSE('s1', __handler, { cursor: 7 })");
      run(ctx, '_lastSeq = 12');   // events arrived while connected
    },
    vis: v => { doc.visibilityState = v; run(ctx, '_onSessionVisibility()'); },
  };
}

// Idle and hidden past the grace: the stream goes; coming back resumes it.
await runScenario(buildSse, async h => {
  h.connect();
  h.vis('hidden');
  out.idle_closedInsideGrace = h.sources[0].closed;
  await wait(60);
  out.idle_closedAfterGrace = h.sources[0].closed;
  out.idle_flag = h.peek('_closedForHidden');
  out.idle_keptSession = h.peek('_sessionId');
  h.vis('visible');
  await settle();
  out.idle_reopened = h.sources.length;
  out.idle_reopenUrl = h.sources[1] && h.sources[1].url;
  out.idle_events = h.handlerEvents();
  out.idle_flagAfter = h.peek('_closedForHidden');
});

// Mid-turn: the stream stays until the turn ends, then goes.
await runScenario(buildSse, async h => {
  h.connect();
  h.busy(true);
  h.vis('hidden');
  await wait(70);
  out.busy_stillOpen = !h.sources[0].closed;
  h.busy(false);
  await wait(60);
  out.busy_closedOnceIdle = h.sources[0].closed;
});

// An alt-tab and back inside the grace costs nothing.
await runScenario(buildSse, async h => {
  h.connect();
  h.vis('hidden');
  h.vis('visible');
  await wait(60);
  out.flicker_sources = h.sources.length;
  out.flicker_closed = h.sources[0].closed;
  out.flicker_checkedStale = h.events.includes('checkStale');
});

// No busy check registered at all: an unknown state never closes.
await runScenario(buildSse, async h => {
  run(h.ctx, 'setStreamBusyCheck(null)');
  h.connect();
  h.vis('hidden');
  await wait(60);
  out.unknown_stillOpen = !h.sources[0].closed;
});

// Coming back while offline waits for the network; disconnect forgets it all.
await runScenario(buildSse, async h => {
  h.connect();
  h.vis('hidden');
  await wait(60);
  h.setOnline(false);
  h.vis('visible');
  await settle();
  out.offline_noReopen = h.sources.length;
  out.offline_flagCleared = h.peek('_closedForHidden');
  run(h.ctx, 'disconnectSSE()');
  out.disconnect_timer = h.peek('_hiddenTimer');
  out.disconnect_flag = h.peek('_closedForHidden');
});

// ── 3. the session-list reload guard ───────────────────────────────────────

const GUARD_CODE = [
  decls(['_SESSIONS_MIN_GAP_MS', '_SESSIONS_BURST', '_sessionsInFlight', '_sessionsTrailing',
         '_sessionsLastStart', '_sessionsCalls', '_sessionsBurstWarnedAt'], APP),
  fns(['loadSessions', '_startSessionsLoad'], APP),
].join('\n\n');

function buildGuard(stubbed) {
  const starts = [], warns = [];
  const base = {
    setTimeout, clearTimeout, Date, Promise, Error,
    console: { warn: (...a) => warns.push(String(a[0])) },
    _loadSessionsNow: () => { starts.push(Date.now()); return new Promise(r => setTimeout(r, 15)); },
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, GUARD_CODE);
  stubMissing(ctx, callees(GUARD_CODE));
  return { ctx, starts, warns, call: () => run(ctx, 'loadSessions()') };
}

// A tight loop of calls, each awaiting the last — the shape seen on the box,
// where the same loop ran at ~58 fetches a second.
await runScenario(buildGuard, async h => {
  const t0 = Date.now();
  for (let i = 0; i < 12; i++) await h.call();
  out.loop_ms = Date.now() - t0;
  out.loop_fetches = h.starts.length;
  const gaps = h.starts.slice(1).map((t, i) => t - h.starts[i]);
  out.loop_minGap = Math.min(...gaps);
});

// 30 calls at once share one in-flight fetch and one trailing fetch, and every
// caller's promise settles only after a fetch that started after its call.
await runScenario(buildGuard, async h => {
  const first = h.call();
  const rest = [];
  for (let i = 0; i < 29; i++) rest.push(h.call());
  const calledAt = Date.now();
  await Promise.all([first, ...rest]);
  out.burst_fetches = h.starts.length;
  out.burst_trailingAfterCalls = h.starts[h.starts.length - 1] >= calledAt;
  out.burst_sameTrailing = rest.every(p => p === rest[0]);
  out.burst_warned = h.warns.filter(w => w.includes('session list asked for')).length;
  // A second burst inside the same minute does not log again.
  for (let i = 0; i < 30; i++) h.call();
  await wait(300);
  out.burst_warnedAfterSecond = h.warns.filter(w => w.includes('session list asked for')).length;
});

// A lone call after a quiet spell is not delayed.
await runScenario(buildGuard, async h => {
  await h.call();
  await wait(300);
  const t = Date.now();
  await h.call();
  out.lone_ms = Date.now() - t;
  out.lone_fetches = h.starts.length;
});

report(out);
"""


@pytest.fixture(scope="module")
def r(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("slow_reload"))


# ── 1. sw.js ─────────────────────────────────────────────────────────────────


def test_a_fast_server_still_answers_the_navigation(r):
    assert r["fast_answer"] == "network"
    assert r["fast_puts"] == [["/", "network-copy"]]


def test_a_slow_network_gets_the_cached_shell_at_the_deadline(r):
    """The 60-second wait, shortened: 200 ms network, 40 ms deadline."""
    assert r["slow_answer"] == "cached-shell"
    assert r["slow_ms"] < 150
    # ...and the late network answer still refreshes the cached copy, inside
    # a waitUntil so the worker is not stopped before it is written.
    assert r["slow_puts"] == [["/", "network-copy"]]
    assert r["slow_waitUntilCalled"] == 1


def test_with_no_cached_shell_the_navigation_waits_for_the_network(r):
    assert r["slowNoCache_answer"] == "network"


def test_a_server_that_is_down_still_gets_the_offline_shell(r):
    assert r["down_answer"] == "cached-shell"
    assert r["down_puts"] == []


def test_the_shipped_deadline_is_three_seconds():
    src = (REPO_ROOT / "static" / "sw.js").read_text()
    assert "const NAV_NETWORK_TIMEOUT_MS = 3000;" in src
    assert "event.respondWith(_navigate(event));" in src


# ── 2. sse.js ────────────────────────────────────────────────────────────────


def test_an_idle_hidden_tab_gives_the_session_stream_back(r):
    assert r["idle_closedInsideGrace"] is False, "the grace has to outlast an alt-tab"
    assert r["idle_closedAfterGrace"] is True
    assert r["idle_flag"] is True
    # Closed, not disconnected: the tab still knows which session it shows.
    assert r["idle_keptSession"] == "s1"


def test_the_returning_tab_resumes_from_the_last_event(r):
    """The cursor is what makes the server replay what the tab missed; the
    reconnect event is what makes app.js reconcile the transcript."""
    assert r["idle_reopened"] == 2
    assert r["idle_reopenUrl"] == "/api/sessions/s1/events?last_event_id=12"
    assert r["idle_events"] == ["sse.reconnected"]
    assert r["idle_flagAfter"] is False


def test_a_hidden_tab_mid_turn_keeps_its_stream_until_the_turn_ends(r):
    """The reason the 09-14 fix exempted this stream: a background tab has to
    show the turn finishing."""
    assert r["busy_stillOpen"] is True
    assert r["busy_closedOnceIdle"] is True


def test_an_alt_tab_and_back_closes_nothing(r):
    assert r["flicker_sources"] == 1
    assert r["flicker_closed"] is False
    assert r["flicker_checkedStale"] is True


def test_without_a_busy_check_the_stream_is_never_closed(r):
    assert r["unknown_stillOpen"] is True


def test_coming_back_offline_waits_and_disconnect_clears_the_bookkeeping(r):
    assert r["offline_noReopen"] == 1
    # The online handler takes over from here: flag cleared, so it resumes.
    assert r["offline_flagCleared"] is False
    assert r["disconnect_timer"] is None
    assert r["disconnect_flag"] is False


def test_the_shipped_session_grace_and_wiring():
    src = (STATIC_JS / "sse.js").read_text()
    assert "export const SESSION_HIDDEN_GRACE_MS = 30000;" in src
    assert "document.addEventListener('visibilitychange', _onSessionVisibility);" in src
    app = (STATIC_JS / "app.js").read_text()
    assert "setStreamBusyCheck(() => !!state.streaming" in app


# ── 3. app.js reload guard ──────────────────────────────────────────────────


def test_a_tight_reload_loop_is_slowed_to_four_fetches_a_second(r):
    """On the box the loop ran at ~58 fetches a second. Each awaited call
    still gets a fresh list, but the guard spaces the fetches 250 ms apart:
    the loop costs four requests a second instead of fifty-eight."""
    assert r["loop_fetches"] == 12
    assert r["loop_minGap"] >= 240
    assert r["loop_fetches"] / (r["loop_ms"] / 1000) <= 4.5


def test_a_burst_shares_one_trailing_fetch_that_starts_after_every_call(r):
    assert r["burst_fetches"] == 2
    assert r["burst_trailingAfterCalls"] is True
    assert r["burst_sameTrailing"] is True


def test_a_burst_logs_once_a_minute(r):
    """The next occurrence has to say who asked; it must not flood the
    console doing it."""
    assert r["burst_warned"] == 1
    assert r["burst_warnedAfterSecond"] == 1


def test_a_lone_reload_is_not_delayed(r):
    assert r["lone_fetches"] == 2
    assert r["lone_ms"] < 100
