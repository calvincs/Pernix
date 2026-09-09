"""2026-09-08: the Learning tab counted seven proposals and drew none of them.

Explorer -> Self-tuning -> Learning printed "Proposals awaiting review (7)"
above an empty panel — no cards, no Active entries, no batches, no journal.

`payload_json` has two shapes. The adaptive engine and dream write a LIST of
edits; `core/canary/propose.py` writes an OBJECT, `{"canary": {...}}`, because
a proposed canary is a spec rather than a batch of entry edits. The tab parsed
the payload and ran `for (const ed of edits)` over it unconditionally, so the
object threw `TypeError: edits is not iterable`. That rejected the whole
`renderAdaptiveTab` promise mid-pass: the section heading above the throwing
card had already been appended, and every card, entry, batch and journal line
after it never was. The newest pending proposal on the live box was a canary
one, so the very first iteration threw and the count stood alone.

The fix moves one card into `proposalCard()`, iterates edits only when the
payload IS a list, falls back to the server's own `summary` (which
`describe_proposal` computes for every shape) otherwise, and wraps each call
so a shape this code has not met yet costs one row instead of the tab.

Approving a canary is also not an acknowledgement — `approve_proposal`
materializes the CANARY.md and queues a vetting run — so the button label is
driven by whether the payload applies anything, not by `edits.length`.
"""

from __future__ import annotations

from tests.js_harness import requires_node, run_js

pytestmark = requires_node


SCENARIO = r"""
import { mod, fns, makeContext, makeDoc, run, report, runScenario, callees, stubMissing } from './sandbox.mjs';

const ADAPTIVE = mod('components/modals/adaptive.js');
const dom = makeDoc();

// The real bytes of both functions under test — no transcription.
const CODE = fns(['proposalCard', 'renderAdaptiveTab'], ADAPTIVE);

// Three list-shaped proposals with the canary OBJECT newest, exactly as
// `adaptive_list_proposals` orders them (created_at DESC).
const PROPOSALS = [
  {id: 4, producer: 'canary_propose', created_at: '2026-09-08T12:00:00+00:00',
   rationale: "[new canary 'grep-count']",
   payload_json: JSON.stringify({canary: {name: 'grep-count', prompt: 'count', gates: [{name: 'g', command: 'grep -c x f'}]}}),
   summary: "canary_propose: new canary 'grep-count' (waits for a human approve/reject; never auto-approves)"},
  {id: 3, producer: 'dream', created_at: '2026-09-08T11:00:00+00:00', rationale: 'two',
   payload_json: JSON.stringify([{kind: 'prompt_note', action: 'add', title: 't3', content: 'c3'}]), summary: 'dream: add prompt_note'},
  {id: 2, producer: 'dream', created_at: '2026-09-08T10:00:00+00:00', rationale: 'one',
   payload_json: JSON.stringify([{kind: 'prompt_note', action: 'add', title: 't2', content: 'c2'}]), summary: 'dream: add prompt_note'},
];

function build(stubbed) {
  const labels = [];
  const base = {
    console, JSON, Date, Array, Promise, setTimeout,
    // render.js's helpers, in the shapes this file uses them.
    el: (tag, attrs, kids) => {
      const e = dom.element(tag);
      if (attrs && attrs.class) e.className = attrs.class;
      (kids || []).forEach(k => k && e.appendChild(k));
      return e;
    },
    text: v => dom.textNode(String(v)),
    clear: node => { node.childNodes = []; },
    icon: () => dom.element('span'),
    badge: (label, cls) => { const b = dom.element('span'); b.className = 'adaptive-badge ' + (cls || ''); b.appendChild(dom.textNode(String(label))); return b; },
    section: title => { const s = dom.element('div'); s.className = 'adaptive-section-title'; s.appendChild(dom.textNode(title)); return s; },
    relTime: () => '1h ago',
    tabGlossary: () => dom.element('div'),
    takeActionNotice: () => null,
    // Every button records the label it was built with; that is the second
    // assertion (a canary must not read "Acknowledge").
    actionBtn: async (label) => { labels.push(label); const b = dom.element('button'); b.appendChild(dom.textNode(label)); return b; },
    post: async () => ({}),
    del: async () => ({}),
    get: async url => {
      if (url.indexOf('/api/adaptive/proposals') === 0) return {proposals: PROPOSALS};
      if (url.indexOf('/api/adaptive/entries') === 0) return {enabled: true, auto_apply: false, entries: []};
      if (url.indexOf('/api/adaptive/batches') === 0) return {batches: []};
      return {events: []};
    },
  };
  const { ctx } = makeContext(base);
  run(ctx, CODE);
  stubMissing(ctx, callees(CODE).concat(stubbed));
  return { ctx, labels, base };
}

const { stubbed, value } = await runScenario(build, async h => {
  const container = dom.element('div');
  h.ctx.__container = container;
  await run(h.ctx, 'renderAdaptiveTab(__container)');
  const titles = container.querySelectorAll('.adaptive-section-title').map(n => n.textContent);
  const cards = container.querySelectorAll('.proposal');
  return {
    titles,
    cards: cards.length,
    // No card may report itself as undisplayable.
    broken: cards.filter(c => c.textContent.indexOf('could not be displayed') >= 0).length,
    canaryText: cards.length ? cards[0].textContent : '',
    labels: h.labels,
  };
});

report({ ...value, stubbed });
"""


def test_a_dict_payload_no_longer_empties_the_tab(tmp_path):
    r = run_js(SCENARIO, tmp_path)

    # Before the fix: one heading, zero cards. The count said three.
    assert r["cards"] == 3, r
    assert r["broken"] == 0, r

    # And the render got all the way past the proposals to the sections below,
    # which is what actually vanished for the user.
    assert any(t.startswith("Proposals awaiting review (3)") for t in r["titles"]), r
    assert any(t.startswith("Active entries") for t in r["titles"]), r
    assert any(t.startswith("Batches") for t in r["titles"]), r
    assert any(t.startswith("Event journal") for t in r["titles"]), r

    # The canary card falls back to the server's summary rather than rendering
    # nothing but its rationale.
    assert "never auto-approves" in r["canaryText"], r

    # Approving a canary materializes a CANARY.md — never "Acknowledge".
    assert r["labels"][:2] == ["Approve & apply", "Reject"], r["labels"]
    assert "Acknowledge" not in r["labels"], r["labels"]


# The same fixture, but pointed at the source as it stood before the fix. It
# pins what actually broke: without it, the test above passes for any file
# that merely happens to define `proposalCard`.
LEGACY_SCENARIO = r"""
import { mod, fns, makeContext, makeDoc, run, report, runScenario, callees, stubMissing } from './sandbox.mjs';

const dom = makeDoc();
const CODE = fns(['renderAdaptiveTab'], mod('components/modals/adaptive.js'));

const PROPOSALS = [
  {id: 4, producer: 'canary_propose', created_at: '2026-09-08T12:00:00+00:00', rationale: 'canary',
   payload_json: JSON.stringify({canary: {name: 'grep-count'}}), summary: 'canary_propose: new canary'},
  {id: 3, producer: 'dream', created_at: '2026-09-08T11:00:00+00:00', rationale: 'two',
   payload_json: JSON.stringify([{kind: 'prompt_note', action: 'add', title: 't3', content: 'c3'}]), summary: 's'},
];

function build(stubbed) {
  const base = {
    console, JSON, Date, Array, Promise, setTimeout,
    el: (tag, attrs, kids) => {
      const e = dom.element(tag);
      if (attrs && attrs.class) e.className = attrs.class;
      (kids || []).forEach(k => k && e.appendChild(k));
      return e;
    },
    text: v => dom.textNode(String(v)),
    clear: node => { node.childNodes = []; },
    icon: () => dom.element('span'),
    badge: label => { const b = dom.element('span'); b.className = 'adaptive-badge'; b.appendChild(dom.textNode(String(label))); return b; },
    section: title => { const s = dom.element('div'); s.className = 'adaptive-section-title'; s.appendChild(dom.textNode(title)); return s; },
    relTime: () => '1h ago',
    tabGlossary: () => dom.element('div'),
    takeActionNotice: () => null,
    actionBtn: async label => { const b = dom.element('button'); b.appendChild(dom.textNode(label)); return b; },
    post: async () => ({}), del: async () => ({}),
    get: async url => {
      if (url.indexOf('/api/adaptive/proposals') === 0) return {proposals: PROPOSALS};
      if (url.indexOf('/api/adaptive/entries') === 0) return {enabled: true, auto_apply: false, entries: []};
      if (url.indexOf('/api/adaptive/batches') === 0) return {batches: []};
      return {events: []};
    },
  };
  const { ctx } = makeContext(base);
  run(ctx, CODE);
  stubMissing(ctx, callees(CODE).concat(stubbed));
  return { ctx };
}

const { value } = await runScenario(build, async h => {
  const container = dom.element('div');
  h.ctx.__container = container;
  let threw = null;
  try { await run(h.ctx, 'renderAdaptiveTab(__container)'); } catch (e) { threw = String(e && e.message || e); }
  return {
    threw,
    titles: container.querySelectorAll('.adaptive-section-title').map(n => n.textContent),
    cards: container.querySelectorAll('.proposal').length,
  };
});

report(value);
"""


# The last commit before the fix. Pinned rather than `HEAD`, which stops
# proving anything the moment the fix lands.
PRE_FIX_REV = "97e8d2f8bc452cc91900353cc80b2cd984d32001"


def test_the_pre_fix_source_really_did_empty_the_tab(tmp_path):
    import subprocess

    import pytest

    from tests.js_harness import REPO_ROOT

    rel = "static/js/components/modals/adaptive.js"
    proc = subprocess.run(["git", "show", f"{PRE_FIX_REV}:{rel}"], cwd=REPO_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"{PRE_FIX_REV[:7]} is not in this clone: {proc.stderr.strip()}")
    old = proc.stdout
    assert "proposalCard" not in old, "PRE_FIX_REV is not actually pre-fix"

    shadow = tmp_path / "static-js"
    (shadow / "components" / "modals").mkdir(parents=True)
    (shadow / "components" / "modals" / "adaptive.js").write_text(old)

    r = run_js(LEGACY_SCENARIO, tmp_path, static_js=shadow)
    assert r["threw"] and "is not iterable" in r["threw"], r
    assert r["cards"] == 0, r
    # The heading rendered; nothing after the throwing card did.
    assert any(t.startswith("Proposals awaiting review (2)") for t in r["titles"]), r
    assert not any(t.startswith("Active entries") for t in r["titles"]), r
