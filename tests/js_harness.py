"""Run the real static/js sources under node, from pytest.

`check.sh` runs no JavaScript. The only JS-aware test in the tree greps
`sse.js` for event names, and the UI gate replaces `EventSource` with an inert
class in its one streaming pass, so no `stream.token` is ever delivered
anywhere in it. Between them, nothing in CI has ever executed a line of the
streaming client — which is where four of the 3.2.2 audit's six P1 findings
lived.

So these helpers do the cheapest thing that actually exercises it: slice the
byte-exact source text of named top-level functions out of `static/js/*.js`,
run it inside a `node:vm` context against a hand-built fake DOM, and assert on
what it did. Nothing is retyped — `fns()` and `decls()` copy the real bytes, so
a test cannot quietly drift into testing a transcription of the code. The cost
is that the fakes have to be good enough; the benefit is that renaming or
restructuring one of these functions fails the suite loudly rather than
silently un-testing it.

Skips cleanly when node is absent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = REPO_ROOT / "static" / "js"

NODE = shutil.which("node")

requires_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

# The scenario prints exactly one line starting with this marker; everything
# else it writes is diagnostic output that pytest shows on failure.
RESULT_MARKER = "__PERNIX_RESULT__"


# ---------------------------------------------------------------------------
# The JS side. Kept here rather than as a checked-in .mjs so the extraction
# rules and the tests that depend on them cannot drift apart.
# ---------------------------------------------------------------------------

SANDBOX_MJS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

export const APP = process.env.PERNIX_APP_JS;
export const SSE = process.env.PERNIX_SSE_JS;
export const RENDER = process.env.PERNIX_RENDER_JS;
export const MARKER = process.env.PERNIX_RESULT_MARKER;

/** The EXACT source text of one top-level function: the real bytes between
 *  `function NAME(` at column 0 and the matching `}` at column 0. */
export function extract(file, name) {
  const lines = fs.readFileSync(file, 'utf8').split('\n');
  const re = new RegExp(`^(export\\s+)?(async\\s+)?function ${name}\\s*\\(`);
  const start = lines.findIndex(l => re.test(l));
  if (start < 0) throw new Error(`not found: function ${name} in ${file}`);
  let end = -1;
  for (let i = start + 1; i < lines.length; i++) {
    if (lines[i] === '}') { end = i; break; }
  }
  if (end < 0) throw new Error(`no closing brace for ${name} in ${file}`);
  // `export` is dropped, and only that: the body is untouched bytes.
  const text = lines.slice(start, end + 1).join('\n').replace(/^export\s+/, '');
  return { text, start: start + 1, end: end + 1 };
}

/** A multi-line `const NAME = [ ... ];` literal, verbatim, as a `var`. */
export function arrayConst(name, file) {
  const src = fs.readFileSync(file, 'utf8');
  const head = `const ${name} = [`;
  const i = src.indexOf(head);
  if (i < 0) throw new Error(`array const not found: ${name} in ${file}`);
  const j = src.indexOf('\n];', i);
  if (j < 0) throw new Error(`unterminated array const: ${name} in ${file}`);
  return 'var ' + src.slice(i + 6, j + 3);
}

export function fns(names, file = APP) {
  return names.map(n => {
    const e = extract(file, n);
    return `/* ==== ${file.split('/').pop()}:${e.start}-${e.end} ${n}() ==== */\n${e.text}`;
  }).join('\n\n');
}

/** Verbatim single-line module-level declarations, rewritten to `var` so the
 *  vm context sees them as globals. */
export function decls(names, file = APP) {
  const lines = fs.readFileSync(file, 'utf8').split('\n');
  const out = [];
  for (const n of names) {
    const i = lines.findIndex(l => new RegExp(`^(let|const|var) ${n}\\b`).test(l));
    if (i < 0) throw new Error(`decl not found: ${n} in ${file}`);
    const m = lines[i].match(/^(let|const|var) [A-Za-z0-9_$]+ = .*?;/);
    if (!m) throw new Error(`multi-line decl not supported: ${n} -> ${lines[i]}`);
    out.push(m[0].replace(/^(let|const) /, 'var '));
  }
  return out.join('\n');
}

/** Plain-object sandbox. No Proxy: vm's global-proxy interceptors and Proxy
 *  sandboxes disagree about `var`, and unknown identifiers must surface as
 *  real ReferenceErrors so the scenario runner can stub them explicitly. */
export function makeContext(base) {
  const sandbox = Object.assign({}, base);
  sandbox.globalThis = sandbox;
  return { ctx: vm.createContext(sandbox), sandbox };
}

export function run(ctx, code) {
  return vm.runInContext(code, ctx, { filename: 'extracted-from-source' });
}

export function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  promise.catch(() => {});   // the scenario asserts on effects, not rejections
  return { promise, resolve, reject };
}

export const tick = () => new Promise(r => setTimeout(r, 0));

/**
 * Catch a ReferenceError escaping an async call under test.
 *
 * The functions here are started and NOT awaited (that is the whole point —
 * the tests interleave a second click with the first one's awaits), so an
 * unhandled rejection would either crash node or, if the scenario swallows it,
 * hide a missing global behind a confusing later assertion. `guard` files it;
 * `ck` re-throws it where runScenario can turn it into an explicit stub.
 */
export function guard(h, p) {
  if (p && typeof p.catch === 'function') {
    p.catch(e => { if (!h.refErr) h.refErr = e; });
  }
  return p;
}

export function ck(h) {
  if (h.refErr) { const e = h.refErr; h.refErr = null; throw e; }
}

/** Let every pending microtask AND zero-delay timer drain. */
export async function settle(n = 6) {
  for (let i = 0; i < n; i++) await tick();
}

/** Real elapsed time, for the parts under test that are deliberately
 *  time-bounded (the streaming paint cadence). */
export const wait = ms => new Promise(r => setTimeout(r, ms));

/** Drain until `fn()` is truthy. For "the code under test has got as far as
 *  issuing its next request", which is a chain of awaits, not a fixed count. */
export async function until(fn, limit = 60) {
  for (let i = 0; i < limit; i++) {
    if (fn()) return true;
    await tick();
  }
  throw new Error('condition never became true: ' + fn.toString());
}

/**
 * A DOM node with enough surface for the render and message paths: real
 * children, real text nodes, class lists, and a `:scope > .x` querySelector
 * that only has to answer the two selectors the streaming renderer uses.
 */
export function makeDoc() {
  const TEXT = 3;
  function textNode(v) {
    return {
      nodeType: TEXT, nodeValue: v,
      get textContent() { return this.nodeValue; },
      appendData(d) { this.nodeValue += d; },
    };
  }
  function element(tag) {
    const e = {
      tagName: String(tag).toUpperCase(), nodeType: 1, childNodes: [], removed: false,
      attrs: {}, style: {}, dataset: {}, _classes: new Set(),
      get className() { return [...e._classes].join(' '); },
      set className(v) { e._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
      classList: {
        add: (...c) => c.forEach(x => e._classes.add(x)),
        remove: (...c) => c.forEach(x => e._classes.delete(x)),
        contains: c => e._classes.has(c),
        toggle: (c, on) => { if (on === undefined ? e._classes.has(c) : !on) e._classes.delete(c); else e._classes.add(c); },
      },
      get firstChild() { return e.childNodes[0] || null; },
      get children() { return e.childNodes.filter(n => n.nodeType === 1); },
      appendChild(n) {
        if (n && n.__fragment) { n.childNodes.slice().forEach(c => e.appendChild(c)); return n; }
        e.childNodes.push(n); if (n) n.parentNode = e; return n;
      },
      removeChild(n) { const i = e.childNodes.indexOf(n); if (i >= 0) e.childNodes.splice(i, 1); return n; },
      remove() { e.removed = true; if (e.parentNode) e.parentNode.removeChild(e); },
      setAttribute(k, v) { e.attrs[k] = String(v); },
      getAttribute(k) { return e.attrs[k] ?? null; },
      addEventListener() {}, removeEventListener() {}, dispatchEvent() {}, focus() {}, scrollIntoView() {},
      get textContent() { return e.childNodes.map(n => n.textContent ?? '').join(''); },
      set textContent(v) { e.childNodes = []; if (v !== '') e.appendChild(textNode(String(v))); },
      querySelector(sel) { return e.querySelectorAll(sel)[0] || null; },
      querySelectorAll(sel) {
        const direct = /^:scope\s*>\s*\.(\S+)$/.exec(sel);
        if (direct) return e.children.filter(c => c._classes.has(direct[1]));
        const cls = /^\.(\S+)$/.exec(sel);
        const tag = /^([a-z]+)$/i.exec(sel);
        const out = [];
        (function walk(node) {
          for (const c of node.children) {
            if (cls && c._classes.has(cls[1])) out.push(c);
            else if (tag && c.tagName === tag[1].toUpperCase()) out.push(c);
            walk(c);
          }
        })(e);
        return out;
      },
    };
    return e;
  }
  return { element, textNode, TEXT };
}

/**
 * Every identifier used in CALL position in `code`.
 *
 * Load-bearing, not a convenience: app.js swallows errors in bare `catch {}`
 * blocks, so a ReferenceError for a helper the fixture forgot to provide is
 * caught by the code under test, and the scenario silently measures the
 * failure path instead of the one it meant to. Pre-stubbing every callee the
 * fixture does not define turns that into an explicit, inspectable list.
 */
export function callees(code) {
  const out = new Set();
  for (const m of code.matchAll(/(^|[^\w$.])([A-Za-z_$][\w$]*)\s*\(/g)) out.add(m[2]);
  const kw = new Set(['if', 'for', 'while', 'switch', 'catch', 'return', 'typeof', 'function', 'new',
                      'await', 'else', 'do', 'try', 'throw', 'delete', 'void', 'in', 'of', 'case',
                      'yield', 'async', 'instanceof']);
  return [...out].filter(n => !kw.has(n));
}

/**
 * Define a no-op for every name missing from the CONTEXT — checked against the
 * live context rather than the fixture object, so the vm realm's own
 * intrinsics (Object, Promise, Math, ...) are never shadowed. Call it AFTER
 * the extracted source has run, so real declarations always win over a stub.
 */
export function stubMissing(ctx, names) {
  const stubbed = [];
  for (const n of names) {
    if (vm.runInContext(`typeof ${n} !== 'undefined'`, ctx)) continue;
    vm.runInContext(`var ${n} = function ${n}_autoStub() {};`, ctx);
    stubbed.push(n);
  }
  return stubbed;
}

/**
 * Emit the single machine-readable result line pytest parses, then exit.
 *
 * The explicit exit is load-bearing: the code under test arms real timers
 * (a 15 s health interval, a 10 s reconnect banner, the paint cadence) that
 * would otherwise hold node open long past the end of the scenario.
 */
export function report(obj) {
  console.log(MARKER + JSON.stringify(obj));
  process.exit(0);
}

/**
 * Build the context, run the body, and turn any missing global into an
 * explicitly-recorded no-op stub before retrying. app.js swallows errors in
 * bare `catch {}` blocks, so a ReferenceError inside one would otherwise
 * vanish; the retry loop makes every stub visible in the result instead.
 */
export async function runScenario(build, body, { maxStubs = 120 } = {}) {
  const stubbed = [];
  for (let attempt = 0; attempt <= maxStubs; attempt++) {
    const h = build(stubbed);
    try {
      const value = await body(h);
      return { stubbed, value };
    } catch (e) {
      const m = /^(\w[\w$]*) is not defined$/.exec((e && e.message) || '');
      if (m && e && e.name === 'ReferenceError') { stubbed.push(m[1]); continue; }
      throw e;
    }
  }
  throw new Error('too many missing globals: ' + stubbed.join(', '));
}
"""


def run_js(scenario_src: str, tmp_path: Path, *, app_js: Path | None = None) -> dict:
    """Execute one scenario module and return the object it reported.

    `app_js` lets a caller point the extraction at a copy of the source (for
    example one produced by `git show <rev>:static/js/app.js`) instead of the
    working tree.
    """
    work = tmp_path / "js"
    work.mkdir(exist_ok=True)
    (work / "sandbox.mjs").write_text(SANDBOX_MJS)
    (work / "scenario.mjs").write_text(scenario_src)
    env = {
        **os.environ,
        "PERNIX_APP_JS": str(app_js or (STATIC_JS / "app.js")),
        "PERNIX_SSE_JS": str(STATIC_JS / "sse.js"),
        "PERNIX_RENDER_JS": str(STATIC_JS / "render.js"),
        "PERNIX_RESULT_MARKER": RESULT_MARKER,
    }
    proc = subprocess.run(
        [NODE, str(work / "scenario.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(work),
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node exited {proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(RESULT_MARKER)]
    if not lines:
        raise AssertionError(f"scenario reported nothing\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    result = json.loads(lines[-1][len(RESULT_MARKER) :])
    result["_stdout"] = proc.stdout
    return result
