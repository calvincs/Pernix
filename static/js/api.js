// Pernix — Centralized API client

const BASE = '';

let _authToken = localStorage.getItem('pernix_auth_token') || '';

// ---------------------------------------------------------------------------
// Connectivity tracking
// ---------------------------------------------------------------------------
// When the backend is unreachable, fetch() throws TypeError and the browser
// logs a red ERR_CONNECTION_REFUSED at the network layer. To stop spamming
// the console, we mark the API offline on first failure, short-circuit all
// subsequent api() calls (so no fetch is attempted), and run one quiet pinger
// every PING_INTERVAL_MS until the server returns. While offline we dispatch
// 'pernix:offline'; on recovery we dispatch 'pernix:online'. SSE wrappers listen
// to these events to close/reopen their EventSources.

const PING_INTERVAL_MS = 10000;
const PING_PATH = '/api/health';

let _online = true;
let _pingTimer = null;

export function isOnline() { return _online; }

export class OfflineError extends Error {
    constructor() {
        super('offline');
        this.name = 'OfflineError';
        this.offline = true;
    }
}

function _isNetworkError(e) {
    // Browser fetch reports network failures as TypeError. Distinguish from
    // other TypeErrors by message ("Failed to fetch", "NetworkError", "Load failed").
    return e instanceof TypeError && /fetch|network|load failed/i.test(e.message);
}

function _markOffline() {
    if (!_online) return;
    _online = false;
    window.dispatchEvent(new CustomEvent('pernix:offline'));
    if (!_pingTimer) {
        _pingTimer = setInterval(_ping, PING_INTERVAL_MS);
    }
}

function _markOnline() {
    if (_online) return;
    _online = true;
    if (_pingTimer) {
        clearInterval(_pingTimer);
        _pingTimer = null;
    }
    window.dispatchEvent(new CustomEvent('pernix:online'));
}

async function _ping() {
    try {
        const resp = await fetch(`${BASE}${PING_PATH}`, {
            method: 'GET',
            headers: { ..._authHeaders() },
            cache: 'no-store',
        });
        if (resp.ok || resp.status === 401) _markOnline();
    } catch {
        // Still offline — quietly retry next tick.
    }
}

export function setAuthToken(token) {
    _authToken = token;
    localStorage.setItem('pernix_auth_token', token);
    // Set cookie so EventSource/SSE connections authenticate without query params.
    // SameSite=Lax (not Strict) allows cookies in cross-site EventSource/top-level
    // navigation — needed for remote clients in network mode.
    // Secure flag added when on HTTPS so the token isn't sent over plaintext.
    const _secure = window.location.protocol === 'https:' ? '; Secure' : '';
    if (token) {
        document.cookie = `pernix_auth=${encodeURIComponent(token)}; path=/; SameSite=Lax${_secure}; max-age=31536000`;
    } else {
        document.cookie = `pernix_auth=; path=/; SameSite=Lax${_secure}; max-age=0`;
    }
}

export function getAuthToken() {
    return _authToken;
}

function _authHeaders() {
    const h = {};
    if (_authToken) h['Authorization'] = `Bearer ${_authToken}`;
    return h;
}

export async function api(method, path, body = null) {
    if (!_online) throw new OfflineError();
    const opts = { method, headers: { ..._authHeaders() } };
    if (body) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
    }
    let resp;
    try {
        resp = await fetch(`${BASE}${path}`, opts);
    } catch (e) {
        if (_isNetworkError(e)) {
            _markOffline();
            throw new OfflineError();
        }
        throw e;
    }
    if (resp.status === 401) {
        window.dispatchEvent(new CustomEvent('pernix:auth-required'));
        throw new Error('Authentication required');
    }
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ error: resp.statusText }));
        throw new Error(err.detail || err.error || resp.statusText);
    }
    return resp;
}

export async function apiJson(method, path, body = null) {
    const resp = await api(method, path, body);
    return resp.json();
}

// Convenience
export const get = (path) => apiJson('GET', path);
export const post = (path, body) => apiJson('POST', path, body);
export const del = (path) => apiJson('DELETE', path);
