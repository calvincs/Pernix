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
// by exact `event:` name — the API has no wildcard listener, so the client
// cannot fail open here. An event missing from this list is dropped before JS
// ever sees it, and because `seq` rides on the payload, _lastSeq never
// advances for it either; gap-detection in app.js then fires a spurious soft
// reload when the next subscribed event arrives. The symptom (a transcript
// that reloads itself) points nowhere near the cause (a name added in Python).
//
// tests/test_sse_event_sync.py enforces this list against the emitters in
// core/, sessions/, api/ in both directions, so drift fails the suite instead
// of shipping. Add the name here when that test tells you to.
const EVENT_TYPES = [
    // Stream lifecycle
    'stream.token', 'stream.done', 'stream.error',
    'stream.fallback', 'stream.retry', 'stream.length_continuation',
    'stream.budget_exhausted',
    // Tools / context / scout
    'tool.start', 'tool.call',
    'context.compacting', 'context.compacted', 'context.reset',
    'context.view_pruned',
    'scout.start', 'scout.step', 'scout.done',
    // Session lifecycle
    'session.queued', 'session.title', 'session.cancelled',
    'session.state_changed', 'session.prompt_rejected',
    'session.waiting_llm', 'session.message_combined',
    'session.message_combine_skipped',
    'session.queue_dropped', 'session.queue_full',
    'session.queue_removed',
    // Goals (budget checkpoints + auto-continuation)
    'goal.budget_exceeded', 'goal.continuation',
    // Injected mid-turn messages
    'message.injected',
    // Workers
    'worker.started', 'worker.done', 'worker.failed',
    // RLM runs (recursive processing — live progress on the parent's stream)
    'rlm.started', 'rlm.activity', 'rlm.heartbeat', 'rlm.done',
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
    'reflect.budget_exhausted', 'reflect.circuit_breaker',
    // Eval (autonomous evaluation pass) + goal gates
    'eval.start', 'eval.pass', 'eval.done', 'eval.retry', 'eval.exhausted',
    'gates.done',
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
// Consider the stream suspect after 45s without an event. Note "event", not
// "event or heartbeat": the server's keepalive is an SSE *comment* line
// (": heartbeat"), and the EventSource parser discards comments without
// dispatching anything, so heartbeats are invisible to this file by
// construction. Silence here therefore means one of three things — the agent
// is simply idle, the connection died, or the server dropped us as a slow
// subscriber (queue full) and is now writing heartbeats into a stream nobody
// is subscribed to. _checkStale() tells them apart before acting.
const STALE_THRESHOLD = 45000;

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

let _staleProbeInFlight = false;

async function _checkStale() {
    if (!_source || _staleProbeInFlight) return;

    const elapsed = Date.now() - _lastEventTime;
    if (elapsed <= STALE_THRESHOLD) return;

    // Silence alone is not evidence of a dead stream — most sessions spend
    // most of their time idle. Blindly reconnecting here tore down and rebuilt
    // a perfectly healthy connection every 45s for any open-but-idle session,
    // flickering the health dot and firing a status round-trip each time.
    // Ask the server where its event counter is instead: if it has not moved
    // past us, we have missed nothing and the stream is merely quiet.
    _staleProbeInFlight = true;
    let behind;
    try {
        const resp = await fetch(`/api/sessions/${_sessionId}/status`);
        if (resp.status === 404) {
            // Session deleted elsewhere — same terminal case _probeSessionExists
            // handles for hard errors. Stop pretending it might come back.
            const sid = _sessionId;
            const handler = _onEvent;
            console.warn(`SSE: session ${sid} no longer exists — stopping reconnect attempts`);
            disconnectSSE();
            if (handler) handler({ type: 'sse.session_gone', session_id: sid });
            return;
        }
        if (!resp.ok) throw new Error(String(resp.status));
        const status = await resp.json();
        // _lastSeq is 0 until a live event arrives on THIS connection, so it
        // cannot be compared against the server's counter yet — a session with
        // history would always look "behind" and reconnect forever. Nothing has
        // been received, so nothing has been missed; app.js's own reconciler
        // owns the "transcript is behind the server" case.
        behind = _lastSeq > 0 && (status.event_seq || 0) > _lastSeq;
    } catch {
        // Server unreachable or the probe failed — treat as a dead stream and
        // rebuild, which is the pre-existing behaviour for genuine outages.
        behind = true;
    } finally {
        _staleProbeInFlight = false;
    }

    if (!_source) return;  // disconnected while the probe was in flight

    if (!behind) {
        // Alive and quiet. Reset the clock so the probe backs off to one
        // request per STALE_THRESHOLD rather than one per health tick.
        _lastEventTime = Date.now();
        return;
    }

    // The server has events we never received: either the connection died or
    // we were dropped as a slow subscriber (queue full), in which case the
    // socket stays open and heartbeats keep arriving forever. Rebuild it.
    console.warn(`SSE: no events for ${Math.round(elapsed / 1000)}s and the server has moved ahead — reconnecting`);
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

function _updateHealthIndicator(state) {
    const el = document.getElementById('sse-health');
    if (!el) return;
    el.className = `sse-health ${state}`;
    el.title = `SSE: ${state}`;
}
