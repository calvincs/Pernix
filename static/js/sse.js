// Pernix — SSE client with health monitoring and reconnection

import { isOnline } from './api.js';

// Auth token is sent via cookie (set by api.js setAuthToken), no query params needed

let _source = null;
let _onEvent = null;
let _sessionId = null;
let _lastEventTime = 0;
let _healthTimer = null;

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

export function connectSSE(sessionId, onEvent) {
    disconnectSSE();
    _onEvent = onEvent;
    _sessionId = sessionId;
    if (!isOnline()) return;
    _lastEventTime = Date.now();
    _source = new EventSource(`/api/sessions/${sessionId}/events`);

    _source.onopen = () => {
        _connectionState = 'connected';
        _lastEventTime = Date.now();
        _updateHealthIndicator('connected');
        console.debug('SSE connected');
    };

    _source.onerror = () => {
        _connectionState = 'reconnecting';
        _updateHealthIndicator('reconnecting');
        console.warn('SSE error, reconnecting...');
    };

    // Listen to all event types
    const eventTypes = [
        'stream.token', 'stream.done', 'stream.error',
        'tool.call', 'context.info', 'context.compacting', 'context.compacted', 'context.reset',
        'session.queued', 'session.title', 'session.cancelled',
        'session.state_changed', 'session.prompt_rejected',
        'message.injected',
        'scout.start', 'scout.step', 'scout.done',
        'worker.started', 'worker.done', 'worker.failed',
        'partial.saved',
        'dialog.question', 'user_question', 'dialog.answered', 'dialog.dismissed', 'dialog.notification',
        'browse.start', 'browse.done',
        'reflect.start', 'reflect.done', 'reflect.retry', 'reflect.exhausted', 'reflect.escalate',
        'turn.complete',
    ];

    eventTypes.forEach(type => {
        _source.addEventListener(type, (e) => {
            _lastEventTime = Date.now();
            if (_connectionState !== 'connected') {
                _connectionState = 'connected';
                _updateHealthIndicator('connected');
            }
            try {
                const data = JSON.parse(e.data);
                _onEvent({ type, ...data });
            } catch {
                _onEvent({ type, raw: e.data });
            }
        });
    });

    // Start health monitoring
    _startHealthCheck();
}

export function disconnectSSE() {
    if (_source) {
        _source.close();
        _source = null;
    }
    _sessionId = null;
    _connectionState = 'disconnected';
    _updateHealthIndicator('disconnected');
    if (_healthTimer) {
        clearInterval(_healthTimer);
        _healthTimer = null;
    }
}

export function getSSEState() {
    return _connectionState;
}

function _startHealthCheck() {
    if (_healthTimer) clearInterval(_healthTimer);
    _healthTimer = setInterval(() => {
        if (!_source) return;

        const elapsed = Date.now() - _lastEventTime;
        if (elapsed > STALE_THRESHOLD) {
            // Connection appears dead — force reconnect
            console.warn(`SSE: no events for ${Math.round(elapsed / 1000)}s, forcing reconnect`);
            _connectionState = 'reconnecting';
            _updateHealthIndicator('reconnecting');
            // Close and reopen
            const sid = _sessionId;
            const handler = _onEvent;
            _source.close();
            _source = new EventSource(`/api/sessions/${sid}/events`);
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

            // Re-register event listeners
            const eventTypes = [
                'stream.token', 'stream.done', 'stream.error',
                'tool.call', 'context.info', 'context.compacting', 'context.compacted', 'context.reset',
                'session.queued', 'session.title', 'session.cancelled',
                'session.state_changed', 'session.prompt_rejected',
                'message.injected',
                'scout.start', 'scout.step', 'scout.done',
                'worker.started', 'worker.done', 'worker.failed',
                'partial.saved',
                'dialog.question', 'user_question', 'dialog.answered', 'dialog.dismissed', 'dialog.notification',
                'browse.start', 'browse.done',
                'reflect.start', 'reflect.done', 'reflect.retry', 'reflect.exhausted', 'reflect.escalate',
                'turn.complete',
            ];
            eventTypes.forEach(type => {
                _source.addEventListener(type, (e) => {
                    _lastEventTime = Date.now();
                    if (_connectionState !== 'connected') {
                        _connectionState = 'connected';
                        _updateHealthIndicator('connected');
                    }
                    try {
                        const data = JSON.parse(e.data);
                        handler({ type, ...data });
                    } catch {
                        handler({ type, raw: e.data });
                    }
                });
            });
        }
    }, HEALTH_CHECK_INTERVAL);
}

function _updateHealthIndicator(state) {
    const el = document.getElementById('sse-health');
    if (!el) return;
    el.className = `sse-health ${state}`;
    el.title = `SSE: ${state}`;
}
