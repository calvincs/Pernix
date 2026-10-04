"""v42 notification tiers, client side: who buzzes, and what the bell counts.

The server now gives every notification a tier — interrupt (needs the user),
bell (a quiet item), log (activity log only) — and the global SSE event
`dialog.notification` carries it. The client must:

- raise an OS notification for an interrupt only (and for an event with no
  tier at all, which is a pre-v42 server where everything alerted), never for
  a bell or log row and never for a `resolved` refresh, which has no title;
- still refresh the bell for every one of them;
- count questions + open interrupt rows on the badge, and show a dot — not a
  number — when only quiet bell rows are open;
- order Needs you as questions, then interrupts, then bell rows.

These drive the real `notifications.js` and `components/notification-bell.js`
source under node (see tests/js_harness.py).
"""

from __future__ import annotations

import pytest

from tests.js_harness import STATIC_JS, requires_node, run_js

pytestmark = requires_node


SCENARIO = r"""
import { decls, fns, makeContext, mod, run, report, runScenario, callees, stubMissing } from './sandbox.mjs';

const NOTIF = mod('notifications.js');
const BELL = mod('components/notification-bell.js');

const NOTIF_CODE = [
  decls(['_globalSource', '_wantsGlobalConnection', '_hiddenTimer'], NOTIF),
  fns(['shouldAlert', 'connectGlobalNotifications'], NOTIF),
].join('\n\n');

function buildNotif(stubbed) {
  const listeners = {}, alerts = [], events = [];
  const base = {
    console, JSON,
    isOnline: () => true,
    window: { dispatchEvent: e => events.push(e.type), addEventListener() {} },
    CustomEvent: class { constructor(t) { this.type = t; } },
    EventSource: class {
      constructor(url) { this.url = url; }
      addEventListener(name, fn) { listeners[name] = fn; }
      close() {}
    },
    showNotification: (title, body, opts) => alerts.push({ title, urgency: opts && opts.urgency }),
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, NOTIF_CODE);
  stubMissing(ctx, callees(NOTIF_CODE));
  return { ctx, listeners, alerts, events };
}

const out = {};

await runScenario(buildNotif, async h => {
  run(h.ctx, 'connectGlobalNotifications()');
  const fire = data => h.listeners['dialog.notification']({ data: JSON.stringify(data) });
  const cases = {
    interrupt: { title: 'Job failed', tier: 'interrupt', urgency: 'high' },
    bell: { title: 'Embeddings are down', tier: 'bell' },
    log: { title: 'Edits applied', tier: 'log' },
    legacy: { title: 'Old server', urgency: 'normal' },
    resolved: { tier: 'bell', resolved: true, category: 'system.embeddings_down' },
    resolvedInterrupt: { tier: 'interrupt', resolved: true },
  };
  for (const [name, data] of Object.entries(cases)) {
    const before = { alerts: h.alerts.length, events: h.events.length };
    fire(data);
    out['alert_' + name] = h.alerts.length - before.alerts;
    out['refresh_' + name] = h.events.slice(before.events);
  }
  out.interruptUrgency = h.alerts[0] && h.alerts[0].urgency;
});

// The bell's pure helpers.
const BELL_CODE = fns(['_badgeState', '_needsOrder'], BELL);
{
  const { ctx } = makeContext({ Number, JSON });
  run(ctx, BELL_CODE);
  const badge = (q, c) => run(ctx, `JSON.stringify(_badgeState(${q}, ${JSON.stringify(c)}))`);
  out.badge_interrupt = JSON.parse(badge(0, { needs_you: 1, bell: 2, unread: 5 }));
  out.badge_questions = JSON.parse(badge(2, { needs_you: 1, bell: 0, unread: 0 }));
  out.badge_quiet = JSON.parse(badge(0, { needs_you: 0, bell: 3, unread: 9 }));
  out.badge_logOnly = JSON.parse(badge(0, { needs_you: 0, bell: 0, unread: 9 }));
  out.badge_questionBeatsDot = JSON.parse(badge(1, { needs_you: 0, bell: 3, unread: 0 }));
  out.order = JSON.parse(run(ctx, `JSON.stringify(_needsOrder(
    [{ id: 'q1', _kind: 'question', created_at: '2026-10-01T00:00:00Z' }],
    [
      { id: 'b-new', _kind: 'notification', tier: 'bell', created_at: '2026-10-02T09:00:00Z' },
      { id: 'i-old', _kind: 'notification', tier: 'interrupt', created_at: '2026-10-01T09:00:00Z' },
      { id: 'b-old', _kind: 'notification', tier: 'bell', created_at: '2026-10-01T08:00:00Z' },
      { id: 'i-new', _kind: 'notification', tier: 'interrupt', created_at: '2026-10-02T08:00:00Z' },
    ]).map(i => i.id))`));
}

report(out);
"""


@pytest.fixture(scope="module")
def r(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("bell_tiers"))


def test_only_an_interrupt_raises_an_os_notification(r):
    assert r["alert_interrupt"] == 1
    assert r["interruptUrgency"] == "high"
    assert r["alert_bell"] == 0
    assert r["alert_log"] == 0


def test_a_pre_v42_event_without_a_tier_still_alerts(r):
    """An older server sends no tier; every notification alerted then."""
    assert r["alert_legacy"] == 1


def test_a_resolved_refresh_never_alerts(r):
    """`resolved` carries no title — it only means "re-read the bell"."""
    assert r["alert_resolved"] == 0
    assert r["alert_resolvedInterrupt"] == 0


def test_every_tier_still_refreshes_the_bell(r):
    for name in ("interrupt", "bell", "log", "legacy", "resolved", "resolvedInterrupt"):
        assert r[f"refresh_{name}"] == ["pernix:bell-update"], name


def test_the_badge_counts_questions_and_interrupts_only(r):
    assert r["badge_interrupt"] == {"count": 1, "dot": False}
    assert r["badge_questions"] == {"count": 3, "dot": False}


def test_quiet_rows_make_a_dot_not_a_number(r):
    assert r["badge_quiet"] == {"count": 0, "dot": True}
    # A number always wins over the dot.
    assert r["badge_questionBeatsDot"] == {"count": 1, "dot": False}


def test_log_rows_and_unread_never_touch_the_badge(r):
    assert r["badge_logOnly"] == {"count": 0, "dot": False}


def test_needs_you_orders_questions_then_interrupts_then_bell_rows(r):
    assert r["order"] == ["q1", "i-new", "i-old", "b-new", "b-old"]


def test_the_bell_reads_the_cheap_counts_and_the_log_view():
    src = (STATIC_JS / "components" / "notification-bell.js").read_text()
    assert "get('/api/notifications/counts')" in src
    assert "/api/notifications?view=log" in src
    assert "/api/notifications?view=bell" in src
    assert "post('/api/notifications/dismiss-all')" in src
    assert "post('/api/notifications/read-all')" in src
    # The closed-panel cadence is slower than the open one.
    assert "const POLL_CLOSED_MS = 15000;" in src
    assert "const POLL_OPEN_MS = 5000;" in src
