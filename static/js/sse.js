// Pernix — SSE client with health monitoring and reconnection

import { isOnline } from './api.js';

// Auth token is sent via cookie (set by api.js setAuthToken), no query params needed

let _source = null;
let _onEvent = null;
let _sessionId = null;
let _lastEventTime = 0;
let _healthTimer = null;
// Tracked here (separate from app.js's _lastSeq) so the watchdog can pass it
// on reconnect via query param — EventSource cannot set request headers, so
// `Last-Event-ID` is unreachable for JS-driven reconnects.
let _lastSeq = 0;

// Single source of truth for every event type the server may emit on the
// per-session SSE stream. EventSource only dispatches to listeners registered
// by exact `event:` name, so an event missing from this list is silently
// dropped on the client — _lastSeq never advances for it, which then causes
// gap-detection in app.js to fire a spurious soft reload whenever the next
// subscribed event arrives. Keep this list synced with the emitters in
// core/, sessions/, api/ — verify with:
//   grep -rEho '"type":\s*"[a-z][a-z_.]+"' core/ sessions/ api/ | sort -u
const EVENT_TYPES = [
    // Stream lifecycle
    'stream.token', 'stream.done', 'stream.error',
    'stream.fallback', 'stream.retry', 'stream.length_continuation',
    'stream.budget_exhausted',
    // Tools / context / scout
    'tool.start', 'tool.call',
    'context.compacting', 'context.compacted', 'context.reset',
    'scout.start', 'scout.step', 'scout.done',
    // Session lifecycle
    'session.queued', 'session.title', 'session.cancelled',
    'session.state_changed', 'session.prompt_rejected',
    'session.waiting_llm', 'session.message_combined',
    'session.queue_dropped', 'session.queue_full',
    'session.queue_removed',
    // Injected mid-turn messages
    'message.injected',
    // Workers
    'worker.started', 'worker.done', 'worker.failed',
    // Partial save (mid-stream persistence)
    'partial.saved',
    // Ask-user dialogs (questions + notifications + replies)
    'dialog.question', 'user_question',
    'dialog.answered', 'dialog.dismissed', 'dialog.notification',
    // Browse (visible web-tool activity)
    'browse.start', 'browse.done',
    // Reflect (post-turn verification)
    'reflect.start', 'reflect.done', 'reflect.skipped',
    'reflect.retry', 'reflect.exhausted', 'reflect.escalate',
    'reflect.budget_exhausted',
    // Eval (autonomous evaluation pass)
    'eval.start', 'eval.pass', 'eval.done', 'eval.retry', 'eval.exhausted',
    // Snooze (idle-time consolidation)
    'snooze.start', 'snooze.activity', 'snooze.done',
    // Model switches (mid-turn override + scout-routed pill)
    'model.divider', 'model.override',
    // Workflows (orchestration extension)
    'workflow.started', 'workflow.completed', 'workflow.cancelled',
    'workflow.wave_started',
    'workflow.step_started', 'workflow.step_completed',
    'workflow.step_retry', 'workflow.step_skipped',
    // Turn boundary (safety-net for button reset)
    'turn.complete',
];

window.addEventListener('pernix:offline', () => {
    if (_source) { _source.close(); _source = null; }
    if (_healthTimer) { clearInterval(_healthTimer); _healthTimer = null; }
    _connectionState = 'disconnected';
    _updateHealthIndicator('disconnected');
});
window.addEventListener('pernix:online', () => {
    if (_sessionId && _onEvent && !_source) {
        connectSSE(_sessionId, _onEvent);
    }
});

// Connection states: 'connected' | 'reconnecting' | 'disconnected'
let _connectionState = 'disconnected';

const HEALTH_CHECK_INTERVAL = 15000;  // Check every 15s
const STALE_THRESHOLD = 45000;        // Consider dead after 45s without any event/heartbeat

function _attachListeners(source, handler) {
    EVENT_TYPES.forEach(type => {
        source.addEventListener(type, (e) => {
            _lastEventTime = Date.now();
            if (_connectionState !== 'connected') {
                _connectionState = 'connected';
                _updateHealthIndicator('connected');
            }
            try {
                const data = JSON.parse(e.data);
                if (typeof data.seq === 'number' && data.seq > _lastSeq) _lastSeq = data.seq;
                handler({ type, ...data });
            } catch {
                handler({ type, raw: e.data });
            }
        });
    });
}

let _consecutiveErrors = 0;

export function connectSSE(sessionId, onEvent) {
    disconnectSSE();
    _onEvent = onEvent;
    _sessionId = sessionId;
    if (!isOnline()) return;
    _lastEventTime = Date.now();
    _consecutiveErrors = 0;
    _source = new EventSource(`/api/sessions/${sessionId}/events`);

    _source.onopen = () => {
        _connectionState = 'connected';
        _lastEventTime = Date.now();
        _consecutiveErrors = 0;
        _updateHealthIndicator('connected');
        console.debug('SSE connected');
    };

    _source.onerror = () => {
        _connectionState = 'reconnecting';
        _updateHealthIndicator('reconnecting');
        console.warn('SSE error, reconnecting...');
        // EventSource hides the HTTP status, so a 404 (session deleted on
        // another device) looked identical to a flaky network — the dot spun
        // on "reconnecting" forever. After a few consecutive failures, probe
        // the status endpoint and stop for good if the session is gone.
        _consecutiveErrors++;
        if (_consecutiveErrors === 3) _probeSessionExists();
    };

    _attachListeners(_source, onEvent);

    // Start health monitoring
    _startHealthCheck();
}

async function _probeSessionExists() {
    const sid = _sessionId;
    const handler = _onEvent;
    if (!sid) return;
    try {
        const resp = await fetch(`/api/sessions/${sid}/status`);
        if (resp.status === 404) {
            console.warn(`SSE: session ${sid} no longer exists — stopping reconnect attempts`);
            disconnectSSE();
            if (handler) handler({ type: 'sse.session_gone', session_id: sid });
        }
    } catch { /* network issue — keep retrying as before */ }
}

export function disconnectSSE() {
    if (_source) {
        _source.close();
        _source = null;
    }
    _sessionId = null;
    _connectionState = 'disconnected';
    _updateHealthIndicator('disconnected');
    _lastSeq = 0;
    if (_healthTimer) {
        clearInterval(_healthTimer);
        _healthTimer = null;
    }
}

export function getSSEState() {
    return _connectionState;
}

// Browsers throttle timers in background tabs, so after a phone unlock the
// 15s health interval may not have run for minutes — check immediately on
// becoming visible instead of staring at stale state for up to a minute.
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') _checkStale();
});

function _startHealthCheck() {
    if (_healthTimer) clearInterval(_healthTimer);
    _healthTimer = setInterval(_checkStale, HEALTH_CHECK_INTERVAL);
}

function _checkStale() {
    if (!_source) return;

    const elapsed = Date.now() - _lastEventTime;
    if (elapsed > STALE_THRESHOLD) {
        // Connection appears dead — force reconnect
        console.warn(`SSE: no events for ${Math.round(elapsed / 1000)}s, forcing reconnect`);
        _connectionState = 'reconnecting';
        _updateHealthIndicator('reconnecting');
        // Close and reopen. Pass last seen seq as a query param so the
        // server replays anything we missed — EventSource won't let us
        // set the Last-Event-ID header on a JS-instantiated reconnect.
        const sid = _sessionId;
        const handler = _onEvent;
        _source.close();
        const replayQuery = _lastSeq > 0 ? `?last_event_id=${_lastSeq}` : '';
        _source = new EventSource(`/api/sessions/${sid}/events${replayQuery}`);
        _lastEventTime = Date.now();

        _source.onopen = () => {
            _connectionState = 'connected';
            _lastEventTime = Date.now();
            _updateHealthIndicator('connected');
            console.debug('SSE reconnected');
            // Notify app to check session status (button state recovery)
            handler({ type: 'sse.reconnected' });
        };
        _source.onerror = () => {
            _connectionState = 'reconnecting';
            _updateHealthIndicator('reconnecting');
        };

        _attachListeners(_source, handler);
    }
}

function _updateHealthIndicator(state) {
    const el = document.getElementById('sse-health');
    if (!el) return;
    el.className = `sse-health ${state}`;
    el.title = `SSE: ${state}`;
}
