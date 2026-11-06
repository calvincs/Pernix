"""2026-11-06 sidebar pass.

- Worker rows showed under a collapsed "This Month". They were not in that
  group at all: archiving a session stamped only its own row, so its workers
  stayed live with no parent in the list, and the sidebar drew such orphans
  loose after the last group, where nothing could fold them. 62 workers and
  23 RLM runs on the live box were in that state.
- Folds were kept in localStorage forever, and Pinned, Yesterday and every
  space defaulted open. Every open now starts with Today open and the rest
  folded.
- A fresh load opened no session even when one was running.
"""

from __future__ import annotations

import re

from db import models as db
from tests.js_harness import STATIC_JS, requires_node, run_js

SIDEBAR = (STATIC_JS / "components" / "sidebar.js").read_text()
APP = (STATIC_JS / "app.js").read_text()


def _fn(src: str, head: str) -> str:
    m = re.search(re.escape(head) + r".*?\n}\n", src, re.S)
    assert m, head
    return m.group(0)


def _archived(sid: str) -> bool:
    return bool(db.get_session(sid).get("archived_at"))


# --- archiving takes children along ----------------------------------------


def test_archiving_a_session_archives_its_workers_and_their_rlm_runs():
    parent = db.create_session("chat")
    worker = db.create_session("w", session_type="worker", parent_session_id=parent)
    rlm = db.create_session("r", session_type="rlm", parent_session_id=worker)
    other = db.create_session("other chat")

    db.set_session_meta(parent, archived=True)
    assert _archived(parent) and _archived(worker) and _archived(rlm)
    assert not _archived(other)
    stamp = db.get_session(parent)["archived_at"]
    assert db.get_session(worker)["archived_at"] == stamp

    db.set_session_meta(parent, archived=False)
    assert not _archived(parent) and not _archived(worker) and not _archived(rlm)


def test_archiving_does_not_reach_a_non_child_session_with_a_parent_link():
    parent = db.create_session("chat")
    linked = db.create_session("linked chat", parent_session_id=parent)
    db.set_session_meta(parent, archived=True)
    assert not _archived(linked)


def test_repair_archives_children_left_behind_by_the_old_archive():
    parent = db.create_session("chat")
    worker = db.create_session("w", session_type="worker", parent_session_id=parent)
    rlm = db.create_session("r", session_type="rlm", parent_session_id=worker)
    live_parent = db.create_session("live")
    live_worker = db.create_session("lw", session_type="worker", parent_session_id=live_parent)
    # The pre-fix shape: only the parent row carries the stamp.
    with db.connect_sessions() as conn:
        conn.execute("UPDATE sessions SET archived_at = '2026-09-01T00:00:00+00:00' WHERE id = ?", (parent,))

    assert db.archive_stranded_children() == 2
    assert _archived(worker) and _archived(rlm)
    assert not _archived(live_worker)
    assert db.archive_stranded_children() == 0, "the repair is idempotent"


# --- the sidebar ------------------------------------------------------------


def test_orphans_are_bucketed_rows_not_loose_rows_after_the_last_group():
    body = _fn(SIDEBAR, "export function renderSessionList(")
    assert "_orphan" not in body and "renderedParents" not in body
    assert ".concat(visibleChildren.filter(isStray))" in body


def test_only_today_starts_open():
    m = re.search(r"const DEFAULT_COLLAPSED = \{(.*?)\};", SIDEBAR, re.S)
    entries = dict(re.findall(r"'?([A-Z][A-Za-z ]*?)'?: (true|false)", m.group(1)))
    assert {k for k, v in entries.items() if v == "false"} == {"Today"}
    assert "sidebarState.collapsed?.[ARCHIVED_GROUP] ?? true" in SIDEBAR
    space = _fn(SIDEBAR, "function _renderSpaceGroup(")
    assert "savedCollapsed !== undefined ? savedCollapsed : !hasActive" in space


@requires_node
def test_folds_live_in_memory_and_never_reach_storage(tmp_path):
    scenario = r"""
import { fns, decls, makeContext, run, report, mod } from './sandbox.mjs';
const store = { 'pernix:sidebar': JSON.stringify({ hiddenTypes: { cron: true },
                 collapsed: { 'This Week': false }, parentCollapsed: { a: false } }) };
const localStorage = { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); } };
const { ctx } = makeContext({ localStorage });
const F = mod('components/sidebar.js');
run(ctx, decls(['STORAGE_KEY'], F));
run(ctx, `var _VIEW_KEYS = ['collapsed', 'parentCollapsed']; var _view = { collapsed: {}, parentCollapsed: {} };`);
run(ctx, fns(['_loadState', '_writeState', '_saveCollapsed', '_saveParentCollapsed'], F));
const first = run(ctx, '_loadState()');
run(ctx, `_saveCollapsed('Older', false); _saveParentCollapsed('p', false);
          const s = _loadState(); s.showArchived = true; _writeState(s);`);
report({
  oldFoldIgnored: first.collapsed['This Week'] === undefined,
  hiddenKept: first.hiddenTypes.cron === true,
  foldInMemory: run(ctx, '_loadState().collapsed.Older') === false,
  stored: JSON.parse(store['pernix:sidebar']),
});
"""
    r = run_js(scenario, tmp_path)
    assert r["oldFoldIgnored"] and r["hiddenKept"] and r["foldInMemory"]
    assert r["stored"] == {"hiddenTypes": {"cron": True}, "showArchived": True}


# --- landing on the running session -----------------------------------------


@requires_node
def test_a_fresh_load_lands_on_the_newest_running_conversation(tmp_path):
    scenario = r"""
import { fns, decls, makeContext, run, report, mod } from './sandbox.mjs';
const { ctx } = makeContext({});
const F = mod('components/sidebar.js');
run(ctx, decls(['CHILD_TYPES', 'BACKGROUND_TYPES'], F));
run(ctx, fns(['_getTypeKey', 'pickLandingSession'], F));
const busy = s => s.state_v2 === 'processing';
const S = (id, o) => Object.assign({ id, session_type: 'normal', state_v2: 'idle', updated_at: '2026-11-06T10:00' }, o);
const pick = list => run(ctx, 'pickLandingSession')(list, busy);
report({
  none: pick([S('a'), S('b')]),
  newest: pick([S('a', { state_v2: 'processing', updated_at: '2026-11-06T09:00' }),
                S('b', { state_v2: 'processing', updated_at: '2026-11-06T11:00' })]),
  workerToParent: pick([S('p'), S('w', { session_type: 'worker', parent_session_id: 'p', state_v2: 'processing' })]),
  rlmToTop: pick([S('p'), S('w', { session_type: 'worker', parent_session_id: 'p' }),
                  S('r', { session_type: 'rlm', parent_session_id: 'w', state_v2: 'processing' })]),
  skipsBackground: pick([S('c', { session_type: 'cron', state_v2: 'processing', updated_at: '2026-11-06T12:00' }),
                         S('k', { session_type: 'canary', state_v2: 'processing' }),
                         S('t', { title: 'Cron: nightly', state_v2: 'processing' }),
                         S('a', { state_v2: 'processing', updated_at: '2026-11-06T08:00' })]),
  cronWorker: pick([S('c', { session_type: 'cron' }),
                    S('w', { session_type: 'worker', parent_session_id: 'c', state_v2: 'processing' })]),
});
"""
    r = run_js(scenario, tmp_path)
    r.pop("_stdout", None)
    assert r == {
        "none": None,
        "newest": "b",
        "workerToParent": "p",
        "rlmToTop": "p",
        "skipsBackground": "a",
        "cronWorker": None,
    }


def test_boot_opens_the_landing_session_only_without_a_notification_target():
    i = APP.index("const _notifSession = new URLSearchParams")
    block = APP[i : i + 900]
    assert "} else if (!state.sid) {" in block
    assert "pickLandingSession(state.sessions, s => _BUSY_STATES.has(s.state_v2))" in block
    # The list is loaded before the pick reads it.
    assert APP.index("await loadSessions();") < i
