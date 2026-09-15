"""2026-09-15: a Learning-tab card that the person holding the button can read.

Explorer -> Self-tuning -> Learning rendered each pending adaptive proposal as
the database row that produced it: a producer badge, `#id · 3d ago —
<rationale written by one LLM for another>`, and a line per edit reading
`policy` `create` `<entry_id>`. The owner's verdict on the live box: "I as the
user have no idea what's happening or being described here by these items. The
verbiage is quite opaque and hard to read."

Worse than opaque, it was undifferentiated. Three different objects share that
list — one that applies itself once its veto window passes, one (a proposed
self-test) that never applies without a click, and one the clock refuses to
take because its evidence points at nothing recorded — and the card said
nothing about which was which, so all three read as one to-do list.

The server now annotates every listed row with `explanation`
(`core/adaptive/explain.py` via `annotate_proposal`): plain-English `what`,
`why` and `fate`, plus a machine-readable `fate_kind` of auto / needs_you /
held. This pins the card that renders it:

  * the three sentences appear, each behind its own label;
  * the fate pill's LABEL follows fate_kind, so the three kinds are
    distinguishable at a glance and not only by reading;
  * the raw row is still there, collapsed under "Details" — including the
    canary OBJECT payload's `summary`, which is the 2026-09-08 bug's fallback
    and must survive the redesign;
  * `explanation: null` (the server could not build one) falls back to the old
    card rather than to a blank one;
  * "Chat about this" mints a session and hands it to the composer through the
    `pernix:compose` event, because `send()`/`selectSession()` live in app.js
    and an import back from the Explorer would be a cycle.
"""

from __future__ import annotations

from tests.js_harness import requires_node, run_js

pytestmark = requires_node


# The real bytes of both functions, exactly as the 2026-09-08 test takes them —
# nothing here is a transcription of the source.
PRELUDE = r"""
import { mod, fns, makeContext, makeDoc, run, report, runScenario, callees, stubMissing } from './sandbox.mjs';

const ADAPTIVE = mod('components/modals/adaptive.js');
const dom = makeDoc();
const CODE = fns(['proposalCard', 'renderAdaptiveTab'], ADAPTIVE);

// One of each of the three fates, plus the canary OBJECT payload that emptied
// the tab in September, plus a row the explainer could not describe.
const AUTO = {
  id: 11, producer: 'dream', created_at: '2026-09-12T10:00:00+00:00',
  rationale: 'pm drift on grep-heavy turns (not auto-admitted: cites nothing)',
  payload_json: JSON.stringify([{kind: 'routing_hint', action: 'create', title: 'prefer-rg', content: 'use rg'}]),
  evidence_json: JSON.stringify(['pm:4821', 'session:3dc5a307d751']),
  summary: 'dream: create routing_hint',
  explanation: {
    what: "Dream wants to add a hint about which tool to reach for called 'prefer-rg'.",
    why: 'It is backed by 1 graded turn the system recorded.',
    fate: 'Applies on its own in 9 h (2026-09-15 21:00 UTC) unless you reject it first.',
    fate_kind: 'auto',
    producer_label: 'Dream (the overnight review that looks back over past sessions)',
  },
};
const CANARY = {
  id: 12, producer: 'canary_propose', created_at: '2026-09-14T10:00:00+00:00',
  rationale: "[new canary 'grep-count']",
  payload_json: JSON.stringify({canary: {name: 'grep-count', prompt: 'count', gates: [{name: 'g'}]}}),
  evidence_json: JSON.stringify(['session:abc']),
  summary: "canary_propose: new canary 'grep-count' (waits for a human approve/reject; never auto-approves)",
  explanation: {
    what: "Refine wants to add a new self-test called 'grep-count'.",
    why: 'It came out of session abc.',
    fate: 'Waits for you. New self-tests never apply on their own.',
    fate_kind: 'needs_you',
    producer_label: 'Refine, via the self-test suite',
  },
};
const HELD = {
  id: 13, producer: 'refine', created_at: '2026-09-13T10:00:00+00:00', rationale: 'held one',
  payload_json: JSON.stringify([{kind: 'policy', action: 'create', title: 'p', content: 'c'}]),
  evidence_json: JSON.stringify([]),
  summary: 'refine: create policy',
  explanation: {
    what: 'Refine wants to add a rule the agent must follow in every session.',
    why: 'It cites nothing the system recorded — only its own reasoning.',
    fate: 'Held for you. Its evidence points to nothing the system recorded.',
    fate_kind: 'held',
    producer_label: 'Refine (the after-session grader)',
  },
};
// `annotate_proposal` sets explanation to null when the explainer raised.
const UNEXPLAINED = {
  id: 14, producer: 'agent', created_at: '2026-09-11T10:00:00+00:00', rationale: 'raw rationale text',
  payload_json: JSON.stringify([]), evidence_json: '[]',
  summary: 'agent: acknowledge only', explanation: null,
};

function makeBase(PROPOSALS, sink) {
  return {
    console, JSON, Date, Array, Promise, setTimeout, Error,
    el: (tag, attrs, kids) => {
      const e = dom.element(tag);
      if (attrs && attrs.class) e.className = attrs.class;
      // The real el() turns an `on*` attr into addEventListener; the fake DOM
      // drops listeners, so park the handler where the scenario can fire it.
      if (attrs && attrs.onClick) e.__onClick = attrs.onClick;
      if (attrs && attrs.title) e.setAttribute('title', attrs.title);
      (kids || []).forEach(k => k && e.appendChild(k));
      return e;
    },
    text: v => dom.textNode(String(v)),
    clear: node => { node.childNodes = []; },
    icon: () => dom.element('span'),
    badge: (label, cls) => {
      const b = dom.element('span');
      b.className = 'adaptive-badge ' + (cls || '');
      b.appendChild(dom.textNode(String(label)));
      return b;
    },
    section: title => { const s = dom.element('div'); s.className = 'adaptive-section-title'; s.appendChild(dom.textNode(title)); return s; },
    relTime: () => '1h ago',
    tabGlossary: line => { const d = dom.element('div'); d.className = 'fp-tab-desc'; d.appendChild(dom.textNode(String(line))); return d; },
    takeActionNotice: () => null,
    setActionNotice: (m, err) => sink.notices.push([String(m), !!err]),
    // The real one is a keyboard/ARIA wrapper; all this scenario needs is
    // that the header it is handed becomes a disclosure control.
    makeDisclosure: (head) => { head.setAttribute('role', 'button'); return head; },
    actionBtn: async (label) => {
      sink.labels.push(label);
      const b = dom.element('button');
      b.appendChild(dom.textNode(label));
      return b;
    },
    post: async (url, body) => { sink.posts.push(url); return sink.postReply(url, body); },
    del: async () => ({}),
    get: async url => {
      if (url.indexOf('/api/adaptive/proposals') === 0) return {proposals: PROPOSALS};
      if (url.indexOf('/api/adaptive/entries') === 0) return {enabled: true, auto_apply: true, entries: []};
      if (url.indexOf('/api/adaptive/batches') === 0) return {batches: []};
      return {events: []};
    },
    // `new CustomEvent(...)` + window.dispatchEvent, recorded rather than fired.
    CustomEvent: function CustomEvent(type, init) { return {type, detail: (init || {}).detail}; },
    window: { dispatchEvent: (ev) => { sink.events.push({type: ev.type, detail: ev.detail}); return true; } },
  };
}

function cardsIn(container) {
  return container.querySelectorAll('.proposal');
}
"""


CARD_SCENARIO = PRELUDE + r"""
const PROPOSALS = [CANARY, HELD, AUTO, UNEXPLAINED];

function build(stubbed) {
  const sink = {labels: [], posts: [], events: [], notices: [], postReply: async () => ({})};
  const { ctx } = makeContext(makeBase(PROPOSALS, sink));
  run(ctx, CODE);
  stubMissing(ctx, callees(CODE).concat(stubbed));
  return { ctx, sink };
}

const { stubbed, value } = await runScenario(build, async h => {
  const container = dom.element('div');
  h.ctx.__container = container;
  await run(h.ctx, 'renderAdaptiveTab(__container)');
  const cards = cardsIn(container);
  const read = c => ({
    text: c.textContent,
    explains: c.querySelectorAll('.adaptive-explain').map(x => x.textContent),
    labels: c.querySelectorAll('.adaptive-explain-label').map(x => x.textContent),
    fate: c.querySelectorAll('.adaptive-fate').map(x => ({label: x.textContent, cls: x.className, title: x.getAttribute('title')})),
    details: c.querySelectorAll('.adaptive-details-body').map(x => x.textContent),
    // Only the ones NOT inside a collapsed disclosure — what is on screen.
    editLines: c.querySelectorAll('.adaptive-edit-line').filter(x => !x.parentNode.classList.contains('adaptive-details-body')).map(x => x.textContent),
    detailsHead: c.querySelectorAll('.adaptive-details-head').map(x => ({t: x.textContent, role: x.getAttribute('role')})),
    buttons: c.querySelectorAll('button').map(b => b.textContent),
  });
  return {
    n: cards.length,
    broken: cards.filter(c => c.textContent.indexOf('could not be displayed') >= 0).length,
    canary: read(cards[0]), held: read(cards[1]), auto: read(cards[2]), plain: read(cards[3]),
    glossary: container.querySelectorAll('.fp-tab-desc').map(x => x.textContent),
    labels: h.sink.labels,
  };
});

report({ ...value, stubbed });
"""


def test_the_card_says_what_why_and_what_happens_if_you_walk_away(tmp_path):
    r = run_js(CARD_SCENARIO, tmp_path)
    assert r["n"] == 4, r
    assert r["broken"] == 0, r

    # (a) The three sentences, each behind its own label, in reading order.
    auto = r["auto"]
    assert auto["labels"] == ["What", "Why", "If you do nothing"], auto
    assert "wants to add a hint about which tool to reach for" in auto["explains"][0], auto
    assert "backed by 1 graded turn" in auto["explains"][1], auto
    assert "Applies on its own in 9 h" in auto["explains"][2], auto

    # ...and a pill whose LABEL, not only its colour, follows fate_kind. The
    # whole point is telling the three kinds apart without reading three
    # sentences first, so the three labels must differ.
    assert len(auto["fate"]) == 1, auto
    assert auto["fate"][0]["label"] == "applies on its own", auto["fate"]
    assert "ok" in auto["fate"][0]["cls"] and "adaptive-fate-auto" in auto["fate"][0]["cls"], auto["fate"]
    # The short label drops the deadline; the tooltip keeps it.
    assert "2026-09-15 21:00 UTC" in (auto["fate"][0]["title"] or ""), auto["fate"]

    canary_pill = r["canary"]["fate"][0]
    held_pill = r["held"]["fate"][0]
    assert canary_pill["label"] == "waiting for you", canary_pill
    assert "warn" in canary_pill["cls"] and "adaptive-fate-needs-you" in canary_pill["cls"], canary_pill
    assert held_pill["label"] == "held for you", held_pill
    assert "off" in held_pill["cls"] and "adaptive-fate-held" in held_pill["cls"], held_pill
    assert len({canary_pill["label"], held_pill["label"], auto["fate"][0]["label"]}) == 3

    # The rationale — the LLM-to-LLM text the owner could not read — is off the
    # head line and down in Details, not deleted.
    assert "pm drift on grep-heavy turns" not in auto["explains"][0], auto
    assert "pm drift on grep-heavy turns" in auto["details"][0], auto
    assert "pm:4821" in auto["details"][0], auto  # evidence too
    assert auto["detailsHead"] == [{"t": "Details", "role": "button"}], auto

    # (b) The canary OBJECT payload still renders, and its summary — the
    # 2026-09-08 fallback — is inside Details rather than gone.
    assert r["canary"]["labels"] == ["What", "Why", "If you do nothing"], r["canary"]
    assert len(r["canary"]["details"]) == 1, r["canary"]
    assert "never auto-approves" in r["canary"]["details"][0], r["canary"]

    # (c) explanation: null falls all the way back to the old card — rationale
    # on the head line, summary line, no explanation block, no pill.
    plain = r["plain"]
    assert plain["explains"] == [] and plain["fate"] == [], plain
    assert "#14 · 1h ago — raw rationale text" in plain["text"], plain
    assert "agent: acknowledge only" in plain["text"], plain
    # And VISIBLY so: with no explanation those lines are the whole card, so
    # burying them in a collapsed disclosure would leave a row that says less
    # than the one this replaced.
    assert plain["details"] == [] and plain["detailsHead"] == [], plain
    assert "agent: acknowledge only" in " ".join(plain["editLines"]), plain

    # Every card still offers all three actions, chat first, and a canary is
    # never an "Acknowledge" (approving it writes a CANARY.md and vets it).
    for key in ("auto", "canary", "held", "plain"):
        assert r[key]["buttons"][0] == "Chat about this", (key, r[key]["buttons"])
    assert r["labels"] == [
        "Approve & apply",
        "Reject",  # canary
        "Approve & apply",
        "Reject",  # held
        "Approve & apply",
        "Reject",  # auto
        "Acknowledge",
        "Reject",  # nothing to apply
    ], r["labels"]

    # The tab header says the same thing the cards do, so the list is not read
    # as one undifferentiated queue before a single card is opened.
    gloss = " ".join(r["glossary"]).lower()
    assert "veto window" in gloss and "self-test" in gloss, r["glossary"]


CHAT_SCENARIO = PRELUDE + r"""
const PROPOSALS = [AUTO];

function build(stubbed) {
  const sink = {
    labels: [], posts: [], events: [], notices: [],
    postReply: async (url) => {
      if (url.indexOf('/discuss') > 0) return {session_id: 'sess-777', opener: 'Tell me about #11', title: 'Proposal #11'};
      return {};
    },
  };
  const { ctx } = makeContext(makeBase(PROPOSALS, sink));
  run(ctx, CODE);
  stubMissing(ctx, callees(CODE).concat(stubbed));
  return { ctx, sink };
}

const { stubbed, value } = await runScenario(build, async h => {
  const container = dom.element('div');
  h.ctx.__container = container;
  await run(h.ctx, 'renderAdaptiveTab(__container)');
  const card = cardsIn(container)[0];
  const chat = card.querySelectorAll('button').filter(b => b.textContent === 'Chat about this')[0];
  if (!chat) throw new Error('no chat button');
  if (typeof chat.__onClick !== 'function') throw new Error('chat button has no click handler');
  await chat.__onClick();
  return {
    posts: h.sink.posts,
    events: h.sink.events,
    notices: h.sink.notices,
    title: chat.getAttribute('title'),
    disabled: !!chat.disabled,
  };
});

report({ ...value, stubbed });
"""


def test_chat_about_this_mints_a_session_and_hands_it_to_the_composer(tmp_path):
    r = run_js(CHAT_SCENARIO, tmp_path)

    # (d) One POST to the proposal's own discuss endpoint...
    assert r["posts"] == ["/api/adaptive/proposals/11/discuss"], r
    # ...and the session it minted goes to app.js, which owns the composer.
    assert len(r["events"]) == 1, r
    ev = r["events"][0]
    assert ev["type"] == "pernix:compose", ev
    assert ev["detail"] == {"session_id": "sess-777", "text": "Tell me about #11", "send": True}, ev
    # Nothing failed, so nothing was parked for the next render pass.
    assert r["notices"] == [], r
    assert r["title"], "the button needs to say what it will do"


FAILURE_SCENARIO = PRELUDE + r"""
const PROPOSALS = [AUTO];

function build(stubbed) {
  const sink = {
    labels: [], posts: [], events: [], notices: [],
    postReply: async (url) => {
      if (url.indexOf('/discuss') > 0) throw new Error('503: sessions are full');
      return {};
    },
  };
  const { ctx } = makeContext(makeBase(PROPOSALS, sink));
  run(ctx, CODE);
  stubMissing(ctx, callees(CODE).concat(stubbed));
  return { ctx, sink };
}

const { stubbed, value } = await runScenario(build, async h => {
  const container = dom.element('div');
  h.ctx.__container = container;
  await run(h.ctx, 'renderAdaptiveTab(__container)');
  const card = cardsIn(container)[0];
  const chat = card.querySelectorAll('button').filter(b => b.textContent === 'Chat about this')[0];
  let threw = null;
  try { await chat.__onClick(); } catch (e) { threw = String(e && e.message || e); }
  return {threw, notices: h.sink.notices, events: h.sink.events};
});

report({ ...value, stubbed });
"""


def test_a_failed_discuss_call_says_so_instead_of_doing_nothing(tmp_path):
    """A dead button is the worst version of this: the panel refreshes on
    every action, so a message written inline would be wiped a frame later —
    hence setActionNotice, which the next render pass prints at the top."""
    r = run_js(FAILURE_SCENARIO, tmp_path)
    assert r["threw"] is None, r  # the click handler owns its errors
    assert r["events"] == [], r  # and does NOT pretend it opened a chat
    assert len(r["notices"]) == 1, r
    message, is_error = r["notices"][0]
    assert message.startswith("Could not open a chat:"), r["notices"]
    assert "503" in message, r["notices"]
    assert is_error is True, r["notices"]
