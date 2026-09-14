"""Live audit 2026-09-14 / L01: Dismiss hung in the browser, not the server.

The user's POST to `/api/notifications/{id}/dismiss` never reached uvicorn —
it appears in no access log, while the same POST from inside the container
answers in 2 ms. At the time of the report the machine held exactly six
established connections to the box. Uvicorn is HTTP/1.1 only and Chrome
allows six connections per host on HTTP/1.1, and every Pernix tab opens THREE
EventSources: the session stream, `/api/notifications/events` and
`/api/jobs/events`. Two windows therefore spend the whole budget on streams
and every further fetch queues in the browser forever.

The fix makes the two SHARED streams page-visibility aware: a tab that has
been hidden for `STREAM_HIDDEN_GRACE_MS` closes them, and reopens them (with a
catch-up read) when it comes back. A hidden tab then holds one connection
instead of three. The session stream is deliberately exempt — it is what lets
a background tab show a finished turn.

These cases drive the real `notifications.js` and `components/jobs-indicator.js`
sources under the DOM stub, with the grace shortened so the test does not
sleep for five seconds; `test_the_grace_is_the_documented_five_seconds` pins
the shipped value.
"""

from __future__ import annotations

import pytest

from tests.js_harness import STATIC_JS, requires_node, run_js

pytestmark = requires_node


SCENARIO = r"""
import { decls, fns, makeContext, mod, run, report, runScenario, settle, wait,
         callees, stubMissing } from './sandbox.mjs';

const NOTIF = mod('notifications.js');
const JOBS = mod('components/jobs-indicator.js');

// ── the notifications stream ────────────────────────────────────────────────

const NOTIF_CODE = [
  decls(['_globalSource', '_wantsGlobalConnection', '_hiddenTimer'], NOTIF),
  fns(['_onVisibilityChange', 'connectGlobalNotifications', 'disconnectGlobalNotifications'], NOTIF),
].join('\n\n');

function buildNotif(stubbed) {
  const sources = [], events = [];
  const doc = { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} };
  const base = {
    console, setTimeout, clearTimeout, JSON,
    // The real value is 5000; a test that honoured it would sleep for five
    // seconds. The shipped constant is pinned in Python instead.
    STREAM_HIDDEN_GRACE_MS: 20,
    isOnline: () => true,
    document: doc,
    window: { dispatchEvent: e => events.push(e.type), addEventListener() {} },
    CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } },
    EventSource: class {
      constructor(url) { this.url = url; this.closed = false; sources.push(this); }
      addEventListener() {}
      close() { this.closed = true; }
    },
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, NOTIF_CODE);
  stubMissing(ctx, callees(NOTIF_CODE));
  return {
    ctx, sources, events, doc,
    peek: expr => run(ctx, `(${expr})`),
    vis: v => { doc.visibilityState = v; run(ctx, '_onVisibilityChange()'); },
  };
}

// ── the jobs stream ─────────────────────────────────────────────────────────

const JOBS_CODE = [
  decls(['_eventSource', '_hiddenTimer', '_el', '_status'], JOBS),
  fns(['_onVisibilityChange', '_connectSSE', '_refresh'], JOBS),
].join('\n\n');

function buildJobs(stubbed) {
  const sources = [], gets = [];
  const doc = { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} };
  const base = {
    console, setTimeout, clearTimeout, JSON, Math,
    STREAM_HIDDEN_GRACE_MS: 20,
    isOnline: () => true,
    get: url => { gets.push(url); return Promise.resolve({ running_jobs: 0, scheduled_count: 0 }); },
    document: doc,
    window: { dispatchEvent() {}, addEventListener() {} },
    EventSource: class {
      constructor(url) { this.url = url; this.closed = false; sources.push(this); }
      addEventListener() {}
      close() { this.closed = true; }
    },
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, JOBS_CODE);
  stubMissing(ctx, callees(JOBS_CODE));
  run(ctx, '_el = { classList: { toggle() {} }, style: {}, setAttribute() {} }');
  return {
    ctx, sources, gets,
    peek: expr => run(ctx, `(${expr})`),
    vis: v => { doc.visibilityState = v; run(ctx, '_onVisibilityChange()'); },
  };
}

const out = {};

// -- notifications: hidden past the grace gives the connection back ----------
await runScenario(buildNotif, async h => {
  run(h.ctx, 'connectGlobalNotifications()');
  out.notif_openedAtStart = h.sources.length;
  h.vis('hidden');
  out.notif_closedInsideGrace = h.sources[0].closed;
  await wait(60);
  out.notif_closedAfterGrace = h.sources[0].closed;
  out.notif_sourceCleared = h.peek('_globalSource === null');
  out.notif_stillWanted = h.peek('_wantsGlobalConnection');
  // ...and comes back, with a catch-up read of /api/notifications.
  h.vis('visible');
  await settle();
  out.notif_reopened = h.sources.length;
  out.notif_secondOpen = !h.sources[1].closed;
  out.notif_catchUp = h.events.slice();
});

// -- notifications: a hide and show inside the grace costs nothing -----------
await runScenario(buildNotif, async h => {
  run(h.ctx, 'connectGlobalNotifications()');
  h.vis('hidden');
  h.vis('visible');
  await wait(60);
  out.notif_flicker_sources = h.sources.length;
  out.notif_flicker_closed = h.sources.map(s => s.closed);
  out.notif_flicker_timer = h.peek('_hiddenTimer === null');
});

// -- notifications: a tab that never connected arms nothing ------------------
await runScenario(buildNotif, async h => {
  h.vis('hidden');
  await wait(60);
  out.notif_neverConnected_sources = h.sources.length;
  h.vis('visible');
  await settle();
  out.notif_neverConnected_reopened = h.sources.length;
});

// -- jobs: same contract, and the catch-up is the status GET -----------------
await runScenario(buildJobs, async h => {
  run(h.ctx, '_connectSSE()');
  out.jobs_openedAtStart = h.sources.length;
  h.vis('hidden');
  out.jobs_closedInsideGrace = h.sources[0].closed;
  await wait(60);
  out.jobs_closedAfterGrace = h.sources[0].closed;
  out.jobs_sourceCleared = h.peek('_eventSource === null');
  h.gets.length = 0;
  h.vis('visible');
  await settle();
  out.jobs_reopened = h.sources.length;
  out.jobs_secondOpen = !h.sources[1].closed;
  out.jobs_catchUp = h.gets.slice();
});

// -- jobs: a hide and show inside the grace costs nothing --------------------
await runScenario(buildJobs, async h => {
  run(h.ctx, '_connectSSE()');
  h.vis('hidden');
  h.vis('visible');
  await wait(60);
  out.jobs_flicker_sources = h.sources.length;
  out.jobs_flicker_closed = h.sources.map(s => s.closed);
});

report(out);
"""


@pytest.fixture(scope="module")
def l01(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("l01"))


# ── the two shared streams, one contract ─────────────────────────────────────


def test_a_hidden_tab_gives_the_notifications_stream_back(l01):
    """One of the two connections a background tab was holding for nothing."""
    assert l01["notif_openedAtStart"] == 1
    assert l01["notif_closedInsideGrace"] is False, "the grace has to outlast an alt-tab"
    assert l01["notif_closedAfterGrace"] is True
    assert l01["notif_sourceCleared"] is True
    # Closed, not disconnected: coming back must not need a new subscriber.
    assert l01["notif_stillWanted"] is True


def test_the_returning_tab_reopens_and_catches_up(l01):
    """Reopening alone would silently drop every notification raised while the
    stream was closed. `pernix:bell-update` is the refetch signal."""
    assert l01["notif_reopened"] == 2
    assert l01["notif_secondOpen"] is True
    assert l01["notif_catchUp"] == ["pernix:bell-update"]


def test_a_hide_and_show_inside_the_grace_closes_nothing(l01):
    """Alt-tab and back is the common case; it must cost no reconnect."""
    assert l01["notif_flicker_sources"] == 1
    assert l01["notif_flicker_closed"] == [False]
    assert l01["notif_flicker_timer"] is True
    assert l01["jobs_flicker_sources"] == 1
    assert l01["jobs_flicker_closed"] == [False]


def test_a_tab_that_never_connected_is_left_alone(l01):
    """No subscriber, nothing to close — and nothing to reopen on return."""
    assert l01["notif_neverConnected_sources"] == 0
    assert l01["notif_neverConnected_reopened"] == 0


def test_a_hidden_tab_gives_the_jobs_stream_back(l01):
    assert l01["jobs_openedAtStart"] == 1
    assert l01["jobs_closedInsideGrace"] is False
    assert l01["jobs_closedAfterGrace"] is True
    assert l01["jobs_sourceCleared"] is True


def test_the_returning_tab_refetches_the_jobs_status(l01):
    """The live count is what a background tab loses; the GET restores it."""
    assert l01["jobs_reopened"] == 2
    assert l01["jobs_secondOpen"] is True
    assert l01["jobs_catchUp"] == ["/api/jobs/status"]


# ── the numbers the scenario deliberately does not use ───────────────────────


def test_the_grace_is_the_documented_five_seconds():
    """The scenario shortens the grace to 20 ms so it does not sleep. The
    shipped value is the one the doc note and the live assertion talk about."""
    src = (STATIC_JS / "notifications.js").read_text()
    assert "export const STREAM_HIDDEN_GRACE_MS = 5000;" in src


def test_the_jobs_indicator_shares_that_one_number():
    """Two copies of "5 s" is two things to change. The jobs indicator imports
    the constant rather than restating it."""
    src = (STATIC_JS / "components" / "jobs-indicator.js").read_text()
    assert "import { STREAM_HIDDEN_GRACE_MS } from '../notifications.js';" in src
    assert "5000" not in src


def test_the_session_stream_is_not_in_the_scheme():
    """Deliberate: a background tab still has to show a finished turn. sse.js
    watches visibility for staleness only — it never closes on hidden."""
    src = (STATIC_JS / "sse.js").read_text()
    assert "if (document.visibilityState === 'visible') _checkStale();" in src
    assert "document.hidden" not in src
    assert "'hidden'" not in src
