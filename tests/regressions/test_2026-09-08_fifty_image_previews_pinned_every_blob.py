"""Audit 3.2.2 / S21, 2026-09-08: every image the Explorer previewed stayed in
memory for the life of the browser tab.

`viewFile` turned an image response into a blob with `URL.createObjectURL`, and
exactly one place in the whole file ever called `URL.revokeObjectURL`: the
viewer's Back button. A blob URL is an allocation on the *document*, not on the
`<img>` that renders it — dropping the element, the string, or the entire panel
frees nothing until that exact string is revoked. So Back was the only exit
that did not leak, and there were four others:

  * switching Explorer tabs (`_selectTab` nulled `currentFile` and walked away)
  * opening the next file (image→image and image→text both overwrote it)
  * deleting the file being previewed
  * the skill viewer's own Back button

Driving the real code paths with an instrumented `URL`: ten open/Back cycles
revoked all ten; ten open/tab-switch cycles revoked none; ten image→image and
ten image→text replacements revoked none. A realistic session — fifty
screenshots ending on a tab change — left fifty blobs outstanding, roughly
100 MB pinned at 2 MB a screenshot.

Underneath the leak sat a worse bug the audit did not name. `viewFile` is
`async` with no sequence guard, so two clicks whose fetches resolve out of
order left the viewer showing the file the user had navigated AWAY from — the
slow first request overwrote the fast second one. That affects text files as
much as images; `_wsSeq` had been guarding directory listings against exactly
this shape since it was written.

The fix is a single-owner contract with generations. `_previewUrl` is the one
object URL the panel owns, non-null only while `currentFile` is the image it
belongs to; `_installPreview` revokes the outgoing URL as it installs the new
one, and `_disposePreview` retires the preview from every leave-the-viewer path
(Back, tab change, delete, panel close). Each load claims a generation on entry
and re-checks it before installing, so an overtaken response revokes the blob
it just created instead of pushing it over a newer view.

There is no JavaScript runner in check.sh, so this test is the only automated
protection these paths have: it extracts the real functions out of
static/js/components/file-panel.js by content anchor and runs them under node
against a stub DOM. It fails loudly if the anchors stop matching, rather than
quietly testing a transcription.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed — the browser-side harness cannot run")


# The harness. It reads the shipping file, cuts the functions out of it by
# anchor, evaluates them with a stubbed fetch/URL/state, and prints one JSON
# blob of scenario results.
HARNESS = r"""
import fs from 'node:fs';
import path from 'node:path';

const root = process.argv[2];
const FILE = path.join(root, 'static/js/components/file-panel.js');
const SRC = fs.readFileSync(FILE, 'utf8');

function must(i, what) {
    if (i < 0) throw new Error(`anchor not found in file-panel.js: ${what}`);
    return i;
}

// Everything between two anchors, inclusive of the closing one.
function span(startAnchor, endAnchor) {
    const i = must(SRC.indexOf(startAnchor), startAnchor);
    const j = must(SRC.indexOf(endAnchor, i), endAnchor);
    return SRC.slice(i, j + endAnchor.length);
}

// A whole function declaration, by brace matching from its signature.
function fn(anchor) {
    const i = must(SRC.indexOf(anchor), anchor);
    let depth = 0;
    for (let k = SRC.indexOf('{', i); k >= 0 && k < SRC.length; k++) {
        if (SRC[k] === '{') depth++;
        else if (SRC[k] === '}' && --depth === 0) return SRC.slice(i, k + 1);
    }
    throw new Error(`unbalanced braces after ${anchor}`);
}

const PIECES = [
    // extension predicates + the two size ceilings
    span('const EXT_LANG = {', "function isMarkdown(name) { return ['.md', '.markdown'].includes(getExt(name)); }"),
    // the ownership contract itself
    span('// ── preview ownership (S21)', '// ── end preview ownership'),
    fn('async function viewFile(path, source'),
    fn('function _mtimeOf(resp)'),
    fn('function _selectTab(key)'),
];

for (const [name, needle] of [
    ['preview ownership block', '_disposePreview'],
    ['generation guard in viewFile', '_previewSuperseded'],
    ['install helper', '_installPreview'],
    ['image ceiling', 'MAX_IMAGE_SIZE'],
]) {
    if (!PIECES.join('\n').includes(needle)) throw new Error(`extracted source is missing ${name} (${needle})`);
}

const FACTORY = new Function('deps', `
"use strict";
const {
    _state, _authHdr, notify, renderCurrentTab, renderTabs, renderSkills,
    loadTabData, saveState, guardDirty, clearElapsedTimers, _groupOf, fetch, URL,
} = deps;
${PIECES.join('\n\n')}
return {
    viewFile, _disposePreview, _backToTree, _closeViewerFor, _selectTab,
    ownedUrl: () => _previewUrl,
    generation: () => _previewSeq,
};
`);

const tick = async (n = 6) => { for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r)); };

function makeEnv() {
    const created = [];
    const revoked = [];
    const doubleRevoked = [];
    const notifications = [];
    const state = {
        tab: 'workspace', group: 'files', groupTabs: {},
        viewMode: 'tree', currentFile: null, dirty: false,
    };
    const routes = new Map();
    const pending = new Map();

    const URLStub = {
        createObjectURL() {
            const u = `blob:pernix/${created.length + 1}`;
            created.push(u);
            return u;
        },
        revokeObjectURL(u) {
            if (revoked.includes(u)) doubleRevoked.push(u);
            revoked.push(u);
        },
    };

    const fetchStub = (url) => {
        const route = routes.get(url);
        const respond = () => {
            if (!route || route.ok === false) {
                return { ok: false, status: 404, statusText: 'Not Found', headers: { get: () => null } };
            }
            return {
                ok: true, status: 200, statusText: 'OK',
                headers: { get: (h) => (h.toLowerCase() === 'content-length' ? String(route.size) : null) },
                blob: () => Promise.resolve({ size: route.blobSize ?? route.size }),
                text: () => Promise.resolve(route.body ?? ''),
            };
        };
        if (!route || !route.deferred) return Promise.resolve(respond());
        return new Promise((resolve) => pending.set(url, () => resolve(respond())));
    };

    const api = FACTORY({
        _state: state,
        _authHdr: () => ({}),
        notify: (...a) => notifications.push(a),
        renderCurrentTab: () => {},
        renderTabs: () => {},
        renderSkills: () => {},
        loadTabData: () => {},
        saveState: () => {},
        guardDirty: () => true,
        clearElapsedTimers: () => {},
        _groupOf: () => ({ key: 'files' }),
        fetch: fetchStub,
        URL: URLStub,
    });

    return {
        api, state, created, revoked, doubleRevoked, notifications,
        serve(p, opts) { routes.set(`/workspace/${p}`, opts); },
        release(p) {
            const key = `/workspace/${p}`;
            const r = pending.get(key);
            if (!r) throw new Error(`nothing pending for ${key}`);
            pending.delete(key);
            r();
        },
        outstanding: () => created.filter((u) => !revoked.includes(u)),
    };
}

function report(env, extra = {}) {
    return {
        created: env.created.length,
        revoked: env.revoked.length,
        outstanding: env.outstanding().length,
        doubleRevoked: env.doubleRevoked.length,
        notifications: env.notifications.length,
        viewMode: env.state.viewMode,
        currentPath: env.state.currentFile ? env.state.currentFile.path : null,
        currentType: env.state.currentFile ? env.state.currentFile.type : null,
        currentContent: env.state.currentFile ? env.state.currentFile.content : null,
        ownedUrl: env.api.ownedUrl(),
        ...extra,
    };
}

const MB = 1024 * 1024;
const image = (over = {}) => ({ size: 2 * MB, ...over });
const textFile = (body) => ({ size: body.length, body });

const out = {};

// 1 — the one path that already worked, kept working.
{
    const env = makeEnv();
    for (let i = 0; i < 10; i++) {
        env.serve(`shot-${i}.png`, image());
        await env.api.viewFile(`shot-${i}.png`);
        env.api._backToTree(() => {});
    }
    out.open_then_back_x10 = report(env);
}

// 2 — the leak the audit measured: ten previews retired by a tab change.
{
    const env = makeEnv();
    for (let i = 0; i < 10; i++) {
        env.serve(`shot-${i}.png`, image());
        await env.api.viewFile(`shot-${i}.png`);
        env.api._selectTab(i % 2 === 0 ? 'memory' : 'workspace');
    }
    out.open_then_tab_switch_x10 = report(env);
}

// 3 — image replaced by image, never going back to the tree.
{
    const env = makeEnv();
    for (let i = 0; i < 10; i++) {
        env.serve(`shot-${i}.png`, image());
        await env.api.viewFile(`shot-${i}.png`);
    }
    const during = report(env);
    env.api._disposePreview();
    out.image_to_image_x10 = { ...during, after_dispose: report(env) };
}

// 4 — image replaced by a text file.
{
    const env = makeEnv();
    for (let i = 0; i < 10; i++) {
        env.serve(`shot-${i}.png`, image());
        await env.api.viewFile(`shot-${i}.png`);
        env.serve(`notes-${i}.txt`, textFile(`note ${i}`));
        await env.api.viewFile(`notes-${i}.txt`);
    }
    out.image_to_text_x10 = report(env);
}

// 5 — the field scenario: fifty screenshots, tab changes throughout, and the
//     session ends on a tab change rather than on Back.
{
    const env = makeEnv();
    for (let i = 0; i < 50; i++) {
        env.serve(`run/shot-${i}.png`, image());
        await env.api.viewFile(`run/shot-${i}.png`);
        if (i % 10 === 9) env.api._selectTab(i % 20 === 9 ? 'memory' : 'workspace');
    }
    env.api._selectTab('skills');
    out.fifty_screenshots = report(env);
}

// 6 — two image loads resolving in reverse order.
{
    const env = makeEnv();
    env.serve('slow.png', image({ deferred: true }));
    env.serve('fast.png', image({ deferred: true }));
    const slow = env.api.viewFile('slow.png');
    await tick(1);
    const fast = env.api.viewFile('fast.png');
    await tick(1);
    env.release('fast.png');
    await tick();
    env.release('slow.png');
    await tick();
    await Promise.all([slow, fast]);
    out.reverse_order_images = report(env);
}

// 7 — the same race with text files, which the audit's leak framing missed.
{
    const env = makeEnv();
    env.serve('slow.txt', { ...textFile('the file the user left'), deferred: true });
    env.serve('fast.txt', { ...textFile('the file the user clicked'), deferred: true });
    const slow = env.api.viewFile('slow.txt');
    await tick(1);
    const fast = env.api.viewFile('fast.txt');
    await tick(1);
    env.release('fast.txt');
    await tick();
    env.release('slow.txt');
    await tick();
    await Promise.all([slow, fast]);
    out.reverse_order_text = report(env);
}

// 8 — panel teardown / any other disposal while an image is on screen.
{
    const env = makeEnv();
    env.serve('a.png', image());
    await env.api.viewFile('a.png');
    env.api._disposePreview();
    const first = report(env);
    env.api._disposePreview();   // idempotent: no double revoke
    out.teardown = { ...first, after_second_dispose: report(env) };
}

// 9 — deleting the previewed file, and deleting the directory above it.
{
    const env = makeEnv();
    env.serve('shots/a.png', image());
    await env.api.viewFile('shots/a.png');
    const matched = env.api._closeViewerFor('shots/a.png');
    const direct = { ...report(env), matched };

    const env2 = makeEnv();
    env2.serve('shots/b.png', image());
    await env2.api.viewFile('shots/b.png');
    const matchedParent = env2.api._closeViewerFor('shots');

    const env3 = makeEnv();
    env3.serve('shots/c.png', image());
    await env3.api.viewFile('shots/c.png');
    const matchedOther = env3.api._closeViewerFor('elsewhere/d.png');

    out.delete_open_file = {
        ...direct,
        parent_dir: { ...report(env2), matched: matchedParent },
        unrelated: { ...report(env3), matched: matchedOther },
    };
}

// 10 — the missing size guard: a 40MB image never becomes a blob at all.
{
    const env = makeEnv();
    env.serve('huge.png', image({ size: 40 * MB }));
    await env.api.viewFile('huge.png');
    const declared = report(env);

    // ...and the same when the server declares no length (chunked).
    const env2 = makeEnv();
    env2.serve('huge.svg', { size: 0, blobSize: 40 * MB });
    await env2.api.viewFile('huge.svg');

    // An ordinary screenshot is still previewed.
    const env3 = makeEnv();
    env3.serve('ok.png', image({ size: 2 * MB }));
    await env3.api.viewFile('ok.png');

    out.size_guard = { declared, chunked: report(env2), normal: report(env3) };
}

// 11 — a failed load must not break the preview already on screen.
{
    const env = makeEnv();
    env.serve('good.png', image());
    await env.api.viewFile('good.png');
    await env.api.viewFile('missing.png');
    out.failed_load = report(env);
}

// 12 — a failure that arrives after the user moved on stays silent.
{
    const env = makeEnv();
    env.serve('doomed.png', { ok: false, deferred: true, size: 0 });
    env.serve('wanted.txt', textFile('here'));
    const doomed = env.api.viewFile('doomed.png');
    await tick(1);
    await env.api.viewFile('wanted.txt');
    env.release('doomed.png');
    await tick();
    await doomed;
    out.superseded_failure = report(env);
}

// 13 — a video preview owns no URL, and replacing an image with one still
//      releases the image's blob.
{
    const env = makeEnv();
    env.serve('a.png', image());
    await env.api.viewFile('a.png');
    await env.api.viewFile('clip.mp4');
    out.video_replacing_image = report(env);
}

process.stdout.write(JSON.stringify(out, null, 2));
"""


@pytest.fixture(scope="module")
def r(tmp_path_factory):
    """Run the browser-side scenarios once; every test below reads a slice."""
    harness = tmp_path_factory.mktemp("s21") / "preview-lifecycle.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(harness), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout)


# ── the leak, one exit at a time ─────────────────────────────────────────────


def test_ten_open_and_back_cycles_leave_nothing_behind(r):
    """The path that always worked. If this ever regresses the contract has
    been rewired the wrong way round."""
    s = r["open_then_back_x10"]
    assert s["created"] == 10
    assert s["revoked"] == 10
    assert s["outstanding"] == 0


def test_switching_explorer_tabs_revokes_the_preview(r):
    """Measured at 10 created / 0 revoked before the fix: `_selectTab` nulled
    `currentFile` and the blob outlived every trace of the file it came from."""
    s = r["open_then_tab_switch_x10"]
    assert s["created"] == 10
    assert s["outstanding"] == 0, "a tab change still pins the image it stopped showing"
    assert s["viewMode"] == "tree"


def test_opening_the_next_image_frees_the_previous_one(r):
    """Ten images opened back to back: nine retired, one live — the one being
    looked at. Disposing that one leaves nothing."""
    s = r["image_to_image_x10"]
    assert s["created"] == 10
    assert s["revoked"] == 9
    assert s["outstanding"] == 1, "only the image on screen may still hold a URL"
    assert s["ownedUrl"] == s["currentContent"], "the owned URL must be the one the viewer renders"
    assert s["after_dispose"]["outstanding"] == 0


def test_opening_a_text_file_frees_the_image_it_replaced(r):
    """The replacement does not have to be another image — and a text preview
    owns no URL of its own."""
    s = r["image_to_text_x10"]
    assert s["created"] == 10
    assert s["outstanding"] == 0
    assert s["currentType"] == "text"
    assert s["ownedUrl"] is None


def test_fifty_screenshots_ending_on_a_tab_change_pin_nothing(r):
    """The measured field case: ~100 MB retained for the life of the tab."""
    s = r["fifty_screenshots"]
    assert s["created"] == 50
    assert s["outstanding"] == 0, f"{s['outstanding']} of 50 screenshots still pinned"


def test_the_deleted_file_takes_its_preview_with_it(r):
    """Delete matches the open file or any directory above it, and leaves an
    unrelated preview alone."""
    s = r["delete_open_file"]
    assert s["matched"] is True
    assert s["outstanding"] == 0
    assert s["currentPath"] is None
    assert s["parent_dir"]["matched"] is True
    assert s["parent_dir"]["outstanding"] == 0
    assert s["unrelated"]["matched"] is False
    assert s["unrelated"]["outstanding"] == 1, "deleting another file must not revoke the live preview"


def test_closing_the_panel_disposes_the_preview_and_is_idempotent(r):
    """Panel teardown, and the double-Back that used to be possible: a URL is
    revoked exactly once, and the retired file does not stay in state where a
    repaint would render a dead blob."""
    s = r["teardown"]
    assert s["outstanding"] == 0
    assert s["currentPath"] is None
    assert s["viewMode"] == "tree"
    assert s["after_second_dispose"]["doubleRevoked"] == 0


def test_a_video_preview_owns_no_url_but_still_frees_the_image(r):
    s = r["video_replacing_image"]
    assert s["currentType"] == "video"
    assert s["ownedUrl"] is None
    assert s["outstanding"] == 0


# ── the race the audit missed ────────────────────────────────────────────────


def test_two_image_loads_resolving_backwards_show_the_file_the_user_clicked(r):
    """Requested slow-then-fast, resolved fast-then-slow. Before the fix the
    viewer ended on `slow.png` — the file the user had navigated away from —
    with both blobs outstanding."""
    s = r["reverse_order_images"]
    assert s["currentPath"] == "fast.png", "the overtaken load overwrote the newer view"
    assert s["created"] == 2
    assert s["revoked"] == 1, "the obsolete response's own URL must be revoked, not installed"
    assert s["outstanding"] == 1
    assert s["ownedUrl"] == s["currentContent"], "the live image must not be the URL that got revoked"


def test_the_same_race_decides_text_files_too(r):
    """Nothing about the guard is image-specific; a slow README overwriting the
    file you actually clicked is the same bug without the leak."""
    s = r["reverse_order_text"]
    assert s["currentPath"] == "fast.txt"
    assert s["currentContent"] == "the file the user clicked"


def test_a_failed_load_leaves_the_current_preview_intact(r):
    """Disposal happens on install, not on request: a 404 must not revoke the
    image still on screen and turn it into a broken <img>."""
    s = r["failed_load"]
    assert s["currentPath"] == "good.png"
    assert s["outstanding"] == 1
    assert s["ownedUrl"] == s["currentContent"]
    assert s["notifications"] == 1, "the user should still be told the open failed"


def test_a_superseded_failure_does_not_shout_over_the_new_file(r):
    s = r["superseded_failure"]
    assert s["currentPath"] == "wanted.txt"
    assert s["notifications"] == 0


# ── the ceiling, which is a complement to the lifecycle and not a substitute ──


def test_an_oversized_image_never_becomes_a_blob(r):
    """`resp.blob()` used to buffer an image of any size, and IMAGE_EXTS
    includes .svg. Both the declared length and the chunked case stop short of
    createObjectURL, and an ordinary screenshot is unaffected."""
    s = r["size_guard"]
    assert s["declared"]["created"] == 0
    assert s["declared"]["currentType"] == "too-large"
    assert s["chunked"]["created"] == 0
    assert s["chunked"]["currentType"] == "too-large"
    assert s["normal"]["created"] == 1
    assert s["normal"]["currentType"] == "image"


# ── the invariant that has to hold across every scenario above ───────────────


def test_no_url_is_ever_revoked_twice_anywhere(r):
    """Revoking a replacement's URL instead of the disposed preview's would
    show up here as a double revoke or as an outstanding count that never
    drops."""
    offenders = _walk(r, "doubleRevoked")
    assert not offenders, f"double revocations in: {offenders}"


def test_every_scenario_that_ends_in_the_tree_ends_with_nothing_outstanding(r):
    """Belt and braces over the individual assertions: any scenario left
    showing no file must also be holding no URL."""
    leaks = []
    for name, node in _flatten(r):
        if node.get("currentPath") is None and node.get("outstanding"):
            leaks.append((name, node["outstanding"]))
    assert not leaks, f"URLs outstanding with no preview on screen: {leaks}"


def _flatten(node, prefix="root"):
    """Scenario reports nest (a scenario may report a follow-up state)."""
    if isinstance(node, dict):
        if "outstanding" in node:
            yield prefix, node
        for k, v in node.items():
            if isinstance(v, dict):
                yield from _flatten(v, f"{prefix}.{k}")


def _walk(node, key):
    return [name for name, n in _flatten(node) if n.get(key)]
