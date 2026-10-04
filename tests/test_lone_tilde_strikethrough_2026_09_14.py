"""Live audit 2026-09-14 / L03: a lone `~` struck out the rest of the line.

`render.js` sets `marked.setOptions({ breaks: true, gfm: true })`, and GFM
strikethrough accepts a SINGLE tilde: `~x~` is `<del>x</del>`. Approximate
numbers are a normal way for a model to write — 182 of the 4 121 assistant
messages in the month to 09-14 contain a lone tilde — so a reply pairing two
of them renders everything between them struck through, and any `**bold**`
whose asterisks the `del` token split shows up as raw asterisks.

The fix registers a `del` tokenizer override through `marked.use` that only
matches `~~text~~`. `~5` and `~100K` are plain text again; `~~strike~~` is
untouched.

One correction to the finding, recorded because the example in the plan is
the thing a reader would reach for first: `"the ~256-byte vocabulary (vs
~100K tokens) …"` does NOT reproduce it. GFM forbids whitespace immediately
inside a fence, and the character before the second tilde there is a space.
What does reproduce it is any line where the second lone tilde is attached to
the word before it — `"takes ~4s; see data/~tmp/out"`, `"about ~20 runs, i.e.
3~4 per night"`, `"down from 1.2GB~1.4GB"` — all three of which are ordinary
model prose. `test_the_example_in_the_finding_never_reproduced` pins the
correction so nobody re-derives it.

The scenario runs the real vendored marked in two vm contexts: one with the
shipped options only (what the box renders today) and one that has run the
real `initMarked()`.
"""

from __future__ import annotations

import pytest

from tests.js_harness import requires_node, run_js

pytestmark = requires_node


SCENARIO = r"""
import fs from 'node:fs';
import { decls, fns, makeContext, mod, run, report } from './sandbox.mjs';

const RENDER = mod('render.js');
const MARKED = mod('../vendor/marked.min.js');
const MARKED_SRC = fs.readFileSync(MARKED, 'utf8');

const CODE = [
  decls(['DOUBLE_TILDE_DEL'], RENDER),
  fns(['initMarked'], RENDER),
].join('\n\n');

/** A context holding the real marked. `fixed` also runs the real initMarked. */
function parser(fixed) {
  const { ctx } = makeContext({ console });
  run(ctx, MARKED_SRC);
  if (fixed) {
    run(ctx, 'var _marked = null;\n' + CODE);
    run(ctx, 'initMarked()');
  } else {
    // What ships today: the options, and nothing else.
    run(ctx, 'marked.setOptions({ breaks: true, gfm: true })');
  }
  return md => { ctx.__md = md; return run(ctx, 'marked.parse(__md)').trim(); };
}

const before = parser(false), after = parser(true);

const CASES = {
  // Ordinary prose with two lone tildes, the second attached to a word.
  attached: 'takes ~4s; see data/~tmp/out and **bold**',
  counts: 'about ~20 runs, i.e. 3~4 per night, and **bold**',
  sizes: 'we cut it to ~300MB (down from 1.2GB~1.4GB) and **bold**',
  // Two lines of ONE paragraph: breaks:true does not stop the inline lexer
  // pairing a tilde on the first line with one on the second.
  acrossLines: 'first line about ~40 tokens\nsecond line, a 3~4x win',
  // The bold the finding reported as raw asterisks: a del token that splits
  // an emphasis pair takes the emphasis with it.
  splitBold: 'a ~5 **bold~ text** end',
  // The example in the finding. A space before the closing tilde, so GFM
  // never struck it in the first place.
  planExample: 'the ~256-byte vocabulary (vs ~100K tokens) is **the point**',
  // Real strikethrough, which has to keep working.
  doubled: '~~really gone~~ and then some',
  doubledBold: 'nested ~~strike with **bold**~~ ok',
  doubledTwice: '~~a~~ then ~5 then ~~b~~',
  // GFM's own "no whitespace inside the fence" rule, kept.
  spaced: '~~ not a strike ~~',
  home: 'copied to ~/Desktop and ~/tmp',
};

const out = { before: {}, after: {} };
for (const [k, md] of Object.entries(CASES)) {
  out.before[k] = before(md);
  out.after[k] = after(md);
}
report(out);
"""


@pytest.fixture(scope="module")
def l03(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("l03"))


# ── the failure ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", ["attached", "counts", "sizes", "acrossLines"])
def test_an_approximate_number_no_longer_strikes_the_rest_of_the_line(l03, case):
    """Each of these is prose a model writes weekly, and each of them lost a
    clause to <del> before the fix."""
    assert "<del>" in l03["before"][case], "the case no longer reproduces the bug"
    assert "<del>" not in l03["after"][case]
    assert "~" in l03["after"][case], "the tildes are literal text, not swallowed"


def test_the_bold_after_a_struck_span_comes_back(l03):
    """The reported symptom: a `del` that split an emphasis pair took the
    emphasis with it, so the reader saw raw asterisks."""
    assert "<del>" in l03["before"]["splitBold"]
    assert "<strong>" not in l03["before"]["splitBold"]
    assert "**" in l03["before"]["splitBold"]
    assert l03["after"]["splitBold"] == "<p>a ~5 <strong>bold~ text</strong> end</p>"


def test_the_example_in_the_finding_never_reproduced(l03):
    """A correction, pinned so it is not re-derived: GFM forbids whitespace
    just inside a fence, and the plan's example has a space before its second
    tilde. It renders correctly with and without the fix."""
    assert "<del>" not in l03["before"]["planExample"]
    assert l03["after"]["planExample"] == l03["before"]["planExample"]
    assert "<strong>the point</strong>" in l03["after"]["planExample"]


# ── what must not change ─────────────────────────────────────────────────────


def test_a_real_double_tilde_strike_still_strikes(l03):
    assert l03["after"]["doubled"] == "<p><del>really gone</del> and then some</p>"
    assert l03["after"]["doubledBold"] == "<p>nested <del>strike with <strong>bold</strong></del> ok</p>"
    assert l03["after"]["doubledTwice"] == "<p><del>a</del> then ~5 then <del>b</del></p>"


def test_gfms_own_fence_rules_are_kept(l03):
    """`~~ x ~~` is literal in GFM; the override keeps that rather than
    inventing a looser rule of its own."""
    assert l03["after"]["spaced"] == "<p>~~ not a strike ~~</p>"
    assert l03["before"]["spaced"] == l03["after"]["spaced"]


def test_a_home_path_is_left_alone(l03):
    assert l03["after"]["home"] == "<p>copied to ~/Desktop and ~/tmp</p>"
