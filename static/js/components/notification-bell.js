// Pernix — Unified notification bell: questions + notifications in one panel

import { el, text } from '../render.js';
import { get, post } from '../api.js';
import { getPermission, requestPermission } from '../notifications.js';
import { announce, openOverlay } from '../a11y.js';

let _overlay = null;
let _closeOverlay = null;  // teardown from a11y.js openOverlay()
let _selectSessionFn = null;  // set by initBell — jump-to-session for item chips
let _pollTimer = null;
let _items = [];  // merged list of questions + notifications
let _hadItemsWhileOpen = false;  // for auto-close when last item is cleared

// ---------------------------------------------------------------------------
// Init + polling
// ---------------------------------------------------------------------------

export function initBell({ selectSession } = {}) {
    _selectSessionFn = selectSession || null;
    document.getElementById('notification-bell').addEventListener('click', openBellPanel);
    _poll();
    _pollTimer = setInterval(_poll, 5000);

    // Real-time updates from global notification SSE
    window.addEventListener('pernix:bell-update', _poll);
}

async function _poll() {
    try {
        const [qData, nData] = await Promise.all([
            get('/api/questions'),
            get('/api/notifications'),
        ]);

        const questions = (qData.questions || []).map(q => ({ ...q, _kind: 'question' }));
        const notifications = (nData.notifications || []).map(n => ({ ...n, _kind: 'notification' }));

        _items = [...questions, ...notifications].sort(
            (a, b) => (b.created_at || '').localeCompare(a.created_at || '')
        );

        _updateBadge(_items.length);

        if (_overlay) {
            if (_items.length > 0) _hadItemsWhileOpen = true;
            if (_hadItemsWhileOpen && _items.length === 0) {
                closeBellPanel();
                return;
            }
            _renderItems();
        }
    } catch { /* silent */ }
}

export function refreshBell() { _poll(); }

// ---------------------------------------------------------------------------
// Badge
// ---------------------------------------------------------------------------

let _lastBadgeCount = 0;
let _badgeSeen = false;   // first poll is the existing backlog, not an arrival

function _updateBadge(count) {
    const badge = document.getElementById('bell-badge');
    const bell = document.getElementById('notification-bell');
    if (!badge) return;
    badge.textContent = count;
    badge.classList.toggle('has-items', count > 0);
    if (bell) bell.classList.toggle('has-notifications', count > 0);
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
    _poll();  // refresh before opening

    const itemsContainer = el('div', { class: 'bell-items', id: 'bell-items' });
    const banner = _permissionBanner();
    const card = el('div', { class: 'modal-card bell-panel' }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text('Notifications')]),
            el('button', {
                class: 'modal-close',
                title: 'Close notifications',
                'aria-label': 'Close notifications',
                onClick: closeBellPanel,
            }, [text('\u00d7')]),
        ]),
        el('div', { class: 'modal-body' }, banner ? [banner, itemsContainer] : [itemsContainer]),
    ]);

    _overlay = el('div', { class: 'modal-overlay', onClick: (e) => {
        if (e.target === _overlay) closeBellPanel();
    }}, [card]);

    document.body.appendChild(_overlay);
    _closeOverlay = openOverlay(card, { onClose: closeBellPanel });
    _renderItems();
}

export function closeBellPanel() {
    if (_closeOverlay) { _closeOverlay(); _closeOverlay = null; }
    if (_overlay) {
        document.body.removeChild(_overlay);
        _overlay = null;
    }
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

function _renderItems() {
    const container = document.getElementById('bell-items');
    if (!container) return;

    // Save any in-progress answer text keyed by question id
    const savedInputs = {};
    container.querySelectorAll('[data-qid]').forEach(row => {
        const ta = row.querySelector('.question-answer');
        if (ta && ta.value) savedInputs[row.dataset.qid] = ta.value;
    });

    // Skip wipe if a non-button element is focused OR any textarea still has content
    const focused = document.activeElement;
    if (
        (container.contains(focused) && focused.tagName !== 'BUTTON') ||
        Object.keys(savedInputs).length > 0
    ) return;

    container.innerHTML = '';

    if (_items.length === 0) {
        container.appendChild(
            el('div', { class: 'bell-empty' }, [text('No notifications')])
        );
        return;
    }

    for (const item of _items) {
        container.appendChild(item._kind === 'question' ? _renderQuestion(item) : _renderNotification(item));
    }

    // Restore saved values after re-render
    container.querySelectorAll('[data-qid]').forEach(row => {
        const ta = row.querySelector('.question-answer');
        if (ta && savedInputs[row.dataset.qid]) ta.value = savedInputs[row.dataset.qid];
    });
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

    const row = el('div', { class: 'notif-item notif-question', 'data-qid': q.id }, [
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
                    'aria-label': 'Dismiss this question',
                    onClick: () => _dismissQuestion(q.id),
                }, [text('Dismiss')]),
                el('button', { class: 'btn btn-primary btn-sm', 'aria-label': 'Send your answer', onClick: async () => {
                    const answer = answerInput.value.trim();
                    if (!answer) { statusEl.textContent = 'Type an answer'; return; }
                    try {
                        await post(`/api/questions/${q.id}/answer`, { answer });
                        statusEl.textContent = 'Sent!';
                        setTimeout(_poll, 300);
                    } catch (e) { statusEl.textContent = `Error: ${e.message}`; }
                }}, [text('Send')]),
            ]),
        ]),
    ].filter(Boolean));
    return row;
}

function _renderNotification(n) {
    return el('div', { class: 'notif-item notif-notification' }, [
        el('div', { class: 'notif-item-header' }, [
            el('span', { class: 'notif-item-type' }, [text(n.title || 'Notification')]),
            el('span', { class: 'notif-item-meta' }, [
                _sessionChip(n.session_id),
                el('span', { class: 'notif-item-time' }, [text(_timeAgo(n.created_at))]),
            ].filter(Boolean)),
        ]),
        n.body ? el('div', { class: 'notif-item-text' }, [text(n.body)]) : null,
        el('div', { class: 'notif-item-actions' }, [
            el('div', { class: 'notif-item-buttons' }, [
                el('button', {
                    class: 'btn btn-secondary btn-sm',
                    'aria-label': `Dismiss notification: ${n.title || 'Notification'}`,
                    onClick: () => _dismissNotification(n.id),
                }, [text('Dismiss')]),
            ]),
        ]),
    ].filter(Boolean));
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function _dismissQuestion(id) {
    try { await post(`/api/questions/${id}/dismiss`); } catch {}
    _poll();
}

async function _dismissNotification(id) {
    try { await post(`/api/notifications/${id}/dismiss`); } catch {}
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
