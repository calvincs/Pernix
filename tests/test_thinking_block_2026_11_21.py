"""The thinking block: a reasoning model's chain of thought, folded (2026-11-21).

`stream.reasoning` carries the model's reasoning apart from `stream.token`.
The transcript shows it as a native <details>, collapsed by default, above
the answer or the tool calls of the round it belongs to — never inside the
answer text — and a reload renders the same block from the row's
`reasoning` column.

These run the real app.js functions under node against the harness's fake
DOM (tests/js_harness.py). What they cannot show is the rendering itself:
the CSS, the disclosure triangle, the phone layout.
"""

from __future__ import annotations

import pytest

from tests.js_harness import requires_node, run_js

pytestmark = requires_node


SCENARIO = r"""
import { decls, fns, makeContext, makeDoc, run, report } from './sandbox.mjs';

const doc = makeDoc();

// The harness element has no insertBefore / isConnected; the code under test
// uses both, so they are added here with the DOM's semantics.
function node(tag) {
  const e = doc.element(tag);
  e.insertBefore = (n, ref) => {
    const i = e.childNodes.indexOf(ref);
    if (i < 0) return e.appendChild(n);
    e.childNodes.splice(i, 0, n); n.parentNode = e; return n;
  };
  Object.defineProperty(e, 'isConnected', {
    get() { return !e.removed && !!e.parentNode && (e.parentNode === inner || e.parentNode.isConnected); },
  });
  return e;
}
const inner = node('div');
inner.removed = false;

function el(tag, attrs = {}, children = []) {
  const e = node(tag);
  if (attrs.class) e.className = attrs.class;
  for (const c of children) e.appendChild(c);
  return e;
}
const text = s => doc.textNode(String(s));

const log = [];
const { ctx } = makeContext({
  console, Date, el, text,
  document: { getElementById: () => inner },
  state: { streaming: false },
  _renderTarget: null,
  _streamingEl: null, _collected: '',
  _toolGroup: null, _toolGroupCount: 0, _toolGroupErrors: 0, _toolGroupLatency: 0, _toolGroupRunning: 0,
  _lastMsgTs: 0, _replayingTranscript: false,
  closeToolGroup: () => log.push('closeToolGroup'),
  _showStopButton: () => log.push('stop'),
  _clearScoutStatus: () => {},
  scrollToBottom: () => {},
  _toolCallIndex: () => ({}),
  _parseRowMetadata: () => ({}),
  _toolItemKey: () => '',
  appendToolToGroup: (name) => { log.push('tool:' + name); inner.appendChild(el('div', { class: 'tool-group' })); },
  appendMessage: (role, content) => { log.push(role + ':' + content); return inner.appendChild(el('div', { class: 'message ' + role })); },
});
run(ctx, [
  decls(['_thinkingEl', '_thinkingAttemptEl']),
  fns(['_messagesInner', '_humanizeMs', '_thinkingLabel', '_setThinkingSummary', 'buildThinkingBlock',
       '_appendThinking', '_endThinkingRun', '_closeThinkingRound', '_discardThinkingAttempt', '_replayMessages']),
].join('\n\n'));

const kids = () => inner.childNodes.map(c => c.className);
const summary = b => b.querySelector('summary').textContent;
const out = {};

// --- live: send() has opened the empty answer bubble ---
run(ctx, `_streamingEl = el('div', { class: 'message assistant' }); _messagesInner().appendChild(_streamingEl);`);
run(ctx, `_appendThinking('Let me '); _appendThinking('think.');`);
const live = run(ctx, '_thinkingEl');
out.liveOrder = kids();
out.liveSummary = summary(live);
out.liveBody = live.querySelector('.thinking-body').textContent;
out.liveOpen = live.getAttribute('open');
out.liveTag = live.tagName;
out.streaming = run(ctx, 'state.streaming');

// first answer token: the block stops being live, keeps its text
run(ctx, `_endThinkingRun()`);
out.doneClasses = live.className;
out.doneSummary = summary(live);
out.sameBlockAfterToken = run(ctx, '_thinkingAttemptEl') === live;

// --- a retry discards this attempt's thinking with its partial ---
run(ctx, `_discardThinkingAttempt()`);
out.afterReset = kids();

// --- next round after a tool call: no open bubble, block goes at the end ---
run(ctx, `_closeThinkingRound(); _streamingEl.remove(); _streamingEl = null;`);
inner.appendChild(el('div', { class: 'tool-group' }));
run(ctx, `_appendThinking('Round two.')`);
out.roundTwoOrder = kids();
run(ctx, `_closeThinkingRound()`);
run(ctx, `_appendThinking('Round three.')`);
out.roundThreeBlocks = inner.childNodes.filter(c => c.className.startsWith('thinking')).length;

// --- labels ---
out.labels = [
  run(ctx, `_thinkingLabel(3400, 12000, false)`),
  run(ctx, `_thinkingLabel(250, 400, false)`),
  run(ctx, `_thinkingLabel(0, 0, true)`),
];

// --- reload: the same blocks, in the same places ---
inner.childNodes = [];
log.length = 0;
run(ctx, `_replayMessages(${JSON.stringify([
  { role: 'user', content: 'q' },
  { role: 'assistant', content: '', reasoning: 'I will run ls.', tool_calls: '[]' },
  { role: 'tool', content: 'a b c', tool_call_id: 'c1' },
  { role: 'assistant', content: 'The answer.', reasoning: 'ls worked.' },
  { role: 'assistant', content: 'Old row, no reasoning.' },
])})`);
out.replayOrder = kids();
out.replayLog = log.slice();
out.replayBodies = inner.childNodes.filter(c => c.className === 'thinking')
  .map(b => b.querySelector('.thinking-body').textContent);
out.replayOpen = inner.childNodes.filter(c => c.className === 'thinking').map(b => b.getAttribute('open'));
report(out);
"""


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("thinking"))


def test_live_thinking_is_a_collapsed_details_above_the_answer_bubble(ran):
    assert ran["liveTag"] == "DETAILS"
    assert ran["liveOpen"] is None  # collapsed by default
    assert ran["liveOrder"] == ["thinking thinking-live", "message assistant"]
    assert ran["liveBody"] == "Let me think."
    assert ran["liveSummary"] == "Thinking… · 13 chars"
    assert ran["streaming"] is True


def test_the_first_answer_token_ends_the_live_block(ran):
    assert ran["doneClasses"] == "thinking"
    assert ran["doneSummary"] == "Thinking · 13 chars"
    assert ran["sameBlockAfterToken"] is True


def test_a_retry_removes_the_discarded_attempts_thinking(ran):
    assert ran["afterReset"] == ["message assistant"]


def test_each_round_gets_its_own_block_after_its_tools(ran):
    assert ran["roundTwoOrder"] == ["tool-group", "thinking thinking-live"]
    assert ran["roundThreeBlocks"] == 2


def test_labels_are_short(ran):
    assert ran["labels"] == [
        "Thinking · 12s · 3.4k chars",
        "Thinking · 250 chars",
        "Thinking…",
    ]


def test_reload_renders_the_same_blocks_in_the_same_places(ran):
    assert ran["replayOrder"] == [
        "message user",
        "thinking",
        "tool-group",
        "thinking",
        "message assistant",
        "message assistant",
    ]
    assert ran["replayBodies"] == ["I will run ls.", "ls worked."]
    assert ran["replayOpen"] == [None, None]
    # The answer bubble gets the answer only; the reasoning never reaches it.
    assert "assistant:The answer." in ran["replayLog"]
    assert not any("ls worked." in entry for entry in ran["replayLog"])
