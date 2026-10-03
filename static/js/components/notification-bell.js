// Pernix — Unified notification bell: questions + notifications in one panel
//
// Two tabs since v42. "Needs you" is what the badge counts — open questions
// and open interrupt rows — followed by the quiet bell rows that are worth a
// look but never buzz. "Activity" is the log: every row in every state,
// newest first, a day at a time, including what was dismissed or resolved.
// Dismiss is soft, so a row cleared from Needs you is still in Activity.

import { el, text } from '../render.js';
import { icon } from '../icons.js';
import { get, post } from '../api.js';
import { getPermission, requestPermission } from '../notifications.js';
import { announce, openOverlay } from '../a11y.js';
import { openFilePanel } from './file-panel.js';
import { openSettings } from './modals/settings.js';

let _overlay = null;
let _closeOverlay = null;  // teardown from a11y.js openOverlay()
let _selectSessionFn = null;  // set by initBell — jump-to-session for item chips
let _pollTimer = null;
let _items = [];  // Needs you: questions, then open interrupt rows, then open bell rows
let _hadItemsWhileOpen = false;  // for auto-close when last item is cleared
let _tab = 'needs';  // 'needs' | 'activity'
let _questionCount = 0;
let _counts = { needs_you: 0, bell: 0, unread: 0 };

// Activity (view=log) — paged with `before`, filtered by area.
const LOG_PAGE = 50;
let _log = [];
let _logDone = false;
let _logLoading = false;
let _logArea = '';
let _areas = new Set();   // every area seen this visit, so a filter keeps its siblings
let _visitNew = 0;        // "N new since your last visit" — held for the whole visit
let _newIds = new Set();  // rows that were unread when this visit saw them
let _readAllBusy = false;

// The badge only needs counts; the lists are fetched while the panel shows them.
const POLL_OPEN_MS = 5000;
const POLL_CLOSED_MS = 15000;

// link {"kind":"tab"} → the surface that owns that subsystem. Explorer leaf
// keys are file-panel.js's own; Settings is its own modal.
const TAB_TARGETS = {
    // Rows written before 3.2 link to the retired Learning tab; they open
    // the Self-tuning group instead.
    learning: { label: 'Self-tuning', open: () => openFilePanel({ tab: 'tuning' }) },
    canary: { label: 'Self-checks', open: () => openFilePanel({ tab: 'canary' }) },
    skills: { label: 'Skills', open: () => openFilePanel({ tab: 'skills' }) },
    jobs: { label: 'Jobs', open: () => openFilePanel({ tab: 'jobs' }) },
    mcp: { label: 'Servers', open: () => openFilePanel({ tab: 'mcp' }) },
    dream: { label: 'Memory', open: () => openFilePanel({ tab: 'memory' }) },
    settings: { label: 'Settings', open: () => openSettings() },
};

// ---------------------------------------------------------------------------
// Init + polling
// ---------------------------------------------------------------------------

export function initBell({ selectSession } = {}) {
    _selectSessionFn = selectSession || null;
    document.getElementById('notification-bell').addEventListener('click', openBellPanel);
    _poll();

    // Real-time updates from global notification SSE
    window.addEventListener('pernix:bell-update', _poll);
}

function _schedule() {
    clearTimeout(_pollTimer);
    _pollTimer = setTimeout(_poll, _overlay ? POLL_OPEN_MS : POLL_CLOSED_MS);
}

async function _poll() {
    clearTimeout(_pollTimer);
    const open = !!_overlay;
    const tab = _tab;
    try {
        const reqs = [get('/api/questions'), get('/api/notifications/counts')];
        if (open && tab === 'needs') reqs.push(get('/api/notifications?view=bell'));
        // Re-read as many rows as are loaded, so a poll never drops a page
        // the user paged in with Load more.
        if (open && tab === 'activity') reqs.push(get(_logUrl('', Math.min(500, Math.max(LOG_PAGE, _log.length)))));
        const [qData, counts, list] = await Promise.all(reqs);

        const questions = (qData.questions || []).map(q => ({ ...q, _kind: 'question' }));
        _questionCount = questions.length;
        _counts = _normCounts(counts);

        if (list && tab === 'needs') {
            const rows = (list.notifications || []).map(n => ({ ...n, _kind: 'notification' }));
            _items = _needsOrder(questions, rows);
        } else {
            _items = _needsOrder(questions, _items.filter(i => i._kind !== 'question'));
        }
        if (list && tab === 'activity' && _tab === 'activity') {
            _setLog(list.notifications || [], true);
        }

        _updateBadge(_questionCount, _counts);

        if (_overlay) {
            if (_tab === 'needs') {
                if (_items.length > 0) _hadItemsWhileOpen = true;
                if (_hadItemsWhileOpen && _items.length === 0) {
                    closeBellPanel();
                    return;
                }
            }
            _renderItems();
            // Something arrived while Activity is on screen: it has been seen.
            if (_tab === 'activity' && _counts.unread > 0) _readVisit();
        }
    } catch { /* silent */ } finally {
        _schedule();
    }
}

export function refreshBell() { _poll(); }

function _normCounts(c) {
    return {
        needs_you: Number(c?.needs_you) || 0,
        bell: Number(c?.bell) || 0,
        unread: Number(c?.unread) || 0,
    };
}

/**
 * Needs you order: questions, then open interrupt rows, then open bell rows,
 * each newest first. A question or an interrupt is something the agent is
 * blocked on or must say; a bell row can wait behind them.
 */
function _needsOrder(questions, rows) {
    const newest = (a, b) => (b.created_at || '').localeCompare(a.created_at || '');
    const rank = i => (i._kind === 'question' ? 0 : i.tier === 'interrupt' ? 1 : 2);
    return [...questions, ...rows].sort((a, b) => (rank(a) - rank(b)) || newest(a, b));
}

/**
 * What the bell shows: a NUMBER for things that need the user (open
 * questions + open interrupt rows), else a DOT when only quiet bell rows are
 * open, else nothing. Log rows never touch it.
 */
function _badgeState(questionCount, counts) {
    const count = (Number(questionCount) || 0) + (Number(counts?.needs_you) || 0);
    return { count, dot: count === 0 && (Number(counts?.bell) || 0) > 0 };
}

// ---------------------------------------------------------------------------
// Badge
// ---------------------------------------------------------------------------

let _lastBadgeCount = 0;
let _badgeSeen = false;   // first poll is the existing backlog, not an arrival

function _updateBadge(questionCount, counts) {
    const { count, dot } = _badgeState(questionCount, counts);
    const badge = document.getElementById('bell-badge');
    const bell = document.getElementById('notification-bell');
    if (!badge) return;
    badge.textContent = dot ? '' : count;
    badge.classList.toggle('has-items', count > 0 || dot);
    badge.classList.toggle('is-dot', dot);
    if (bell) {
        bell.classList.toggle('has-notifications', count > 0);
        // The badge is decoration to a screen reader; the button says it.
        bell.setAttribute('aria-label', count > 0
            ? `Notifications, ${count} need${count === 1 ? 's' : ''} you`
            : dot ? 'Notifications, quiet items waiting' : 'Notifications');
    }
    // A badge going 0 -> 1 is invisible to a screen reader (and to anyone not
    // looking at the corner of the status bar). Only announce arrivals; a
    // count going DOWN is the user clearing items, which needs no narration.
    if (_badgeSeen && count > _lastBadgeCount) {
        const added = count - _lastBadgeCount;
        announce(added === 1
            ? `1 new notification, ${count} waiting`
            : `${added} new notifications, ${count} waiting`);
    }
    _lastBadgeCount = count;
    _badgeSeen = true;
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export function openBellPanel() {
    if (_overlay) { closeBellPanel(); return; }  // toggle
    _hadItemsWhileOpen = false;
    _tab = 'needs';

    const itemsContainer = el('div', {
        class: 'bell-items', id: 'bell-items', role: 'tabpanel', 'aria-labelledby': 'bell-tab-needs',
    });
    const banner = _permissionBanner();
    const tabs = el('div', { class: 'tab-bar bell-tabs', role: 'tablist', 'aria-label': 'Notification views' }, [
        _tabButton('needs', 'Needs you'),
        _tabButton('activity', 'Activity'),
    ]);
    const card = el('div', { class: 'modal-card bell-panel' }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text('Notifications')]),
            el('button', {
                class: 'modal-close',
                title: 'Close notifications',
                'aria-label': 'Close notifications',
                onClick: closeBellPanel,
            }, [icon('x', { size: 14 })]),
        ]),
        tabs,
        el('div', { class: 'modal-body' }, banner ? [banner, itemsContainer] : [itemsContainer]),
    ]);

    _overlay = el('div', { class: 'modal-overlay', onClick: (e) => {
        if (e.target === _overlay) closeBellPanel();
    }}, [card]);

    document.body.appendChild(_overlay);
    _closeOverlay = openOverlay(card, { onClose: closeBellPanel });
    _syncTabs();
    _renderItems();
    _poll();  // the lists are only fetched while the panel shows them
}

export function closeBellPanel() {
    if (_closeOverlay) { _closeOverlay(); _closeOverlay = null; }
    if (_overlay) {
        document.body.removeChild(_overlay);
        _overlay = null;
    }
    _tab = 'needs';
    _schedule();  // back to the closed-panel cadence
}

function _tabButton(key, label) {
    return el('button', {
        class: 'tab-btn bell-tab',
        role: 'tab',
        id: `bell-tab-${key}`,
        'data-tab': key,
        'aria-controls': 'bell-items',
        onClick: () => _switchTab(key),
        onKeydown: (e) => {
            if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight' && e.key !== 'Home' && e.key !== 'End') return;
            e.preventDefault();
            const next = (e.key === 'Home') ? 'needs' : (e.key === 'End') ? 'activity'
                : (_tab === 'needs' ? 'activity' : 'needs');
            _switchTab(next);
            document.getElementById(`bell-tab-${next}`)?.focus();
        },
    }, [text(label), el('span', { class: 'bell-tab-count', 'aria-hidden': 'true' })]);
}

function _syncTabs() {
    if (!_overlay) return;
    for (const key of ['needs', 'activity']) {
        const btn = document.getElementById(`bell-tab-${key}`);
        if (!btn) continue;
        const on = key === _tab;
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-selected', String(on));
        btn.setAttribute('tabindex', on ? '0' : '-1');
        const n = key === 'needs' ? _items.length : _visitNew;
        const badge = btn.querySelector('.bell-tab-count');
        if (badge) badge.textContent = n > 0 ? String(n) : '';
        btn.setAttribute('aria-label', key === 'needs'
            ? (n > 0 ? `Needs you, ${n}` : 'Needs you')
            : (n > 0 ? `Activity, ${n} new` : 'Activity'));
    }
    document.getElementById('bell-items')?.setAttribute('aria-labelledby', `bell-tab-${_tab}`);
}

function _switchTab(key) {
    if (key === _tab || !_overlay) return;
    _tab = key;
    if (key === 'activity') {
        _openActivity();
    } else {
        _hadItemsWhileOpen = false;
        _syncTabs();
        _renderItems(true);
        _poll();
    }
}

async function _openActivity() {
    _log = [];
    _logDone = false;
    _logArea = '';
    _areas = new Set();
    _newIds = new Set();
    _visitNew = 0;
    _syncTabs();
    _renderItems(true);
    try {
        _logLoading = true;
        const [counts, data] = await Promise.all([
            get('/api/notifications/counts'),
            get(_logUrl('', LOG_PAGE)),
        ]);
        if (_tab !== 'activity' || !_overlay) return;
        _counts = _normCounts(counts);
        // Held for this visit: the read-all below zeroes the server's count,
        // and the header should still say what was new when the tab opened.
        _visitNew = _counts.unread;
        _setLog(data.notifications || [], true);
        _logDone = (data.notifications || []).length < LOG_PAGE;
    } catch { /* silent — the empty state says so */ } finally {
        _logLoading = false;
    }
    _syncTabs();
    _renderItems(true);
    if (_visitNew > 0) _readVisit();
}

/** Opening Activity is reading it: mark every row read, keep the count shown. */
async function _readVisit() {
    if (_readAllBusy) return;
    _readAllBusy = true;
    try {
        // Rows that land while the tab is open join this visit's "new".
        _visitNew = Math.max(_visitNew, _newIds.size);
        await post('/api/notifications/read-all');
        _counts = { ..._counts, unread: 0 };
    } catch { /* silent */ } finally {
        _readAllBusy = false;
    }
    _syncTabs();
}

function _logUrl(before, limit) {
    let url = `/api/notifications?view=log&limit=${limit}`;
    if (_logArea) url += `&area=${encodeURIComponent(_logArea)}`;
    if (before) url += `&before=${encodeURIComponent(before)}`;
    return url;
}

function _setLog(rows, replace) {
    const seen = new Set();
    const merged = [];
    for (const r of (replace ? rows : [..._log, ...rows])) {
        if (seen.has(r.id)) continue;
        seen.add(r.id);
        merged.push(r);
        if (r.area) _areas.add(r.area);
        if (!r.read_at) _newIds.add(r.id);
    }
    _log = merged;
}

async function _loadMore() {
    if (_logLoading || _logDone || !_log.length) return;
    _logLoading = true;
    _renderItems(true);
    try {
        const last = _log[_log.length - 1];
        const data = await get(_logUrl(last.created_at, LOG_PAGE));
        const rows = data.notifications || [];
        _setLog(rows, false);
        _logDone = rows.length < LOG_PAGE;
        if (rows.some(r => !r.read_at)) _readVisit();
    } catch { /* silent */ } finally {
        _logLoading = false;
    }
    _renderItems(true);
    // Load more is gone or moved; keep focus in the list instead of on body.
    const btn = document.querySelector('#bell-items [data-act="more"]');
    if (btn) btn.focus({ preventScroll: true });
}

async function _setArea(area) {
    if (area === _logArea) return;
    _logArea = area;
    _log = [];
    _logDone = false;
    _logLoading = true;
    _renderItems(true);
    try {
        const data = await get(_logUrl('', LOG_PAGE));
        _setLog(data.notifications || [], true);
        _logDone = (data.notifications || []).length < LOG_PAGE;
    } catch { /* silent */ } finally {
        _logLoading = false;
    }
    _renderItems(true);
    const chip = document.querySelector(`#bell-items [data-key="area:${area || '*'}"] [data-act="area"], #bell-items [data-key="area:${area || '*'}"][data-act="area"]`);
    if (chip) chip.focus({ preventScroll: true });
}

async function _markAllRead() {
    try { await post('/api/notifications/read-all'); } catch {}
    _visitNew = 0;
    _newIds = new Set();
    _counts = { ..._counts, unread: 0 };
    announce('All activity marked read');
    _syncTabs();
    _renderItems(true);
    document.getElementById('bell-tab-activity')?.focus({ preventScroll: true });
}

/**
 * Permission banner — browsers suppress permission prompts that aren't
 * triggered by a user gesture, so the bell panel (a gesture) is the right
 * place to offer enabling push alerts. Without this there was no UI
 * anywhere to (re-)enable notifications once the silent on-load prompt
 * was suppressed — "agent finished / agent has a question" alerts never
 * fired for most users.
 */
function _permissionBanner() {
    const perm = getPermission();
    if (perm === 'granted' || perm === 'unsupported') return null;

    // Not a UA test: on an iPad in desktop mode the UA says "Macintosh", so the
    // Home Screen instructions below — the only route to notifications on iOS —
    // never appeared on the device that needs them. touch-boot.js owns this.
    const isIOS = document.documentElement.hasAttribute('data-touch-ui');
    const isStandalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone;
    if (isIOS && !isStandalone) {
        return el('div', { class: 'bell-perm-banner' }, [
            text('To get notifications on iOS, add Pernix to your Home Screen first (Share → Add to Home Screen), then enable them here.'),
        ]);
    }
    if (perm === 'denied') {
        return el('div', { class: 'bell-perm-banner' }, [
            text('Notifications are blocked for this site. Re-enable them in your browser’s site settings to get alerts when the agent finishes or asks a question.'),
        ]);
    }
    const banner = el('div', { class: 'bell-perm-banner' });
    const btn = el('button', { class: 'btn btn-primary', onClick: async () => {
        const ok = await requestPermission();
        banner.textContent = ok
            ? 'Notifications enabled — you’ll be alerted when the agent finishes or has a question.'
            : 'Permission was not granted.';
    }}, [text('Enable notifications')]);
    banner.appendChild(text('Get alerted when the agent finishes a long task or asks a question. '));
    banner.appendChild(btn);
    return banner;
}

// ---------------------------------------------------------------------------
// Render items
// ---------------------------------------------------------------------------

// In-progress answers, by question id. Module-level so a tab switch — which
// rebuilds the list — does not throw away what the user was typing.
let _drafts = {};

function _renderItems(force = false) {
    const container = document.getElementById('bell-items');
    if (!container) return;

    // Save any in-progress answer text keyed by question id
    container.querySelectorAll('[data-qid]').forEach(row => {
        const ta = row.querySelector('.question-answer');
        if (ta) {
            if (ta.value) _drafts[row.dataset.qid] = ta.value;
            else delete _drafts[row.dataset.qid];
        }
    });
    const typing = [...container.querySelectorAll('.question-answer')].some(ta => ta.value);

    // Skip wipe if a non-button element is focused OR any textarea still has
    // content — unless the user asked for a different view (a tab, a filter).
    const focused = document.activeElement;
    if (!force && (
        (container.contains(focused) && focused.tagName !== 'BUTTON') || typing
    )) return;

    // A button IS allowed to be focused across the wipe — but the node it
    // lives on is about to be replaced, so remember which one it was. The
    // panel rebuilds every five seconds; without this, Tab lands the user
    // back at the top of the document on every poll.
    const mark = (focused && container.contains(focused))
        ? {
            key: focused.closest('[data-key]')?.getAttribute('data-key') || null,
            act: focused.getAttribute('data-act') || null,
        }
        : null;

    container.innerHTML = '';
    _syncTabs();

    if (_tab === 'activity') _renderActivity(container);
    else _renderNeeds(container);

    // Restore saved values after re-render
    container.querySelectorAll('[data-qid]').forEach(row => {
        const ta = row.querySelector('.question-answer');
        if (ta && _drafts[row.dataset.qid]) ta.value = _drafts[row.dataset.qid];
    });

    if (mark && mark.key && mark.act) {
        const host = container.querySelector(`[data-key="${mark.key}"]`);
        const btn = host && (host.matches(`[data-act="${mark.act}"]`)
            ? host : host.querySelector(`[data-act="${mark.act}"]`));
        if (btn) btn.focus({ preventScroll: true });
    }
}

function _renderNeeds(container) {
    if (_items.length === 0) {
        container.appendChild(
            el('div', { class: 'bell-empty' }, [text(
                'Nothing needs you. Questions from the agent and anything it must tell you '
                + 'land here — routine background work goes to Activity.'
            )])
        );
        return;
    }

    const notes = _items.filter(i => i._kind === 'notification');
    if (notes.length > 0) {
        const urgent = notes.filter(n => n.tier === 'interrupt').length;
        const summary = [];
        if (urgent) summary.push(`${urgent} need${urgent === 1 ? 's' : ''} you`);
        if (notes.length - urgent) summary.push(`${notes.length - urgent} to look at`);
        container.appendChild(el('div', { class: 'bell-toolbar', 'data-key': 'tool' }, [
            el('span', { class: 'bell-toolbar-text' }, [text(summary.join(' · '))]),
            el('button', {
                class: 'btn btn-secondary btn-sm',
                'data-act': 'clear',
                title: 'Dismiss every notification here (questions stay; Activity keeps them all)',
                'aria-label': 'Clear all notifications. Questions stay, and Activity keeps them.',
                onClick: _dismissAll,
            }, [text('Clear')]),
        ]));
    }

    for (const item of _items) {
        container.appendChild(item._kind === 'question' ? _renderQuestion(item) : _renderNotification(item));
    }
}

function _renderActivity(container) {
    const bar = el('div', { class: 'bell-toolbar', 'data-key': 'tool' }, [
        el('span', { class: 'bell-toolbar-text bell-new-count' + (_visitNew > 0 ? ' has-new' : '') }, [text(
            _visitNew > 0 ? `${_visitNew} new since your last visit` : 'Nothing new since your last visit'
        )]),
        el('button', {
            class: 'btn btn-secondary btn-sm',
            'data-act': 'read-all',
            'aria-label': 'Mark all activity read',
            onClick: _markAllRead,
        }, [text('Mark all read')]),
    ]);
    container.appendChild(bar);

    if (_areas.size > 1 || _logArea) {
        const chips = el('div', { class: 'bell-chips', role: 'group', 'aria-label': 'Filter activity by area' });
        for (const area of ['', ...[..._areas].sort()]) {
            const on = area === _logArea;
            chips.appendChild(el('button', {
                class: 'bell-chip' + (on ? ' active' : ''),
                'data-key': `area:${area || '*'}`,
                'data-act': 'area',
                'aria-pressed': String(on),
                onClick: () => _setArea(area),
            }, [text(area ? _areaLabel(area) : 'All')]));
        }
        container.appendChild(chips);
    }

    if (_log.length === 0) {
        container.appendChild(el('div', { class: 'bell-empty' }, [text(
            _logLoading ? 'Loading activity…'
                : _logArea ? `Nothing from ${_areaLabel(_logArea)} yet.`
                    : 'No activity yet. Everything the system tells you lands here, newest first — '
                    + 'including what you dismissed and what resolved on its own.'
        )]));
        return;
    }

    const list = el('div', { class: 'bell-log', role: 'list' });
    let day = null;
    let group = null;
    for (const row of _log) {
        const key = _dayKey(row.created_at);
        if (key !== day) {
            day = key;
            group = el('div', { class: 'notif-day-group', role: 'listitem' }, [
                el('h3', { class: 'notif-day' }, [text(_dayLabel(row.created_at))]),
            ]);
            list.appendChild(group);
        }
        group.appendChild(_renderLogRow(row));
    }
    container.appendChild(list);

    if (!_logDone) {
        const more = el('button', {
            class: 'btn btn-secondary btn-sm',
            'data-act': 'more',
            onClick: _loadMore,
        }, [text(_logLoading ? 'Loading…' : 'Load more')]);
        more.disabled = _logLoading;
        container.appendChild(el('div', { class: 'bell-more', 'data-key': 'more' }, [more]));
    }
}

function _renderLogRow(n) {
    const quiet = !!(n.dismissed_at || n.resolved_at);
    const state = n.resolved_at ? 'Resolved' : n.dismissed_at ? 'Dismissed' : '';
    const isNew = _newIds.has(n.id);
    return el('div', {
        class: 'notif-item notif-log'
            + (quiet ? ' is-quiet' : '')
            + (isNew ? ' is-new' : '')
            + (!quiet && n.tier === 'interrupt' ? ' urgent' : ''),
        'data-key': `l:${n.id}`,
    }, [
        el('div', { class: 'notif-item-header' }, [
            _titleLine(n),
            el('span', { class: 'notif-item-meta' }, [
                isNew ? el('span', { class: 'notif-new-tag' }, [text('New')]) : null,
                state ? el('span', { class: 'notif-state' }, [text(state)]) : null,
                el('span', { class: 'notif-area' }, [text(_areaLabel(n.area || 'legacy'))]),
                el('time', { class: 'notif-item-time', datetime: n.created_at || '' }, [text(_clock(n.created_at))]),
            ].filter(Boolean)),
        ]),
        n.body ? el('div', { class: 'notif-item-text' }, [text(n.body)]) : null,
        n.link ? el('div', { class: 'notif-item-actions' }, [
            el('div', { class: 'notif-item-buttons' }, [_linkButton(n)]),
        ]) : null,
    ].filter(Boolean));
}

/** Title, with "×N" when a repeat was folded into this row. */
function _titleLine(n) {
    const occ = Number(n.occurrences) || 1;
    return el('span', { class: 'notif-item-title' }, [
        el('span', { class: 'notif-item-type' }, [text(n.title || 'Notification')]),
        occ > 1 ? el('span', {
            class: 'notif-occ',
            title: `Happened ${occ} times`,
            'aria-label': `${occ} times`,
        }, [text(`×${occ}`)]) : null,
    ].filter(Boolean));
}

/** The "open" button for a row's link: a session, or the tab that owns it. */
function _linkButton(n) {
    const link = n.link;
    if (!link || typeof link !== 'object') return null;
    let label = 'Open';
    let go = null;
    if (link.kind === 'session' && link.id) {
        label = 'Open session';
        go = () => { if (_selectSessionFn) _selectSessionFn(link.id); };
    } else if (link.kind === 'tab' && link.tab) {
        const target = TAB_TARGETS[link.tab];
        label = target ? `Open ${target.label}` : 'Open';
        go = target ? target.open : () => openFilePanel({ tab: link.tab });
    }
    if (!go) return null;
    return el('button', {
        class: 'btn btn-secondary btn-sm',
        'data-act': 'link',
        'aria-label': `${label}: ${n.title || 'Notification'}`,
        onClick: () => {
            if (!n.read_at) post(`/api/notifications/${n.id}/read`).catch(() => {});
            closeBellPanel();
            go();
        },
    }, [text(label)]);
}

/**
 * Session chip for an item header: the session id as a link that closes the
 * panel and opens that session. Without it a notification says something
 * happened but not WHERE — the user had to hunt the sidebar for the source.
 */
function _sessionChip(sessionId) {
    if (!sessionId) return null;
    return el('a', {
        class: 'notif-session-link',
        href: '#',
        'data-act': 'open',
        title: 'Open this session',
        'aria-label': `Open session ${sessionId}`,
        onClick: (e) => {
            e.preventDefault();
            closeBellPanel();
            if (_selectSessionFn) _selectSessionFn(sessionId);
        },
    }, [text(sessionId)]);
}

function _renderQuestion(q) {
    const answerInput = el('textarea', {
        class: 'question-answer',
        placeholder: 'Type your answer...',
        'aria-label': 'Your answer',
        rows: '2',
    });
    const statusEl = el('span', { class: 'notif-status', role: 'status' });

    const row = el('div', {
        class: 'notif-item notif-question' + (q.urgency === 'high' ? ' urgent' : ''),
        'data-qid': q.id,
        'data-key': `q:${q.id}`,
    }, [
        el('div', { class: 'notif-item-header' }, [
            el('span', { class: 'notif-item-type' }, [text(q.session_title ? `Question from: ${q.session_title}` : 'Agent Question')]),
            el('span', { class: 'notif-item-meta' }, [
                _sessionChip(q.session_id),
                el('span', { class: 'notif-item-time' }, [text(_timeAgo(q.created_at))]),
            ].filter(Boolean)),
        ]),
        el('div', { class: 'notif-item-text' }, [text(q.question)]),
        q.context ? el('div', { class: 'notif-item-context' }, [text(q.context)]) : null,
        el('div', { class: 'notif-item-actions' }, [
            answerInput,
            el('div', { class: 'notif-item-buttons' }, [
                statusEl,
                el('button', {
                    class: 'btn btn-secondary btn-sm',
                    'data-act': 'dismiss',
                    'aria-label': 'Dismiss this question',
                    onClick: () => _dismissQuestion(q.id),
                }, [text('Dismiss')]),
                el('button', { class: 'btn btn-primary btn-sm', 'data-act': 'send', 'aria-label': 'Send your answer', onClick: async () => {
                    const answer = answerInput.value.trim();
                    if (!answer) { statusEl.textContent = 'Type an answer'; return; }
                    try {
                        await post(`/api/questions/${q.id}/answer`, { answer });
                        statusEl.textContent = 'Sent!';
                        // An answer left in the box would hold the list still
                        // (a filled textarea blocks the rebuild) forever.
                        answerInput.value = '';
                        delete _drafts[q.id];
                        setTimeout(_poll, 300);
                    } catch (e) { statusEl.textContent = `Error: ${e.message}`; }
                }}, [text('Send')]),
            ]),
        ]),
    ].filter(Boolean));
    return row;
}

function _renderNotification(n) {
    const link = _linkButton(n);
    return el('div', {
        class: 'notif-item notif-notification'
            + (n.tier === 'interrupt' ? ' notif-interrupt' : ' notif-quiet-tier')
            + (n.urgency === 'high' ? ' urgent' : ''),
        'data-key': `n:${n.id}`,
    }, [
        el('div', { class: 'notif-item-header' }, [
            _titleLine(n),
            el('span', { class: 'notif-item-meta' }, [
                _sessionChip(n.session_id),
                el('span', { class: 'notif-item-time' }, [text(_timeAgo(n.updated_at || n.created_at))]),
            ].filter(Boolean)),
        ]),
        n.body ? el('div', { class: 'notif-item-text' }, [text(n.body)]) : null,
        el('div', { class: 'notif-item-actions' }, [
            el('div', { class: 'notif-item-buttons' }, [
                link,
                el('button', {
                    class: 'btn btn-secondary btn-sm',
                    'data-act': 'dismiss',
                    'aria-label': `Dismiss notification: ${n.title || 'Notification'}`,
                    onClick: () => _dismissNotification(n.id),
                }, [text('Dismiss')]),
            ].filter(Boolean)),
        ]),
    ].filter(Boolean));
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function _dismissQuestion(id) {
    delete _drafts[id];
    try { await post(`/api/questions/${id}/dismiss`); } catch {}
    _poll();
}

// Soft: the row leaves Needs you and stays in Activity.
async function _dismissNotification(id) {
    try { await post(`/api/notifications/${id}/dismiss`); } catch {}
    _poll();
}

async function _dismissAll() {
    try { await post('/api/notifications/dismiss-all'); } catch {}
    announce('Notifications cleared — Activity still has them');
    _poll();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _timeAgo(isoStr) {
    if (!isoStr) return '';
    const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

function _dayKey(when) {
    const d = new Date(when || 0);
    return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function _dayLabel(when) {
    const d = new Date(when || 0);
    const today = new Date();
    const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
    if (_dayKey(d) === _dayKey(today)) return 'Today';
    if (_dayKey(d) === _dayKey(yesterday)) return 'Yesterday';
    return d.toLocaleDateString(undefined, {
        weekday: 'short', month: 'short', day: 'numeric',
        year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric',
    });
}

function _clock(isoStr) {
    if (!isoStr) return '';
    return new Date(isoStr).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

function _areaLabel(area) {
    const a = String(area || '');
    return a ? a.charAt(0).toUpperCase() + a.slice(1) : 'Other';
}
