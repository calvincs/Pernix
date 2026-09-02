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
        // The message is the copy the user reads: several call sites render
        // `e.message` straight into the transcript, where a bare "offline"
        // answered neither "did my message go?" nor "do I have to retype it?".
        // (OFFLINE_MESSAGE is defined below; this body runs long after.)
        super(OFFLINE_MESSAGE);
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

// ---------------------------------------------------------------------------
// Error copy
// ---------------------------------------------------------------------------
// Every error the user sees used to be whatever string happened to be on the
// exception — a FastAPI 422 rendered as "[object Object]", a provider 429 as
// a raw JSON blob, a dead server as "Failed to fetch". None of them say what
// to do next, which is the only thing an error message is for.

export const OFFLINE_MESSAGE =
    "Can't reach the server — your message was not sent; the text is kept.";

const RATE_LIMIT_RE = /rate[_\s-]?limit|too many requests|\b429\b/i;
const PROVIDER_AUTH_RE =
    /api[_\s-]?key|invalid_api_key|authentication_error|incorrect api key|no auth credentials|\bforbidden\b/i;
const CONTEXT_FULL_RE =
    /context\s*(?:window\s*)?(?:is\s*)?full|maximum context length|context_length_exceeded|prompt is too long|reduce the length of the messages/i;
const NETWORK_RE = /failed to fetch|networkerror|load failed|connection refused|err_connection/i;

/** FastAPI hands a 422 back as a LIST of {loc, msg, type}; String() on that
 *  is "[object Object]". Flatten it to something readable. */
function _detailText(detail) {
    if (Array.isArray(detail)) {
        return detail.map((d) => {
            if (typeof d === 'string') return d;
            if (!d || typeof d !== 'object') return String(d);
            const where = Array.isArray(d.loc) ? d.loc.filter((p) => p !== 'body').join('.') : '';
            const msg = d.msg || d.message || d.type || 'invalid value';
            return where ? `${where}: ${msg}` : msg;
        }).filter(Boolean).join('; ');
    }
    if (detail && typeof detail === 'object') {
        return detail.msg || detail.message || detail.detail || detail.error || '';
    }
    return detail == null ? '' : String(detail);
}

/**
 * One actionable line for an error, whatever shape it arrived in: an Error,
 * an OfflineError, a parsed FastAPI body, or a bare provider string.
 *
 * @param {Error|string|object} err
 * @returns {string}
 */
export function humanizeError(err) {
    if (!err) return 'Something went wrong.';
    if (err.offline === true || err.name === 'OfflineError') return OFFLINE_MESSAGE;

    const status = err.status ?? err.statusCode ?? null;
    const text = String(
        typeof err === 'string'
            ? err
            : (_detailText(err.detail) || err.message || _detailText(err.error) || _detailText(err)),
    ).trim();

    if (NETWORK_RE.test(text)) return OFFLINE_MESSAGE;
    // Context first: a full context often arrives WITH a provider status code,
    // and "compact or start a new session" is the only advice that helps.
    if (CONTEXT_FULL_RE.test(text)) return 'Context is full — send /compact or start a new session.';
    if (status === 429 || RATE_LIMIT_RE.test(text)) return 'The model provider is rate-limiting; retrying…';
    // Our own 401 (an expired access token) is handled by the login overlay and
    // must keep its own wording; this branch is for the model provider's.
    if ((status === 401 || status === 403 || /\b40[13]\b/.test(text)) && PROVIDER_AUTH_RE.test(text)) {
        return 'API key rejected — check Settings → Models.';
    }
    if (PROVIDER_AUTH_RE.test(text) && /reject|invalid|unauthor/i.test(text)) {
        return 'API key rejected — check Settings → Models.';
    }
    return text || 'Something went wrong.';
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
        const body = await resp.json().catch(() => ({ error: resp.statusText }));
        const e = new Error(humanizeError({
            status: resp.status,
            detail: body.detail,
            message: body.error || resp.statusText,
        }));
        // The raw parts stay on the error for callers that branch on them.
        e.status = resp.status;
        e.detail = body.detail;
        throw e;
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
export const patch = (path, body) => apiJson('PATCH', path, body);
export const put = (path, body) => apiJson('PUT', path, body);
