// Pernix — Sidebar component: session list with grouping, dots, tooltips, legend
import { el, text, clear } from '../render.js';
import { icon } from '../icons.js';
import { isTouch, isCompact } from '../mobile.js';
import { announce } from '../a11y.js';
import { get, post, patch } from '../api.js';
import { openSpaceModal, openSpaceDeleteDialog } from './modals/spaces.js';
import { confirmDanger } from './modals/confirm.js';
import { actionSheet } from './modals/sheet.js';
import { notify } from '../feedback.js';

// ---------------------------------------------------------------------------
// Session type definitions
// ---------------------------------------------------------------------------

// The label is what a user reads; `term` is the internal word the docs, the
// settings and the agent's own logs still use. Naming the row after the
// machinery ("Cron", "RLM", "Canary") taught a first-time reader six words
// for things they have never made — so the row says what the session IS and
// keeps the internal term one hover away, in the title. (N9)
//
// `api` is the same type under the name the server knows it by — the
// session_type column's own word, which is what ?exclude_types= and
// type_counts speak. The legend's key is 'chat' because that is what the
// user made; the column has always called it 'normal'.
const SESSION_TYPES = {
    chat:   { label: 'Session',     cls: 'chat',   color: 'var(--accent)',   api: 'normal' },
    cron:   { label: 'Scheduled',   cls: 'cron',   color: 'var(--info)',     term: 'cron',   api: 'cron' },
    worker: { label: 'Worker',      cls: 'worker', color: 'var(--teal-dim)', api: 'worker' },
    snooze: { label: 'Dream',       cls: 'snooze', color: 'var(--dream)',    api: 'snooze' },
    rlm:    { label: 'Large-input', cls: 'rlm',    color: 'var(--rlm)',      term: 'RLM',    api: 'rlm' },
    canary: { label: 'Self-check',  cls: 'canary', color: 'var(--canary)',   term: 'canary', api: 'canary' },
};

// "Hide Scheduled sessions (cron)" — the internal term rides along so the
// legend stays searchable by the word the rest of the system uses.
function _legendTitle(def, isHidden) {
    const term = def.term ? ` (${def.term})` : '';
    return `${isHidden ? 'Show' : 'Hide'} ${def.label} sessions${term}`;
}

// Types that nest under their parent session instead of the top-level list.
const CHILD_TYPES = new Set(['worker', 'rlm']);

// app.js asks for /api/sessions?limit=500. A full page means the tail was
// silently cut off — and the sessions that fall off are exactly the old ones
// a user goes looking for, so the list has to admit it is not everything.
const SESSION_PAGE_LIMIT = 500;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const STORAGE_KEY = 'pernix:sidebar';
let _onSelect = null;
let _onDelete = null;
let _tooltipTimer = null;
let _lastJson = '';
let _spaces = [];  // last /api/sessions payload's spaces list

// Live activity text per session (cleared on idle)
const _activity = new Map(); // sessionId → string

// The last payload, keyed by id. openSessionSheet() takes an id — the
// session header knows which session it is showing, not which object the
// sidebar last rendered — and this is where it resolves one.
const _sessionsById = new Map();

// Sessions the user has deleted but that the server still has, because the
// undo window has not closed yet. They are filtered out of every render, so
// the row goes the instant the user confirms while the API call waits.
const _deferredDeletes = new Set();
const UNDO_MS = 5000;

// The archive: sessions that have left the list without leaving the database.
// The COUNT rides on every /api/sessions answer, so the legend can offer
// "Archived (12)" for free; the ROWS are fetched only once the user turns
// that entry on, so nobody pays for an archive they never open.
let _archivedCount = 0;
let _archived = [];
let _archivedLoading = false;
let _archivedFailed = false;
const ARCHIVED_GROUP = 'Archived';

// Sessions the user has archived but that app.js's payload may still carry,
// because the PATCH and the ten-second list poll are two different clocks.
// The optimistic mutation cannot live on the payload object itself: a poll
// replaces the whole array with fresh objects, and the row would reappear
// for as long as it took the next fetch to notice. Same idea as
// _deferredDeletes, and it clears itself the moment the server agrees.
const _archivedLocal = new Set();

// The legend's numbers. `_typeCounts` is the server's `type_counts` — every
// LIVE session of each type, whatever this page happens to hold — and
// `_payloadCounts` is the fallback: what is actually on screen.
//
// The distinction exists because the filter is server-side now. A hidden
// type's rows are not in the payload to be counted, so counting the payload
// would show "Self-check 0" the moment self-checks were switched off, and
// the legend would be naming a population it had made invisible to itself.
let _typeCounts = null;
let _payloadCounts = null;

function _reconcileArchived(sessions) {
    if (!_archivedLocal.size) return;
    const present = new Set();
    for (const s of sessions) {
        present.add(s.id);
        if (s.archived_at) _archivedLocal.delete(s.id);
    }
    for (const id of _archivedLocal) {
        if (!present.has(id)) _archivedLocal.delete(id);
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export function initSidebar(onSelect, onDelete) {
    _onSelect = onSelect;
    _onDelete = onDelete;
    _createTooltip();
    _createLegend();
    _createSearchBox();
    _initResizer();
    // The throttled visit stamps are worth nothing if the tab closes before
    // one lands.
    window.addEventListener('pagehide', _flushVisited);
}

/**
 * Told by app.js after every /api/sessions response.
 *
 * The list endpoint reports `archived_count` alongside the page, so the
 * legend can say how many sessions are filed away without a second round
 * trip — and, when the answer is zero, say nothing at all.
 *
 * @param {number} n
 */
export function setArchivedCount(n) {
    const next = Number(n) || 0;
    const changed = next !== _archivedCount;
    _archivedCount = next;
    _updateArchivedLegend();
    // The group is open and its contents just changed underneath it.
    if (changed && _showArchived()) _loadArchived();
}

/**
 * Told by app.js after every /api/sessions response — the whole payload,
 * because two things on it belong to the sidebar rather than to the list.
 *
 * @param {{archived_count?: number, type_counts?: Object}} data
 */
export function setListMeta(data) {
    setArchivedCount(data?.archived_count || 0);
    setTypeCounts(data?.type_counts || null);
}

/** The server's per-type census, or null to fall back to counting the page. */
export function setTypeCounts(counts) {
    _typeCounts = counts && typeof counts === 'object' ? counts : null;
    _updateLegendCounts();
}

// ---------------------------------------------------------------------------
// What the session list asks for
// ---------------------------------------------------------------------------
// The legend hid types in the browser for as long as it has existed, and that
// cannot help with the problem it looks like it solves: the row was already
// on the page it was being hidden from. On a machine-heavy instance the 500
// most recently updated sessions are mostly canary self-checks, workers and
// cron runs — so a third of the user's chats made page one and the rest sat
// behind "Load older sessions", with "Self-check" switched off the whole time.
//
// So the hidden set drives the REQUEST now. The server drops those types in
// SQL before the LIMIT, which is what makes the page refill with what is left
// rather than merely get shorter. The browser-side filter in
// renderSessionList stays as a second line: a payload can be unfiltered (a
// poll already in flight when the toggle was clicked), and a legacy cron
// session typed 'normal' is one the server's column-keyed clause cannot see.
//
// These two are the seam. app.js owns the fetch and the sidebar owns the
// legend, so the sidebar has to be asked what the legend currently means.

/** Session types the legend is hiding, under the names the API knows. */
export function getHiddenTypes() {
    const hidden = _loadState().hiddenTypes || {};
    return Object.entries(SESSION_TYPES)
        .filter(([key]) => hidden[key])
        .map(([, def]) => def.api);
}

/**
 * The query-string fragment /api/sessions should carry, or '' for none.
 * Leading '&', so it appends to a URL that already has its `?limit=`.
 */
export function sessionsQuery() {
    const hidden = getHiddenTypes();
    return hidden.length ? `&exclude_types=${hidden.join(',')}` : '';
}

function _showArchived() {
    return !!_loadState().showArchived;
}

async function _loadArchived() {
    if (_archivedLoading) return;
    _archivedLoading = true;
    _archivedFailed = false;
    _repaint();
    try {
        // The legend's hidden set applies to the archive too: a type the
        // user has switched off is one they do not want to read here either.
        const data = await get(`/api/sessions?archived=1&limit=${SESSION_PAGE_LIMIT}${sessionsQuery()}`);
        _archived = data.items || [];
        _archivedCount = data.archived_count ?? _archived.length;
    } catch {
        _archived = [];
        _archivedFailed = true;
    } finally {
        _archivedLoading = false;
        _updateArchivedLegend();
        _repaint();
    }
}

// ---------------------------------------------------------------------------
// The sidebar's width
//
// 270px was a constant, and a constant is a guess about somebody else's
// screen: too narrow for a user who names sessions in sentences, too wide for
// one who works in a 13" window. The edge is a control now — drag it, or
// focus it and use the arrow keys — and the number it writes is the token
// --sidebar-w on the root element, so layout.css, the 1024px media rule and
// touch.css's Explorer clamp all follow without knowing anything about it.
//
// sidebar-boot.js owns the storage key, the range and the clamp, and applies
// the stored width in <head> before the first paint. This is only the input.
// ---------------------------------------------------------------------------

const WIDTH_STEP = 16;            // one arrow key
const WIDTH_ANNOUNCE_MS = 300;    // quiet before the live region hears it
const WIDTH_RECLAMP_MS = 150;     // quiet before a window resize re-clamps

function _currentWidth() {
    const v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w'));
    return Number.isFinite(v) ? Math.round(v) : 270;
}

function _initResizer() {
    const handle = document.getElementById('sidebar-resizer');
    const sidebar = document.getElementById('sidebar');
    // Published by sidebar-boot.js. If that script did not run there is
    // nothing holding the stored width, so the handle stays inert rather
    // than becoming a second, drifting copy of the same rules.
    const gate = window.__pernixSidebarWidth;
    if (!handle || !sidebar || !gate) return;

    const clamp = (w) => Math.round(Math.min(Math.max(w, gate.MIN), gate.max()));

    let announceTimer = null;
    const syncAria = (say) => {
        const w = _currentWidth();
        handle.setAttribute('aria-valuemin', String(gate.MIN));
        handle.setAttribute('aria-valuemax', String(gate.max()));
        handle.setAttribute('aria-valuenow', String(w));
        if (!say) return;
        // A held arrow key is one decision, not thirty: the live region hears
        // where the edge came to rest, not every pixel on the way.
        clearTimeout(announceTimer);
        announceTimer = setTimeout(() => announce(`Sidebar width ${w} pixels`), WIDTH_ANNOUNCE_MS);
    };
    syncAria(false);

    const store = () => {
        try { localStorage.setItem(gate.KEY, String(_currentWidth())); } catch { /* storage blocked */ }
    };

    let dragging = false;
    handle.addEventListener('pointerdown', (e) => {
        // The handle is display:none on the other two tiers, so this only
        // ever fires under a mouse. The guard is here for the reason
        // file-panel.js has the same one: a stylesheet is not a contract.
        if (isTouch() || isCompact() || e.button !== 0) return;
        dragging = true;
        // Capture, so a pointer that outruns a 6px strip — which it will —
        // keeps sending its moves here instead of to whatever it flew over.
        handle.setPointerCapture(e.pointerId);
        handle.classList.add('dragging');
        sidebar.classList.add('resizing');
        document.body.classList.add('sidebar-resizing');
    });
    handle.addEventListener('pointermove', (e) => {
        // The sidebar starts at x=0 on this tier, so the pointer's x IS the
        // width it is asking for.
        if (dragging) gate.apply(clamp(e.clientX));
    });
    const endDrag = (e) => {
        if (!dragging) return;
        dragging = false;
        if (handle.hasPointerCapture(e.pointerId)) handle.releasePointerCapture(e.pointerId);
        handle.classList.remove('dragging');
        sidebar.classList.remove('resizing');
        document.body.classList.remove('sidebar-resizing');
        // Once, at the end. A width written on every pointermove is sixty
        // storage writes a second for one decision.
        store();
        syncAria(true);
    };
    handle.addEventListener('pointerup', endDrag);
    handle.addEventListener('pointercancel', endDrag);

    // Double-click puts the width back to whatever the stylesheets say, which
    // is not one number: 270 on a desktop, 220 under the 1024px rule.
    handle.addEventListener('dblclick', () => {
        gate.clear();
        syncAria(true);
    });

    handle.addEventListener('keydown', (e) => {
        let w = null;
        if (e.key === 'ArrowLeft') w = _currentWidth() - WIDTH_STEP;
        else if (e.key === 'ArrowRight') w = _currentWidth() + WIDTH_STEP;
        else if (e.key === 'Home') w = gate.MIN;
        else if (e.key === 'End') w = gate.max();
        else return;
        e.preventDefault();
        gate.apply(clamp(w));
        store();
        syncAria(true);
    });

    // A width chosen on a monitor is half the screen on a laptop, and the
    // 45% cap is only true of the window it was measured against.
    let reclampTimer = null;
    window.addEventListener('resize', () => {
        clearTimeout(reclampTimer);
        reclampTimer = setTimeout(() => {
            // Only re-clamp a width the user actually chose. An untouched
            // sidebar has no inline property and must keep following the
            // stylesheet, media rule included.
            if (!document.documentElement.style.getPropertyValue('--sidebar-w')) return;
            const w = _currentWidth();
            const capped = clamp(w);
            if (capped !== w) gate.apply(capped);
            syncAria(false);
        }, WIDTH_RECLAMP_MS);
    });
}

// Every selection the sidebar makes goes through here so the visit is
// recorded exactly once, at the moment the user actually opens the session.
function _select(sessionId) {
    _touchVisited(sessionId, { force: true });
    if (_onSelect) _onSelect(sessionId);
}

// ---------------------------------------------------------------------------
// Session search — FTS5 over all message content. The index has powered
// scout's cross-session lookups all along; this finally exposes it to the
// user ("find the session where we discussed X").
// ---------------------------------------------------------------------------

let _searchActive = false;
let _searchTimer = null;
let _activeSid = null;   // search rendering runs outside renderSessionList

function _createSearchBox() {
    const sidebar = document.getElementById('sidebar');
    const list = document.getElementById('session-list');
    if (!sidebar || !list || document.getElementById('session-search')) return;
    const input = el('input', {
        id: 'session-search',
        type: 'search',
        placeholder: 'Search sessions…',
        autocomplete: 'off',
    });
    sidebar.insertBefore(input, list);
    input.addEventListener('input', () => {
        clearTimeout(_searchTimer);
        const q = input.value.trim();
        if (q.length < 2) {
            if (_searchActive) {
                _searchActive = false;
                _lastJson = '';  // force the normal list to re-render
                window.dispatchEvent(new CustomEvent('pernix:sidebar-refresh'));
            }
            return;
        }
        _searchTimer = setTimeout(() => _runSearch(q), 300);
    });
}

// The endpoint caps at 20 message hits, so a full page of results means
// there are probably more behind it — say so rather than implying the list
// is everything.
const SEARCH_LIMIT = 20;

async function _runSearch(q) {
    _searchActive = true;
    const list = document.getElementById('session-list');
    if (!list) return;
    try {
        const data = await get(`/api/sessions/search?q=${encodeURIComponent(q)}`);
        if (!_searchActive) return;  // user cleared the box while we fetched
        clear(list);
        const results = data.results || [];
        if (results.length === 0) {
            list.appendChild(el('div', { class: 'search-empty' }, [
                text(`Nothing matches “${q}”. Search reads message text, not just titles — try a phrase from the conversation.`),
            ]));
            return;
        }
        const n = results.length;
        list.appendChild(el('div', { class: 'search-count', role: 'status' }, [
            text(`${n} session${n === 1 ? '' : 's'} match${n === 1 ? 'es' : ''}`
                + (n >= SEARCH_LIMIT ? ' (top 20)' : '')),
        ]));
        for (const r of results) {
            const sp = r.space_id ? spaceById(r.space_id) : null;
            const titleKids = [];
            if (sp) {
                titleKids.push(el('span', {
                    class: 'space-chip',
                    style: `--space-color: ${sp.color}`,
                    title: `Space: ${sp.label}`,
                    'aria-hidden': 'true',
                }));
            }
            titleKids.push(text(r.title || 'untitled'));
            // Search is the one surface archiving deliberately does NOT
            // remove a session from — that is the whole promise — so the row
            // has to say why this one is not in the list above it. The mark
            // goes on the meta line rather than after the title: the title
            // is a nowrap ellipsis, and a long one would clip the very word
            // that explains the row.
            const metaKids = [];
            if (r.archived) {
                metaKids.push(el('span', {
                    class: 'archived-chip',
                    title: 'Archived — open it to restore',
                }, [text('archived')]));
            }
            metaKids.push(text(`${r.matches} match${r.matches === 1 ? '' : 'es'}`
                + (r.updated_at ? ' · ' + _relativeTime(r.updated_at) : '')));
            const open = () => _select(r.session_id);
            const hit = el('div', {
                class: 'session-item search-hit' + (r.session_id === _activeSid ? ' active' : ''),
                'data-sid': r.session_id,
                role: 'button',
                tabindex: '0',
                'aria-label': `${r.title || 'untitled'}${r.archived ? ', archived' : ''}, `
                    + `${r.matches} match${r.matches === 1 ? '' : 'es'}`,
                onClick: open,
            }, [
                el('div', { class: 'session-title' }, titleKids),
                el('div', { class: 'search-snippet' }, [text(r.snippet || '')]),
                el('div', { class: 'search-meta' }, metaKids),
            ]);
            _activateOnKey(hit, open);
            list.appendChild(hit);
        }
    } catch {
        clear(list);
        list.appendChild(el('div', { class: 'search-empty' }, [text('Search failed — check the connection and try again.')]));
    }
}

// Search owns the list until it is cleared, but the sessions behind those
// rows keep moving: one of them becomes the active session, another finishes
// a turn. Repaint just those two things in place rather than freezing the
// panel until the user empties the search box.
function _refreshSearchRows(sessions, activeSid) {
    const list = document.getElementById('session-list');
    if (!list) return;
    const byId = new Map((sessions || []).map(s => [s.id, s]));
    for (const hit of list.querySelectorAll('.search-hit[data-sid]')) {
        const sid = hit.getAttribute('data-sid');
        hit.classList.toggle('active', sid === activeSid);
        const session = byId.get(sid);
        if (!session) continue;
        const meta = hit.querySelector('.search-meta');
        if (!meta) continue;
        const stale = meta.querySelector('.session-attn');
        if (stale) stale.remove();
        const attn = _attentionBadge(session);
        if (attn) meta.appendChild(attn);
    }
}

// The activity ticker is an infinite marquee. Under prefers-reduced-motion
// the global rule in tokens.css collapses its duration to .01ms, which would
// leave the text parked at translateX(-100%) — off-screen — instead of
// stopping it. So don't start it at all: .session-activity is already
// overflow:hidden with a fade mask, so the line simply truncates.
// Queried per call, not cached: the OS setting can change while the app runs.
function _reducedMotion() {
    return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;
}

export function updateSessionActivity(sessionId, activityText) {
    if (activityText) {
        _activity.set(sessionId, activityText);
    } else {
        _activity.delete(sessionId);
    }
    // Update just the activity element — no full re-render
    const actEl = document.querySelector(`.session-item[data-sid="${sessionId}"] .session-activity-text`);
    if (!actEl) return;

    actEl.classList.remove('scrolling');

    // Update text and check overflow for scrolling
    actEl.textContent = activityText || '';
    if (activityText && !_reducedMotion()) {
        requestAnimationFrame(() => {
            if (actEl.scrollWidth > actEl.parentElement.clientWidth) {
                const duration = Math.max(8, actEl.scrollWidth / 25);
                actEl.style.setProperty('--ticker-duration', duration + 's');
                actEl.classList.add('scrolling');
            }
        });
    }
}

// ---------------------------------------------------------------------------
// Focus preservation across rebuilds
//
// renderSessionList throws the whole list away and rebuilds it — on every SSE
// tick and every poll. Now that rows and headers are focusable that silently
// dropped keyboard focus back on <body>, mid-Tab, several times a minute.
// Mark what had focus by (row or group key, control class) and hand focus to
// the node that replaced it.
// ---------------------------------------------------------------------------

const FOCUS_CONTROLS = [
    'session-id-badge', 'session-pin', 'session-rename', 'session-space-move',
    'session-delete', 'sg-toggle', 'spaces-new-btn', 'space-add-btn',
    'space-gear-btn', 'space-del-btn', 'session-menu-btn', 'space-menu-btn',
];

function _focusMark(list) {
    const active = document.activeElement;
    if (!active || !list.contains(active) || !active.closest) return null;
    const control = FOCUS_CONTROLS.find(c => active.classList.contains(c)) || null;
    const host = active.closest('[data-sid]') || active.closest('[data-group]');
    if (!host) return null;
    const attr = host.hasAttribute('data-sid') ? 'data-sid' : 'data-group';
    return { key: `[${attr}="${host.getAttribute(attr)}"]`, control };
}

function _restoreFocus(list, mark) {
    if (!mark) return false;
    const host = list.querySelector(mark.key);
    if (!host) return false;                       // the row is gone; nothing to restore
    const target = (mark.control && host.querySelector('.' + mark.control)) || host;
    if (typeof target.focus !== 'function') return false;
    target.focus({ preventScroll: true });         // the scrollTop restore already ran
    return true;
}

export function renderSessionList(sessions, activeSid, spaces = []) {
    _spaces = spaces || [];
    _activeSid = activeSid;
    // Sessions can also be opened from the palette, a URL or a notification —
    // stamping the active one on every render catches all of those without
    // app.js having to know the ledger exists.
    _touchVisited(activeSid);
    if (_searchActive) { _refreshSearchRows(sessions, activeSid); return; }
    // Sessions inside their undo window are gone as far as the list is
    // concerned, and so are archived ones — leaving the list is what
    // archiving IS. The server already excludes them; the local check is
    // what makes an optimistic archive move the row on the click rather
    // than on the round trip. Filtering before the guard's fingerprint is
    // what makes both sets part of it — the payload itself has not changed.
    _reconcileArchived(sessions || []);
    sessions = (sessions || []).filter(
        s => !_deferredDeletes.has(s.id) && !s.archived_at && !_archivedLocal.has(s.id));
    _sessionsById.clear();
    for (const s of sessions) _sessionsById.set(s.id, s);
    // Archived sessions are not in the list, but the session header still
    // has to resolve one by id when the user opens it from search.
    for (const s of _archived) _sessionsById.set(s.id, s);
    // The guard must see spaces too: a label/color edit with an unchanged
    // session list would otherwise never repaint.
    const json = JSON.stringify(sessions) + '|' + JSON.stringify(spaces) + '|' + activeSid
        + '|' + (_showArchived() ? _archived.map(s => s.id).join(',') : 'off')
        + '|' + _archivedLoading + _archivedFailed;
    if (json === _lastJson) return;
    _lastJson = json;

    const list = document.getElementById('session-list');
    const scrollTop = list.scrollTop;
    const focusMark = _focusMark(list);
    clear(list);

    const sidebarState = _loadState();
    const hidden = sidebarState.hiddenTypes || {};

    // Separate top-level from child sessions (workers, RLM runs), apply type filter
    const allTopLevel = sessions.filter(s => !CHILD_TYPES.has(s.session_type));
    const allChildren = sessions.filter(s => CHILD_TYPES.has(s.session_type));

    // What this page holds, per type — the legend's fallback when no server
    // census has arrived. setTypeCounts() supersedes it the moment one does.
    const counts = { chat: 0, cron: 0, worker: 0, snooze: 0, rlm: 0, canary: 0 };
    for (const s of sessions) counts[_getTypeKey(s)]++;
    _payloadCounts = counts;
    _updateLegendCounts();

    // Hidden types are left out of the REQUEST now (see sessionsQuery), so
    // this is the second line rather than the first: it catches a payload
    // fetched before the toggle, and a legacy cron session typed 'normal',
    // which the server's column-keyed clause cannot see as a cron session.
    const topLevel = allTopLevel.filter(s => !hidden[_getTypeKey(s)]);
    const children = allChildren.filter(s => !hidden[_getTypeKey(s)]);

    const childrenByParent = {};
    for (const c of children) {
        const pid = c.parent_session_id || '_orphan';
        if (!childrenByParent[pid]) childrenByParent[pid] = [];
        childrenByParent[pid].push(c);
    }

    // Space groups render ABOVE the time buckets. Space sessions leave the
    // buckets entirely — they live in their space, never "roll off" into a
    // collapsed Older group. An empty space still renders (its + button is
    // how the first session gets in).
    const bySpace = {};
    for (const s of topLevel) {
        if (s.space_id) {
            (bySpace[s.space_id] = bySpace[s.space_id] || []).push(s);
        }
    }
    // On a fresh install this header sat above nothing at all, so the very
    // first thing a new user saw was an empty section for a feature they had
    // no sessions to use yet. It appears with the first space; the way to
    // make that first space is the row at the bottom of the list.
    if (_spaces.length) {
        list.appendChild(el('div', { class: 'spaces-header', 'data-group': 'spaces' }, [
            el('span', { class: 'spaces-header-label' }, [text('Spaces')]),
            el('button', {
                class: 'space-btn spaces-new-btn',
                title: 'New space',
                'aria-label': 'New space',
                onClick: (e) => { e.stopPropagation(); openSpaceModal(null); },
            }, [text('+')]),
        ]));
        for (const space of _spaces) {
            _renderSpaceGroup(space, bySpace[space.id] || [], list, activeSid, childrenByParent, sidebarState);
        }
    }
    // Sessions pointing at a deleted/unknown space fall back to the buckets.
    const knownSpaceIds = new Set(_spaces.map(sp => sp.id));
    const ungrouped = topLevel.filter(s => !s.space_id || !knownSpaceIds.has(s.space_id));

    // Bucket by time group — pinned sessions get their own group on top.
    const GROUP_ORDER = ['Pinned', 'Today', 'Yesterday', 'This Week', 'This Month', 'Older'];
    const DEFAULT_COLLAPSED = { Pinned: false, Today: false, Yesterday: false, 'This Week': true, 'This Month': true, Older: true };
    const buckets = {};
    for (const g of GROUP_ORDER) buckets[g] = [];
    for (const s of ungrouped) buckets[s.pinned ? 'Pinned' : _timeGroup(s.updated_at)].push(s);
    // Within each group: real conversations first, auto-created cron
    // sessions after — a busy schedule otherwise crowds chats out of view.
    for (const g of GROUP_ORDER) {
        buckets[g].sort(
            (a, b) =>
                (['cron', 'snooze', 'canary'].includes(_getTypeKey(a)) ? 1 : 0) -
                (['cron', 'snooze', 'canary'].includes(_getTypeKey(b)) ? 1 : 0)
        );
    }

    for (const label of GROUP_ORDER) {
        const group = buckets[label];
        if (!group.length) continue;

        const hasActive = group.some(s => s.id === activeSid) ||
            group.some(s => (childrenByParent[s.id] || []).some(w => w.id === activeSid ||
                (childrenByParent[w.id] || []).some(g => g.id === activeSid)));
        // User's saved choice wins over hasActive — otherwise clicking to
        // collapse the group containing the active session "un-toggles"
        // itself on the next SSE redraw because hasActive forces uncollapsed.
        // Fall back to hasActive (force-open) only when there's no saved
        // preference for this group.
        const savedCollapsed = sidebarState.collapsed?.[label];
        const collapsed = savedCollapsed !== undefined
            ? savedCollapsed
            : (hasActive ? false : DEFAULT_COLLAPSED[label]);

        const header = el('div', {
            class: 'session-group-header' + (collapsed ? ' collapsed' : ''),
            'data-group': label,
            role: 'button',
            tabindex: '0',
            'aria-expanded': String(!collapsed),
        }, [
            el('span', { class: 'sg-arrow', 'aria-hidden': 'true' }, [text('\u25BC')]),
            text(label),
            el('span', { class: 'sg-count' }, [text(String(group.length))]),
        ]);

        const body = el('div', { class: 'session-group-body' + (collapsed ? ' collapsed' : '') });

        const toggleGroup = () => {
            const isCollapsed = header.classList.toggle('collapsed');
            body.classList.toggle('collapsed', isCollapsed);
            header.setAttribute('aria-expanded', String(!isCollapsed));
            _saveCollapsed(label, isCollapsed);
        };
        header.addEventListener('click', toggleGroup);
        _activateOnKey(header, toggleGroup);

        for (const s of group) {
            _renderSessionWithWorkers(s, body, activeSid, childrenByParent, sidebarState);
        }

        list.appendChild(header);
        list.appendChild(body);
    }

    _renderListEnd(list, sessions.length);

    // Orphaned children (parent filtered out or missing)
    const renderedParents = new Set(topLevel.map(s => s.id));
    for (const [pid, ws] of Object.entries(childrenByParent)) {
        if (pid === '_orphan' || !renderedParents.has(pid)) {
            for (const w of ws) {
                _renderSessionItem(w, list, activeSid, true);
            }
        }
    }

    // Empty states. "Nothing here" is only useful with the next action
    // attached to it — and the two ways of ending up with an empty list want
    // different next actions.
    if (!list.querySelector('.session-item')) {
        list.appendChild(el('div', { class: 'sidebar-empty' }, [text(
            sessions.length
                ? 'Every session type is hidden — turn one back on in the legend below.'
                : _archivedCount
                    ? 'Nothing live here — your archived sessions are under Archived below.'
                    : 'No sessions yet — press + new or just start typing.'
        )]));
    }

    // With no spaces there is no Spaces header, so this is the only way to
    // make the first one. It stays off the true first run (no sessions):
    // there is nothing to organize yet.
    if (!_spaces.length && sessions.length) {
        list.appendChild(el('div', { class: 'spaces-create-row', 'data-group': 'spaces' }, [
            el('button', {
                class: 'spaces-create-btn',
                type: 'button',
                title: 'Group long-lived sessions into a named, colored space',
                onClick: () => openSpaceModal(null),
            }, [text('+ New space')]),
        ]));
    }

    // The archive goes last: it is a place sessions went, not a bucket they
    // are in, and it must never push the live list down the panel.
    _renderArchivedGroup(list, activeSid, sidebarState);

    list.scrollTop = scrollTop;
    _restoreFocus(list, focusMark);
}

// ---------------------------------------------------------------------------
// The Archived group (v34)
//
// One collapsed group at the foot of the list, fed by its own
// `?archived=1` fetch rather than by the payload above — the point of
// archiving is that those sessions are NOT in the payload above.
// ---------------------------------------------------------------------------

function _renderArchivedGroup(list, activeSid, sidebarState) {
    if (!_showArchived()) return;

    const collapsed = sidebarState.collapsed?.[ARCHIVED_GROUP] ?? false;
    const header = el('div', {
        class: 'session-group-header archived-group-header' + (collapsed ? ' collapsed' : ''),
        'data-group': ARCHIVED_GROUP,
        role: 'button',
        tabindex: '0',
        'aria-expanded': String(!collapsed),
    }, [
        el('span', { class: 'sg-arrow', 'aria-hidden': 'true' }, [text('\u25BC')]),
        text(ARCHIVED_GROUP),
        el('span', { class: 'sg-count' }, [text(String(_archivedCount))]),
    ]);

    const body = el('div', { class: 'session-group-body' + (collapsed ? ' collapsed' : '') });

    const toggleGroup = () => {
        const isCollapsed = header.classList.toggle('collapsed');
        body.classList.toggle('collapsed', isCollapsed);
        header.setAttribute('aria-expanded', String(!isCollapsed));
        _saveCollapsed(ARCHIVED_GROUP, isCollapsed);
    };
    header.addEventListener('click', toggleGroup);
    _activateOnKey(header, toggleGroup);

    if (_archivedFailed) {
        body.appendChild(el('div', { class: 'space-empty' }, [text('Could not load the archive — try again.')]));
    } else if (_archivedLoading && !_archived.length) {
        body.appendChild(el('div', { class: 'space-empty' }, [text('Loading…')]));
    } else if (!_archived.length) {
        body.appendChild(el('div', { class: 'space-empty' }, [text('Nothing archived yet.')]));
    }
    for (const s of _archived) {
        _renderSessionItem(s, body, activeSid, false);
    }

    list.appendChild(header);
    list.appendChild(body);
}

// ---------------------------------------------------------------------------
// Space groups (v33) — one collapsible group per space, above the buckets
// ---------------------------------------------------------------------------

async function _openSpaceSheet(space) {
    const pick = await actionSheet({
        title: space.label,
        items: [
            { id: 'settings', label: 'Space settings', icon: 'settings' },
            { id: 'archive-idle', label: 'Archive idle sessions…', icon: 'archive' },
            { id: 'delete', label: 'Delete space', icon: 'trash', danger: true },
        ],
    });
    if (pick === 'settings') openSpaceModal(space);
    else if (pick === 'archive-idle') await _archiveIdleInSpace(space);
    else if (pick === 'delete') openSpaceDeleteDialog(space);
}

/**
 * Archive everything in one space that has gone quiet.
 *
 * Two calls, deliberately: a dry run first, so the number in the dialog is
 * the number the second call then archives rather than an estimate the user
 * has to trust. The horizon comes back from the server — it is a setting,
 * not a constant the sidebar gets to invent.
 */
async function _archiveIdleInSpace(space) {
    let dry;
    try {
        dry = await post('/api/sessions/archive-idle', { space_id: space.id, dry_run: true });
    } catch (err) {
        notify('error', `Could not check “${space.label}” for idle sessions — ${_reason(err)}.`);
        return;
    }
    const n = dry.count || 0;
    const days = dry.days || 0;
    if (!n) {
        notify('info', `Nothing in “${space.label}” has been idle for more than ${days} days.`);
        return;
    }
    const titles = (dry.sample || []).slice(0, 5).map(x => `· ${x.title || 'untitled'}`);
    const ok = await confirmDanger({
        title: 'Archive idle sessions?',
        body: [
            `Archive ${n} session${n === 1 ? '' : 's'} in “${space.label}” idle for more than ${days} days?`,
            'Nothing is deleted: they leave the sidebar, keep every message, stay searchable, '
                + 'and one click brings any of them back.',
            ...titles,
            ...(n > titles.length ? [`…and ${n - titles.length} more.`] : []),
        ],
        verb: `Archive ${n}`,
        cancelLabel: 'Keep',
    });
    if (!ok) return;
    try {
        const res = await post('/api/sessions/archive-idle', { space_id: space.id, days });
        const done = res.count || 0;
        notify('success', `Archived ${done} session${done === 1 ? '' : 's'} from “${space.label}”.`);
        announce(`${done} session${done === 1 ? '' : 's'} archived`);
        _lastJson = '';
        window.dispatchEvent(new CustomEvent('pernix:sessions-changed'));
        if (_showArchived()) _loadArchived();
    } catch (err) {
        notify('error', `Could not archive idle sessions in “${space.label}” — ${_reason(err)}.`);
    }
}

function _renderSpaceGroup(space, group, list, activeSid, childrenByParent, sidebarState) {
    // Pinned first, then recency (the never-roll-off union can append stale
    // sessions out of order — sort locally instead of trusting API order).
    group.sort((a, b) =>
        (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) ||
        String(b.updated_at || '').localeCompare(String(a.updated_at || '')));

    const collapseKey = 'space:' + space.id;
    const hasActive = group.some(s => s.id === activeSid ||
        (childrenByParent[s.id] || []).some(w => w.id === activeSid ||
            (childrenByParent[w.id] || []).some(g => g.id === activeSid)));
    const savedCollapsed = sidebarState.collapsed?.[collapseKey];
    const collapsed = savedCollapsed !== undefined ? savedCollapsed : (hasActive ? false : false);

    // The disclosure control is an inner role=button, not the whole header:
    // the header also holds three real buttons, and an interactive element
    // inside a button is invalid.
    const toggle = el('div', {
        class: 'sg-toggle',
        role: 'button',
        tabindex: '0',
        'aria-expanded': String(!collapsed),
        'aria-label': `Space ${space.label}, ${group.length} session${group.length === 1 ? '' : 's'}`,
    }, [
        el('span', { class: 'sg-arrow', 'aria-hidden': 'true' }, [icon('chevron-down', { size: 9 })]),
        el('span', { class: 'space-dot', 'aria-hidden': 'true' }),
        el('span', { class: 'space-label' }, [text(space.label)]),
        el('span', { class: 'sg-count' }, [text(String(group.length))]),
    ]);

    const addBtn = el('button', {
        class: 'space-btn space-add-btn',
        title: `New session in ${space.label}`,
        'aria-label': `New session in ${space.label}`,
        onClick: async (e) => {
            e.stopPropagation();
            try {
                const r = await post('/api/sessions', { title: 'New session', space_id: space.id });
                _lastJson = '';
                window.dispatchEvent(new CustomEvent('pernix:sessions-changed'));
                if (r.session_id) _select(r.session_id);
            } catch (err) {
                notify('error', `Could not start a session in “${space.label}” — ${_reason(err)}.`);
            }
        },
    }, [text('+')]);

    // "+" is the one thing a space header is for often enough to keep as its
    // own target; settings and delete are rare and destructive, so on touch
    // they move behind the same overflow control the rows use. (P1)
    //
    // The mouse header keeps all three, and on this tier they come out of the
    // line: in flow they reserved 48px whether or not anyone was pointing at
    // the header, which left a 253px row's label 122 pixels — seventeen
    // characters of a space's name. They go in a .space-actions overlay,
    // revealed on hover or focus over the label's tail, exactly as the
    // session rows below them do it. (S2)
    const controls = isTouch()
        ? [el('button', {
            class: 'space-btn space-menu-btn',
            type: 'button',
            title: `Actions for ${space.label}`,
            'aria-label': `Actions for the space ${space.label}`,
            'aria-haspopup': 'dialog',
            onClick: (e) => { e.stopPropagation(); _openSpaceSheet(space); },
        }, [icon('more', { size: 18 })])]
        : [
            el('button', {
                class: 'space-btn space-gear-btn',
                title: `Space settings — ${space.label}`,
                'aria-label': `Space settings — ${space.label}`,
                onClick: (e) => {
                    e.stopPropagation();
                    openSpaceModal(space);
                },
            }, [icon('settings', { size: 12 })]),
            el('button', {
                class: 'space-btn space-archive-btn',
                title: `Archive idle sessions in ${space.label}`,
                'aria-label': `Archive idle sessions in the space ${space.label}`,
                onClick: (e) => {
                    e.stopPropagation();
                    _archiveIdleInSpace(space);
                },
            }, [icon('archive', { size: 12 })]),
            el('button', {
                class: 'space-btn space-del-btn',
                title: `Delete space ${space.label}`,
                'aria-label': `Delete space ${space.label}`,
                onClick: (e) => {
                    e.stopPropagation();
                    openSpaceDeleteDialog(space);
                },
            }, [icon('x', { size: 12 })]),
        ];

    // Touch keeps its two controls in the line — they are 44px and 36px and
    // meant to be seen. The mouse tier's three go in the overlay together,
    // "+" included: it was 12x11 in the line and it is a 24px target in the
    // strip, for the moment it is actually aimed at, and out of the label's
    // way the rest of the time.
    const header = el('div', {
        class: 'session-group-header space-group-header' + (collapsed ? ' collapsed' : ''),
        'data-group': 'space:' + space.id,
        style: `--space-color: ${space.color}`,
    }, isTouch()
        ? [toggle, addBtn, ...controls]
        : [toggle, el('div', { class: 'space-actions' }, [addBtn, ...controls])]);

    const body = el('div', { class: 'session-group-body' + (collapsed ? ' collapsed' : '') });

    const toggleSpace = () => {
        const isCollapsed = header.classList.toggle('collapsed');
        body.classList.toggle('collapsed', isCollapsed);
        toggle.setAttribute('aria-expanded', String(!isCollapsed));
        _saveCollapsed(collapseKey, isCollapsed);
    };
    toggle.addEventListener('click', toggleSpace);
    _activateOnKey(toggle, toggleSpace);

    if (!group.length) {
        body.appendChild(el('div', { class: 'space-empty' }, [text('No sessions yet — use + to start one')]));
    }
    for (const s of group) {
        _renderSessionWithWorkers(s, body, activeSid, childrenByParent, sidebarState);
    }

    list.appendChild(header);
    list.appendChild(body);
}

export function spaceById(id) {
    return _spaces.find(sp => sp.id === id) || null;
}

// ---------------------------------------------------------------------------
// Type detection
// ---------------------------------------------------------------------------

// The column first, the title only as a fallback. The scheduler stamps
// session_type='cron' on everything it creates, and that is the word
// ?exclude_types= speaks; keying on the title alone made the legend and the
// server disagree about a cron session the titler had renamed. The title
// clause stays for sessions written before the scheduler stamped the column.
function _getTypeKey(session) {
    if (session.session_type === 'worker') return 'worker';
    if (session.session_type === 'snooze') return 'snooze';
    if (session.session_type === 'rlm') return 'rlm';
    if (session.session_type === 'canary') return 'canary';
    if (session.session_type === 'cron') return 'cron';
    if (session.title && session.title.startsWith('Cron:')) return 'cron';
    return 'chat';
}

// ---------------------------------------------------------------------------
// Rendering helpers
// ---------------------------------------------------------------------------

// A div that carries role="button" has to implement the key half of a button
// itself. Rows and group headers cannot be real <button>s because they hold
// their own action buttons (pin, rename, delete, the space gear) and nested
// buttons are invalid HTML, so they get role=button + tabindex=0 and this.
// Keys that started inside a nested control are left alone — those own their
// own keyboard behaviour (the rename input, the meta buttons).
function _activateOnKey(elem, fn) {
    elem.addEventListener('keydown', (e) => {
        if (e.target !== elem) return;
        if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
        e.preventDefault();   // Space would scroll the list
        fn(e);
    });
}

function _renderSessionWithWorkers(session, container, activeSid, childrenByParent, sidebarState) {
    _renderSessionItem(session, container, activeSid, false);

    const children = childrenByParent[session.id];
    if (!children || children.length === 0) return;

    delete childrenByParent[session.id];

    const wasCollapsed = sidebarState.parentCollapsed?.[session.id];
    const collapsed = wasCollapsed ?? true;

    const nWorkers = children.filter(c => c.session_type === 'worker').length;
    const nGrandRlm = children.reduce(
        (acc, c) => acc + (childrenByParent[c.id] || []).length, 0);
    const nRlm = children.length - nWorkers + nGrandRlm;
    const parts = [];
    if (nWorkers) parts.push(`${nWorkers} worker${nWorkers > 1 ? 's' : ''}`);
    if (nRlm) parts.push(`${nRlm} RLM run${nRlm > 1 ? 's' : ''}`);
    const summary = el('div', {
        class: 'worker-summary' + (collapsed ? ' collapsed' : ''),
        role: 'button',
        tabindex: '0',
        'aria-expanded': String(!collapsed),
    }, [
        el('span', { class: 'ws-arrow', 'aria-hidden': 'true' }, [text('\u25BC')]),
        text(parts.join(' \u00B7 ')),
    ]);

    const group = el('div', { class: 'worker-group' + (collapsed ? ' collapsed' : '') });

    const toggleWorkers = (e) => {
        if (e && e.stopPropagation) e.stopPropagation();
        const isCollapsed = summary.classList.toggle('collapsed');
        group.classList.toggle('collapsed', isCollapsed);
        summary.setAttribute('aria-expanded', String(!isCollapsed));
        _saveParentCollapsed(session.id, isCollapsed);
    };
    summary.addEventListener('click', toggleWorkers);
    _activateOnKey(summary, toggleWorkers);

    container.appendChild(summary);
    for (const w of children) {
        _renderSessionItem(w, group, activeSid, true);
        // Grandchildren: RLM runs owned by a worker nest under that worker
        // (one extra indent level) instead of falling to the orphan list.
        const grand = childrenByParent[w.id];
        if (grand && grand.length) {
            delete childrenByParent[w.id];
            for (const g of grand) {
                _renderSessionItem(g, group, activeSid, true, 2);
            }
        }
    }
    container.appendChild(group);
}

// Needs-attention badge: "?" = blocked waiting for your input, "✓" = a
// background turn finished since you last looked. Shared by the session rows
// and the search hits so a search does not hide the one signal that says a
// session wants you.
function _attentionOf(session) {
    if (session._attention === 'input') return 'input';
    // app.js's in-memory "finished just now" set still wins; the persisted
    // ledger is what survives a reload.
    if (session._attention === 'done') return 'done';
    return _finishedWhileAway(session) ? 'done' : null;
}

function _attentionBadge(session) {
    const kind = _attentionOf(session);
    if (kind === 'input') {
        return el('span', {
            class: 'session-attn attn-input',
            title: 'Waiting for your input',
        }, [text('?')]);
    }
    if (kind === 'done') {
        return el('span', {
            class: 'session-attn attn-done',
            title: 'Finished while you were away',
        }, [text('✓')]);
    }
    return null;
}

// The row's title, cleaned of the prefixes the dot already says. Split out
// of _renderSessionItem so the action sheet names itself exactly the way the
// row does — a sheet headed "Cron: nightly sweep" over a row that reads
// "nightly sweep" looks like it belongs to something else.
function _displayTitle(session, typeKey = _getTypeKey(session)) {
    let titleText = session.title || 'New session';
    // Strip thinking model garbage from title display
    if (/^(Thinking Process|<think|Thought Process)/i.test(titleText)) {
        titleText = 'New session';
    }
    // Strip "Cron: " prefix — the dot indicates type
    if (titleText.startsWith('Cron: ') && typeKey === 'cron') {
        titleText = titleText.slice(6);
    }
    // Strip "RLM: " prefix — the dot carries the type; the rest is the task.
    if (typeKey === 'rlm' && titleText.startsWith('RLM: ')) {
        titleText = titleText.slice(5);
    }
    // Journal titles read as "Dream Jul 31" — the shorthand carries the
    // meaning, the dot carries the color.
    if (typeKey === 'snooze' && titleText.startsWith('Dream journal — ')) {
        const d = new Date(titleText.slice(16) + 'T00:00:00');
        titleText = isNaN(d)
            ? titleText
            : `Dream ${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`;
    }
    return titleText;
}

// ---------------------------------------------------------------------------
// What a session row can do — ONE definition
//
// The desktop's four hover buttons and the touch overflow sheet are two ways
// of reaching the same five actions. Both call these, so there is no second
// copy of "pin optimistically, then put the pin back if the PATCH failed" to
// drift out of step with the first. (P1)
// ---------------------------------------------------------------------------

function _sessionActions(session, titleText) {
    return {
        async togglePin() {
            const was = session.pinned;
            const next = !session.pinned;
            // Optimistic: a pin that waits on a round trip before the row
            // moves reads as a click that did not register.
            session.pinned = next ? 1 : 0;
            _repaint();
            try {
                await patch(`/api/sessions/${session.id}`, { pinned: next });
            } catch (err) {
                session.pinned = was;
                _repaint();
                notify('error', `Could not ${next ? 'pin' : 'unpin'} “${titleText}” — ${_reason(err)}.`);
            }
        },
        archive() { return _setArchived(session, true, titleText); },
        restore() { return _setArchived(session, false, titleText); },
        rename() { _startRename(session); },
        move(anchor) { _openMoveMenu(session, anchor); },
        copyId(btn) { return _copySessionId(btn, session.id); },
        async remove() {
            const n = session.message_count;
            const what = n == null ? '' : ` and its ${n} message${n === 1 ? '' : 's'}`;
            const ok = await confirmDanger({
                title: 'Delete this session?',
                body: [
                    `“${titleText}”${what} will be removed.`,
                    'You get five seconds to undo. After that it cannot be undone.',
                ],
                verb: 'Delete',
                cancelLabel: 'Keep',
            });
            if (ok) _deleteWithUndo(session.id, titleText);
        },
    };
}

/**
 * Archive or restore one session.
 *
 * Optimistic like the pin, and for the same reason: waiting on a round trip
 * to watch your own decision land is what made delete the only affordance
 * anyone trusted. The row leaves on the click; the PATCH follows; a failure
 * puts it straight back and says why.
 *
 * @param {object} session  the row's payload (mutated in place)
 * @param {boolean} archived  true to archive, false to restore
 * @param {string} titleText  the display title, for the failure message
 */
async function _setArchived(session, archived, titleText) {
    // The row this was clicked on is about to leave the list under the
    // pointer, and an element removed while hovered never fires mouseleave —
    // its tooltip would sit there over the transcript with nothing under it.
    _hideTooltip();
    const was = session.archived_at || null;
    const wasCount = _archivedCount;
    session.archived_at = archived ? new Date().toISOString() : null;
    if (archived) {
        _archivedLocal.add(session.id);
        _archived = [session, ..._archived.filter(x => x.id !== session.id)];
        _archivedCount += 1;
    } else {
        _archivedLocal.delete(session.id);
        _archived = _archived.filter(x => x.id !== session.id);
        _archivedCount = Math.max(0, _archivedCount - 1);
    }
    _updateArchivedLegend();
    _repaint();
    try {
        await patch(`/api/sessions/${session.id}`, { archived });
        announce(`${titleText} ${archived ? 'archived' : 'restored'}`);
        // The row has to move between two lists that come from two different
        // fetches; ask app.js for the live one rather than guessing at it.
        window.dispatchEvent(new CustomEvent('pernix:sessions-changed'));
        if (_showArchived()) _loadArchived();
    } catch (err) {
        session.archived_at = was;
        _archivedCount = wasCount;
        if (archived) _archivedLocal.delete(session.id);
        _archived = archived
            ? _archived.filter(x => x.id !== session.id)
            : [session, ..._archived.filter(x => x.id !== session.id)];
        _updateArchivedLegend();
        _repaint();
        notify('error', `Could not ${archived ? 'archive' : 'restore'} “${titleText}” — ${_reason(err)}.`);
    }
}

// The move dropdown is a hover-anchored list of 22px rows: fine under a
// mouse, unusable under a thumb. On touch the same choice is a second sheet
// — same spaces, same PATCH, same failure message, just a list you can hit.
// A leading space cannot collide with a real space id.
const NO_SPACE = ' none';

async function _moveSheet(session, titleText) {
    const items = _spaces.map(sp => ({
        id: sp.id,
        label: sp.label,
        icon: 'folder',
        disabled: (session.space_id || null) === sp.id,
        hint: (session.space_id || null) === sp.id ? 'current' : undefined,
    }));
    if (session.space_id) {
        items.push({ id: NO_SPACE, label: 'Remove from space', icon: 'x' });
    }
    const pick = await actionSheet({ title: `Move “${titleText}” to…`, items });
    if (pick == null) return;
    await _moveSessionToSpace(session, pick === NO_SPACE ? null : pick);
}

/**
 * Open a session's overflow sheet by id. Exported so the session header's
 * title can offer the same actions the row does, rather than growing its own
 * second set of controls.
 *
 * @param {string} sessionId
 * @returns {Promise<void>} resolves once the chosen action has been started.
 */
export function openSessionSheet(sessionId) {
    const session = _sessionsById.get(sessionId);
    if (!session) return Promise.resolve();
    return _openSessionSheet(session);
}

async function _openSessionSheet(session, { anchor = null } = {}) {
    const typeKey = _getTypeKey(session);
    const titleText = _displayTitle(session, typeKey);
    // Workers and RLM runs belong to their parent: they have no pin, no
    // rename and no space of their own on the desktop row either.
    const isChild = CHILD_TYPES.has(session.session_type);
    const act = _sessionActions(session, titleText);

    const archived = !!session.archived_at;
    const items = [];
    if (!isChild) {
        items.push({ id: 'pin', label: session.pinned ? 'Unpin' : 'Pin to top', icon: 'pin' });
        items.push({ id: 'rename', label: 'Rename', icon: 'edit' });
        if (_spaces.length) items.push({ id: 'move', label: 'Move to space…', icon: 'move' });
        // Above Delete on purpose: it is the answer to the question Delete
        // was being asked, and the safe one.
        items.push(archived
            ? { id: 'restore', label: 'Restore', icon: 'unarchive' }
            : { id: 'archive', label: 'Archive', icon: 'archive' });
    }
    items.push({ id: 'copy', label: 'Copy session id', icon: 'copy' });
    items.push({ id: 'delete', label: 'Delete', icon: 'trash', danger: true });

    switch (await actionSheet({ title: titleText, items })) {
        case 'pin': return act.togglePin();
        case 'rename': return act.rename();
        case 'move': return _moveSheet(session, titleText);
        case 'archive': return act.archive();
        case 'restore': return act.restore();
        case 'copy': return act.copyId(anchor);
        case 'delete': return act.remove();
        default: return undefined;   // cancel, Escape, backdrop
    }
}

function _renderSessionItem(session, container, activeSid, isWorker, depth = 1) {
    const typeKey = _getTypeKey(session);
    const typeDef = SESSION_TYPES[typeKey];
    const titleText = _displayTitle(session, typeKey);
    const act = _sessionActions(session, titleText);

    const meta = [];

    // Time
    if (session.updated_at) {
        meta.push(el('span', { class: 'session-time' }, [text(_relativeTime(session.updated_at))]));
    }

    // On a finger device the four hover-revealed controls become ONE 44px
    // overflow button. Four 24px targets in a 280px drawer left the title —
    // the only thing that tells two rows apart — about seventy pixels, and
    // three of the four were invisible until a hover that never comes. (P1)
    //
    // The mouse row went the other way and paid for it. The same four
    // controls plus the id badge kept their 24px targets IN the line, so a
    // 270px sidebar spent 130 of its pixels on five buttons that are
    // invisible until you point at them and left the title seven characters.
    // They are an overlay now: out of flow, revealed over the title's tail
    // on hover or focus, so the title gets the width all of the time and the
    // targets stay 24px for the moment they are actually aimed at. (S1)
    let menuBtn = null;
    const actions = [];
    if (isTouch()) {
        menuBtn = el('button', {
            class: 'session-menu-btn',
            type: 'button',
            title: 'Actions',
            'aria-label': `Actions for ${titleText}`,
            'aria-haspopup': 'dialog',
            onClick: (e) => {
                e.stopPropagation();
                _openSessionSheet(session, { anchor: e.currentTarget });
            },
        }, [icon('more', { size: 18 })]);
    } else {
        // Session ID badge — hover shows full id, click copies to clipboard
        actions.push(el('button', {
            class: 'session-id-badge',
            title: session.id,
            'aria-label': `Copy session id ${session.id}`,
            onClick: (e) => {
                e.stopPropagation();
                act.copyId(e.currentTarget);
            },
        }, [text('#')]));

        // Pin toggle — pinned sessions live in their own group at the top.
        if (!isWorker) {
            actions.push(el('button', {
                class: `session-pin${session.pinned ? ' pinned' : ''}`,
                title: session.pinned ? 'Unpin session' : 'Pin session to top',
                'aria-label': session.pinned ? `Unpin ${titleText}` : `Pin ${titleText} to top`,
                'aria-pressed': String(!!session.pinned),
                onClick: (e) => { e.stopPropagation(); act.togglePin(); },
            }, [icon(session.pinned ? 'pin-filled' : 'pin', { size: 12 })]));

            // The pin toggle used to be the pinned state as well: `.pinned`
            // held it at opacity 1 while its four neighbours waited for a
            // hover. Inside the overlay it cannot do that job any more, so a
            // pinned row keeps an 11px mark in the line — the state, in flow
            // and always readable, for fifteen pixels — and the button in the
            // overlay stays what changes it.
            if (session.pinned) {
                meta.push(el('span', {
                    class: 'session-pinned-mark',
                    'aria-hidden': 'true',
                    title: 'Pinned',
                }, [icon('pin-filled', { size: 11 })]));
            }

            // Rename — swaps the title for an inline editor.
            actions.push(el('button', {
                class: 'session-rename',
                title: 'Rename session',
                'aria-label': `Rename ${titleText}`,
                onClick: (e) => { e.stopPropagation(); act.rename(); },
            }, [icon('edit', { size: 12 })]));

            // Move to space — dropdown of spaces (+ "No space"). Only rendered
            // when at least one space exists; membership changes never bump
            // recency (set_session_meta contract).
            if (_spaces.length) {
                actions.push(el('button', {
                    class: 'session-space-move',
                    title: session.space_id ? 'Move to another space' : 'Move to space',
                    'aria-label': session.space_id
                        ? `Move ${titleText} to another space`
                        : `Move ${titleText} to a space`,
                    onClick: (e) => { e.stopPropagation(); act.move(e.currentTarget); },
                }, [icon('move', { size: 12 })]));
            }

            // Archive / Restore. It joins the overlay rather than the
            // line, so the sixth control costs the title nothing: the strip
            // is absolutely positioned and the row does not move.
            const isArchived = !!session.archived_at;
            actions.push(el('button', {
                class: 'session-archive',
                title: isArchived ? 'Restore session' : 'Archive session — keeps every message',
                'aria-label': isArchived ? `Restore ${titleText}` : `Archive ${titleText}`,
                onClick: (e) => {
                    e.stopPropagation();
                    if (isArchived) act.restore();
                    else act.archive();
                },
            }, [icon(isArchived ? 'unarchive' : 'archive', { size: 12 })]));
        }

        // Delete button. It asks first, naming what it is about to delete,
        // and then leaves five seconds to take it back.
        actions.push(el('button', {
            class: 'session-delete',
            title: 'Delete session',
            'aria-label': `Delete ${titleText}`,
            onClick: (e) => { e.stopPropagation(); act.remove(); },
        }, [text('×')]));
    }

    const classes = ['session-item'];
    // The row is in the Archived group rather than the list. It reads a shade
    // quieter there, and the class is what tells the two apart for anything
    // that walks the list.
    if (session.archived_at) classes.push('archived-row');
    if (session.id === activeSid) classes.push('active');
    if (isWorker) classes.push('worker');
    if (depth > 1) classes.push('depth-2');
    if (typeKey === 'cron') classes.push('cron-session');
    if (typeKey === 'snooze') classes.push('snooze-session');
    if (typeKey === 'rlm') classes.push('rlm-session');

    // Title with colored dot prefix
    const titleChildren = [];
    if (isWorker) {
        titleChildren.push(el('span', { class: 'worker-prefix' }, [text('\u21B3')]));
    }
    const isActive = _isBusy(session);
    const dotCls = `session-dot ${typeDef.cls}${isActive ? ' active-pulse' : ''}`;
    titleChildren.push(el('span', { class: dotCls, 'aria-hidden': 'true' }));
    titleChildren.push(el('span', { class: 'session-title-text' }, [text(titleText)]));

    const attn = _attentionBadge(session);
    if (attn) titleChildren.push(attn);

    // Activity ticker line: live activity when processing, subtitle/preview when idle
    let liveActivity = _activity.get(session.id);
    // Clear stale live activity for sessions that have returned to idle
    // (e.g. cron sessions whose turn.complete wasn't received because SSE was on another session)
    if (liveActivity && !isActive) {
        _activity.delete(session.id);
        liveActivity = null;
    }
    const idlePreview = session.subtitle || _cleanPreview(session.first_message);
    const activityStr = liveActivity || idlePreview;
    const actTextEl = el('span', { class: 'session-activity-text' }, activityStr ? [text(activityStr)] : []);
    const actLine = el('div', { class: 'session-activity' }, [actTextEl]);

    // The row is a control, not decoration: Tab reaches it, Enter/Space
    // opens it. role=button rather than a real <button> because the row
    // contains the pin/rename/move/delete buttons. The explicit name keeps
    // the dot, the ticker text and those buttons out of what gets read.
    const nameParts = [titleText];
    if (typeKey !== 'chat') nameParts.push(typeDef.label);
    const attentionKind = _attentionOf(session);
    if (attentionKind === 'input') nameParts.push('waiting for your input');
    else if (attentionKind === 'done') nameParts.push('finished while you were away');

    const select = () => _select(session.id);
    const item = el('div', {
        class: classes.join(' '),
        'data-sid': session.id,
        role: 'button',
        tabindex: '0',
        'aria-label': nameParts.join(', '),
        onClick: select,
    }, [
        el('div', { class: 'session-top' }, [
            el('span', { class: 'session-title' }, titleChildren),
            el('span', { class: 'session-meta' }, meta),
        ]),
        actLine,
    ]);

    // Outside .session-top on purpose: a 44px square inside the title's flex
    // row would eat the title's width on the very line that needs it, and a
    // two-line row wants the control centred against both lines rather than
    // baseline-aligned with the first. It is positioned against the row.
    if (menuBtn) item.appendChild(menuBtn);
    // Same reasoning, one tier over: the mouse row's five controls sit
    // against the row rather than in the line, so they cost the title
    // nothing until they are shown.
    if (actions.length) item.appendChild(el('div', { class: 'session-actions' }, actions));

    _activateOnKey(item, select);

    // Tooltip events. A hover tooltip needs a hover, and touch.css hides
    // #session-tooltip outright — so this is a pointer question, not a width
    // one: a wide tablet must not attach them either.
    if (!isTouch()) {
        item.addEventListener('mouseenter', (e) => _showTooltip(e, session, typeDef));
        item.addEventListener('mouseleave', () => _hideTooltip());
        item.addEventListener('mousemove', (e) => _positionTooltip(e));
    }

    container.appendChild(item);

    // Scroll only live activity text (not idle previews which look like stuck status)
    if (liveActivity && !_reducedMotion()) {
        requestAnimationFrame(() => {
            if (actTextEl.scrollWidth > actTextEl.parentElement.clientWidth) {
                const duration = Math.max(8, actTextEl.scrollWidth / 25);
                actTextEl.style.setProperty('--ticker-duration', duration + 's');
                actTextEl.classList.add('scrolling');
            }
        });
    }
}

// ---------------------------------------------------------------------------
// Delete with an undo window
//
// The row goes the moment the user confirms — waiting on a round trip to see
// your own decision land is what makes a delete feel unsafe — but the API
// call does not. It fires when the undo toast goes away, so Undo is a real
// reprieve rather than a delete followed by a recreate that would lose the
// conversation anyway. _onDelete is app.js's deleteSession; the callback and
// its contract are untouched, only its timing.
// ---------------------------------------------------------------------------

function _deleteWithUndo(sessionId, titleText) {
    _deferredDeletes.add(sessionId);
    _repaint();

    let undone = false;
    const restore = () => {
        undone = true;
        clearTimeout(timer);
        _deferredDeletes.delete(sessionId);
        _repaint();
    };
    const dismiss = notify('info', `Deleted “${titleText}”`, {
        action: restore,
        actionLabel: 'Undo',
        ttl: UNDO_MS,
    });

    const timer = setTimeout(() => {
        if (undone) return;
        // Take the toast away with it: a toast whose timer the user paused by
        // hovering must not keep offering an Undo that no longer exists.
        dismiss();
        Promise.resolve(_onDelete ? _onDelete(sessionId) : null)
            .catch((err) => {
                notify('error', `Could not delete “${titleText}” — ${_reason(err)}.`);
            })
            .finally(() => {
                // Whatever happened, stop hiding it: either the server no
                // longer has it, or the delete failed and it is still there.
                _deferredDeletes.delete(sessionId);
                _repaint();
            });
    }, UNDO_MS);
}

function _repaint() {
    _lastJson = '';
    window.dispatchEvent(new CustomEvent('pernix:sidebar-refresh'));
}

// Every one of these used to be `catch { /* leave as-is */ }`: the pin did
// not move, the rename did not stick, the session did not change space, and
// nothing anywhere said why. api.js gives us either an OfflineError or the
// server's own detail string.
function _reason(err) {
    if (err && err.offline) return 'you are offline';
    const msg = err && (err.message || err.detail);
    return msg ? String(msg) : 'the server did not say why';
}

// ---------------------------------------------------------------------------
// Move-to-space dropdown — one floating menu at a time; click-away closes.
// ---------------------------------------------------------------------------

// The move itself, shared by the dropdown and the touch sheet.
async function _moveSessionToSpace(session, spaceId) {
    if ((spaceId || null) === (session.space_id || null)) return;
    try {
        await patch(`/api/sessions/${session.id}`, { space_id: spaceId });
        _lastJson = '';
        window.dispatchEvent(new CustomEvent('pernix:sessions-changed'));
    } catch (err) {
        const to = spaceId ? (spaceById(spaceId)?.label || 'that space') : 'no space';
        notify('error', `Could not move “${session.title || 'this session'}” to ${to} — ${_reason(err)}.`);
    }
}

function _openMoveMenu(session, anchor) {
    document.querySelector('.space-move-menu')?.remove();
    const menu = el('div', { class: 'space-move-menu' });
    const choose = async (spaceId) => {
        menu.remove();
        await _moveSessionToSpace(session, spaceId);
    };
    for (const sp of _spaces) {
        menu.appendChild(el('div', {
            class: 'space-move-item' + (session.space_id === sp.id ? ' current' : ''),
            onClick: (e) => { e.stopPropagation(); choose(sp.id); },
        }, [
            el('span', { class: 'space-dot', style: `--space-color: ${sp.color}` }),
            text(sp.label),
        ]));
    }
    if (session.space_id) {
        menu.appendChild(el('div', {
            class: 'space-move-item',
            onClick: (e) => { e.stopPropagation(); choose(null); },
        }, [text('Remove from space')]));
    }
    const r = anchor.getBoundingClientRect();
    menu.style.top = `${r.bottom + 4}px`;
    menu.style.left = `${Math.max(8, r.right - 180)}px`;
    document.body.appendChild(menu);
    setTimeout(() => {
        const away = (ev) => {
            if (!menu.contains(ev.target)) {
                menu.remove();
                document.removeEventListener('click', away);
            }
        };
        document.addEventListener('click', away);
    }, 0);
}

// ---------------------------------------------------------------------------
// Inline rename — replaces the title span with an input; Enter/blur saves,
// Esc cancels. Rename does not bump recency (server contract).
// ---------------------------------------------------------------------------

function _startRename(session) {
    const item = document.querySelector(`.session-item[data-sid="${session.id}"]`);
    const titleSpan = item?.querySelector('.session-title-text');
    if (!titleSpan || item.querySelector('.session-rename-input')) return;

    const input = el('input', {
        class: 'session-rename-input',
        type: 'text',
        value: session.title || '',
    });
    input.addEventListener('click', (e) => e.stopPropagation());

    let finished = false;
    const finish = async (save) => {
        if (finished) return;
        finished = true;
        item.classList.remove('renaming');
        const newTitle = input.value.trim();
        if (save && newTitle && newTitle !== session.title) {
            const was = session.title;
            // Optimistic, then put the old name back if the save failed —
            // silently keeping the old title looked identical to the rename
            // never having been typed.
            session.title = newTitle;
            _repaint();
            try {
                const res = await patch(`/api/sessions/${session.id}`, { title: newTitle });
                session.title = res.title || newTitle;
            } catch (err) {
                session.title = was;
                notify('error', `Could not rename to “${newTitle}” — ${_reason(err)}.`);
            }
        }
        _repaint();
    };

    input.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter') { e.preventDefault(); finish(true); }
        else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));

    // Focusing the input puts :focus-within on the row, which is one of the
    // two things that shows the action overlay — and the overlay lies over
    // the tail of the very field being typed into. While a rename is open
    // the row says so and the overlay stands down.
    item.classList.add('renaming');
    titleSpan.replaceWith(input);
    input.focus();
    input.select();
}

function _cleanPreview(msg) {
    if (!msg) return '';
    let s = msg.trim();
    // Truncate to a concise preview of the user's request
    if (s.length > 60) s = s.slice(0, 57) + '\u2026';
    return s;
}

// ---------------------------------------------------------------------------
// Legend footer
// ---------------------------------------------------------------------------

function _createLegend() {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar || document.getElementById('sidebar-legend')) return;

    const sidebarState = _loadState();
    const hidden = sidebarState.hiddenTypes || {};

    const legend = el('div', { id: 'sidebar-legend' });

    for (const [key, def] of Object.entries(SESSION_TYPES)) {
        const isHidden = !!hidden[key];
        const item = el('button', {
            class: `legend-item ${def.cls}${isHidden ? ' dimmed' : ''}`,
            title: _legendTitle(def, isHidden),
            'data-type': key,
            onClick: () => _toggleType(key),
        }, [
            el('span', { class: `legend-dot ${def.cls}` }),
            el('span', { class: 'legend-label' }, [text(def.label)]),
            el('span', { class: 'legend-count', 'data-count-type': key }, [text('0')]),
        ]);
        legend.appendChild(item);
    }

    // Not a session type — a place sessions go — so it comes after the key
    // and toggles a group rather than filtering one. Hidden until there is
    // something in the archive to show.
    const archivedItem = el('button', {
        class: 'legend-item archived',
        id: 'legend-archived',
        type: 'button',
        'aria-pressed': 'false',
        onClick: () => _toggleArchived(),
    }, [
        el('span', { class: 'legend-dot archived' }),
        el('span', { class: 'legend-label' }, [text('Archived')]),
        el('span', { class: 'legend-count', id: 'legend-archived-count' }, [text('0')]),
    ]);
    archivedItem.hidden = true;
    legend.appendChild(archivedItem);

    sidebar.appendChild(legend);
    _updateArchivedLegend();
    // A user who left the group open last time expects it open now.
    if (_showArchived()) _loadArchived();
}

function _updateArchivedLegend() {
    const item = document.getElementById('legend-archived');
    if (!item) return;
    const on = _showArchived();
    item.hidden = _archivedCount === 0 && !on;
    item.classList.toggle('dimmed', !on);
    item.setAttribute('aria-pressed', String(on));
    item.title = on
        ? 'Hide the archived sessions'
        : `Show ${_archivedCount} archived session${_archivedCount === 1 ? '' : 's'}`;
    const count = document.getElementById('legend-archived-count');
    if (count) count.textContent = String(_archivedCount);
    // The legend hides itself when it is a key to an empty list; an archive
    // with something in it is not an empty list.
    const legend = document.getElementById('sidebar-legend');
    if (legend && !item.hidden) legend.hidden = false;
}

function _toggleArchived() {
    const state = _loadState();
    state.showArchived = !state.showArchived;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    _updateArchivedLegend();
    if (state.showArchived) {
        _loadArchived();
        announce('Showing archived sessions');
    } else {
        _archived = [];
        _archivedFailed = false;
        announce('Archived sessions hidden');
        _repaint();
    }
}

function _toggleType(typeKey) {
    const state = _loadState();
    if (!state.hiddenTypes) state.hiddenTypes = {};
    state.hiddenTypes[typeKey] = !state.hiddenTypes[typeKey];
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));

    // Update legend UI
    const btn = document.querySelector(`.legend-item[data-type="${typeKey}"]`);
    if (btn) {
        btn.classList.toggle('dimmed', !!state.hiddenTypes[typeKey]);
        btn.title = _legendTitle(SESSION_TYPES[typeKey], !!state.hiddenTypes[typeKey]);
    }

    // Force re-render
    _lastJson = '';

    // Three events, because a toggle now changes three things.
    //
    // The repaint is what the sidebar owes the rows already on screen. The
    // announcement is what it owes a screen reader. And the REFETCH is the
    // new one: the excluded type is left out in SQL, so the sessions that
    // were sitting behind it do not exist on this client until the list is
    // asked again. Without it, switching a type off would empty its rows and
    // leave the page that much shorter instead of refilling it.
    //
    // pernix:sessions-query carries the new exclusion for anything that
    // builds the request without importing this module.
    document.dispatchEvent(new CustomEvent('sidebar:filter-changed'));
    window.dispatchEvent(new CustomEvent('pernix:sessions-query', {
        detail: { excludeTypes: getHiddenTypes(), query: sessionsQuery() },
    }));
    window.dispatchEvent(new CustomEvent('pernix:sessions-changed'));
    if (_showArchived()) _loadArchived();
    announce(state.hiddenTypes[typeKey]
        ? `${SESSION_TYPES[typeKey].label} sessions hidden`
        : `${SESSION_TYPES[typeKey].label} sessions shown`);
}

/**
 * The number beside each dot: the server's census when there is one, and
 * otherwise what the page in hand holds.
 *
 * The server's numbers win because they answer the question the legend is
 * actually asking — how many self-checks are there — rather than how many of
 * them survived this page's LIMIT and this session's filter.
 */
function _legendCounts() {
    if (!_typeCounts) return _payloadCounts || {};
    const out = {};
    for (const [key, def] of Object.entries(SESSION_TYPES)) out[key] = _typeCounts[def.api] || 0;
    return out;
}

// A legend is a key to what is on screen. Listing all six types on a fresh
// install taught a new user six words for things they have never seen, and
// four of them are machinery they never create by hand. Only types that are
// actually present get a row — 'chat' always stays, because it is the one
// the user makes themselves and the filter has to remain reachable.
//
// So does a type the user has switched off, whatever it counts. Once the
// filter is server-side its rows are not in the payload at all, and hiding
// the entry at zero would take away the only control that turns it back on.
function _updateLegendCounts() {
    const counts = _legendCounts();
    const hidden = _loadState().hiddenTypes || {};
    let total = 0;
    for (const key of Object.keys(SESSION_TYPES)) {
        const n = counts[key] || 0;
        total += n;
        const span = document.querySelector(`.legend-count[data-count-type="${key}"]`);
        if (span) span.textContent = String(n);
        const item = document.querySelector(`.legend-item[data-type="${key}"]`);
        if (item) item.hidden = n === 0 && key !== 'chat' && !hidden[key];
    }
    // Nothing at all: the legend is a key to an empty list.
    const legend = document.getElementById('sidebar-legend');
    if (legend) legend.hidden = total === 0;
}

// ---------------------------------------------------------------------------
// Copy session id
// ---------------------------------------------------------------------------

// `btn` is the badge to flash — null when the copy came from the action
// sheet, which has already closed by the time the clipboard write lands. The
// toast is the confirmation there.
async function _copySessionId(btn, sessionId) {
    try {
        await navigator.clipboard.writeText(sessionId);
    } catch {
        // Fallback for non-secure contexts
        const ta = document.createElement('textarea');
        ta.value = sessionId;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch {}
        document.body.removeChild(ta);
    }
    if (!btn || !btn.isConnected) {
        notify('info', 'Session id copied.');
        return;
    }
    btn.classList.add('copied');
    setTimeout(() => btn.classList.remove('copied'), 900);
}

// ---------------------------------------------------------------------------
// Tooltip
// ---------------------------------------------------------------------------

function _createTooltip() {
    if (document.getElementById('session-tooltip')) return;
    const tt = el('div', { id: 'session-tooltip' });
    document.body.appendChild(tt);
}

function _showTooltip(event, session, typeDef) {
    clearTimeout(_tooltipTimer);
    _tooltipTimer = setTimeout(() => {
        const tt = document.getElementById('session-tooltip');
        if (!tt) return;

        clear(tt);

        // Type + Title
        const titleRow = el('div', { class: 'tt-title' }, [
            el('span', { class: `session-dot ${typeDef.cls}` }),
            text(session.title || 'New session'),
        ]);
        tt.appendChild(titleRow);
        tt.appendChild(el('div', { class: 'tt-divider' }));

        // First message preview
        if (session.first_message) {
            const preview = session.first_message.length > 120
                ? session.first_message.slice(0, 120) + '...'
                : session.first_message;
            tt.appendChild(el('div', { class: 'tt-preview' }, [text(preview)]));
        }

        // Stats
        const stats = [];
        if (session.message_count != null) {
            stats.push(el('span', {}, [text(`${session.message_count} msgs`)]));
        }
        if (session.total_tokens) {
            stats.push(el('span', {}, [text(`${_formatTokens(session.total_tokens)} tokens`)]));
        }
        // Money builds trust for an agent that burns tokens autonomously —
        // cron jobs run while the user sleeps; they should see the bill.
        if (session.total_cost && session.total_cost >= 0.005) {
            stats.push(el('span', {}, [text(`$${session.total_cost.toFixed(2)}`)]));
        }
        if (stats.length) {
            tt.appendChild(el('div', { class: 'tt-stats' }, stats));
        }

        // Timestamps
        const times = [];
        if (session.created_at) times.push(`Created ${_formatDate(session.created_at)}`);
        if (session.updated_at) times.push(`Updated ${_relativeTime(session.updated_at)}`);
        if (times.length) {
            tt.appendChild(el('div', { class: 'tt-stats' }, [
                el('span', {}, [text(times.join(' \u00b7 '))]),
            ]));
        }

        // State
        if (session.state && session.state !== 'idle') {
            tt.appendChild(el('div', { class: 'tt-stats' }, [
                el('span', {}, [text(`State: ${session.state}`)]),
            ]));
        }

        _positionTooltip(event);
        tt.classList.add('visible');
    }, 350);
}

function _hideTooltip() {
    clearTimeout(_tooltipTimer);
    const tt = document.getElementById('session-tooltip');
    if (tt) tt.classList.remove('visible');
}

function _positionTooltip(event) {
    const tt = document.getElementById('session-tooltip');
    if (!tt || !tt.classList.contains('visible')) return;

    const sidebar = document.getElementById('sidebar');
    const sidebarRect = sidebar.getBoundingClientRect();
    const ttRect = tt.getBoundingClientRect();

    let left = sidebarRect.right + 8;
    let top = event.clientY - 20;

    if (left + ttRect.width > window.innerWidth) {
        left = sidebarRect.left - ttRect.width - 8;
    }
    if (top + ttRect.height > window.innerHeight) {
        top = window.innerHeight - ttRect.height - 8;
    }
    if (top < 8) top = 8;

    tt.style.left = left + 'px';
    tt.style.top = top + 'px';
}

// ---------------------------------------------------------------------------
// Time helpers
// ---------------------------------------------------------------------------

function _timeGroup(isoStr) {
    if (!isoStr) return 'Older';
    const now = new Date();
    const d = new Date(isoStr.replace(/\+00:00$/, 'Z'));
    if (isNaN(d.getTime())) return 'Older';
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const dStart = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const diffDays = Math.floor((todayStart - dStart) / 86400000);
    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    if (diffDays <= 7) return 'This Week';
    if (diffDays <= 30) return 'This Month';
    return 'Older';
}

function _relativeTime(isoStr) {
    if (!isoStr) return '';
    let s = isoStr.replace(/\+00:00$/, 'Z');
    if (!/[Z+-]\d{2}/.test(s)) s += 'Z';
    const date = new Date(s);
    if (isNaN(date.getTime())) return '';
    const diffSec = Math.floor((Date.now() - date.getTime()) / 1000);
    if (diffSec < 60) return 'now';
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`;
    if (diffSec < 604800) return `${Math.floor(diffSec / 86400)}d`;
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function _formatDate(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr.replace(/\+00:00$/, 'Z'));
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function _formatTokens(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
    return String(n);
}

// ---------------------------------------------------------------------------
// Visit ledger — when you last had each session open
//
// The "finished while you were away" tick used to live in an app.js Set that
// only knew about turns that completed with the tab open. Reload the page and
// every tick vanished, which is the one moment you most want them: you were
// away. Persisting the last-visited stamp lets the tick be derived instead —
// updated since you last looked, and not running now.
// ---------------------------------------------------------------------------

const VISITED_MAX = 600;         // stamps kept; oldest dropped past this
const VISITED_FLUSH_MS = 15000;  // renders are frequent; writes need not be

let _visited = null;             // sid → epoch ms, lazily loaded
let _visitedDirty = false;
let _visitedFlushAt = 0;

function _visitedMap() {
    if (!_visited) _visited = _loadState().lastVisited || {};
    return _visited;
}

function _touchVisited(sid, { force = false } = {}) {
    if (!sid) return;
    _visitedMap()[sid] = Date.now();
    _visitedDirty = true;
    if (force || Date.now() - _visitedFlushAt > VISITED_FLUSH_MS) _flushVisited();
}

function _flushVisited() {
    if (!_visitedDirty) return;
    const seen = _visitedMap();
    const ids = Object.keys(seen);
    if (ids.length > VISITED_MAX) {
        ids.sort((a, b) => seen[a] - seen[b])
            .slice(0, ids.length - VISITED_MAX)
            .forEach(id => delete seen[id]);
    }
    const state = _loadState();
    state.lastVisited = seen;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    _visitedDirty = false;
    _visitedFlushAt = Date.now();
}

function _epoch(isoStr) {
    if (!isoStr) return 0;
    let s = isoStr.replace(/\+00:00$/, 'Z');
    if (!/[Z+-]\d{2}/.test(s)) s += 'Z';
    const t = new Date(s).getTime();
    return isNaN(t) ? 0 : t;
}

// A session is doing something right now. state_v2 is the real state machine;
// the legacy `state` column stopped updating when v2 landed (it reads 'idle'
// even mid-turn), so RLM view sessions are the only readers left of it.
function _isBusy(session) {
    const sv = session.state_v2 || session.state;
    return !!sv && !['idle', 'idle_ready', 'awaiting_user', 'error'].includes(sv);
}

function _finishedWhileAway(session) {
    if (!session.updated_at) return false;
    if (session.id === _activeSid) return false;   // you are looking at it
    if (_isBusy(session)) return false;            // still going: not finished
    const seen = _visitedMap()[session.id];
    if (!seen) return false;                       // never opened: never away from
    return _epoch(session.updated_at) > seen;
}

// ---------------------------------------------------------------------------
// localStorage persistence
// ---------------------------------------------------------------------------

function _loadState() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); }
    catch { return {}; }
}

function _saveCollapsed(label, isCollapsed) {
    const state = _loadState();
    if (!state.collapsed) state.collapsed = {};
    state.collapsed[label] = isCollapsed;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function _saveParentCollapsed(orchId, isCollapsed) {
    const state = _loadState();
    if (!state.parentCollapsed) state.parentCollapsed = {};
    state.parentCollapsed[orchId] = isCollapsed;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

// ---------------------------------------------------------------------------
// List end — the horizon, and the way past it
// ---------------------------------------------------------------------------
// The footer used to be a dead end: "Showing the 500 most recent · search to
// find older sessions." Search is no help when what you remember is the
// session rather than a phrase inside it, so everything past the horizon was
// effectively gone. app.js now knows the true total (the list endpoint reports
// it) and can fetch the next page on demand; this is the control that asks.

let _paging = { total: 0, loaded: 0, hasMore: false, loading: false };

/** Told by app.js after every /api/sessions response. */
export function setSessionPaging({ total = 0, loaded = 0, hasMore = false } = {}) {
    _paging = { ..._paging, total, loaded, hasMore };
}

/** Called by app.js while a page is in flight, so the button can say so. */
export function setSessionPagingBusy(busy) {
    _paging.loading = !!busy;
    const btn = document.getElementById('sidebar-load-older');
    if (btn) {
        btn.disabled = !!busy;
        btn.textContent = busy ? 'Loading…' : _loadOlderLabel();
    }
}

function _loadOlderLabel() {
    const remaining = Math.max(0, _paging.total - _paging.loaded);
    return remaining ? `Load older sessions (${remaining} more)` : 'Load older sessions';
}

function _renderListEnd(list, shownCount) {
    // Nothing behind the horizon and nothing was cut: say nothing.
    if (!_paging.hasMore && shownCount < SESSION_PAGE_LIMIT) return;
    if (!_paging.hasMore) {
        list.appendChild(el('div', { class: 'sidebar-truncated' }, [
            text(`All ${_paging.total || shownCount} sessions loaded.`),
        ]));
        return;
    }
    const btn = el('button', {
        id: 'sidebar-load-older',
        class: 'btn btn--ghost btn--sm sidebar-load-older',
        type: 'button',
    }, [text(_paging.loading ? 'Loading…' : _loadOlderLabel())]);
    btn.disabled = _paging.loading;
    btn.addEventListener('click', () => {
        window.dispatchEvent(new CustomEvent('pernix:load-older-sessions'));
    });
    list.appendChild(btn);
}
