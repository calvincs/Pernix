"""2026-11-04: after deleting three sessions in a row, the UI opened one of them.

The server log showed DELETE for three sessions and then, in the same second,
the five requests that opening a session makes — status, transcript,
feedback, context, events — all 404 for the last one deleted.

The sidebar hides a row while its undo window runs and lets go of the id the
moment the DELETE settles, then repaints from the list it already holds.
That list can be older than the DELETE (two list fetches can land out of
order), so the deleted row came back for up to one poll and a click on it
opened a session the server no longer had. A failed DELETE had the opposite
problem: app.js's deleteSession swallowed the error, so the sidebar never said
"Could not delete".

Now a settled delete is remembered for the life of the page — every render
filters it and selectSession refuses it — and a failed one rejects, so the row
stays and the error is shown.
"""

from __future__ import annotations

import re

import pytest

from tests.js_harness import STATIC_JS, requires_node, run_js

pytestmark = requires_node

SIDEBAR = (STATIC_JS / "components" / "sidebar.js").read_text()
APP = (STATIC_JS / "app.js").read_text()

SCENARIO = r"""
import { decls, fns, makeContext, mod, run, report, runScenario, wait,
         callees, stubMissing } from './sandbox.mjs';

const SB = mod('components/sidebar.js');
const CODE = [
  decls(['_deferredDeletes', '_deletedForGood'], SB),
  'let _archived = []; let _onDelete = null;',
  fns(['_deleteWithUndo', 'isDeletedSession'], SB),
].join('\n\n');

function build(stubbed) {
  const notes = [];
  const base = {
    console, setTimeout, clearTimeout, Promise, Set,
    UNDO_MS: 10,
    notify: (kind, msg) => { notes.push(kind); return () => {}; },
    _repaint: () => {},
    _reason: e => String(e && e.message),
  };
  for (const n of stubbed) if (!(n in base)) base[n] = function autoStub() {};
  const { ctx } = makeContext(base);
  run(ctx, CODE);
  stubMissing(ctx, callees(CODE));
  return { ctx, notes, peek: e => run(ctx, `(${e})`) };
}

const out = {};

// A delete that goes through stays gone, even after the undo set lets go.
await runScenario(build, async h => {
  run(h.ctx, `_onDelete = () => Promise.resolve(); _archived = [{id: 'a'}, {id: 'z'}];`);
  run(h.ctx, `_deleteWithUndo('a', 'A')`);
  out.hiddenDuringUndo = h.peek(`_deferredDeletes.has('a')`);
  out.notYetForGood = h.peek(`isDeletedSession('a')`);
  await wait(60);
  out.undoSetReleased = !h.peek(`_deferredDeletes.has('a')`);
  out.rememberedForGood = h.peek(`isDeletedSession('a')`);
  out.archiveDropped = h.peek(`_archived.map(x => x.id).join(',')`);
  out.okNotes = h.notes.slice();
});

// A delete that fails is not remembered, and says so.
await runScenario(build, async h => {
  run(h.ctx, `_onDelete = () => Promise.reject(new Error('boom'))`);
  run(h.ctx, `_deleteWithUndo('b', 'B')`);
  await wait(60);
  out.failedNotRemembered = !h.peek(`isDeletedSession('b')`);
  out.failedRowBack = !h.peek(`_deferredDeletes.has('b')`);
  out.failNotes = h.notes.slice();
});

report(out);
"""


@pytest.fixture(scope="module")
def r(tmp_path_factory):
    return run_js(SCENARIO, tmp_path_factory.mktemp("deleted"))


def test_a_settled_delete_is_remembered_after_the_undo_set_lets_go(r):
    assert r["hiddenDuringUndo"] is True
    assert r["notYetForGood"] is False, "Undo must still be able to bring it back"
    assert r["undoSetReleased"] is True
    assert r["rememberedForGood"] is True
    assert r["archiveDropped"] == "z"
    assert r["okNotes"] == ["info"]


def test_a_failed_delete_is_not_remembered_and_says_so(r):
    assert r["failedNotRemembered"] is True
    assert r["failedRowBack"] is True
    assert r["failNotes"] == ["info", "error"]


def test_every_render_filters_a_deleted_session():
    body = re.search(r"export function renderSessionList\(.*?\n}\n", SIDEBAR, re.S).group(0)
    assert "!_deletedForGood.has(s.id)" in body


def test_select_session_refuses_a_deleted_one():
    body = re.search(r"async function selectSession\(sid\) \{.*?\n}\n", APP, re.S).group(0)
    head = body[: body.index("const prevSid")]
    assert "if (isDeletedSession(sid)) return;" in head


def test_delete_session_rethrows_so_the_sidebar_can_report_it():
    body = re.search(r"async function deleteSession\(sid\) \{.*?\n}\n", APP, re.S).group(0)
    assert "throw e;" in body
