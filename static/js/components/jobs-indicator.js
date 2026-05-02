// Pernix — Jobs status bar indicator + SSE subscription

import { el, text } from '../render.js';
import { get, isOnline } from '../api.js';

let _el = null;
let _iconEl = null;
let _countEl = null;
let _pollInterval = null;
let _eventSource = null;
let _openPanel = null;  // callback to open jobs panel

let _status = { running_jobs: 0, scheduled_count: 0, snooze: { running: false } };

export function initJobsIndicator(container, { onOpenPanel }) {
    _openPanel = onOpenPanel;

    _iconEl = el('span', { class: 'jobs-indicator-icon' }, ['\u23F1']);  // stopwatch
    _countEl = el('span', { class: 'jobs-indicator-count' });

    _el = el('span', {
        class: 'jobs-indicator',
        title: 'Background jobs',
        onClick: () => { if (_openPanel) _openPanel(); },
    }, [_iconEl, _countEl]);

    container.insertBefore(_el, document.getElementById('notification-bell'));

    // Start polling
    _refresh();
    _pollInterval = setInterval(_refresh, 10000);

    // SSE for real-time events
    if (isOnline()) _connectSSE();

    window.addEventListener('pernix:offline', () => {
        if (_eventSource) { _eventSource.close(); _eventSource = null; }
    });
    window.addEventListener('pernix:online', () => {
        _refresh();
        if (!_eventSource) _connectSSE();
    });
}

async function _refresh() {
    try {
        _status = await get('/api/jobs/status');
        _render();
    } catch {
        // silent — endpoint may not exist yet during dev
    }
}

function _render() {
    const running = _status.running_jobs || 0;
    const scheduled = _status.scheduled_count || 0;
    const snoozing = _status.snooze?.running || false;

    if (scheduled === 0 && !snoozing) {
        _el.style.display = 'none';
        return;
    }
    _el.style.display = '';

    _el.classList.toggle('has-running', running > 0);
    _el.classList.toggle('has-snooze', snoozing && running === 0);

    if (running > 0) {
        _iconEl.textContent = '\u23F1';  // stopwatch
        _countEl.textContent = String(running);
        _el.title = `${running} job${running > 1 ? 's' : ''} running`;
    } else if (snoozing) {
        _iconEl.textContent = '\u25D0';  // circle half
        _countEl.textContent = 'snooze';
        _el.title = 'Snooze cycle active';
    } else {
        _iconEl.textContent = '\u23F1';
        _countEl.textContent = String(scheduled);
        _el.title = `${scheduled} job${scheduled > 1 ? 's' : ''} scheduled`;
    }
}

function _connectSSE() {
    try {
        // Auth token sent via cookie (set by api.js setAuthToken), no query params needed
        _eventSource = new EventSource('/api/jobs/events');

        for (const type of ['job.started', 'job.completed', 'job.error',
                            'snooze.start', 'snooze.done', 'snooze.activity']) {
            _eventSource.addEventListener(type, (e) => {
                try {
                    const data = JSON.parse(e.data);
                    _handleEvent(type, data);
                } catch { /* ignore parse errors */ }
            });
        }

        _eventSource.onerror = () => {
            // Browser will auto-reconnect
        };
    } catch {
        // SSE not available
    }
}

function _handleEvent(type, data) {
    if (type === 'job.started') {
        _status.running_jobs = (_status.running_jobs || 0) + 1;
    } else if (type === 'job.completed' || type === 'job.error') {
        _status.running_jobs = Math.max(0, (_status.running_jobs || 0) - 1);
    } else if (type === 'snooze.start') {
        _status.snooze = { ..._status.snooze, running: true };
    } else if (type === 'snooze.done') {
        _status.snooze = { ..._status.snooze, running: false };
    }
    _render();

    // Dispatch custom event so the jobs panel can update too
    window.dispatchEvent(new CustomEvent('pernix:job-event', { detail: { type, data } }));
}

export function destroyJobsIndicator() {
    if (_pollInterval) clearInterval(_pollInterval);
    if (_eventSource) _eventSource.close();
    if (_el && _el.parentNode) _el.parentNode.removeChild(_el);
}
