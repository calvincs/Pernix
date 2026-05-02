// Pernix — Unified notification bell: questions + notifications in one panel

import { el, text } from '../render.js';
import { get, post } from '../api.js';

let _overlay = null;
let _pollTimer = null;
let _items = [];  // merged list of questions + notifications

// ---------------------------------------------------------------------------
// Init + polling
// ---------------------------------------------------------------------------

export function initBell() {
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

        // If panel is open, re-render
        if (_overlay) _renderItems();
    } catch { /* silent */ }
}

export function refreshBell() { _poll(); }

// ---------------------------------------------------------------------------
// Badge
// ---------------------------------------------------------------------------

function _updateBadge(count) {
    const badge = document.getElementById('bell-badge');
    const bell = document.getElementById('notification-bell');
    if (!badge) return;
    badge.textContent = count;
    badge.classList.toggle('has-items', count > 0);
    if (bell) bell.classList.toggle('has-notifications', count > 0);
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export function openBellPanel() {
    if (_overlay) { closeBellPanel(); return; }  // toggle
    _poll();  // refresh before opening

    const itemsContainer = el('div', { class: 'bell-items', id: 'bell-items' });
    const card = el('div', { class: 'modal-card bell-panel' }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text('Notifications')]),
            el('button', { class: 'modal-close', onClick: closeBellPanel }, [text('\u00d7')]),
        ]),
        el('div', { class: 'modal-body' }, [itemsContainer]),
    ]);

    _overlay = el('div', { class: 'modal-overlay', onClick: (e) => {
        if (e.target === _overlay) closeBellPanel();
    }}, [card]);

    document.body.appendChild(_overlay);
    document.addEventListener('keydown', _onEsc);
    _renderItems();
}

export function closeBellPanel() {
    if (_overlay) {
        document.body.removeChild(_overlay);
        _overlay = null;
    }
    document.removeEventListener('keydown', _onEsc);
}

function _onEsc(e) { if (e.key === 'Escape') closeBellPanel(); }

// ---------------------------------------------------------------------------
// Render items
// ---------------------------------------------------------------------------

function _renderItems() {
    const container = document.getElementById('bell-items');
    if (!container) return;
    // Don't wipe the panel while the user is typing an answer
    if (container.contains(document.activeElement) && document.activeElement.tagName !== 'BUTTON') return;
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
}

function _renderQuestion(q) {
    const answerInput = el('textarea', {
        class: 'question-answer',
        placeholder: 'Type your answer...',
        rows: '2',
    });
    const statusEl = el('span', { class: 'notif-status' });

    const row = el('div', { class: 'notif-item notif-question' }, [
        el('div', { class: 'notif-item-header' }, [
            el('span', { class: 'notif-item-type' }, [text(q.session_title ? `Question from: ${q.session_title}` : 'Agent Question')]),
            el('span', { class: 'notif-item-time' }, [text(_timeAgo(q.created_at))]),
        ]),
        el('div', { class: 'notif-item-text' }, [text(q.question)]),
        q.context ? el('div', { class: 'notif-item-context' }, [text(q.context)]) : null,
        el('div', { class: 'notif-item-actions' }, [
            answerInput,
            el('div', { class: 'notif-item-buttons' }, [
                statusEl,
                el('button', { class: 'btn btn-secondary btn-sm', onClick: () => _dismissQuestion(q.id) }, [text('Dismiss')]),
                el('button', { class: 'btn btn-primary btn-sm', onClick: async () => {
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
            el('span', { class: 'notif-item-time' }, [text(_timeAgo(n.created_at))]),
        ]),
        n.body ? el('div', { class: 'notif-item-text' }, [text(n.body)]) : null,
        el('div', { class: 'notif-item-actions' }, [
            el('div', { class: 'notif-item-buttons' }, [
                el('button', { class: 'btn btn-secondary btn-sm', onClick: () => _dismissNotification(n.id) }, [text('Dismiss')]),
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
