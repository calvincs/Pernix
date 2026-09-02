// Pernix — Main application entry point

import { state, subscribe } from './store.js';
import { get, post, del, patch, getAuthToken, setAuthToken, humanizeError } from './api.js';
import { connectSSE, disconnectSSE } from './sse.js';
import { getPermission, requestPermission, connectGlobalNotifications, registerServiceWorker, subscribePush } from './notifications.js';
import { el, text, clear, initMarked, renderMarkdown } from './render.js';
import { initSigil } from './sigil.js';
import { icon } from './icons.js';
import { openSettings } from './components/modals/settings.js';
import { openTimeline, appendTimelineRow, appendTimelineToolRow, appendTimelineToolStart, isTimelineOpen } from './components/modals/timeline.js';
import { initBell, openBellPanel, closeBellPanel, refreshBell } from './components/notification-bell.js';
import { initJobsIndicator } from './components/jobs-indicator.js';
import {
    initSidebar, renderSessionList as renderSidebar, updateSessionActivity,
    setSessionPaging, setSessionPagingBusy,
} from './components/sidebar.js';
import { initFilePanel, toggleFilePanel, openFilePanel } from './components/file-panel.js';
import { openRlmViewer, closeRlmViewer } from './components/rlm-viewer.js';
import { initMobile, isCompact, isTouch, closeSidebar, syncDrawerInert } from './mobile.js';
import { initVoice, stopVoice } from './voice.js';
import { announce, openOverlay } from './a11y.js';
import { setTheme, isLight } from './theme.js';
import { notify } from './feedback.js';
import { confirmDanger } from './components/modals/confirm.js';
import { actionSheet } from './components/modals/sheet.js';

// ---------------------------------------------------------------------------
// File uploads state
// ---------------------------------------------------------------------------

let _pendingFiles = []; // { file, name, uploading, uploaded, serverName, size }
let _scoutContainer = null;

// ---------------------------------------------------------------------------
// Message container helpers (inner for content, outer for scroll)
// ---------------------------------------------------------------------------

// While an earlier page is being built it is rendered into a detached
// fragment, so the visible transcript is never cleared and never re-rendered.
// Every append/render helper in this file reaches its container through
// _messagesInner(), so pointing that at the fragment is the whole trick. Set
// and cleared synchronously inside _replayMessages — nothing can interleave.
let _renderTarget = null;

function _messagesInner() {
    return _renderTarget || document.getElementById('messages-inner');
}

function _messagesScroll() {
    return document.getElementById('messages');
}

// ---------------------------------------------------------------------------
// Pinned scrolling — only autoscroll while the user is at (near) the bottom.
// Force-jumping on every append yanked the user down mid-read during long
// multi-minute turns; meanwhile the streaming re-render never scrolled, so a
// growing answer ran below the fold. Both route through here now.
// ---------------------------------------------------------------------------
let _scrollPinned = true;          // user is at/near the bottom
const _PIN_THRESHOLD = 100;        // px from bottom that still counts as pinned

function _updateScrollPin() {
    const scroll = _messagesScroll();
    if (!scroll) return;
    _scrollPinned = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < _PIN_THRESHOLD;
    const jump = document.getElementById('jump-to-bottom');
    if (jump) jump.classList.toggle('visible', !_scrollPinned);
}

function scrollToBottom(force = false) {
    const scroll = _messagesScroll();
    if (!scroll) return;
    if (force) _scrollPinned = true;
    if (!_scrollPinned) return;
    scroll.scrollTop = scroll.scrollHeight;
    const jump = document.getElementById('jump-to-bottom');
    if (jump) jump.classList.remove('visible');
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', async () => {
    // Auto-extract auth token from URL (shared link / QR code onboarding).
    //
    // The fragment is the carrier, not the query string: browsers never send a
    // fragment to the server, so it cannot reach an access log, a proxy log or
    // a Referer header. `?token=` used to be written verbatim into uvicorn's
    // access log — a live credential in `docker compose logs`, readable by
    // anyone with log access and preserved anywhere those logs get shipped.
    //
    // The query form is still accepted because links and QR codes handed out
    // before the change are still in circulation; run.py redacts that form on
    // the way into the log. Both are stripped from the address bar below, so a
    // shoulder-surfer or a screenshot does not catch it either.
    const _hashParams = new URLSearchParams(window.location.hash.replace(/^#/, ''));
    const _urlParams = new URLSearchParams(window.location.search);
    const _urlToken = _hashParams.get('token') || _urlParams.get('token');
    if (_urlToken) {
        setAuthToken(_urlToken);
        _hashParams.delete('token');
        _urlParams.delete('token');
        const _q = _urlParams.toString();
        const _h = _hashParams.toString();
        history.replaceState(
            null,
            '',
            window.location.pathname + (_q ? `?${_q}` : '') + (_h ? `#${_h}` : ''),
        );
    } else {
        // Restore pernix_auth cookie from sessionStorage on page reload.
        // setAuthToken() sets both storage AND cookie, but on reload only storage
        // survives — the cookie must be re-set so SSE/EventSource connections auth.
        const _stored = localStorage.getItem('pernix_auth_token');
        if (_stored) setAuthToken(_stored);
    }

    registerServiceWorker();
    initMarked();
    initMobile();
    initSidebar(selectSession, deleteSession);
    await loadSessions();
    await loadHealth();
    setupInput();
    setupNewSession();
    setupFileDrop();
    initVoice({
        textarea: () => document.getElementById('msg-input'),
        addPendingFiles,
        appendMessage,
        send,
    });
    showEmptyState();

    // Sidebar toggle (desktop only — mobile handled by mobile.js)
    const sidebarToggle = document.getElementById('sidebar-toggle');
    const sidebar = document.getElementById('sidebar');
    // A collapsed sidebar is width:0 + overflow:hidden, which hides it from
    // the eye but not from the keyboard: Tab still walked the whole invisible
    // session list, and a screen reader still read it. inert takes it out of
    // both. The COMPACT drawer is a separate mechanism (mobile.js owns
    // .mobile-open and its own inert) so it is deliberately left alone. Note
    // this is a layout question, not a pointer one: a docked sidebar on a
    // tablet collapses exactly as it does under a mouse.
    const syncSidebarInert = () => {
        // Two mechanisms, one attribute. Below 900px the sidebar is a drawer
        // and mobile.js owns the rule (closed = inert); above it, "collapsed"
        // is what makes it unreachable. Both run on the same resize, so this
        // delegates rather than racing mobile.js to the last write.
        if (isCompact()) { syncDrawerInert(); return; }
        sidebar.toggleAttribute('inert', sidebar.classList.contains('collapsed'));
    };
    if (localStorage.getItem('pernix:sidebar-hidden') === '1') {
        sidebar.classList.add('collapsed');
    }
    syncSidebarInert();
    // Narrowing a desktop window past the compact breakpoint turns the same
    // element into the drawer, which mobile.js opens with .mobile-open — a
    // stale inert from the docked state would leave that drawer visible but
    // dead. Re-evaluated on resize because mobile.js owns the media query.
    window.addEventListener('resize', syncSidebarInert);
    // Crossing 900px swaps the worker strip's whole shape (P3): chips above
    // the line, one summary line below it.
    window.addEventListener('pernix:tier-change', () => _renderWorkerStrip());
    sidebarToggle.addEventListener('click', () => {
        if (isCompact()) return;   // the drawer is mobile.js's to open
        sidebar.classList.toggle('collapsed');
        localStorage.setItem('pernix:sidebar-hidden', sidebar.classList.contains('collapsed') ? '1' : '0');
        syncSidebarInert();
    });

    document.getElementById('session-header-title')?.addEventListener('click', _startHeaderRename);

    // One rename path for the whole app. The header raises the intent; this
    // listener is what talks to the server, so the sidebar's own inline rename
    // and this one cannot drift apart.
    window.addEventListener('pernix:rename-session', async (e) => {
        const { sid, title } = (e && e.detail) || {};
        if (!sid || !title) return;
        try {
            const res = await patch(`/api/sessions/${sid}`, { title });
            const sess = (state.sessions || []).find((x) => x.id === sid);
            if (sess) sess.title = res.title || title;
            renderSidebar(state.sessions, state.sid, state.spaces);
            _renderSessionHeader();
        } catch (err) {
            notify('error', `Rename failed: ${err.message}`);
        }
    });

    // The wordmark is the one thing on the page that looks like a home link
    // and href="#" made it a no-op that also scrolled the page to the top.
    document.querySelector('.brand')?.addEventListener('click', (e) => {
        e.preventDefault();
        goHome();
    });

    // Escape closes the Explorer when nothing else owns the key. Overlays get
    // theirs from openOverlay (which is scoped to the top of its own stack);
    // this is the one pane that is not an overlay and had no way out but the
    // button that opened it.
    document.addEventListener('keydown', _handleGlobalEscape);

    _setupPaneHistory();

    // Space/session mutations from the sidebar need a re-FETCH, not just a repaint
    window.addEventListener('pernix:sessions-changed', () => loadSessions());

    // Restore the normal session list when the sidebar search box clears
    window.addEventListener('pernix:sidebar-refresh', () => renderSidebar(state.sessions, state.sid, state.spaces));

    // Pinned-scroll tracking + jump-to-bottom affordance
    const _msgScroll = _messagesScroll();
    if (_msgScroll) _msgScroll.addEventListener('scroll', _updateScrollPin, { passive: true });
    document.getElementById('jump-to-bottom')?.addEventListener('click', () => scrollToBottom(true));

    // Pause/resume the active session (gentler than cancel for long turns)
    document.getElementById('pause-btn')?.addEventListener('click', async () => {
        if (!state.sid) return;
        const btn = document.getElementById('pause-btn');
        const action = btn._paused ? 'resume' : 'pause';
        btn.disabled = true;
        try {
            await post(`/api/sessions/${state.sid}/${action}`, {});
        } catch (e) {
            appendMessage('system', `${action} failed: ${e.message}`);
        } finally {
            btn.disabled = false;
        }
    });

    // Settings + bell + jobs + files + transcript buttons
    document.getElementById('settings-btn').addEventListener('click', openSettings);
    document.getElementById('state-badge')?.addEventListener('click', openTimeline);
    document.getElementById('files-btn').addEventListener('click', toggleFilePanel);
    document.getElementById('copy-transcript-btn').addEventListener('click', copyTranscript);
    document.getElementById('export-transcript-btn')?.addEventListener('click', exportTranscript);
    document.getElementById('status-model')?.addEventListener('click', (e) => {
        e.stopPropagation();
        _openModelMenu();
    });
    initFilePanel({ selectSession });
    initBell({ selectSession });

    // Keyboard shortcuts: Ctrl/Cmd+F → transcript search (with a session
    // open), Ctrl/Cmd+K → session switcher palette.
    document.addEventListener('keydown', (e) => {
        const mod = e.ctrlKey || e.metaKey;
        if (!mod) return;
        // openOverlay() makes the background inert, but inert does not cover a
        // handler bound to `document` — so these two fired straight through an
        // open Settings or bell dialog, stacking a second focus trap on top of
        // the first with no way back except Escape twice.
        if (_modalOverlayOpen()) return;
        // Ctrl/Cmd+F is the browser's own find, and taking it away everywhere
        // meant a reader could not search the sidebar, the Explorer or any
        // other pane at all. Claim it only where the transcript search is
        // actually the better answer: with focus inside the chat column.
        if (e.key === 'f' && state.sid && _focusInsideMain()) {
            e.preventDefault();
            openTranscriptSearch();
        } else if (e.key === 'k') {
            e.preventDefault();
            openSessionPalette();
        }
    });

    // Questions/notifications can arrive for non-active sessions via the
    // global stream — refresh the list so attention badges appear promptly.
    window.addEventListener('pernix:bell-update', () => loadSessions());

    // Global notification SSE — connects immediately, no session required.
    // Handles browser notifications for dialog.notification and dialog.question events.
    connectGlobalNotifications();

    // Permission is requested from the bell panel (a user gesture) —
    // browsers suppress non-gesture prompts, so asking on load did nothing.
    // Re-subscribe on every load — idempotent upsert on the server
    if (getPermission() === 'granted') subscribePush();

    // Handle ?session= URL param — set by SW when opening a new window from a notification click
    const _notifSession = new URLSearchParams(window.location.search).get('session');
    if (_notifSession) {
        history.replaceState(null, '', window.location.pathname);
        setTimeout(() => selectSession(_notifSession), 0);
    }

    // Jobs indicator
    initJobsIndicator(document.getElementById('status-bar'), {
        onOpenPanel: () => openFilePanel({ tab: 'jobs' }),
    });

    // Auth login screen — fired by api.js on 401 responses
    let _authScreenShown = false;
    window.addEventListener('pernix:auth-required', () => {
        if (_authScreenShown) return;
        _authScreenShown = true;
        _showLoginScreen();
    });

    // Re-render sidebar when type filter toggles
    document.addEventListener('sidebar:filter-changed', () => {
        renderSidebar(state.sessions, state.sid, state.spaces);
    });

    // The sidebar's list-end button asks for the page behind the loaded ones.
    window.addEventListener('pernix:load-older-sessions', () => loadOlderSessions());

    // Poll — guarded by isOnline() inside the functions / api layer.
    // Skipped while the tab is hidden: the enriched session list runs
    // full-table aggregates server-side, and a backgrounded phone tab was
    // burning battery/data polling a list nobody could see. A fresh load
    // fires on the visibilitychange→visible handler below.
    setInterval(() => { if (document.visibilityState === 'visible') loadSessions(); }, 10000);
    setInterval(() => { if (document.visibilityState === 'visible') loadHealth(); }, 30000);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
            loadSessions();
            loadHealth();
        }
    });

    // Connectivity overlay
    window.addEventListener('pernix:offline', _showOfflineOverlay);
    window.addEventListener('pernix:online', () => {
        _hideOfflineOverlay();
        loadSessions();
        loadHealth();
    });
});

/**
 * Is the caret (or the focus ring) inside the chat column?
 *
 * With nothing focused at all the answer is yes: the composer is where the
 * page starts, the transcript is what fills the screen, and Ctrl+F on a fresh
 * page load is asking about the conversation. Focus anywhere else — the
 * sidebar, the Explorer, a settings field — leaves the browser's own find
 * alone, which is the only search those panes have.
 */
function _focusInsideMain() {
    const main = document.getElementById('main');
    if (!main) return true;
    const active = document.activeElement;
    if (!active || active === document.body || active === document.documentElement) return true;
    return main.contains(active);
}

/** True when a modal overlay is on screen. The session palette is excluded so
 *  Ctrl+K still toggles it shut — it is the thing the shortcut owns. */
function _modalOverlayOpen() {
    const nodes = document.querySelectorAll('.modal-overlay, .auth-overlay, [role="dialog"]');
    for (const node of nodes) {
        if (_paletteEl && (node === _paletteEl || _paletteEl.contains(node))) continue;
        return true;
    }
    return false;
}

/** Escape, when nothing nearer owns it. */
function _handleGlobalEscape(e) {
    if (e.key !== 'Escape' || e.defaultPrevented) return;
    if (_modalOverlayOpen() || _searchBarEl) return;
    const panel = document.getElementById('file-panel');
    if (panel && panel.classList.contains('open')) {
        e.preventDefault();
        toggleFilePanel();
    }
}

/** Back to the hero screen with no session selected — the state the app boots
 *  into, and the only thing the wordmark could sensibly mean. */
function goHome() {
    if (isCompact()) closeSidebar();
    _flushDraft();
    state.sid = null;
    _lastSeq = 0;           // seqs are per-session (see deleteSession)
    _selectSeq++;           // cancel any select still in flight
    disconnectSSE();
    closeRlmViewer();
    _activeWorkers.clear();
    _recentDeadWorkers.clear();
    _activeRlmRuns.clear();
    _workerCard = null;
    _renderWorkerStrip();
    state.streaming = false;
    _setComposerReadOnly(false);
    _showSendButton();
    updateStatus('');
    _clearToolStatus();
    _resetContextReadout();
    _applyStateBadge('idle_ready', '');
    _renderModelBadge();   // no session -> the override badge goes quiet (P2)
    showEmptyState();
    _restoreDraft();
    renderSidebar(state.sessions, state.sid, state.spaces);
}

// ---------------------------------------------------------------------------
// Back-button support for the two panes that cover the screen on a phone.
// Their open/close lives in mobile.js (the drawer) and file-panel.js (the
// Explorer); this watches the class each of them sets rather than reaching
// into either module. Without it, Back on Android left the app entirely while
// a full-screen drawer was covering the transcript.
// ---------------------------------------------------------------------------
let _paneHistoryDepth = 0;

function _panesOpen() {
    const sidebar = document.getElementById('sidebar');
    const panel = document.getElementById('file-panel');
    return !!((sidebar && sidebar.classList.contains('mobile-open'))
        || (panel && panel.classList.contains('open')));
}

function _closePanes() {
    const panel = document.getElementById('file-panel');
    if (panel && panel.classList.contains('open')) toggleFilePanel();
    const sidebar = document.getElementById('sidebar');
    if (sidebar && sidebar.classList.contains('mobile-open')) closeSidebar();
}

function _setupPaneHistory() {
    if (typeof MutationObserver !== 'function') return;
    let wasOpen = _panesOpen();

    const sync = () => {
        const now = _panesOpen();
        if (now === wasOpen) return;
        wasOpen = now;
        if (now) {
            _paneHistoryDepth++;
            history.pushState({ pernixPane: _paneHistoryDepth }, '');
        } else if (_paneHistoryDepth > 0) {
            // Closed by a tap, not by Back. Spend our entry now, or the user's
            // next Back press would be swallowed doing nothing.
            _paneHistoryDepth--;
            history.back();
        }
    };

    const observer = new MutationObserver(sync);
    for (const id of ['sidebar', 'file-panel']) {
        const node = document.getElementById(id);
        if (node) observer.observe(node, { attributes: true, attributeFilter: ['class'] });
    }

    window.addEventListener('popstate', () => {
        if (!_panesOpen()) return;
        // Set the tracking state BEFORE closing, so the observer sees no
        // change and does not push a second history.back() on top of this one.
        wasOpen = false;
        if (_paneHistoryDepth > 0) _paneHistoryDepth--;
        _closePanes();
    });
}

function _showOfflineOverlay() {
    // A banner, not a modal — the old full-screen overlay blocked reading
    // (and copying from) the transcript during server downtime, which is
    // exactly when you want to re-read what the agent said.
    if (document.getElementById('offline-banner')) return;
    const banner = document.createElement('div');
    banner.id = 'offline-banner';
    banner.className = 'offline-banner';
    banner.innerHTML = `
        <span class="offline-spinner"></span>
        <span>Disconnected from server — reconnecting&hellip; The transcript below is still readable.</span>
    `;
    document.body.appendChild(banner);
}

function _hideOfflineOverlay() {
    const o = document.getElementById('offline-banner');
    if (o) o.remove();
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

// Needs-attention tracking: sessions whose background turn finished while the
// user was elsewhere keep a "done" tick until visited; sessions blocked in
// awaiting_user get a "?" badge. Both derive from the polled session list
// (state_v2 is persisted on every transition).
const _BUSY_STATES = new Set([
    'scouting', 'processing', 'compacting', 'awaiting_workers',
    'finalizing', 'pause_requested', 'cancelling',
]);
const _prevBusy = new Map();        // session id → was busy at last poll
const _recentlyFinished = new Set(); // session ids with an unvisited finished turn

// One page of the session list is 500 rows (SESSION_PAGE_LIMIT in
// sidebar.js). "Load older sessions" widens the window rather than holding an
// offset the poll would immediately blow away: the ten-second refresh has to
// come back with everything already on screen, or a page loaded by hand would
// vanish a heartbeat later.
const SESSION_PAGE = 500;
let _sessionWindow = SESSION_PAGE;
let _loadingOlderSessions = false;

async function loadSessions() {
    try {
        const data = await get(`/api/sessions?limit=${_sessionWindow}`);
        state.sessions = data.items || [];
        state.spaces = data.spaces || [];
        setSessionPaging({
            total: data.total || state.sessions.length,
            loaded: state.sessions.length,
            hasMore: !!data.has_more,
        });
        for (const s of state.sessions) {
            const busy = _BUSY_STATES.has(s.state_v2);
            if (_prevBusy.get(s.id) && !busy && s.id !== state.sid) {
                _recentlyFinished.add(s.id);
            }
            _prevBusy.set(s.id, busy);
            s._attention = s.state_v2 === 'awaiting_user' ? 'input'
                : (_recentlyFinished.has(s.id) && s.id !== state.sid) ? 'done'
                : null;
        }
        renderSidebar(state.sessions, state.sid, state.spaces);
        _renderSessionHeader();   // titles are generated a beat after the first turn
    } catch (e) {
        if (!e.offline) console.warn('Failed to load sessions:', e);
    }
}

/**
 * Fetch the page behind the loaded ones and append it.
 *
 * The offset fetch is what lands on screen straight away; widening the window
 * is what makes the next ten-second poll come back with those rows still in
 * it. Doing only one of the two either costs a full re-fetch of everything
 * already loaded, or shows rows that disappear again on the next tick.
 */
async function loadOlderSessions() {
    if (_loadingOlderSessions) return;
    _loadingOlderSessions = true;
    setSessionPagingBusy(true);
    try {
        const offset = _sessionWindow;
        const data = await get(`/api/sessions?limit=${SESSION_PAGE}&offset=${offset}`);
        const known = new Set((state.sessions || []).map((s) => s.id));
        const fresh = (data.items || []).filter((s) => !known.has(s.id));
        state.sessions = [...(state.sessions || []), ...fresh];
        _sessionWindow = offset + SESSION_PAGE;
        setSessionPaging({
            total: data.total || state.sessions.length,
            loaded: state.sessions.length,
            hasMore: !!data.has_more,
        });
        renderSidebar(state.sessions, state.sid, state.spaces);
        announce(fresh.length
            ? `${fresh.length} older sessions loaded`
            : 'No more sessions to load');
    } catch (e) {
        if (!e.offline) console.warn('Failed to load older sessions:', e);
        notify('error', `Couldn't load older sessions — ${humanizeError(e)}`);
    } finally {
        _loadingOlderSessions = false;
        setSessionPagingBusy(false);
    }
}

async function deleteSession(sid) {
    try {
        await del(`/api/sessions/${sid}`);
        if (state.sid === sid) {
            state.sid = null;
            // Event sequences are per-session and restart at 1. Carrying this
            // session's high-water mark into the next one made the dedup at
            // handleEvent drop every event of the new session — an empty
            // assistant bubble and a stop button that never cleared.
            _lastSeq = 0;
            disconnectSSE();
            closeRlmViewer();
            showEmptyState();
        }
        await loadSessions();
    } catch (e) {
        console.error('Failed to delete session:', e);
    }
}

// A phone has no Shift+Enter and no hover, so the two keyboards need two
// different sentences — and the coarse one has to be visible, not a tooltip.
function _isCoarsePointer() {
    return typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
}

function _composerHint() {
    return _isCoarsePointer() ? 'Tap send' : 'Enter to send · Shift+Enter for a new line';
}

// The fine-pointer placeholder used to carry both key bindings, which ran
// past the end of the textarea at any realistic width — with the Explorer
// open at 1280px it wrapped to a second line the composer clips, so it read
// "…for a new lin". The second binding is a discovery, not an instruction:
// it belongs in the tooltip and the accessible description, where it has as
// much room as it needs. _composerHint() still carries both.
function _composerPlaceholder() {
    return _isCoarsePointer()
        ? 'Message Pernix — tap send'
        : 'Message Pernix… Enter to send';
}

function _setComposerReadOnly(readonly, reason) {
    const input = document.getElementById('msg-input');
    const btn = document.getElementById('send-btn');
    if (!input || !btn) return;
    input.disabled = readonly;
    btn.disabled = readonly;
    // The composer is more than the textarea. Attach and mic both feed a
    // session the server will refuse, so leaving them live offered a path
    // whose only possible ending was an error.
    for (const id of ['attach-btn', 'voice-btn']) {
        const b = document.getElementById(id);
        if (b) b.disabled = readonly;
    }
    input.placeholder = readonly
        ? (reason || 'This session is read-only')
        : _composerPlaceholder();
    input.title = readonly ? (reason || 'This session is read-only') : _composerHint();
    input.setAttribute('aria-description', input.title);
}

// Monotonic token for session switches. Every await in selectSession (and
// loadMessages) is a chance for a second click to overtake the first: the
// slower chain used to finish last and write its own _lastSeq, badge and
// connectSSE over the newer session's, leaving a mixed transcript wired to
// the wrong stream. file-panel.js already guards its loads this way.
let _selectSeq = 0;

async function selectSession(sid) {
    const mySeq = ++_selectSeq;
    if (isCompact()) closeSidebar();
    // Land any half-typed draft for the session we are LEAVING before state.sid
    // moves and _restoreDraft overwrites the textarea.
    _flushDraft();
    state.sid = sid;
    _expandedKeys = new Set();     // open tool rows are remembered per session
    _recentlyFinished.delete(sid);  // visiting clears the "done" attention tick
    _restoreDraft();
    // The previous session's context reading is wrong the moment you switch,
    // and loadContextInfo only lands after the transcript fetch — several
    // hundred ms of "ctx: 84%" belonging to a session you already left.
    _resetContextReadout();
    // Reset streaming state to prevent cross-session leakage
    _streamingEl = null;
    _lastStreamModel = null;
    _collected = '';
    _toolGroup = null;
    _toolGroupCount = 0;
    _toolGroupErrors = 0;
    _toolGroupLatency = 0;
    _clearRunningTools();
    if (_parseTimer) { clearTimeout(_parseTimer); _parseTimer = null; }
    closeRlmViewer();
    renderSidebar(state.sessions, state.sid, state.spaces);
    _renderSessionHeader();

    // RLM run views have no transcript — the chat area renders the live
    // trace viewer instead, and the composer stays off (the server enforces
    // the same read-only policy via sessions.policy).
    const _sess = (state.sessions || []).find(s => s.id === sid);
    if (_sess?.session_type === 'rlm') {
        disconnectSSE();
        _activeWorkers.clear();
        _activeRlmRuns.clear();
        _workerCard = null;
        _renderWorkerStrip();
        _setComposerReadOnly(true, _sess.read_only_reason);
        state.streaming = false;
        _lastSeq = 0;  // no SSE stream here — a stale counter must not feed the reconciler
        _showSendButton();
        updateStatus('');
        _clearToolStatus();
        _applyStateBadge('idle_ready', '');
        _sessionModelOverride = null;
        _renderModelBadge();
        await openRlmViewer(_messagesInner(), sid);
        return;
    }

    await loadMessages(sid);
    if (mySeq !== _selectSeq) return;
    await loadContextInfo(sid);
    if (mySeq !== _selectSeq) return;
    _seedWorkerStrip(sid);

    // Read-only sessions (dream journals): policy rides on the session payload.
    _setComposerReadOnly(!!_sess?.read_only, _sess?.read_only_reason);

    // Fetch session status to get event_seq and streaming state BEFORE connecting SSE
    state.streaming = false;
    _showSendButton();
    updateStatus('');
    _clearToolStatus();
    _applyStateBadge('idle_ready', '');  // reset badge before fetching real state
    _sessionModelOverride = null;
    _renderModelBadge();
    try {
        const status = await get(`/api/sessions/${sid}/status`);
        if (mySeq !== _selectSeq) return;
        // Set _lastSeq to server's current event_seq so SSE dedup skips
        // events already rendered from DB — prevents the load+replay race
        _lastSeq = status.event_seq || 0;
        _applyStateBadge(status.state || 'idle_ready', '');
        _sessionModelOverride = status.model_override || null;
        _renderModelBadge();

        if (status.status === 'processing' || status.status === 'scouting') {
            state.streaming = true;
            _showStopButton();
            _streamingEl = appendMessage('assistant', '');
            _collected = '';
        }
    } catch {
        if (mySeq !== _selectSeq) return;
        _lastSeq = 0;
    }

    await loadPendingQuestions(sid);
    if (mySeq !== _selectSeq) return;
    connectSSE(sid, handleEvent);

}

const HISTORY_PAGE = 200;   // messages fetched and rendered per page

// The transcript pages BACKWARDS: the newest page first, then older pages
// prepended above it. `_oldestMsgId` is the cursor handed to the next fetch,
// `_historyHasMore` whether anything is left behind it. Growing a limit and
// re-fetching (what "load earlier" used to do) threw away and rebuilt every
// message already on screen — the reader's scroll position, their open tool
// rows and several thousand nodes, to show two hundred new ones.
let _oldestMsgId = null;
let _historyHasMore = false;
let _historyTotal = 0;
let _historyLoaded = 0;

// Tool rows and tool groups the reader has opened. Keyed by something that
// survives a re-render: the tool_call_id where there is one (a live row has
// one before it has a database id, and the persisted row carries the same
// value), else the message id. Re-armed per session in selectSession.
let _expandedKeys = new Set();

/** Stable identity for one tool row across live render, replay and reload. */
function _toolItemKey(callId, msgId) {
    if (callId) return `tc:${callId}`;
    if (msgId != null && msgId !== '') return `msg:${msgId}`;
    return '';
}

/** Record one disclosure's state so the next render can put it back. A bare
 *  key means "open"; the same key behind '!' means "the reader closed this".
 *  Both are needed: absence has to keep meaning "no opinion, use the default",
 *  or a hand-collapsed group springs open on every reload. */
function _rememberExpanded(key, expanded) {
    if (!key) return;
    _expandedKeys.delete(key);
    _expandedKeys.delete(`!${key}`);
    _expandedKeys.add(expanded ? key : `!${key}`);
}

/** true / false / null, where null is "never touched — use the default". */
function _recallExpanded(key) {
    if (!key) return null;
    if (_expandedKeys.has(key)) return true;
    if (_expandedKeys.has(`!${key}`)) return false;
    return null;
}

/** A persisted row's `metadata` column — a JSON string, or already an object
 *  on paths that hand it over parsed. Never throws; a malformed value is the
 *  same as no metadata. */
function _parseRowMetadata(raw) {
    if (!raw) return {};
    if (typeof raw === 'object') return raw;
    try { return JSON.parse(raw) || {}; } catch { return {}; }
}

/** Arguments off one persisted tool_call entry. Providers put them at
 *  `arguments` (object or JSON string) or `function.arguments` (a string,
 *  which is what the OpenAI wire format uses). */
function _parseToolArgs(tc) {
    const raw = tc.arguments ?? (tc.function || {}).arguments;
    if (raw == null) return null;
    if (typeof raw === 'object') return raw;
    try {
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === 'object' ? parsed : null;
    } catch { return null; }
}

/** tool_call_id → {name, args} for one page of rows. The ARGUMENTS matter as
 *  much as the name: appendToolToGroup builds its one-line summary ("$ ls -la",
 *  the file path) from them, so a replayed transcript without them fell back to
 *  the raw output tail and read nothing like the live view of the same turn. */
function _toolCallIndex(messages) {
    const map = {};
    for (const m of messages) {
        if (m.role !== 'assistant' || !m.tool_calls) continue;
        try {
            const tcs = typeof m.tool_calls === 'string' ? JSON.parse(m.tool_calls) : m.tool_calls;
            for (const tc of (Array.isArray(tcs) ? tcs : [])) {
                const id = tc.id || '';
                // Handle both formats: {name: "..."} and {function: {name: "..."}}
                const name = tc.name || (tc.function || {}).name || '';
                if (id && name) map[id] = { name, args: _parseToolArgs(tc) };
            }
        } catch { /* skip malformed tool_calls */ }
    }
    return map;
}

/**
 * Render one page of persisted rows.
 *
 * With `container`, the page is built into that detached node instead of the
 * live transcript, and every piece of cross-message render state (the open
 * tool group, the gap-divider clock) is saved and put back afterwards. That
 * is what lets an earlier page be assembled without touching — or even
 * reading — the messages already on screen.
 */
function _replayMessages(messages, { container = null } = {}) {
    const prevTarget = _renderTarget;
    const prevGroup = _toolGroup;
    const prevCount = _toolGroupCount;
    const prevErrors = _toolGroupErrors;
    const prevLatency = _toolGroupLatency;
    const prevRunning = _toolGroupRunning;
    const prevTs = _lastMsgTs;
    if (container) {
        _renderTarget = container;
        _toolGroup = null;
        _toolGroupCount = 0;
        _toolGroupErrors = 0;
        _toolGroupLatency = 0;
        _toolGroupRunning = 0;
        _lastMsgTs = 0;
    }
    const toolNameMap = _toolCallIndex(messages);
    _replayingTranscript = true;
    try {
        for (const m of messages) {
            if (m.role === 'compaction') continue;
            if (m.role === 'scout') {
                // Render persisted scout report
                closeToolGroup();
                try {
                    const scoutData = JSON.parse(m.content);
                    renderScoutReport(scoutData);
                } catch { /* skip malformed scout data */ }
                continue;
            }
            if (m.role === 'reflect') {
                closeToolGroup();
                try {
                    const reflectData = JSON.parse(m.content);
                    renderReflectCard(reflectData);
                } catch { /* skip malformed reflect data */ }
                continue;
            }
            if (m.role === 'model_divider') {
                // Persisted mid-turn model switch — replay the pill divider.
                closeToolGroup();
                try {
                    const info = m.metadata ? JSON.parse(m.metadata) : {};
                    renderModelDivider(info);
                } catch { /* skip malformed divider metadata */ }
                continue;
            }
            if (m.role === 'eval') {
                closeToolGroup();
                try {
                    const evalData = JSON.parse(m.content);
                    // Two different producers share role='eval': the feature
                    // judge ({results, all_passed}) and the deterministic gate
                    // runner ({kind:'gate', attempt, gates}). Dispatch on the
                    // row's own kind — handing a gate row to renderEvalCard
                    // rendered it as a red "fail — eval — 0/0 passed", because
                    // it finds no `results` array and no `all_passed`. Every
                    // gate row on the reference deployment was a PASS shown as
                    // a failure.
                    if (evalData && evalData.kind === 'gate') {
                        renderGateCard(evalData);
                    } else {
                        renderEvalCard(evalData);
                    }
                } catch { /* skip malformed eval data */ }
                continue;
            }
            // A tool-round assistant row carries the tool_calls and no text.
            // Rendering it appended a bare "ASSISTANT" bubble with nothing in
            // it AND closed the open tool group, so one round of five tools
            // replayed as five one-item groups with an empty bubble between
            // each. The live path folds these away; this is that fold.
            if (m.role === 'assistant' && !(m.content || '').trim()) continue;
            if (m.role === 'tool') {
                const content = m.content || '';
                const preview = content.slice(0, 300);
                const info = toolNameMap[m.tool_call_id] || null;
                const toolName = (info && info.name) || m.tool_call_id || '';
                const meta = _parseRowMetadata(m.metadata);
                const latency = meta.latency_ms ?? m.latency_ms ?? 0;
                appendToolToGroup(
                    toolName, preview, content, content.length > 300,
                    !!meta.was_error, Number(latency) || 0, info ? info.args : null,
                    false, null, _toolItemKey(m.tool_call_id, m.id),
                );
            } else if (m.role === 'user' && (m.content || '').startsWith('[User answered your question]')) {
                closeToolGroup();
                renderAnsweredQuestion(m.content);
            } else if (m.role === 'user' && (m.content || '').startsWith('[User dismissed your question')) {
                closeToolGroup();
                renderDismissedQuestion(m.content);
            } else if (m.role === 'notice') {
                // Persisted notices (cancellations, reflect-skipped, queue-dropped, etc.)
                // render with system-message styling so they're visible but unobtrusive,
                // matching how the live SSE handlers display the same events.
                closeToolGroup();
                appendMessage('system', m.content || '', { createdAt: m.created_at });
            } else {
                closeToolGroup();
                appendMessage(m.role, m.content, {
                    createdAt: m.created_at,
                    messageId: m.id,
                    // The assistant chip's two facts: which model answered and
                    // what it cost. Metadata first, the column as the fallback
                    // for rows written before the metadata carried it.
                    metadata: m.metadata,
                    latencyMs: m.latency_ms,
                });
            }
        }
        closeToolGroup();
    } finally {
        _replayingTranscript = false;
        if (container) {
            _renderTarget = prevTarget;
            _toolGroup = prevGroup;
            _toolGroupCount = prevCount;
            _toolGroupErrors = prevErrors;
            _toolGroupLatency = prevLatency;
            _toolGroupRunning = prevRunning;
            _lastMsgTs = prevTs;
        }
    }
}

/** The "load earlier" control lives at the very top of the transcript and is
 *  updated in place — it is the one node a prepend must not push down. */
function _renderLoadEarlier(sid) {
    const inner = document.getElementById('messages-inner');
    if (!inner) return null;
    let btn = inner.querySelector('.load-earlier-btn');
    if (!_historyHasMore) {
        if (btn) btn.remove();
        return null;
    }
    const remaining = Math.max(0, _historyTotal - _historyLoaded);
    const label = remaining
        ? `Load earlier messages (${remaining} more)`
        : 'Load earlier messages';
    if (!btn) {
        // The stylesheet only carries this control's placement — its skin is
        // the shared secondary button, which it was never actually given.
        btn = el('button', { class: 'btn btn--secondary load-earlier-btn', type: 'button' }, [text(label)]);
        btn.addEventListener('click', () => _loadEarlier(sid, btn));
        inner.insertBefore(btn, inner.firstChild);
    } else {
        btn.disabled = false;
        btn.textContent = label;
    }
    return btn;
}

/**
 * Fetch the page BEHIND what is on screen and prepend it.
 *
 * Nothing already rendered is touched: the fetch asks only for rows older
 * than the current cursor, the page is built detached, and the reader's place
 * is held by measuring one node that was already visible and putting it back
 * where it was.
 */
async function _loadEarlier(sid, btn) {
    if (!_historyHasMore || _oldestMsgId == null) return;
    const inner = document.getElementById('messages-inner');
    const scroll = _messagesScroll();
    if (!inner || !btn) return;
    btn.disabled = true;
    btn.textContent = 'Loading…';
    let data;
    try {
        data = await get(`/api/sessions/${sid}?limit=${HISTORY_PAGE}&before_id=${_oldestMsgId}`);
    } catch (e) {
        // A dead fetch must leave the button usable — this is the only way
        // back to the rest of the conversation.
        btn.disabled = false;
        btn.textContent = `Couldn't load earlier messages (${humanizeError(e)}) — retry`;
        return;
    }
    // The reader switched sessions while the page was in flight; that
    // transcript is gone and this page belongs to nothing.
    if (sid !== state.sid || !btn.isConnected) return;
    const messages = data.messages || [];
    if (!messages.length) {
        _historyHasMore = false;
        btn.remove();
        return;
    }
    // Distance-from-top of the first node that was already on screen. After
    // the prepend it has to sit exactly where it sat before.
    const anchor = btn.nextElementSibling;
    const beforeTop = anchor ? anchor.getBoundingClientRect().top : 0;

    const frag = document.createDocumentFragment();
    _replayMessages(messages, { container: frag });
    inner.insertBefore(frag, btn.nextSibling);

    _oldestMsgId = messages[0].id ?? _oldestMsgId;
    _historyLoaded += messages.length;
    if (typeof data.total_messages === 'number') _historyTotal = data.total_messages;
    _historyHasMore = !!data.has_more;
    _renderLoadEarlier(sid);

    if (anchor && scroll) {
        scroll.scrollTop += anchor.getBoundingClientRect().top - beforeTop;
    }
    _updateScrollPin();
    announce(`${messages.length} earlier messages loaded`);
}

async function loadMessages(sid, { keepScroll = false } = {}) {
    const mySeq = _selectSeq;
    const inner = _messagesInner();
    const scroll = _messagesScroll();
    // Anchor to distance-from-bottom so a soft reload keeps the reader's
    // place instead of dumping them at the end of the transcript.
    const prevBottomDist = keepScroll ? (scroll.scrollHeight - scroll.scrollTop) : null;
    clear(inner);
    _questionBubbles.clear();
    _lastMsgTs = 0;  // gap dividers restart per render
    _oldestMsgId = null;
    _historyHasMore = false;
    _historyTotal = 0;
    _historyLoaded = 0;
    // An empty pane between clicking a session and its transcript arriving
    // reads as "this session has nothing in it" — on a slow link that lie can
    // last a second or more.
    const loadingRow = el('div', { class: 'messages-loading' }, [
        el('span', { class: 'messages-loading-dot', 'aria-hidden': 'true' }),
        text('Loading conversation…'),
    ]);
    inner.appendChild(loadingRow);
    try {
        const data = await get(`/api/sessions/${sid}?limit=${HISTORY_PAGE}`);
        loadingRow.remove();
        // A newer selectSession already cleared and re-rendered this
        // container; appending now would interleave two transcripts.
        if (mySeq !== _selectSeq) return;
        const messages = data.messages || [];
        if (messages.length === 0) {
            showEmptyState();
            return;
        }
        _oldestMsgId = messages[0].id ?? null;
        _historyTotal = data.total_messages || messages.length;
        _historyLoaded = messages.length;
        _historyHasMore = !!data.has_more && _oldestMsgId != null;
        _renderLoadEarlier(sid);
        _replayMessages(messages);
        _markPendingQueued(sid);
        if (prevBottomDist !== null) {
            scroll.scrollTop = scroll.scrollHeight - prevBottomDist;
            _updateScrollPin();
        } else {
            scrollToBottom(true);
        }
    } catch (e) {
        loadingRow.remove();
        appendMessage('system', `Error loading messages: ${e.message}`);
    }
}

function _resetContextReadout() {
    const ctxEl = document.getElementById('status-ctx');
    if (!ctxEl) return;
    ctxEl.textContent = '';
    ctxEl.title = '';
    ctxEl.classList.remove('ctx-healthy', 'ctx-approaching', 'ctx-critical');
}

async function loadContextInfo(sid) {
    try {
        const data = await get(`/api/context/${sid}`);
        const pct = data.utilization_pct ?? 0;
        const compactions = data.compaction_count || 0;
        const status = data.status || 'healthy';
        state.ctxPct = pct;
        const el = document.getElementById('status-ctx');
        const badge = compactions > 0 ? ` · ⟳${compactions}` : '';
        el.textContent = `ctx: ${pct}%${badge}`;
        el.classList.remove('ctx-healthy', 'ctx-approaching', 'ctx-critical');
        el.classList.add(`ctx-${status}`);
        const thresholds = data.thresholds || {};
        const softPct = Math.round((thresholds.compaction || 0.75) * 100);
        const critPct = Math.round((thresholds.critical || 0.85) * 100);
        let title = `history: ${data.history_pct ?? 0}% of ${data.history_budget ?? 0} ` +
                    `(compacts at ${softPct}%, critical at ${critPct}%). ` +
                    `Compactions this session: ${compactions}.`;
        // Session spend, from the enriched list already in memory.
        const sess = (state.sessions || []).find(s => s.id === sid);
        if (sess && (sess.total_tokens || sess.total_cost)) {
            title += ` Usage: ${(sess.total_tokens || 0).toLocaleString()} tokens`;
            if (sess.total_cost >= 0.005) title += ` (~$${sess.total_cost.toFixed(2)})`;
            title += '.';
        }
        // Prompt-cache hit rate plus the autonomy substrate (plan §12.6):
        // active goal, gates, live kernel.
        //
        // Issued together, not one after another. Awaited in series these four
        // added four sequential round-trips to every turn.complete — and all
        // of it only decorates a tooltip, so it must never be the reason the
        // status bar lags behind the turn. Each is caught on its own: a
        // subsystem that is off or an endpoint an older server lacks should
        // cost its own line, not the other three.
        const [usage, goalRes, gatesRes, kernel] = await Promise.all([
            get(`/api/usage/${sid}`).catch(() => null),
            get(`/api/sessions/${sid}/goal`).catch(() => null),
            get(`/api/sessions/${sid}/gates`).catch(() => null),
            get('/api/kernel/status').catch(() => null),
        ]);
        if (usage && usage.cache_read > 0 && usage.prompt > 0) {
            const pct = Math.round((usage.cache_read / usage.prompt) * 100);
            title += ` Cache: ${usage.cache_read.toLocaleString()} prompt tokens read from cache (${pct}%).`;
        }
        // Cache writes = breakpoints being PLACED (plan 1b). Writes with
        // no reads means the breakpoints land on unstable bytes.
        if (usage && usage.cache_write > 0) {
            title += ` ${usage.cache_write.toLocaleString()} written to cache.`;
        }
        if (goalRes && goalRes.goal) {
            const gl = goalRes.goal;
            let goalLine = ` Goal: "${(gl.objective || '').slice(0, 60)}" — ${gl.continuations_used || 0}/${gl.continuation_budget || 0} continuations`;
            if (gl.token_budget) goalLine += `, ${(gl.tokens_used || 0).toLocaleString()}/${gl.token_budget.toLocaleString()} tokens`;
            title += goalLine + '.';
        }
        if (gatesRes && gatesRes.gates && gatesRes.gates.length) {
            title += ` ${gatesRes.gates.length} deterministic gate${gatesRes.gates.length === 1 ? '' : 's'} active.`;
        }
        if (kernel && kernel.enabled && kernel.alive > 0) {
            title += ` Kernel: ${kernel.alive}/${kernel.max} live.`;
        }
        el.title = title;
    } catch {}
}

function setupNewSession() {
    document.getElementById('new-session-btn').addEventListener('click', async () => {
        try {
            const data = await post('/api/sessions', { title: 'New session' });
            await loadSessions();
            selectSession(data.session_id);
        } catch (e) {
            console.error('Failed to create session:', e);
        }
    });
}

// ---------------------------------------------------------------------------
// Session header — which conversation am I in, and in which space
// ---------------------------------------------------------------------------

function _renderSessionHeader() {
    const header = document.getElementById('session-header');
    if (!header) return;
    const titleBtn = document.getElementById('session-header-title');
    const chip = document.getElementById('session-header-space');
    const sess = (state.sessions || []).find((x) => x.id === state.sid);
    if (!state.sid || !sess || !titleBtn) {
        header.hidden = true;
        return;
    }
    header.hidden = false;
    const title = sess.title || 'New session';
    titleBtn.textContent = title;
    titleBtn.title = `${title} — click to rename`;
    titleBtn.setAttribute('aria-label', `Session: ${title}. Click to rename.`);

    const space = sess.space_id ? (state.spaces || []).find((sp) => sp.id === sess.space_id) : null;
    if (chip) {
        if (space) {
            chip.hidden = false;
            chip.style.setProperty('--space-color', space.color || 'var(--accent)');
            chip.textContent = space.label || '';
            chip.title = `Space: ${space.label || ''}`;
        } else {
            chip.hidden = true;
            chip.textContent = '';
        }
    }
    _renderParentBreadcrumb(header, sess);
    // The tab title is the other half of wayfinding: five Pernix tabs all
    // called "Pernix" are indistinguishable in a browser's tab strip.
    document.title = `${title} · Pernix`;
}

/**
 * "← Parent: <title>" for a worker or an RLM trace view.
 *
 * A child session opened from a chip or a notification is a transcript with
 * no context: it says what it is doing but not what it is FOR, and the only
 * way back was to find its parent in the sidebar by eye. Both kinds carry
 * parent_session_id on their payload; this is that field made clickable.
 */
function _renderParentBreadcrumb(header, sess) {
    const isChild = sess.session_type === 'worker' || sess.session_type === 'rlm';
    const pid = isChild ? (sess.parent_session_id || '') : '';
    let crumb = document.getElementById('session-header-parent');
    if (!pid) {
        if (crumb) crumb.remove();
        return;
    }
    const parent = (state.sessions || []).find((x) => x.id === pid);
    // A parent past the loaded window still gets a working link — the label
    // is the only thing that degrades.
    const parentTitle = (parent && parent.title) || 'parent session';
    if (!crumb) {
        crumb = el('button', {
            id: 'session-header-parent',
            class: 'session-header-parent',
            type: 'button',
        });
        crumb.addEventListener('click', (e) => {
            e.stopPropagation();
            const target = crumb.dataset.parent;
            if (target) selectSession(target);
        });
        header.insertBefore(crumb, header.firstChild);
    }
    crumb.dataset.parent = pid;
    clear(crumb);
    crumb.appendChild(icon('arrow-left', { size: 11 }));
    crumb.appendChild(el('span', {}, [text(`Parent: ${parentTitle}`)]));
    crumb.title = `Open the parent session — ${parentTitle}`;
    crumb.setAttribute('aria-label', `Open the parent session: ${parentTitle}`);
}

/**
 * Rename in place from the header. The PATCH is not issued here — a
 * 'pernix:rename-session' event is, so the sidebar (which owns the other
 * rename affordance) and anything else interested see one path.
 */
function _startHeaderRename() {
    const header = document.getElementById('session-header');
    const titleBtn = document.getElementById('session-header-title');
    if (!header || !titleBtn || !titleBtn.isConnected) return;
    const sess = (state.sessions || []).find((x) => x.id === state.sid);
    const current = (sess && sess.title) || '';

    const input = el('input', {
        id: 'session-header-rename',
        type: 'text',
        value: current,
        'aria-label': 'Session title',
    });

    let finished = false;
    const finish = (save) => {
        if (finished) return;
        finished = true;
        const next = input.value.trim();
        input.replaceWith(titleBtn);
        if (save && next && next !== current) {
            window.dispatchEvent(new CustomEvent('pernix:rename-session', {
                detail: { sid: state.sid, title: next },
            }));
        }
        titleBtn.focus();
    };

    input.addEventListener('keydown', (e) => {
        e.stopPropagation();          // Ctrl+K / Ctrl+F must not fire while typing a title
        if (e.key === 'Enter') { e.preventDefault(); finish(true); }
        else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));

    titleBtn.replaceWith(input);
    input.focus();
    input.select();
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

let _sigilStop = null;
function showEmptyState() {
    // The hero screen is its own wayfinding; a header above it would name a
    // session with nothing in it.
    const header = document.getElementById('session-header');
    if (header) header.hidden = true;
    document.title = 'Pernix';
    const inner = _messagesInner();
    if (_sigilStop) { _sigilStop(); _sigilStop = null; }
    clear(inner);
    // Hero-style layout: animated sigil canvas as background, "Pernix" title
    // overlaid in the center (gradient gold serif from the website),
    // kicker etymology below the figure, then the help text.
    const sigil = el('canvas', { class: 'empty-state-sigil', 'aria-hidden': 'true' });
    const title = el('h2', { class: 'empty-state-title' }, [
        el('span', { class: 'empty-state-title-line' }, [text('Pernix')]),
    ]);
    const figure = el('div', { class: 'empty-state-figure' }, [sigil, title]);
    const kicker = el('p', { class: 'empty-state-kicker' }, [
        el('em', {}, [text('per·nix')]),
        text('  '),
        el('span', { class: 'ipa' }, [text('/ˈpɛɾ.nɪks/')]),
        text('  '),
        el('span', { class: 'def' }, [text('— Latin: nimble, swift of foot.')]),
    ]);
    const help = el('p', { class: 'empty-state-help' }, [
        text('Start a conversation or drop a file to begin. '),
        el('br'),
        text('Research questions, scheduled jobs, file work, reminders — '),
        el('br'),
        text('it remembers across sessions and can work while you’re away.'),
        el('br'),
        el('br'),
        text('Type '),
        el('kbd', {}, [text('/help')]),
        text(' for commands and capabilities.'),
    ]);
    const welcome = el('div', { class: 'empty-state' }, [figure, kicker, help]);
    // First-run guidance: with no model configured, a sent message dies in
    // scout → LLM with a cryptic provider error. Point at Settings instead.
    if (!state.model || state.model === '(not set)') {
        welcome.appendChild(el('div', { class: 'empty-state-setup' }, [
            el('p', {}, [text('No model is configured yet — Pernix needs one before it can respond.')]),
            el('button', { class: 'btn btn-primary', onClick: openSettings }, [text('Open Settings')]),
            el('p', { class: 'setup-hint' }, [
                text('Set a model (and an API key for OpenRouter, or a local Ollama URL) under Settings → Models.'),
            ]),
        ]));
    }
    inner.appendChild(welcome);
    // Init after attach so getBoundingClientRect() reflects laid-out size.
    _sigilStop = initSigil(sigil);
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

const SLASH_COMMANDS = {
    '/new': () => document.getElementById('new-session-btn').click(),
    '/clear': async () => {
        if (!state.sid) return appendMessage('system', 'No active session');
        // Destructive and un-undoable — require an explicit second step.
        // window.confirm() is unstyled, unreadable on a phone, and blocks the
        // event loop mid-stream; the shared dialog is none of those. (N6)
        const ok = await confirmDanger({
            title: 'Clear this conversation?',
            body: [
                'Every message in this session is deleted from the database.',
                'The session itself stays; only its transcript goes. This cannot be undone.',
            ],
            verb: 'Clear',
            cancelLabel: 'Keep',
        });
        if (!ok) return;
        await post(`/api/sessions/${state.sid}/clear`);
        loadMessages(state.sid);
    },
    '/status': async () => {
        if (!state.sid) return appendMessage('system', 'No active session');
        const s = await get(`/api/sessions/${state.sid}/status`);
        const badge = s.error ? 'error'
            : (s.status === 'processing' || s.status === 'scouting') ? 'processing'
            : 'idle';
        const rows = [
            { key: 'Status', value: s.status, badge },
            { key: 'Type', value: s.session_type || 'normal' },
            { key: 'Idle', value: `${s.idle_seconds}s` },
            { key: 'Pending', value: String(s.pending_messages) },
        ];
        if (s.worker_ids && s.worker_ids.length)
            rows.push({ key: 'Workers', value: s.worker_ids.join(', ') });
        if (s.has_background_tasks)
            rows.push({ key: 'Background', value: 'active', badge: 'processing' });
        if (s.waiting_for_input)
            rows.push({ key: 'Waiting', value: 'for user input', badge: 'idle' });
        if (s.error)
            rows.push({ key: 'Error', value: s.error });
        appendCommandCard('Status', rows);
    },
    '/compact': async () => {
        if (!state.sid) return appendMessage('system', 'No active session');
        appendMessage('system', 'Compacting...');
        const r = await post(`/api/compact/${state.sid}`);
        const badge = r.compacted ? 'success' : 'idle';
        appendCommandCard('Compact', [
            { key: 'Result', value: r.compacted ? 'Context compacted' : 'Nothing to compact', badge },
        ]);
    },
    '/cancel': async () => {
        if (!state.sid) return appendMessage('system', 'No active session');
        await post(`/api/sessions/${state.sid}/cancel`);
        appendCommandCard('Cancel', [
            { key: 'Result', value: 'Session cancelled', badge: 'success' },
        ]);
    },
    '/retry': async () => {
        if (!state.sid) return appendMessage('system', 'No active session');
        await post(`/api/retry/${state.sid}`);
        appendCommandCard('Retry', [
            { key: 'Result', value: 'Retrying from last message', badge: 'processing' },
        ]);
    },
    '/help': () => {
        const cmds = [
            ['/new',     'Start a new session'],
            ['/clear',   'Clear session messages'],
            ['/status',  'Show session status'],
            ['/compact', 'Compact context window'],
            ['/cancel',  'Cancel active processing'],
            ['/retry',   'Retry from last message'],
            ['/help',    'Show this list'],
        ];
        const list = el('ul', { class: 'cmd-help-list' });
        for (const [cmd, desc] of cmds) {
            list.appendChild(el('li', {}, [
                el('kbd', {}, [text(cmd)]),
                el('span', { class: 'cmd-desc' }, [text(desc)]),
            ]));
        }
        // Capabilities section — these all exist but were invisible: /help
        // only listed session commands, so new users never found scheduling,
        // skills, memory, or attachments.
        const caps = [
            ['Attach files', 'paperclip button, drag & drop, or paste a copied screenshot/file — images and audio go to the model'],
            ['Voice input', 'mic button dictates or records for the model — enable an engine in Settings → Voice Input'],
            ['Ctrl+Shift+M', 'tap to toggle the mic, hold for push-to-talk (mic button works the same); Esc cancels'],
            ['Schedule jobs', 'ask in chat ("every morning at 9, …") or Explorer 📁 → Jobs'],
            ['Skills', 'domain expertise packages the agent loads on demand — Explorer 📁 → Skills'],
            ['Memory', 'the agent remembers across sessions; just ask it to remember/recall things'],
            ['Workers', 'the agent can fan work out to parallel sub-agents for big tasks'],
            ['Search sessions', 'search box at the top of the sidebar finds any past conversation'],
            ['Ctrl+F', 'search within this transcript'],
            ['Ctrl+K', 'jump to another session'],
            ['↑ in empty input', 'recall previous prompts'],
            ['Model badge', 'click the model name in the status bar to override the model for this session'],
        ];
        const capList = el('ul', { class: 'cmd-help-list' });
        for (const [name, desc] of caps) {
            capList.appendChild(el('li', {}, [
                el('kbd', {}, [text(name)]),
                el('span', { class: 'cmd-desc' }, [text(desc)]),
            ]));
        }
        const wrapper = el('div', {}, [
            list,
            el('div', { class: 'cmd-help-subhead' }, [text('What Pernix can do')]),
            capList,
        ]);
        appendCommandCard('Commands', null, wrapper);
    },
};

// ---------------------------------------------------------------------------
// Input drafts (per-session, localStorage) + prompt history (ArrowUp recall)
// ---------------------------------------------------------------------------

const _DRAFT_PREFIX = 'pernix:draft:';
const _PROMPT_HISTORY_KEY = 'pernix:prompt-history';
const _PROMPT_HISTORY_MAX = 50;
let _draftTimer = null;
let _pendingDraftWrite = null;   // the debounced write, so it can be flushed
let _histIdx = -1;        // -1 = not navigating history
let _histStash = '';      // text that was in the input when navigation started

function _draftKey() { return _DRAFT_PREFIX + (state.sid || 'new'); }

function _saveDraft(value) {
    clearTimeout(_draftTimer);
    // The key is bound NOW, not 300ms from now. _draftKey() reads state.sid,
    // and switching session inside the debounce window filed the text you had
    // typed in the OLD session under the NEW session's key — so the draft
    // vanished from where you left it and appeared where you had never typed.
    const key = _draftKey();
    _pendingDraftWrite = () => {
        try {
            if (value.trim()) localStorage.setItem(key, value);
            else localStorage.removeItem(key);
        } catch { /* storage full/unavailable */ }
    };
    _draftTimer = setTimeout(() => {
        _draftTimer = null;
        const write = _pendingDraftWrite;
        _pendingDraftWrite = null;
        if (write) write();
    }, 300);
}

/** Write a pending debounced draft out immediately, under the key it was
 *  captured with. Called before a session switch reads the new one. */
function _flushDraft() {
    if (!_draftTimer) return;
    clearTimeout(_draftTimer);
    _draftTimer = null;
    const write = _pendingDraftWrite;
    _pendingDraftWrite = null;
    if (write) write();
}

function _restoreDraft() {
    const textarea = document.getElementById('msg-input');
    if (!textarea) return;
    let draft = '';
    try { draft = localStorage.getItem(_draftKey()) || ''; } catch { /* unavailable */ }
    textarea.value = draft;
    textarea.dispatchEvent(new Event('input'));
}

function _clearDraft() {
    clearTimeout(_draftTimer);
    _draftTimer = null;
    _pendingDraftWrite = null;   // a queued write would resurrect what we just cleared
    try { localStorage.removeItem(_draftKey()); } catch { /* unavailable */ }
}

function _loadPromptHistory() {
    try { return JSON.parse(localStorage.getItem(_PROMPT_HISTORY_KEY) || '[]'); }
    catch { return []; }
}

function _pushPromptHistory(message) {
    if (!message || !message.trim()) return;
    const hist = _loadPromptHistory();
    if (hist[hist.length - 1] === message) return;  // skip immediate dupes
    hist.push(message);
    try {
        localStorage.setItem(_PROMPT_HISTORY_KEY, JSON.stringify(hist.slice(-_PROMPT_HISTORY_MAX)));
    } catch { /* storage full/unavailable */ }
}

function _navigateHistory(textarea, dir) {
    const hist = _loadPromptHistory();
    if (!hist.length) return false;
    if (_histIdx === -1) {
        if (dir > 0) return false;          // ArrowDown with no navigation active
        _histStash = textarea.value;
        _histIdx = hist.length - 1;
    } else {
        const next = _histIdx + dir;
        if (next >= hist.length) {           // walked past the newest → restore stash
            _histIdx = -1;
            textarea.value = _histStash;
            textarea.dispatchEvent(new Event('input'));
            return true;
        }
        if (next < 0) return true;           // already at the oldest
        _histIdx = next;
    }
    textarea.value = hist[_histIdx];
    textarea.dispatchEvent(new Event('input'));
    // Cursor to end
    textarea.selectionStart = textarea.selectionEnd = textarea.value.length;
    return true;
}

function setupInput() {
    const textarea = document.getElementById('msg-input');
    const btn = document.getElementById('send-btn');

    btn.addEventListener('click', () => {
        if (btn.classList.contains('stop-mode')) {
            _cancelSession();
        } else {
            send();
        }
    });
    textarea.addEventListener('keydown', (e) => {
        // On touch, Enter inserts a newline — soft keyboards have no
        // Shift+Enter, so send-on-Enter made multi-line prompts impossible.
        // The send button is the submit action there. (A hardware keyboard on
        // a tablet is left out in the cold by this; T5/D4 is where that lands.)
        if (e.key === 'Enter' && !e.shiftKey && !isTouch()) {
            e.preventDefault();
            send();
            return;
        }
        // Prompt history: ArrowUp recalls previous prompts when the input is
        // empty or already navigating; ArrowDown walks back toward newest.
        // Multi-line drafts keep normal arrow behavior unless navigating.
        if (e.key === 'ArrowUp' && (_histIdx !== -1 || textarea.value === '')) {
            if (_navigateHistory(textarea, -1)) e.preventDefault();
        } else if (e.key === 'ArrowDown' && _histIdx !== -1) {
            if (_navigateHistory(textarea, +1)) e.preventDefault();
        }
    });

    textarea.addEventListener('input', (e) => {
        textarea.style.height = 'auto';
        textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
        // Typing (a real input event, not our synthetic ones from history
        // navigation) ends history navigation and updates the draft.
        if (e.isTrusted) {
            _histIdx = -1;
            _saveDraft(textarea.value);
        }
    });

    // Top chrome is a quarter of a landscape phone and all of it once the
    // keyboard is up (measured: 94px of a 214px visual viewport, leaving the
    // transcript 0px). Focus is the only signal that says the keyboard is
    // coming, so it is what sets the class; the CSS that folds the session
    // header away while it is set is compact-only. (P5)
    textarea.addEventListener('focus', () => document.body.classList.add('composer-focused'));
    textarea.addEventListener('blur', () => document.body.classList.remove('composer-focused'));

    textarea.placeholder = _composerPlaceholder();
    textarea.title = _composerHint();
    // The binding the placeholder no longer has room for. aria-description
    // reaches a screen reader without needing a visible element to point at.
    textarea.setAttribute('aria-description', _composerHint());

    _restoreDraft();
}

// True from the moment a send starts until its POST resolves. Without it a
// second Enter during a slow upload started the whole loop again on the same
// _pendingFiles — uploading every attachment twice and sending two messages.
let _sending = false;

function _setSendingState(sending, label) {
    _sending = sending;
    const btn = document.getElementById('send-btn');
    // _stopPending is checked too: a stop pressed between the upload finishing
    // and the POST resolving must not have its button handed back here.
    if (btn) btn.disabled = sending || _stopPending || !!document.getElementById('msg-input')?.disabled;
    const infoEl = document.getElementById('status-info');
    if (!infoEl) return;
    if (sending && label) infoEl.textContent = label;
    else if (!sending && /^Uploading /.test(infoEl.textContent || '')) infoEl.textContent = '';
}

async function send() {
    if (_sending) return;
    // A dictation session still running would keep writing into the input
    // after we clear it below.
    stopVoice();
    const textarea = document.getElementById('msg-input');
    const message = textarea.value.trim();
    if (!message && _pendingFiles.length === 0) return;

    // Mirror of the server's cap (api/routers/chat.py). Bouncing it here costs
    // nothing; letting it through costs a full upload of the body and comes
    // back as a bare 413.
    if (message.length > MAX_MESSAGE_CHARS) {
        appendMessage('system',
            `That message is ${message.length.toLocaleString()} characters — over the `
            + `${MAX_MESSAGE_CHARS.toLocaleString()} limit. Save it as a file and attach it, `
            + 'or trim it. Your text is still in the composer.');
        return;
    }

    // Slash commands — check before streaming guard so /cancel works mid-stream
    const cmd = Object.keys(SLASH_COMMANDS).find(c => message.startsWith(c));
    if (cmd) {
        textarea.value = '';
        textarea.style.height = 'auto';
        _clearDraft();
        try {
            await SLASH_COMMANDS[cmd]();
        } catch (e) {
            appendMessage('system', `Command failed: ${e.message}`);
        }
        return;
    }

    if (state.streaming) {
        // Inject message into running session — agent picks it up next cycle
        textarea.value = '';
        textarea.style.height = 'auto';
        _clearDraft();
        _pushPromptHistory(message);
        _histIdx = -1;
        await _injectMessage(message);
        return;
    }

    textarea.value = '';
    textarea.style.height = 'auto';
    _clearDraft();
    _pushPromptHistory(message);
    _histIdx = -1;

    if (!state.sid) {
        try {
            const data = await post('/api/sessions', { title: 'New session' });
            state.sid = data.session_id;
            _lastSeq = 0;  // fresh session, seqs start at 1 (see deleteSession)
            await loadSessions();
            connectSSE(state.sid, handleEvent);
        } catch (e) {
            appendMessage('system', `Failed to create session: ${e.message}`);
            return;
        }
    }

    // Upload pending files first (XHR for per-chip progress — a 100MB file
    // on phone Wi-Fi used to look like a hang).
    _setSendingState(true);
    try {
        const uploadedFiles = [];
        if (_pendingFiles.length > 0) {
            let failed = 0;
            let index = 0;
            const total = _pendingFiles.length;
            for (const pf of _pendingFiles) {
                index++;
                if (pf.uploaded && pf.serverName) {
                    uploadedFiles.push(pf.serverName);
                    continue;
                }
                _setSendingState(true, `Uploading ${index}/${total}…`);
                try {
                    pf.uploading = true;
                    const result = await _uploadWithProgress(pf);
                    pf.uploading = false;
                    pf.uploaded = true;
                    pf.serverName = result.filename;
                    uploadedFiles.push(result.filename);
                } catch (e) {
                    failed++;
                    pf.uploading = false;
                    appendMessage('system', `Upload failed: ${pf.name} — ${e.message}`);
                }
            }
            if (failed > 0) {
                // Don't silently send a message missing its attachments — keep
                // the chips (successful ones stay marked uploaded) and restore
                // the text so the user can remove the failed file or retry.
                appendMessage('system', `${failed} upload(s) failed — message not sent. Remove the failed file(s) or try again.`);
                if (!textarea.value) {
                    textarea.value = message;
                    textarea.dispatchEvent(new Event('input'));
                    _saveDraft(message);
                }
                renderFileChips();
                return;
            }
            clearPendingFiles();
        }

        // Build final message with file references
        let finalMessage = message;
        if (uploadedFiles.length > 0) {
            const fileRefs = uploadedFiles.map(f => `[attached: ${f}]`).join(' ');
            finalMessage = finalMessage ? `${finalMessage}\n\n${fileRefs}` : fileRefs;
        }

        // Remove empty state if present
        const emptyEl = document.querySelector('.empty-state');
        if (emptyEl) emptyEl.remove();

        const userBubble = appendMessage('user', finalMessage);
        state.streaming = true;
        _showStopButton();
        _streamingEl = appendMessage('assistant', '');
        _collected = '';
        _toolGroup = null;

        // All events arrive via the persistent SSE connection.
        // POST /api/chat just accepts the message and returns JSON.
        try {
            // Through the shared client, not a bare fetch: api() is what routes
            // a 401 to the login screen and what trips the offline detector on a
            // network failure. A hand-rolled fetch here surfaced an expired
            // session as an inline "failed to send" that no amount of retrying
            // could fix.
            await post('/api/chat', { session_id: state.sid, message: finalMessage });
        } catch (e) {
            appendMessage('system', `Error: ${e.message}`);
            // The optimistic bubble was never persisted — mark it so it doesn't
            // read as sent (it vanishes on reload), restore the text so the user
            // can retry without retyping, and drop the empty assistant ghost.
            if (userBubble) userBubble.classList.add('rejected');
            if (!textarea.value) {
                textarea.value = message;
                textarea.dispatchEvent(new Event('input'));
                _saveDraft(message);
            }
            if (_streamingEl) _streamingEl.remove();
            state.streaming = false;
            _showSendButton();
            _streamingEl = null;
            _toolGroup = null;
        }
    } finally {
        _setSendingState(false);
    }
    // Streaming cleanup happens in handleEvent() on stream.done / stream.error / turn.complete
}

async function _injectMessage(message) {
    if (!state.sid) return;
    const msgEl = appendMessage('user', message);
    msgEl.classList.add('queued');
    _injectedMessages.push(msgEl);
    try {
        await post('/api/chat/inject', { session_id: state.sid, message });
    } catch (e) {
        const idx = _injectedMessages.indexOf(msgEl);
        if (idx !== -1) _injectedMessages.splice(idx, 1);
        msgEl.classList.remove('queued');
        appendMessage('system', `Inject failed: ${e.message}`);
    }
}

// ---------------------------------------------------------------------------
// Queued-message management — queued (not yet picked up) user messages get a
// remove button wired to DELETE /api/sessions/{sid}/pending/{message_id}.
// ---------------------------------------------------------------------------

function _addQueueRemoveButton(msgEl, messageId) {
    if (!msgEl || msgEl.querySelector('.queued-remove')) return;
    msgEl.dataset.messageId = String(messageId);
    const btn = el('button', {
        class: 'queued-remove',
        title: 'Remove from queue (not yet seen by the agent)',
        'aria-label': 'Remove this queued message',
    }, [icon('x', { size: 12 })]);
    btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        btn.disabled = true;
        try {
            await del(`/api/sessions/${state.sid}/pending/${messageId}`);
            // Bubble removal happens on the session.queue_removed event so
            // other open tabs stay in sync too.
        } catch (err) {
            btn.disabled = false;
            appendMessage('system', `Could not remove queued message: ${err.message}`);
        }
    });
    msgEl.appendChild(btn);
}

/** After a transcript render, mark messages still waiting in the queue. */
async function _markPendingQueued(sid) {
    try {
        const data = await get(`/api/sessions/${sid}/pending`);
        for (const p of (data.pending || [])) {
            const msgEl = _messagesInner()?.querySelector(`.message[data-message-id="${p.message_id}"]`);
            if (msgEl) {
                msgEl.classList.add('queued');
                _addQueueRemoveButton(msgEl, p.message_id);
            }
        }
    } catch { /* queue view is best-effort */ }
}

// ---------------------------------------------------------------------------
// File drop
// ---------------------------------------------------------------------------

function setupFileDrop() {
    const app = document.getElementById('app');
    const inputBar = document.getElementById('input-bar');
    let dragDepth = 0;

    // Prevent default on all drag events
    for (const evt of ['dragenter', 'dragover', 'dragleave', 'drop']) {
        app.addEventListener(evt, (e) => { e.preventDefault(); e.stopPropagation(); });
    }

    app.addEventListener('dragenter', () => {
        dragDepth++;
        if (dragDepth === 1) app.classList.add('page-drop-active');
    });

    app.addEventListener('dragleave', () => {
        dragDepth--;
        if (dragDepth === 0) app.classList.remove('page-drop-active');
    });

    app.addEventListener('drop', (e) => {
        dragDepth = 0;
        app.classList.remove('page-drop-active');
        const files = e.dataTransfer?.files;
        if (files && files.length > 0) {
            addPendingFiles(files);
        }
    });

    // Paperclip button → trigger the static hidden file input
    const fileInput = document.getElementById('attach-input');
    const attachBtn = document.getElementById('attach-btn');
    if (fileInput && attachBtn) {
        attachBtn.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', () => {
            if (fileInput.files.length > 0) {
                addPendingFiles(fileInput.files);
            }
            fileInput.value = '';
        });
    }

    // Clipboard paste — a copied screenshot or file becomes an attachment
    // chip, same as drag & drop. Plain text keeps native textarea behavior.
    // Document-level so it works without the input focused, but pastes into
    // OTHER editables (settings fields, Monaco) are left alone.
    document.addEventListener('paste', (e) => {
        const files = Array.from(e.clipboardData?.files || []);
        if (files.length === 0) return;
        const t = e.target;
        const inMsgInput = t && t.id === 'msg-input';
        if (!inMsgInput && t && (t.isContentEditable || t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) {
            return;
        }
        e.preventDefault();
        addPendingFiles(files.map(_nameClipboardFile));
    });
}

// Clipboard image data arrives as a generic "image.png" — every pasted
// screenshot would collide on the same name. Timestamp them; files copied
// from a file manager keep their real names.
function _nameClipboardFile(file) {
    if (file.name && !/^image\.(png|jpe?g|gif|webp)$/i.test(file.name)) return file;
    const ext = (file.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
    const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    return new File([file], `pasted-${ts}.${ext}`, { type: file.type });
}

// Mirror of the server-side cap (api/routers/workspace.py) — reject before
// the whole body is uploaded only to be bounced.
const MAX_UPLOAD_BYTES = 250 * 1024 * 1024;

// Mirror of the per-message cap in api/routers/chat.py (1MB). Python's len()
// counts characters, so this counts characters too.
const MAX_MESSAGE_CHARS = 1_000_000;

function addPendingFiles(fileList) {
    for (const file of fileList) {
        if (file.size > MAX_UPLOAD_BYTES) {
            appendMessage('system', `${file.name} is ${formatSize(file.size)} — over the 250MB upload limit.`);
            continue;
        }
        _pendingFiles.push({
            file,
            name: file.name,
            size: file.size,
            uploading: false,
            uploaded: false,
            serverName: null,
        });
    }
    renderFileChips();
    // Focus the textarea
    document.getElementById('msg-input').focus();
}

/** Upload one pending file via XHR so we get upload progress events. */
function _uploadWithProgress(pf) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/upload');
        const t = getAuthToken();
        if (t) xhr.setRequestHeader('Authorization', `Bearer ${t}`);
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) _setChipProgress(pf, e.loaded / e.total);
        };
        xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) {
                _setChipProgress(pf, 1);
                try { resolve(JSON.parse(xhr.responseText)); }
                catch { reject(new Error('bad server response')); }
            } else {
                let detail = xhr.statusText;
                try { detail = JSON.parse(xhr.responseText).detail || detail; } catch { /* keep statusText */ }
                reject(new Error(detail));
            }
        };
        xhr.onerror = () => reject(new Error('network error'));
        const fd = new FormData();
        fd.append('file', pf.file);
        xhr.send(fd);
    });
}

function _setChipProgress(pf, frac) {
    const idx = _pendingFiles.indexOf(pf);
    const container = document.getElementById('file-chips');
    const chip = container && container.children[idx];
    if (!chip) return;
    let bar = chip.querySelector('.file-chip-bar');
    if (!bar) {
        bar = el('div', { class: 'file-chip-bar' });
        chip.appendChild(bar);
    }
    bar.style.width = `${Math.round(frac * 100)}%`;
    chip.classList.toggle('uploading', frac < 1);
}

function removePendingFile(index) {
    _pendingFiles.splice(index, 1);
    renderFileChips();
}

function clearPendingFiles() {
    _pendingFiles = [];
    renderFileChips();
}

function renderFileChips() {
    const container = document.getElementById('file-chips');
    clear(container);
    _pendingFiles.forEach((pf, i) => {
        const chip = el('div', { class: 'file-chip' }, [
            el('span', { class: 'file-chip-icon' }, [text('\u25A0')]),
            el('span', { class: 'file-chip-name', title: pf.name }, [text(pf.name)]),
            el('span', { class: 'file-chip-size' }, [text(formatSize(pf.size))]),
            el('button', {
                class: 'file-chip-remove',
                title: 'Remove',
                'aria-label': `Remove attachment ${pf.name}`,
                onClick: () => removePendingFile(i),
            }, [text('\u00d7')]),
        ]);
        container.appendChild(chip);
    });
    const attachBtn = document.getElementById('attach-btn');
    if (attachBtn) attachBtn.classList.toggle('has-files', _pendingFiles.length > 0);
}

function formatSize(bytes) {
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)}KB`;
    return `${(bytes / 1048576).toFixed(1)}MB`;
}

// ---------------------------------------------------------------------------
// Screen-reader announcements
// ---------------------------------------------------------------------------
// The transcript is role=region, not role=log, precisely so a streaming
// answer is not narrated token by token. That leaves everything else silent
// too: a turn finishing, an error, a worker dying, the session going idle —
// all of it arrived as text in a region nobody is told changed. These are the
// events worth interrupting for, coalesced so a burst is one sentence.

let _systemAnnounceTimer = null;
let _systemAnnounceQueue = [];
// Set while loadMessages replays a transcript: forty persisted system lines
// are history, not news.
let _replayingTranscript = false;

function _announceSystem(body) {
    if (_replayingTranscript) return;
    const line = String(body || '').replace(/\s+/g, ' ').trim().slice(0, 160);
    if (!line) return;
    _systemAnnounceQueue.push(line);
    clearTimeout(_systemAnnounceTimer);
    _systemAnnounceTimer = setTimeout(() => {
        _systemAnnounceTimer = null;
        const lines = _systemAnnounceQueue;
        _systemAnnounceQueue = [];
        if (!lines.length) return;
        announce(lines.length === 1
            ? lines[0]
            : `${lines[lines.length - 1]} (and ${lines.length - 1} more)`);
    }, 500);
}

let _stateAnnounceTimer = null;
function _announceState(to) {
    clearTimeout(_stateAnnounceTimer);
    _stateAnnounceTimer = setTimeout(() => {
        _stateAnnounceTimer = null;
        announce(`Session ${_STATE_LABELS[to] || to}`);
    }, 700);
}

// ---------------------------------------------------------------------------
// Event handling
// ---------------------------------------------------------------------------

let _streamingEl = null;
// The model that answered the round in progress, when it is not the session's
// own — set by stream.fallback, read by the per-message chip on stream.done.
let _lastStreamModel = null;
let _collected = '';
let _toolGroup = null;
let _toolGroupCount = 0;
let _toolGroupErrors = 0;      // failures in the OPEN group (drives the header)
let _toolGroupLatency = 0;     // summed ms in the open group
let _toolGroupRunning = 0;     // announced-but-unfinished calls in the open group
let _parseTimer = null;
let _activityTimer = null;
let _lastSeq = 0;  // track last processed event seq for dedup on SSE reconnect
// Set while _softReload re-renders the transcript: stream tokens are buffered
// rather than written into a container that is about to be replaced.
let _reloading = false;
let _bufferedDuringReload = '';
let _reconcileTimer = null;
let _toolStatusTimer = null;

// ---------------------------------------------------------------------------
// Tool activity status helpers
// ---------------------------------------------------------------------------

const _TOOL_ICONS = {
    bash:          ['⌗', args => args.command ? '$ ' + args.command.slice(0, 40) : 'bash'],
    shell:         ['⌗', args => args.command ? '$ ' + args.command.slice(0, 40) : 'shell'],
    read_file:     ['◈', args => args.path || args.file_path || 'read'],
    write_file:    ['◈', args => args.path || args.file_path || 'write'],
    file_edit:     ['◈', args => args.path || args.file_path || 'edit'],
    create_file:   ['◈', args => args.path || args.file_path || 'create'],
    delete_file:   ['◈', args => args.path || args.file_path || 'delete'],
    glob:          ['◈', args => args.pattern || args.path || 'glob'],
    grep:          ['⌕', args => args.pattern || args.query || 'grep'],
    search:        ['⌕', args => args.query || args.q || 'search'],
    tavily_search: ['⌕', args => args.query || 'search'],
    remember:      ['◎', () => 'remember'],
    recall:        ['◎', () => 'recall'],
};

/**
 * Incremental render of the streaming response. The old path re-parsed the
 * ENTIRE accumulated buffer with marked on every 100ms tick and rebuilt the
 * whole DOM subtree — O(response length) per tick, quadratic over a long
 * answer, with visible jank past ~10k chars. Instead: blocks behind the last
 * blank-line boundary are parsed ONCE into a stable container (boundary only
 * advances outside an open ``` fence), and only the small active tail is
 * re-parsed per tick. Cross-chunk artifacts (split lists etc.) are cosmetic
 * and transient — the finalize paths (stream.done / tool.call) still do one
 * full clean re-parse of the complete text.
 */
function _renderStreamIncremental(contentEl) {
    let stable = contentEl.querySelector(':scope > .stream-stable');
    let tail = contentEl.querySelector(':scope > .stream-tail');
    if (!stable || !tail) {
        clear(contentEl);
        stable = el('div', { class: 'stream-stable' });
        tail = el('div', { class: 'stream-tail' });
        contentEl.appendChild(stable);
        contentEl.appendChild(tail);
        contentEl._stableLen = 0;
    }
    const done = contentEl._stableLen || 0;
    const boundary = _collected.lastIndexOf('\n\n');
    if (boundary > done) {
        const prefix = _collected.slice(0, boundary);
        const fenceCount = (prefix.match(/```/g) || []).length;
        if (fenceCount % 2 === 0) {  // never freeze the middle of a code block
            const chunk = _collected.slice(done, boundary);
            if (chunk.trim()) {
                stable.appendChild(renderMarkdown(chunk));
                addCopyButtons(stable);
            }
            contentEl._stableLen = boundary;
        }
    }
    clear(tail);
    const tailText = _collected.slice(contentEl._stableLen || 0);
    if (tailText.trim()) tail.appendChild(renderMarkdown(tailText));
}

// ---------------------------------------------------------------------------
// Worker activity strip — live view of the fleet during a fan-out, instead
// of an opaque "awaiting workers" wait. Click a worker to open its session.
// ---------------------------------------------------------------------------
const _activeWorkers = new Map();  // worker_id → { title, kind, startedAt }
// worker_id → { title, kind, reason, endedAt } — recently-finished workers,
// kept so a dead worker has a UI home (and a Resume button) instead of
// vanishing from the strip the moment it stops. Capped, recency-bounded.
const _recentDeadWorkers = new Map();
const _DEAD_WORKER_CAP = 4;
const _DEAD_WORKER_MAX_AGE_MS = 60 * 60 * 1000;  // showing week-old corpses is noise
// run_id → { uiSid, label, iterations, maxIterations, subcalls, startedAt }
const _activeRlmRuns = new Map();
let _workerTicker = null;

/** The readable name of a worker: whatever the event carried, else the strip's
 *  own record, else the session list, else a short id. */
function _workerLabel(workerId, eventTitle) {
    if (eventTitle) return eventTitle;
    const known = _activeWorkers.get(workerId) || _recentDeadWorkers.get(workerId);
    if (known && known.title) return known.title;
    const sess = (state.sessions || []).find((x) => x.id === workerId);
    if (sess && sess.title) return sess.title;
    return String(workerId || '').slice(0, 8);
}

function _addDeadWorker(wid, entry) {
    _recentDeadWorkers.delete(wid);  // re-insert = move to newest position
    _recentDeadWorkers.set(wid, entry);
    while (_recentDeadWorkers.size > _DEAD_WORKER_CAP) {
        _recentDeadWorkers.delete(_recentDeadWorkers.keys().next().value);
    }
}

// The strip is rebuilt every five seconds to move the elapsed clocks on.
// It used to do that with `innerHTML = ''`, which threw away the pause
// button's disabled state mid-request (so a second click could fire the same
// pause again), its tooltip, and any focus a keyboard user had on it. Nodes
// are created once and their text updated in place now; the map is what makes
// a node findable on the next pass.
const _stripNodes = new Map();   // key → { wrap, chip, label, ctl }

/** Put `nodes` in this exact order under `parent`, moving as few as possible
 *  and never recreating one that is already there. */
function _reconcileChildren(parent, nodes) {
    let ref = parent.firstChild;
    for (const node of nodes) {
        if (ref === node) { ref = node.nextSibling; continue; }
        parent.insertBefore(node, ref);
    }
    while (ref) {
        const next = ref.nextSibling;
        ref.remove();
        ref = next;
    }
}

function _elapsedStr(sinceMs) {
    const elapsed = Math.max(0, Math.round((Date.now() - sinceMs) / 1000));
    const mins = Math.floor(elapsed / 60);
    return mins > 0 ? `${mins}m${elapsed % 60}s` : `${elapsed}s`;
}

/** One chip + its control button, created once and reused. */
function _stripChip(key, { chipClass, dotClass, onOpen, ctlLabel, onCtl }) {
    let entry = _stripNodes.get(key);
    if (entry) return entry;
    const label = el('span', { class: 'worker-chip-text' });
    const chip = el('button', { class: chipClass, type: 'button' }, [
        el('span', { class: dotClass }),
        label,
    ]);
    chip.addEventListener('click', onOpen);
    const wrap = el('span', { class: 'worker-chip-wrap' }, [chip]);
    let ctl = null;
    if (onCtl) {
        ctl = el('button', { class: 'worker-chip-ctl', type: 'button' }, [icon(ctlLabel, { size: 11 })]);
        ctl.addEventListener('click', (e) => { e.stopPropagation(); onCtl(ctl); });
        wrap.appendChild(ctl);
    }
    entry = { wrap, chip, label, ctl };
    _stripNodes.set(key, entry);
    return entry;
}

/** Swap a control button's icon without disturbing anything else about it. */
function _setCtlIcon(ctl, name) {
    if (!ctl || ctl.dataset.icon === name) return;
    ctl.dataset.icon = name;
    clear(ctl);
    ctl.appendChild(icon(name, { size: 11 }));
}

// ---------------------------------------------------------------------------
// The strip has two shapes (P3)
// ---------------------------------------------------------------------------
// Chips are a desktop shape: a row of them is one line at 1180px and three
// rows of 16px targets at 390px, which is 75px of a phone spent on a list you
// cannot read and cannot hit. Below 900px the strip is one 44px line that says
// how many of what, and taps through to the worker card in the transcript —
// where the per-worker controls already live at a usable size. Above 900px
// nothing changes.
let _stripMode = null;             // 'chips' | 'summary' — which shape is built
let _stripSummaryBtn = null;       // the compact one-liner, created once

/** The fan-out card this strip is about: the live one, else the last one still
 *  in the transcript. */
function _visibleWorkerCard() {
    if (_workerCard && _workerCard.el.isConnected) return _workerCard.el;
    const cards = _messagesInner()?.querySelectorAll('.worker-card');
    return cards && cards.length ? cards[cards.length - 1] : null;
}

/** Tapping the summary goes to the card, opening it if it is collapsed. */
function _revealWorkerCard() {
    const card = _visibleWorkerCard();
    if (!card) { _openWorkerSheet(); return; }
    if (!card.classList.contains('expanded')) {
        card.classList.add('expanded');
        _syncDisclosure(card.querySelector('.worker-card-header'), true);
    }
    card.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

/**
 * No card to scroll to — a session resumed from the server, or a fan-out whose
 * card has been scrolled out of a trimmed transcript. The sheet is then the
 * only place the per-worker controls exist on a phone, so it carries them:
 * the same endpoints the chips call, one 48px row each.
 */
async function _openWorkerSheet() {
    const items = [];
    const actions = [];      // parallel to items; the id is the index
    const add = (item, run) => {
        items.push({ ...item, id: String(actions.length) });
        actions.push(run);
    };

    for (const [wid, w] of _activeWorkers) {
        const kindTag = w.kind ? `[${w.kind}] ` : '';
        add({
            label: `${w.paused ? 'Resume' : 'Pause'} ${kindTag}${w.title}`,
            hint: w.paused ? 'paused' : _elapsedStr(w.startedAt),
            icon: w.paused ? 'play' : 'pause',
        }, async () => {
            const action = w.paused ? 'resume' : 'pause';
            try {
                await post(`/api/sessions/${state.sid}/workers/${wid}/${action}`, {});
                w.paused = !w.paused;
                _setWorkerRowState(wid, w.paused ? 'paused' : 'running');
            } catch (err) {
                appendMessage('system', `Worker ${action} failed: ${err.message}`);
            }
            _renderWorkerStrip();
        });
    }
    for (const [wid, d] of _recentDeadWorkers) {
        const kindTag = d.kind ? `[${d.kind}] ` : '';
        add({
            label: `Resume ${kindTag}${d.title}`,
            hint: d.reason,
            icon: 'refresh',
        }, async () => {
            try {
                await post(`/api/sessions/${state.sid}/workers/${wid}/resume`, {});
                _recentDeadWorkers.delete(wid);
            } catch (err) {
                appendMessage('system', `Worker resume failed: ${err.message}`);
            }
            _renderWorkerStrip();
        });
    }
    for (const [, r] of _activeRlmRuns) {
        add({
            label: `Watch ${r.label}`,
            hint: `it ${r.iterations}`,
            icon: 'external',
        }, () => { if (r.uiSid) selectSession(r.uiSid); });
    }
    if (!items.length) return;

    const choice = await actionSheet({ title: 'Workers', items });
    if (choice == null) return;
    await actions[Number(choice)]?.();
}

/** The compact shape: "3 workers · 2 running · 1 paused ›" on one 44px line. */
function _renderWorkerSummary(strip) {
    if (!_stripSummaryBtn) {
        const labelEl = el('span', { class: 'worker-strip-summary-label' });
        const btn = el('button', { class: 'worker-strip-summary', type: 'button' }, [
            labelEl,
            icon('chevron-right', { size: 14 }),
        ]);
        btn.addEventListener('click', _revealWorkerCard);
        _stripSummaryBtn = { btn, labelEl };
    }
    const { btn, labelEl } = _stripSummaryBtn;

    // Same counts the chip row spells out, in the order the label already
    // used: workers first, then the RLM runs and the finished tail.
    let paused = 0;
    for (const w of _activeWorkers.values()) if (w.paused) paused++;
    const running = _activeWorkers.size - paused;
    const parts = [];
    if (_activeWorkers.size) parts.push(`${_activeWorkers.size} worker${_activeWorkers.size === 1 ? '' : 's'}`);
    if (running) parts.push(`${running} running`);
    if (paused) parts.push(`${paused} paused`);
    if (_activeRlmRuns.size) parts.push(`${_activeRlmRuns.size} RLM`);
    if (_recentDeadWorkers.size) parts.push(`${_recentDeadWorkers.size} finished`);

    labelEl.textContent = parts.join(' · ');
    btn.title = 'Show the workers in the transcript';
    btn.setAttribute('aria-label', `${parts.join(', ')}. Show the workers in the transcript.`);
    _reconcileChildren(strip, [btn]);
}

function _renderWorkerStrip() {
    const strip = document.getElementById('worker-strip');
    if (!strip) return;
    // Age out stale dead chips before deciding visibility.
    for (const [wid, d] of _recentDeadWorkers) {
        if (Date.now() - d.endedAt > _DEAD_WORKER_MAX_AGE_MS) _recentDeadWorkers.delete(wid);
    }
    if (_activeWorkers.size === 0 && _activeRlmRuns.size === 0 && _recentDeadWorkers.size === 0) {
        strip.hidden = true;
        clear(strip);
        _stripNodes.clear();
        _stripSummaryBtn = null;
        _stripMode = null;
        if (_workerTicker) { clearInterval(_workerTicker); _workerTicker = null; }
        return;
    }
    strip.hidden = false;

    // Swapping shapes leaves nothing of the other behind: a chip node kept in
    // _stripNodes across the flip would be re-inserted by the next pass, and
    // the summary button would sit beside the chips it replaces.
    const mode = isCompact() ? 'summary' : 'chips';
    if (mode !== _stripMode) {
        clear(strip);
        _stripNodes.clear();
        _stripSummaryBtn = null;
        _stripMode = mode;
    }
    if (mode === 'summary') {
        _renderWorkerSummary(strip);
        _updateWorkerCard();
        if (!_workerTicker) _workerTicker = setInterval(_renderWorkerStrip, 5000);
        return;
    }

    let labelEl = strip.querySelector('.worker-strip-label');
    if (!labelEl) labelEl = el('span', { class: 'worker-strip-label' });
    const labelParts = [];
    if (_activeWorkers.size) labelParts.push(`${_activeWorkers.size} worker${_activeWorkers.size === 1 ? '' : 's'}`);
    if (_activeRlmRuns.size) labelParts.push(`${_activeRlmRuns.size} RLM`);
    if (_recentDeadWorkers.size) labelParts.push(`${_recentDeadWorkers.size} finished`);
    labelEl.textContent = labelParts.join(' · ');

    const order = [labelEl];
    const seen = new Set(['__label']);

    for (const [wid, w] of _activeWorkers) {
        const key = `w:${wid}`;
        seen.add(key);
        // Pause/resume the worker without leaving the parent session — the
        // endpoints exist per-worker; this is their first UI affordance.
        const entry = _stripChip(key, {
            chipClass: 'worker-chip',
            dotClass: 'worker-chip-dot',
            onOpen: () => selectSession(wid),
            ctlLabel: 'pause',
            onCtl: async (ctl) => {
                ctl.disabled = true;
                const action = w.paused ? 'resume' : 'pause';
                try {
                    await post(`/api/sessions/${state.sid}/workers/${wid}/${action}`, {});
                    w.paused = !w.paused;
                    _setWorkerRowState(wid, w.paused ? 'paused' : 'running');
                } catch (err) {
                    appendMessage('system', `Worker ${action} failed: ${err.message}`);
                }
                ctl.disabled = false;
                _renderWorkerStrip();
            },
        });
        const kindTag = w.kind ? `[${w.kind}] ` : '';
        entry.chip.classList.toggle('paused', !!w.paused);
        entry.chip.title = `${kindTag}${w.title} — click to open transcript`;
        entry.label.textContent = ` ${kindTag}${w.title.slice(0, 30)} · ${w.paused ? 'paused' : _elapsedStr(w.startedAt)}`;
        if (entry.ctl) {
            _setCtlIcon(entry.ctl, w.paused ? 'play' : 'pause');
            entry.ctl.title = w.paused ? 'Resume this worker' : 'Pause this worker after its current step';
            entry.ctl.setAttribute('aria-label', w.paused
                ? `Resume worker ${w.title}`
                : `Pause worker ${w.title} after its current step`);
        }
        order.push(entry.wrap);
    }

    // Dead-worker chips: dimmed, with the termination reason and a revive
    // button. Reviving fires worker.resumed, which flips the chip back to
    // the active map.
    for (const [wid, d] of _recentDeadWorkers) {
        const key = `d:${wid}`;
        seen.add(key);
        const entry = _stripChip(key, {
            chipClass: 'worker-chip dead',
            dotClass: 'worker-chip-dot dead',
            onOpen: () => selectSession(wid),
            ctlLabel: 'refresh',
            onCtl: async (ctl) => {
                ctl.disabled = true;
                try {
                    await post(`/api/sessions/${state.sid}/workers/${wid}/resume`, {});
                    _recentDeadWorkers.delete(wid);
                } catch (err) {
                    appendMessage('system', `Worker resume failed: ${err.message}`);
                    ctl.disabled = false;
                }
                _renderWorkerStrip();
            },
        });
        const kindTag = d.kind ? `[${d.kind}] ` : '';
        entry.chip.title = `${kindTag}${d.title} — ended: ${d.reason}. Click to open transcript.`;
        entry.label.textContent = ` ${kindTag}${d.title.slice(0, 24)} · ${d.reason}`;
        if (entry.ctl) {
            entry.ctl.title = 'Resume this worker from where it stopped';
            entry.ctl.setAttribute('aria-label', `Resume worker ${d.title} from where it stopped`);
        }
        order.push(entry.wrap);
    }

    // RLM run chips — live iteration/sub-call counters; click opens the
    // run's read-only trace view (its sidebar pseudo-session).
    for (const [rid, r] of _activeRlmRuns) {
        const key = `r:${rid}`;
        seen.add(key);
        const entry = _stripChip(key, {
            chipClass: 'worker-chip rlm-chip',
            dotClass: 'worker-chip-dot rlm',
            onOpen: () => { const cur = _activeRlmRuns.get(rid); if (cur && cur.uiSid) selectSession(cur.uiSid); },
        });
        entry.wrap.dataset.run = rid;
        const iter = r.maxIterations ? `it ${r.iterations}/${r.maxIterations}` : `it ${r.iterations}`;
        entry.chip.title = `${r.label} — click to watch the run`;
        entry.label.textContent = ` RLM · ${iter} · ${r.subcalls} calls · ${_elapsedStr(r.startedAt)}`;
        order.push(entry.wrap);
    }

    for (const [k, n] of _stripNodes) {
        if (!seen.has(k)) { n.wrap.remove(); _stripNodes.delete(k); }
    }
    _reconcileChildren(strip, order);
    _updateWorkerCard();
    if (!_workerTicker) _workerTicker = setInterval(_renderWorkerStrip, 5000);
}

// ---------------------------------------------------------------------------
// Worker lifecycle card — one card per fan-out, updated in place
// ---------------------------------------------------------------------------
// A fan-out of six workers wrote twelve system lines into the transcript
// ("Worker started: …" six times, then "Worker done: …" six times), scattered
// through the answer and through each other. The one question a reader has —
// how many are still going, and did any of them fail? — took counting, and
// the count was only ever correct if you had been watching the whole time.
// One card answers it, keeps answering it, and holds the per-worker controls
// that were previously only in the strip.

let _workerCard = null;   // { el, headerEl, labelEl, rowsEl, rows: Map(wid → row) }

const _WORKER_STATE_LABEL = {
    running: 'running',
    paused: 'paused',
    done: 'done',
    failed: 'failed',
};

function _newWorkerCard() {
    closeToolGroup();
    const labelEl = el('span', { class: 'wc-label' }, [text('Workers')]);
    const headerEl = el('div', { class: 'worker-card-header' }, [
        el('span', { class: 'wc-toggle' }, [icon('chevron-right', { size: 10 })]),
        labelEl,
    ]);
    const rowsEl = el('div', { class: 'wc-rows' });
    const cardEl = el('div', { class: 'worker-card' }, [headerEl, rowsEl]);
    _makeDisclosure(headerEl, false, () => {
        cardEl.classList.toggle('expanded');
        return cardEl.classList.contains('expanded');
    });
    _messagesInner().appendChild(cardEl);
    scrollToBottom();
    _workerCard = { el: cardEl, headerEl, labelEl, rowsEl, rows: new Map() };
    return _workerCard;
}

/** The card a newly-started worker belongs to: the current one while anything
 *  in it is still alive, a fresh one otherwise. With no batch id on the wire,
 *  that boundary is what "one card per fan-out" can mean. */
function _ensureWorkerCard() {
    if (_workerCard && _workerCard.el.isConnected) {
        for (const r of _workerCard.rows.values()) {
            if (r.state === 'running' || r.state === 'paused') return _workerCard;
        }
    }
    return _newWorkerCard();
}

/** The card to record an ENDING in — never a new one for a worker that this
 *  client watched start, and never a card conjured out of nothing. */
function _openWorkerCard() {
    if (_workerCard && _workerCard.el.isConnected) return _workerCard;
    return _newWorkerCard();
}

function _workerCardStart(wid, { title, kind = '', note = '' }) {
    const card = _ensureWorkerCard();
    const row = card.rows.get(wid) || { wid };
    Object.assign(row, {
        title: title || row.title || String(wid).slice(0, 8),
        kind: kind || row.kind || '',
        note,
        state: 'running',
        startedAt: Date.now(),
        endedAt: 0,
        reason: '',
    });
    card.rows.set(wid, row);
    _updateWorkerCard();
}

function _workerCardEnd(wid, { title = '', state = 'done', reason = '', spawnFailed = false }) {
    const card = _openWorkerCard();
    const row = card.rows.get(wid) || { wid, kind: '', startedAt: Date.now() };
    Object.assign(row, {
        title: title || row.title || String(wid).slice(0, 8),
        state,
        reason,
        spawnFailed: spawnFailed || row.spawnFailed || false,
        endedAt: Date.now(),
    });
    card.rows.set(wid, row);
    _updateWorkerCard();
}

/** Keep the card honest when the strip's pause button is what changed. */
function _setWorkerRowState(wid, state) {
    if (!_workerCard) return;
    const row = _workerCard.rows.get(wid);
    if (!row || (row.state !== 'running' && row.state !== 'paused')) return;
    row.state = state;
    _updateWorkerCard();
}

function _workerRowNode(card, row) {
    if (row.el) return row.el;
    const titleEl = el('span', { class: 'wc-row-title' });
    const stateEl = el('span', { class: 'wc-row-state' });
    const timeEl = el('span', { class: 'wc-row-time' });
    const actions = el('span', { class: 'wc-row-actions' });

    const openBtn = el('button', {
        class: 'btn btn--ghost btn--xs', type: 'button',
    }, [text('open')]);
    openBtn.addEventListener('click', (e) => { e.stopPropagation(); selectSession(row.wid); });

    // One button, two jobs: pause/resume while it runs, revive once it has
    // stopped. Same endpoints the strip uses.
    const ctlBtn = el('button', { class: 'btn btn--ghost btn--xs', type: 'button' }, [text('pause')]);
    ctlBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const live = row.state === 'running' || row.state === 'paused';
        const action = live ? (row.state === 'paused' ? 'resume' : 'pause') : 'resume';
        ctlBtn.disabled = true;
        try {
            await post(`/api/sessions/${state.sid}/workers/${row.wid}/${action}`, {});
            if (live) {
                row.state = action === 'pause' ? 'paused' : 'running';
                const w = _activeWorkers.get(row.wid);
                if (w) w.paused = action === 'pause';
            } else {
                row.state = 'running';
                row.startedAt = Date.now();
                _recentDeadWorkers.delete(row.wid);
                _activeWorkers.set(row.wid, {
                    title: row.title, kind: row.kind, startedAt: Date.now(), paused: false,
                });
            }
        } catch (err) {
            appendMessage('system', `Worker ${action} failed: ${err.message}`);
        }
        ctlBtn.disabled = false;
        _renderWorkerStrip();
        _updateWorkerCard();
    });

    actions.appendChild(ctlBtn);
    actions.appendChild(openBtn);
    row.el = el('div', { class: 'wc-row', 'data-worker': String(row.wid) }, [
        el('span', { class: 'wc-row-dot' }),
        titleEl, stateEl, timeEl, actions,
    ]);
    row.nodes = { titleEl, stateEl, timeEl, openBtn, ctlBtn };
    card.rowsEl.appendChild(row.el);
    return row.el;
}

function _updateWorkerCard() {
    const card = _workerCard;
    if (!card || !card.el.isConnected) return;
    const counts = { running: 0, paused: 0, done: 0, failed: 0 };
    for (const row of card.rows.values()) counts[row.state] = (counts[row.state] || 0) + 1;
    const parts = [];
    for (const k of ['running', 'paused', 'done', 'failed']) {
        if (counts[k]) parts.push(`${counts[k]} ${k}`);
    }
    const total = card.rows.size;
    card.labelEl.textContent = parts.length
        ? `Workers · ${parts.join(' · ')}`
        : `Workers · ${total}`;
    card.el.classList.toggle('has-error', counts.failed > 0);
    card.headerEl.setAttribute(
        'aria-label',
        `${total} worker${total === 1 ? '' : 's'}: ${parts.join(', ') || 'none started'}`,
    );

    for (const row of card.rows.values()) {
        _workerRowNode(card, row);
        const { titleEl, stateEl, timeEl, openBtn, ctlBtn } = row.nodes;
        const kindTag = row.kind ? `[${row.kind}] ` : '';
        titleEl.textContent = `${kindTag}${row.title}`;
        titleEl.title = row.note ? `${row.title} — ${row.note}` : row.title;
        const live = row.state === 'running' || row.state === 'paused';
        stateEl.textContent = row.reason
            ? `${_WORKER_STATE_LABEL[row.state] || row.state} · ${row.reason}`
            : (_WORKER_STATE_LABEL[row.state] || row.state);
        stateEl.className = `wc-row-state ${row.state}`;
        timeEl.textContent = live
            ? _elapsedStr(row.startedAt)
            : _humanizeMs(Math.max(0, (row.endedAt || Date.now()) - row.startedAt));
        row.el.classList.toggle('finished', !live);
        row.el.classList.toggle('failed', row.state === 'failed');
        // A worker that never spawned has no transcript and nothing to revive.
        openBtn.hidden = !!row.spawnFailed;
        ctlBtn.hidden = !!row.spawnFailed;
        if (!row.spawnFailed) {
            ctlBtn.textContent = live ? (row.state === 'paused' ? 'resume' : 'pause') : 'revive';
            ctlBtn.title = live
                ? (row.state === 'paused' ? 'Resume this worker' : 'Pause this worker after its current step')
                : 'Resume this worker from where it stopped';
            ctlBtn.setAttribute('aria-label', `${ctlBtn.textContent} worker ${row.title}`);
            openBtn.setAttribute('aria-label', `Open the transcript of worker ${row.title}`);
        }
    }
}

async function _seedWorkerStrip(sid) {
    _activeWorkers.clear();
    _recentDeadWorkers.clear();
    _activeRlmRuns.clear();
    _workerCard = null;   // the old card belongs to a transcript that is gone
    try {
        const data = await get(`/api/sessions/${sid}/workers`);
        const now = Date.now();
        for (const w of (data.workers || [])) {
            // Defensive: only real workers belong in the worker-chip path
            // (RLM view sessions get their own pink chips from /api/rlm/runs).
            if (w.session_type && w.session_type !== 'worker') continue;
            if (w.state === 'idle_ready' || w.state === 'idle') {
                // Recently-finished workers get a dead chip with a Resume
                // button; anything older stays out of the strip.
                const ended = w.updated_at ? (Date.parse(w.updated_at + 'Z') || 0) : 0;
                if (ended && (now - ended) <= _DEAD_WORKER_MAX_AGE_MS) {
                    _addDeadWorker(w.id, {
                        title: w.title,
                        kind: w.kind || '',
                        reason: w.termination_reason || 'done',
                        endedAt: ended,
                    });
                }
                continue;
            }
            const started = w.created_at ? Date.parse(w.created_at + 'Z') || now : now;
            _activeWorkers.set(w.id, {
                title: w.title,
                kind: w.kind || '',
                startedAt: started,
                paused: w.state === 'paused' || w.state === 'pause_requested',
            });
        }
    } catch { /* strip stays empty */ }
    try {
        const rd = await get(`/api/rlm/runs?session_id=${encodeURIComponent(sid)}&limit=8`);
        for (const run of (rd.runs || [])) {
            if (run.status !== 'running' || run.parent_run_id) continue;
            _activeRlmRuns.set(run.run_id, {
                uiSid: run.ui_session_id,
                label: run.task || run.run_id,
                iterations: run.iterations || 0,
                maxIterations: 0,  // caps live in the manifest; the chip shows plain counts until an event arrives
                subcalls: run.subcalls || 0,
                startedAt: run.created_at ? (Date.parse(run.created_at) || Date.now()) : Date.now(),
            });
        }
    } catch { /* chips stay absent */ }
    // Coming back to a session with a fan-out still in flight should show the
    // fan-out, not just a strip of chips above the composer. Only for workers
    // that are actually still going: a card of nothing but corpses is noise.
    if (_activeWorkers.size) {
        for (const [wid, w] of _activeWorkers) {
            _workerCardStart(wid, { title: w.title, kind: w.kind, note: 'already running' });
            const row = _workerCard.rows.get(wid);
            if (row) {
                row.startedAt = w.startedAt;
                if (w.paused) row.state = 'paused';
            }
        }
        _updateWorkerCard();
    }
    _renderWorkerStrip();
}

// ---------------------------------------------------------------------------
// Per-session model override — clicking the model badge opens a picker.
// Persistent for the session (unlike agent-initiated switch_model, which the
// manager reverts at turn end). Backed by PATCH /api/sessions/{sid}.
// ---------------------------------------------------------------------------

let _sessionModelOverride = null;
let _modelMenuEl = null;     // the anchored menu, or the sheet's overlay
let _modelSheetClose = null; // openOverlay teardown — the sheet only

function _renderModelBadge() {
    const mEl = document.getElementById('status-model');
    if (!mEl) return;
    mEl.classList.remove('has-override');
    mEl.textContent = '';
    // A per-SESSION override needs a session. With none open the badge used to
    // look and feel live and then do nothing at all when tapped — the kind of
    // dead control that reads as a broken app rather than an unavailable one.
    // Say so instead, and let it come back the moment a session opens. (P2)
    if (!state.sid) {
        mEl.disabled = true;
        mEl.setAttribute('aria-disabled', 'true');
        mEl.appendChild(text(state.model || '...'));
        mEl.title = 'Open a session to give it its own model';
        mEl.setAttribute('aria-label',
            `Model: ${state.model || 'not set'}. Open a session to change it.`);
        return;
    }
    mEl.disabled = false;
    mEl.removeAttribute('aria-disabled');
    if (_sessionModelOverride) {
        mEl.appendChild(text(_sessionModelOverride));
        const pin = document.createElement('span');
        pin.className = 'model-session-override';
        pin.textContent = ' ●';
        pin.title = `Session override (default: ${state.model})`;
        mEl.appendChild(pin);
        mEl.title = `This session runs on ${_sessionModelOverride} (default: ${state.model}). Click to change.`;
        mEl.setAttribute('aria-label', `Model: ${_sessionModelOverride} (session override). Change model.`);
    } else {
        mEl.appendChild(text(state.model || '...'));
        mEl.title = 'Model for this session — click to override';
        mEl.setAttribute('aria-label', `Model: ${state.model || 'not set'}. Change model.`);
    }
}

function _closeModelMenu() {
    // The sheet's teardown un-inerts the app and puts focus back on the badge.
    // Cleared BEFORE it is called: openOverlay routes Escape through onClose,
    // which is this function, and it must not re-enter its own teardown.
    const closeSheet = _modelSheetClose;
    _modelSheetClose = null;
    if (closeSheet) closeSheet();
    if (_modelMenuEl) {
        _modelMenuEl.remove();
        _modelMenuEl = null;
        document.removeEventListener('click', _closeModelMenu);
    }
    document.getElementById('status-model')?.setAttribute('aria-expanded', 'false');
}

/**
 * One row of the model picker. Two shapes, one behaviour: the anchored menu's
 * plain line, and the sheet's 48px row with the check in a fixed gutter so the
 * labels line up whether or not a row is the current one.
 */
function _modelPickerItem({ label, value, current, hint, sheet }, onPick) {
    const item = sheet
        ? el('button', {
            class: `sheet-item model-sheet-item${current ? ' current' : ''}`,
            type: 'button',
        }, [
            el('span', { class: 'model-sheet-check' }, current ? [icon('check', { size: 16 })] : []),
            el('span', { class: 'sheet-item-label' }, [text(label)]),
            ...(hint ? [el('span', { class: 'sheet-item-hint' }, [text(hint)])] : []),
        ])
        : el('button', {
            class: `model-menu-item${current ? ' current' : ''}`,
            type: 'button',
        }, [text(label)]);
    if (current) item.setAttribute('aria-current', 'true');
    item.addEventListener('click', () => onPick(value));
    return item;
}

async function _openModelMenu() {
    if (_modelMenuEl) { _closeModelMenu(); return; }
    if (!state.sid) return;

    // Below the tablet line the anchored menu is the wrong shape whatever it
    // is anchored to: it is as wide as the longest model id, it hangs off a
    // 19px badge that is most of the status row, and picking from thirty
    // models means dragging a 40vh scroller with a fingertip. A bottom sheet
    // gets the full width, 48px rows, a scrim and Escape. Wide touch and the
    // desktop keep the menu — there is room for it there. (P2)
    const asSheet = isCompact();
    const list = el('div', { class: asSheet ? 'sheet-items model-sheet-list' : 'model-menu-list' });
    const loadingEl = el('div', { class: 'model-menu-loading' }, [text('Loading models…')]);
    let card = null;

    if (asSheet) {
        list.appendChild(loadingEl);
        const cancelBtn = el('button', {
            class: 'btn sheet-cancel', type: 'button', onClick: () => _closeModelMenu(),
        }, [text('Cancel')]);
        card = el('div', {
            class: 'modal-card sheet-card model-sheet', 'data-sheet': 'model',
        }, [
            el('h2', { class: 'sheet-title' }, [text('Model for this session')]),
            list,
            cancelBtn,
        ]);
        const overlay = el('div', { class: 'modal-overlay sheet-overlay' }, [card]);
        // Only a press that STARTED on the backdrop dismisses, so a drag off
        // the card does not close it. Same rule as sheet.js.
        let downOnBackdrop = false;
        overlay.addEventListener('mousedown', (e) => { downOnBackdrop = e.target === overlay; });
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay && downOnBackdrop) _closeModelMenu();
            downOnBackdrop = false;
        });
        _modelMenuEl = overlay;
        document.body.appendChild(overlay);
        const badge = document.getElementById('status-model');
        badge?.setAttribute('aria-expanded', 'true');
        // openOverlay puts focus back wherever it was when it opened, and on
        // iOS a tap does NOT focus the button it lands on — closing the sheet
        // would drop the user at the top of the page. Claim it first so there
        // is something to go back to.
        badge?.focus();
        // The card itself, not Cancel: the rows are not there yet, and landing
        // on the way out is the wrong first thing to hear.
        _modelSheetClose = openOverlay(card, { onClose: _closeModelMenu, initialFocus: card });
    } else {
        const menu = el('div', { id: 'model-menu' }, [
            el('div', { class: 'model-menu-header' }, [text('Model for this session')]),
            loadingEl,
        ]);
        _modelMenuEl = menu;
        document.body.appendChild(menu);
        document.getElementById('status-model')?.setAttribute('aria-expanded', 'true');

        // Anchored to the badge, on whichever side there is room for it. The
        // status bar is at the BOTTOM on the desktop, so a menu hung above it
        // by its `bottom` is the right shape there; touch.css moves the whole
        // bar to the top of the screen, and the same offset then put the
        // menu's bottom edge 13px down the page with the rest of it off the
        // top. Measured -113px on a phone, -94px on a landscape iPad. (P2)
        const badge = document.getElementById('status-model');
        const rect = badge.getBoundingClientRect();
        const anchorAbove = rect.top >= window.innerHeight / 2;
        if (anchorAbove) {
            menu.style.bottom = `${window.innerHeight - rect.top + 6}px`;
        } else {
            menu.style.top = `${rect.bottom + 6}px`;
        }
        // Left-aligned to the badge until that would hang the menu off the
        // right edge. Re-run once the models land: the menu is 260px wide
        // saying "Loading" and as wide as the longest model id afterwards.
        const clampLeft = () => {
            // The menu is shrink-to-fit against (viewport − left), so measuring
            // it where it currently sits makes the measurement chase its own
            // tail. Measure from the left-most position it could take.
            menu.style.left = '8px';
            const menuW = menu.offsetWidth || 260;
            menu.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - menuW - 8))}px`;
        };
        clampLeft();
        menu._clampLeft = clampLeft;

        // Defer the outside-click closer so the opening click doesn't trigger it.
        setTimeout(() => document.addEventListener('click', _closeModelMenu), 0);
        menu.addEventListener('click', (e) => e.stopPropagation());
    }

    const opened = _modelMenuEl;
    let models = [];
    try {
        const data = await get('/api/models');
        models = data.models || [];
    } catch { /* render with just the default option */ }
    if (_modelMenuEl !== opened) return;  // closed, or reopened, while loading

    loadingEl.remove();

    const pick = async (value) => {
        try {
            await patch(`/api/sessions/${state.sid}`, { model_override: value });
            _sessionModelOverride = value || null;
            _renderModelBadge();
        } catch (e) {
            appendMessage('system', `Model override failed: ${e.message}`);
        }
        _closeModelMenu();
    };

    const providerLabels = { ollama: 'Ollama', openrouter: 'OpenRouter' };
    const labelFor = (p) => providerLabels[p] || p;

    const defaultProvider = models.find((m) => m.id === state.model)?.provider;
    // The sheet has a hint column for the provider, so its rows keep the name
    // short; the menu has only the one line and carries it inline.
    const defaultLabel = asSheet
        ? `Default (${state.model || 'server setting'})`
        : `default (${state.model}${defaultProvider ? ` · ${labelFor(defaultProvider)}` : ''})`;
    list.appendChild(_modelPickerItem({
        label: defaultLabel,
        value: '',
        current: !_sessionModelOverride,
        hint: asSheet && defaultProvider ? labelFor(defaultProvider) : '',
        sheet: asSheet,
    }, pick));

    // Group by provider, Ollama first (same ordering as the settings modal).
    // The sheet says the provider per row instead: a header is one more thing
    // to scroll past on a screen that has room for six rows.
    const byProvider = {};
    for (const m of models) {
        const p = m.provider || 'unknown';
        (byProvider[p] ||= []).push(m);
    }
    const providerOrder = ['ollama', 'openrouter', ...Object.keys(byProvider).filter(p => p !== 'ollama' && p !== 'openrouter')];
    for (const provider of providerOrder) {
        const group = byProvider[provider];
        if (!group?.length) continue;
        if (!asSheet) {
            list.appendChild(el('div', { class: 'model-menu-group' }, [text(labelFor(provider))]));
        }
        for (const m of group) {
            list.appendChild(_modelPickerItem({
                label: m.id,
                value: m.id,
                current: _sessionModelOverride === m.id,
                hint: asSheet ? labelFor(provider) : '',
                sheet: asSheet,
            }, pick));
        }
    }
    if (!models.length) {
        list.appendChild(el('div', { class: 'model-menu-empty' }, [text('No models listed — check provider settings.')]));
    }

    if (asSheet) {
        // Focus was parked on the card while the list was empty. Move it to the
        // row that is already selected — but only if nothing else took it in
        // the meantime.
        if (document.activeElement === card) {
            (card.querySelector('.model-sheet-item.current') || card).focus();
        }
        return;
    }
    _modelMenuEl.appendChild(list);
    _modelMenuEl._clampLeft?.();
}


const _runningTools = new Map();  // name → summarized args, for tools currently executing

function _showToolStatus(name, args, opts = {}) {
    if (name === 'browse_web') return; // handled by #browser-status
    const statusEl = document.getElementById('tool-status');
    if (!statusEl) return;
    const [icon, labelFn] = _TOOL_ICONS[name] || ['⚙', () => name];
    const label = (opts.running ? 'running ' : '') + labelFn(args || {});
    statusEl.innerHTML = '';
    statusEl.appendChild(el('span', { class: 'tool-icon' }, [text(icon)]));
    statusEl.appendChild(text('\u00a0' + label));
    statusEl.classList.add('active');
    if (_toolStatusTimer) { clearTimeout(_toolStatusTimer); _toolStatusTimer = null; }
    // A running tool keeps the indicator up until its tool.call arrives;
    // completed-tool flashes still auto-clear.
    if (!opts.running) _toolStatusTimer = setTimeout(_clearToolStatus, 2500);
}

function _clearToolStatus() {
    if (_toolStatusTimer) { clearTimeout(_toolStatusTimer); _toolStatusTimer = null; }
    _runningTools.clear();
    const statusEl = document.getElementById('tool-status');
    if (statusEl) statusEl.classList.remove('active');
}
let _injectedMessages = [];  // queued message DOM elements awaiting agent pickup
const _questionBubbles = new Map();  // question_id → DOM element for live Q&A bubbles

/**
 * Drop the streaming bubble if nothing was ever written into it.
 *
 * appendMessage('assistant', '') opens an empty card the moment a turn
 * starts. Only tool.call and send()'s catch used to clean it up, so every
 * OTHER way a turn can end — an LLM error, an exhausted budget, a cancel, a
 * reflect that gave up, or a turn that completed with no text at all — left
 * a headed-but-empty "ASSISTANT" card in the transcript that no reload would
 * ever reproduce. A bubble that already holds text is left alone: that is a
 * partial answer, and it is the caller's job to finalize it.
 */
function _dropEmptyStreamingBubble() {
    if (!_streamingEl) return;
    if (_collected) return;
    const contentEl = _streamingEl.querySelector('.content');
    if (contentEl && contentEl.textContent.trim()) return;
    _streamingEl.remove();
    _streamingEl = null;
}

function handleEvent(event) {
    // Guard: reject events from other sessions
    if (event.session_id && event.session_id !== state.sid) return;

    // Dedup: skip already-processed events (SSE reconnect replay)
    const seq = event.seq;
    if (seq != null && seq <= _lastSeq) return;

    // Gap detection: if we skipped sequence numbers, events were lost
    if (seq != null && _lastSeq > 0 && seq > _lastSeq + 1) {
        console.warn(`SSE gap detected: expected seq ${_lastSeq + 1}, got ${seq} (${seq - _lastSeq - 1} events missed)`);
        // Debounce soft reload — don't reload for every gap in a burst
        if (!_reconcileTimer) {
            _reconcileTimer = setTimeout(() => {
                _reconcileTimer = null;
                _softReload();
            }, 500);
        }
    }

    if (seq != null) _lastSeq = seq;

    const type = event.type || '';

    if (type === 'stream.token' && event.content) {
        // Mid-reload: the container is being re-rendered under us. Keep the
        // text and let _softReload place it in the single bubble it creates.
        if (_reloading) {
            _bufferedDuringReload += event.content;
            return;
        }
        closeToolGroup();
        // Recover streaming state if page was refreshed mid-stream
        if (!_streamingEl) {
            state.streaming = true;
            _showStopButton();
            _streamingEl = appendMessage('assistant', '');
            _collected = '';
        }
        // The scout line summarises a step that has already finished; the
        // moment the model starts answering it is stale, and it used to sit
        // in the status bar for the rest of the turn.
        if (!_collected) _clearScoutStatus();
        _collected += event.content;
        // Debounced activity update (first ~40 chars of response)
        if (!_activityTimer && state.sid) {
            _activityTimer = setTimeout(() => {
                const preview = _collected.replace(/^[\s#*>-]+/, '').slice(0, 40);
                if (preview) updateSessionActivity(state.sid, preview + '\u2026');
                _activityTimer = null;
            }, 500);
        }
        // Debounce markdown parse (100ms)
        if (_parseTimer) clearTimeout(_parseTimer);
        _parseTimer = setTimeout(() => {
            if (_streamingEl) {
                const contentEl = _streamingEl.querySelector('.content');
                if (contentEl) {
                    _renderStreamIncremental(contentEl);
                    // Follow the growing answer while the user is pinned to
                    // the bottom (a reader scrolled up is left alone).
                    scrollToBottom();
                }
            }
        }, 100);
    }

    else if (type === 'stream.done') {
        closeToolGroup();
        if (_activityTimer) { clearTimeout(_activityTimer); _activityTimer = null; }
        // Clear pending debounce before final render
        if (_parseTimer) { clearTimeout(_parseTimer); _parseTimer = null; }
        // Final render
        if (_streamingEl && _collected) {
            _streamingEl._rawContent = _collected;  // copy-message reads the raw markdown
            const contentEl = _streamingEl.querySelector('.content');
            if (contentEl) {
                clear(contentEl);
                const rendered = renderMarkdown(_collected);
                contentEl.appendChild(rendered);
                addCopyButtons(contentEl);
                processFileRefs(contentEl);
            }
        }
        // Stamp the answer with what produced it while the bubble is still in
        // hand. The event names the model that actually answered, which is not
        // necessarily the session's — a failover swaps it mid-turn.
        if (_streamingEl && _collected) {
            _setMessageChip(
                _streamingEl,
                event.model || _lastStreamModel || _sessionModelOverride || state.model,
                _streamingEl._openedAt ? Date.now() - _streamingEl._openedAt : 0,
            );
        }
        _collected = '';
        _streamingEl = null;
        announce('Pernix finished responding');
        // Agent finished generating — re-enable input (post-hooks still running)
        state.streaming = false;
        _showSendButton();
        loadSessions();
        // Clear activity indicators (failsafe)
        const bDone = document.getElementById('browser-status');
        if (bDone) bDone.classList.remove('active');
        _clearToolStatus();
    }

    else if (type === 'stream.error') {
        if (_parseTimer) { clearTimeout(_parseTimer); _parseTimer = null; }
        _dropEmptyStreamingBubble();
        const streamMsg = humanizeError(event.error || 'Unknown error');
        // A stream.error ends the turn, so say how to get back: the exception
        // is a rate-limit, which the harness is already retrying by itself.
        const retryHint = /rate-limiting/.test(streamMsg) ? '' : ' · /retry to resend';
        appendMessage('system', `Error: ${streamMsg}${retryHint}`);
        // Assertive: the turn is over and the user is waiting on a reply that
        // is not coming.
        announce(`Error: ${streamMsg}`, { assertive: true });
        _collected = '';
        _streamingEl = null;
        state.streaming = false;
        _showSendButton();
        // Clear activity indicators (failsafe)
        const bErr = document.getElementById('browser-status');
        if (bErr) bErr.classList.remove('active');
        _clearToolStatus();
    }

    else if (type === 'tool.call') {
        // Finalize any in-progress assistant message before showing tool results.
        // Each tool round's text gets its own div, matching the DB layout that
        // loadMessages() renders on page refresh.
        if (_streamingEl && _collected) {
            if (_parseTimer) { clearTimeout(_parseTimer); _parseTimer = null; }
            if (_activityTimer) { clearTimeout(_activityTimer); _activityTimer = null; }
            _streamingEl._rawContent = _collected;
            const contentEl = _streamingEl.querySelector('.content');
            if (contentEl) {
                clear(contentEl);
                contentEl.appendChild(renderMarkdown(_collected));
                addCopyButtons(contentEl);
                processFileRefs(contentEl);
            }
        } else if (_streamingEl && !_collected) {
            _streamingEl.remove();
        }
        _collected = '';
        _streamingEl = null;

        // Recover streaming state if page was refreshed mid-turn
        if (!state.streaming) {
            state.streaming = true;
            _showStopButton();
        }
        const preview = (event.result || '').slice(0, 300);
        const full = event.full_result || event.result || '';
        const isTruncated = event.truncated || full.length > 300;
        const runningEl = _takeRunningTool(event.name, event.call_id || event.tool_call_id);
        appendToolToGroup(
            event.name, preview, full, isTruncated, event.was_error, event.latency_ms,
            event.arguments, !!event.truncated, runningEl,
            // Same key the persisted row will replay under, so a row opened
            // live is still open after the next reload.
            _toolItemKey(event.call_id || event.tool_call_id, null),
        );
        if (isTimelineOpen()) {
            appendTimelineToolRow({
                name: event.name,
                args: event.arguments || null,
                content: full,
                latency_ms: event.latency_ms,
                was_error: event.was_error,
            });
        }
        if (state.sid) updateSessionActivity(state.sid, event.name);
        _runningTools.delete(event.name);
        if (_runningTools.size > 0) {
            // Other tools from this round are still executing — keep showing
            // a live "running" indicator instead of the completed flash.
            const [nextName, nextArgs] = _runningTools.entries().next().value;
            _showToolStatus(nextName, nextArgs, { running: true });
        } else {
            _showToolStatus(event.name, event.arguments || {});
        }
    }

    else if (type === 'tool.start') {
        // Emitted BEFORE execution — without it the user gets no feedback
        // for the entire runtime of a slow bash/search call.
        if (!state.streaming) {
            state.streaming = true;
            _showStopButton();
        }
        _runningTools.set(event.name, event.arguments || {});
        _showToolStatus(event.name, event.arguments || {}, { running: true });
        _appendRunningTool(event.name, event.arguments || {}, event.call_id || event.tool_call_id);
        if (isTimelineOpen()) {
            appendTimelineToolStart({ name: event.name, args: event.arguments || null });
        }
        if (state.sid) updateSessionActivity(state.sid, event.name);
    }

    else if (type === 'scout.start') {
        // Recover streaming state if page was refreshed mid-turn
        if (!state.streaming) {
            state.streaming = true;
            _showStopButton();
        }
        _scoutContainer = null;
        if (state.sid) updateSessionActivity(state.sid, 'scouting\u2026');
    }

    else if (type === 'scout.step') {
        // Steps are now silent — we show the full report on scout.done
    }

    else if (type === 'scout.done') {
        const count = (event.tools || []).length;
        const cache = event.from_cache ? ' (cached)' : event.from_fallback ? ' (fallback)' : '';
        const latency = event.latency_ms >= 1000
            ? `${(event.latency_ms / 1000).toFixed(1)}s`
            : `${event.latency_ms}ms`;
        updateStatus(`Scout: ${count} tools${cache} · ${latency}`);
        renderScoutReport(event);
        _scoutContainer = null;
        if (state.sid) updateSessionActivity(state.sid, `scout: ${count} tools`);
        // Show model switch indicator if scout routed to a different model
        if (event.model && event.model !== state.model) {
            const mEl = document.getElementById('status-model');
            if (mEl) mEl.textContent = `${state.model} \u21c4 ${event.model}`;
        }
    }

    else if (type === 'context.compacting') {
        updateStatus('Compacting context...');
        if (state.sid) {
            updateSessionActivity(state.sid, 'compacting\u2026');
            loadContextInfo(state.sid);
        }
    }

    else if (type === 'context.compacted') {
        const n = event.summarized_messages || 0;
        updateStatus(n ? `Compacted ${n} messages` : 'Compacted');
        if (state.sid) loadContextInfo(state.sid);
    }

    else if (type === 'context.reset') {
        updateStatus('Context reset');
        if (state.sid) loadContextInfo(state.sid);
    }

    else if (type === 'session.title') {
        // Update title and subtitle in session list immediately
        const s = state.sessions.find(s => s.id === state.sid);
        if (s && event.title) {
            s.title = event.title;
            if (event.subtitle) s.subtitle = event.subtitle;
            renderSidebar(state.sessions, state.sid, state.spaces);
            _renderSessionHeader();
        }
    }

    else if (type === 'session.queued') {
        appendMessage('system', 'Message queued (agent is busy)');
        // Tag the optimistic bubble with its persisted id so it can be removed
        // from the queue before pickup.
        if (event.message_id != null) {
            const bubble = [..._injectedMessages].reverse().find(b => !b.dataset.messageId);
            if (bubble) _addQueueRemoveButton(bubble, event.message_id);
        }
    }

    else if (type === 'session.queue_removed') {
        const bubble = _messagesInner()?.querySelector(`.message[data-message-id="${event.message_id}"]`);
        if (bubble) {
            const idx = _injectedMessages.indexOf(bubble);
            if (idx !== -1) _injectedMessages.splice(idx, 1);
            bubble.remove();
        }
    }

    else if (type === 'message.injected') {
        // Agent will see this message at the next tool round — clear queued indicator
        const msgEl = _injectedMessages.shift();
        if (msgEl) {
            msgEl.classList.remove('queued');
            msgEl.querySelector('.queued-remove')?.remove();
        }
    }

    else if (type === 'dialog.question' || type === 'user_question') {
        closeToolGroup();
        if (event.question_id) {
            appendQuestionBubble(event.question_id, event.question || '', event.context || '');
        }
        refreshBell();
        // Browser notification handled by global notification SSE stream
    }

    // dialog.notification — browser notification + bell handled by global SSE stream

    else if (type === 'dialog.answered') {
        if (event.question_id) markQuestionAnswered(event.question_id, event.answer || '');
        refreshBell();
    }

    else if (type === 'dialog.dismissed') {
        if (event.question_id) markQuestionDismissed(event.question_id);
        refreshBell();
    }

    else if (type === 'worker.started') {
        // No system line: the card below is the one place a fan-out is
        // reported, and it stays correct as the fan-out moves.
        const workerModel = event.model ? `on ${event.model}` : '';
        _recentDeadWorkers.delete(event.worker_id);
        _activeWorkers.set(event.worker_id, {
            title: event.title || event.worker_id,
            kind: event.kind || '',
            startedAt: Date.now(),
        });
        _workerCardStart(event.worker_id, {
            title: event.title || event.worker_id,
            kind: event.kind || '',
            note: workerModel,
        });
        _announceSystem(`Worker started: ${event.title || event.worker_id}`);
        _renderWorkerStrip();
    }

    else if (type === 'worker.resumed') {
        // A terminated/reaped worker was revived (resume_worker) — treat it
        // like a fresh start for the activity strip, with an honest note
        // about what it is resuming from.
        const prior = event.prior_termination ? `resumed after ${event.prior_termination}` : 'resumed';
        _recentDeadWorkers.delete(event.worker_id);
        _activeWorkers.set(event.worker_id, {
            title: event.title || event.worker_id,
            kind: event.kind || '',
            startedAt: Date.now(),
        });
        _workerCardStart(event.worker_id, {
            title: event.title || event.worker_id,
            kind: event.kind || '',
            note: prior,
        });
        _announceSystem(`Worker resumed: ${event.title || event.worker_id}`);
        _renderWorkerStrip();
    }

    else if (type === 'worker.done') {
        // A raw uuid told the reader nothing about which of five parallel
        // workers had just finished. The strip and the session list both know
        // its title.
        // Resolve the name BEFORE the strip forgets it — _workerLabel reads
        // _activeWorkers, and the delete below is what empties it.
        const doneLabel = _workerLabel(event.worker_id, event.title);
        const prev = _activeWorkers.get(event.worker_id);
        _activeWorkers.delete(event.worker_id);
        const doneReason = event.error
            ? humanizeError(event.error)
            : (event.termination_reason || 'done');
        _addDeadWorker(event.worker_id, {
            title: doneLabel,
            kind: (prev && prev.kind) || '',
            reason: event.termination_reason || (event.error ? 'error' : 'done'),
            endedAt: Date.now(),
        });
        _workerCardEnd(event.worker_id, {
            title: doneLabel,
            state: event.error ? 'failed' : 'done',
            reason: doneReason,
        });
        // The card is silent to a screen reader; this line is not.
        _announceSystem(`Worker done: ${doneLabel} — ${doneReason}`);
        _renderWorkerStrip();
    }

    else if (type === 'worker.failed') {
        // Spawn-time failure: manager.prompt() raised before the worker
        // ever ran. Distinct from worker.done with an error — that's a
        // worker that ran and errored mid-turn. This is a worker that
        // never started, so there's no transcript or summary to inspect.
        // It keeps its own line: an error the fan-out never recovers from is
        // not something to fold into a collapsed card.
        const wid = event.worker_id || 'unknown';
        const err = event.error || '(no error message)';
        appendMessage('system', `⚠ Worker failed to start: ${wid} — ${err}`);
        _activeWorkers.delete(wid);
        if (_workerCard && _workerCard.rows.has(wid)) {
            _workerCardEnd(wid, { state: 'failed', reason: 'never started', spawnFailed: true });
        }
        _renderWorkerStrip();
    }

    else if (type === 'rlm.started') {
        const models = event.root_model ? ` [${event.root_model} → ${event.sub_model}]` : '';
        appendMessage('system', `RLM run started: ${event.task_preview || event.run_id}${models}`);
        _activeRlmRuns.set(event.run_id, {
            uiSid: event.ui_session_id,
            label: event.task_preview || event.run_id,
            iterations: 0,
            maxIterations: event.max_iterations || 0,
            subcalls: 0,
            startedAt: Date.now(),
        });
        _renderWorkerStrip();
    }

    else if (type === 'rlm.activity' || type === 'rlm.heartbeat') {
        let r = _activeRlmRuns.get(event.run_id);
        if (!r) {
            // Run started before this client connected — synthesize the chip.
            r = {
                uiSid: event.ui_session_id,
                label: event.run_id,
                iterations: 0,
                maxIterations: 0,
                subcalls: 0,
                startedAt: Date.now(),
            };
            _activeRlmRuns.set(event.run_id, r);
        }
        if (typeof event.iterations === 'number') r.iterations = event.iterations;
        if (typeof event.subcalls === 'number') r.subcalls = event.subcalls;
        _renderWorkerStrip();
    }

    else if (type === 'rlm.done') {
        const dur = event.duration ? ` in ${Math.round(event.duration)}s` : '';
        const err = event.error ? ` — ${event.error}` : '';
        appendMessage('system', `RLM run ${event.status}: ${event.iterations} iterations, ${event.subcalls} sub-calls${dur}${err}`);
        _activeRlmRuns.delete(event.run_id);
        _renderWorkerStrip();
    }

    else if (type === 'session.state_changed') {
        _renderStateBadge(event);
        _announceState(event.to || 'idle_ready');
        const idleStates = ['idle_ready', 'awaiting_user', 'awaiting_workers'];
        const isNowIdle = idleStates.includes(event.to || '');
        if (isNowIdle && state.streaming) {
            state.streaming = false;
            _showSendButton();
            updateStatus('');
        } else if (!isNowIdle && !state.streaming) {
            state.streaming = true;
            _showStopButton();
        }
    }

    else if (type === 'turn.forced_followup') {
        // The harness detected an "I'll do X next…" tail with no tool calls
        // and told the agent to keep working within the same turn.
        appendMessage('system', `⟳ Forced follow-up ${event.attempt}/${event.max} — the agent announced more work but stopped; the harness told it to continue.`);
    }

    else if (type === 'tool.call.intercepted') {
        // Gate-level corrections. Rejections already surface as error rows on
        // tool.call; alias rewrites are worth a visible line so users learn
        // the model's tool-name drift. Coercions/param-drops stay quiet here —
        // they ride as a [note:] prefix on the tool result itself.
        if (event.action === 'aliased') {
            appendMessage('system', `Tool call rewritten: ${event.reason} → ${event.name}`);
        }
    }

    else if (type === 'session.prompt_rejected') {
        const reason = event.reason || 'unknown';
        const msgs = {
            awaiting_user: 'Session is waiting for an answer — use the dialog, not a new prompt.',
            cancelling: 'Session is cancelling; wait for it to settle before prompting again.',
            queue_full: 'Too many queued messages on this session. Wait for some to drain.',
        };
        appendMessage('system', msgs[reason] || `Prompt rejected (${reason})`);
    }

    else if (type === 'browse.start') {
        const hostname = _extractHostname(event.url || '');
        if (state.sid) updateSessionActivity(state.sid, 'browsing ' + hostname);
        const statusEl = document.getElementById('browser-status');
        if (statusEl) {
            statusEl.innerHTML = '';
            statusEl.appendChild(el('span', { class: 'browser-icon' }, [text('\uD83C\uDF10')]));
            statusEl.appendChild(text(` ${hostname}\u2026`));
            statusEl.classList.add('active');
        }
    }

    else if (type === 'browse.done') {
        const statusEl = document.getElementById('browser-status');
        if (statusEl) statusEl.classList.remove('active');
    }

    else if (type === 'reflect.start') {
        updateStatus('Reflect: verifying\u2026');
        if (state.sid) updateSessionActivity(state.sid, 'reflecting\u2026');
    }
    else if (type === 'reflect.done') {
        renderReflectCard(event);
        if (event.verdict === 'pass') {
            updateStatus('');
        }
    }
    else if (type === 'reflect.deferred_scheduled') {
        // Interactive turns finalize without waiting for the grade; say so
        // once, quietly, so a reflect-less turn doesn't read as broken.
        updateStatus('');
    }
    else if (type === 'reflect.deferred') {
        // Observe-only grade landing minutes after the turn finished. Same
        // card as a live verdict, but it never retried anything — don't touch
        // streaming state or the status line, a new turn may be running.
        renderReflectCard(event);
    }
    else if (type === 'reflect.skipped') {
        // Render a small inline notice so a finalizing-without-reflect turn
        // doesn't look like a missing piece. The skip is intentional —
        // reflect_min_messages gate avoids spending an LLM call on trivial
        // 1-shot exchanges.
        const reason = event.reason === 'too-few-messages'
            ? `reflect skipped (${event.count}/${event.min} messages — too short to verify)`
            : `reflect skipped (${event.reason || 'unknown'})`;
        appendMessage('system', reason);
    }
    else if (type === 'eval.done') {
        renderEvalCard(event);
    }
    else if (type === 'reflect.retry') {
        // Re-enter streaming state — agent is retrying generation
        if (!state.streaming) {
            state.streaming = true;
            _showStopButton();
        }
        appendMessage('system',
            `Reflect: task incomplete \u2014 retrying (attempt ${event.attempt}/${event.max}). ${event.reasoning || ''}`);
        updateStatus(`Reflect: retry #${event.attempt}\u2026`);
        if (state.sid) updateSessionActivity(state.sid, `reflect: retry #${event.attempt}`);
    }
    else if (type === 'reflect.exhausted') {
        _dropEmptyStreamingBubble();
        appendMessage('system', `Reflect: max retries reached. ${event.reasoning || ''}`);
        updateStatus('');
        state.streaming = false;
        _showSendButton();
    }
    else if (type === 'reflect.escalate') {
        _dropEmptyStreamingBubble();
        appendMessage('system', `Reflect: needs clarification \u2014 ${event.missing || event.reasoning || ''}`);
        updateStatus('');
        state.streaming = false;
        _showSendButton();
    }
    else if (type === 'reflect.circuit_breaker') {
        // Cross-retry breaker: reflect asked for another retry, but the last
        // two attempts failed identically. Retrying is refused rather than
        // burning the remaining budget on the same failure.
        _dropEmptyStreamingBubble();
        appendMessage('system',
            `Reflect: retry stopped \u2014 the same failure repeated across `
            + `${event.attempts || '?'} attempt(s). ${event.reasoning || ''}`);
        updateStatus('');
        state.streaming = false;
        _showSendButton();
    }

    else if (type === 'model.divider') {
        // Mid-turn switch — insert a visible pill-with-rules row in the chat
        renderModelDivider(event);
    }

    else if (type === 'model.override') {
        // Mid-turn switch_model called: render <orig> ⇄ <temp> in the
        // status bar while the override is active; clear when restored.
        const mEl = document.getElementById('status-model');
        if (mEl) {
            if (event.active && event.to) {
                const from = event.from || state.model;
                mEl.textContent = '';
                mEl.appendChild(document.createTextNode(from + ' '));
                const shuffle = document.createElement('span');
                shuffle.className = 'model-override-shuffle';
                shuffle.title = 'Temporary per-turn model override';
                shuffle.textContent = '⇄';
                mEl.appendChild(shuffle);
                mEl.appendChild(document.createTextNode(' '));
                const temp = document.createElement('span');
                temp.className = 'model-override-temp';
                temp.textContent = event.to;
                mEl.appendChild(temp);
                mEl.classList.add('has-override');
            } else {
                _renderModelBadge();
            }
        }
    }

    else if (type === 'stream.reset') {
        // The provider is about to re-stream this answer from the beginning
        // (a retry, or the fallback model). Drop the partial we already
        // rendered, or the viewer reads <partial><full answer> while the
        // database stores only the second one.
        if (_parseTimer) { clearTimeout(_parseTimer); _parseTimer = null; }
        _collected = '';
        _bufferedDuringReload = '';
        if (_streamingEl) {
            const contentEl = _streamingEl.querySelector('.content');
            if (contentEl) clear(contentEl);
            _streamingEl._rawContent = '';
        }
    }

    else if (type === 'stream.fallback') {
        // Rate-limit / provider failover switched the model mid-stream. Hold
        // on to the name: this, not the session default, is what answered.
        _lastStreamModel = event.model || _lastStreamModel;
        appendMessage('system', `LLM failover → ${event.model || 'fallback'}`);
        const mEl = document.getElementById('status-model');
        if (mEl) mEl.textContent = event.model || state.model;
        if (state.sid) updateSessionActivity(state.sid, `failover: ${event.model || ''}`);
    }

    else if (type === 'stream.retry') {
        updateStatus(`LLM retry #${event.attempt || '?'}…`);
        if (state.sid) updateSessionActivity(state.sid, `retrying #${event.attempt || ''}`);
    }

    else if (type === 'stream.length_continuation') {
        const max = event.max ? `/${event.max}` : '';
        updateStatus(`Continuing (length limit hit, ${event.attempt || '?'}${max})…`);
    }

    else if (type === 'stream.budget_exhausted') {
        if (_parseTimer) { clearTimeout(_parseTimer); _parseTimer = null; }
        _dropEmptyStreamingBubble();
        appendMessage('system', `LLM budget exhausted: ${event.message || 'no further retries'}`);
        updateStatus('');
        _collected = '';
        _streamingEl = null;
        state.streaming = false;
        _showSendButton();
        _clearToolStatus();
    }

    else if (type === 'session.waiting_llm') {
        updateStatus('Waiting for model…');
    }

    else if (type === 'session.queue_full') {
        appendMessage('system',
            `⚠ Message queue full (${event.pending}/${event.max}) — wait for some to drain.`);
    }

    else if (type === 'session.queue_dropped') {
        const reason = event.reason ? ` (${event.reason})` : '';
        // 'system', not 'notice': there is no .message.notice rule anywhere in
        // the CSS, so the live line rendered as unstyled body text — while the
        // very same row, replayed from the database, comes back through the
        // role='notice' branch of loadMessages as a system message. Same event,
        // two different-looking lines depending on whether you reloaded.
        appendMessage('system', `[${event.count} queued message(s) dropped${reason}]`);
    }

    else if (type === 'eval.start') {
        updateStatus(`Eval: checking ${event.features || ''} feature(s)…`);
        if (state.sid) updateSessionActivity(state.sid, 'evaluating…');
    }

    else if (type === 'eval.pass') {
        appendMessage('system', `✓ Eval passed (${event.features || 0} feature(s))`);
        updateStatus('');
    }

    else if (type === 'eval.retry') {
        if (!state.streaming) {
            state.streaming = true;
            _showStopButton();
        }
        appendMessage('system',
            `Eval: feature(s) failed — retrying (attempt ${event.attempt}/${event.max}).`);
        updateStatus(`Eval: retry #${event.attempt}…`);
    }

    else if (type === 'eval.exhausted') {
        appendMessage('system', `Eval: max retries reached (${event.attempts}/${event.max}).`);
        updateStatus('');
    }

    else if (type === 'reflect.budget_exhausted') {
        appendMessage('system',
            `Reflect skipped (time budget exhausted, ${event.remaining_s}s remaining < ${event.needed_s}s needed).`);
    }

    else if (type === 'gates.done') {
        const failed = event.failed || 0;
        if (failed) {
            appendMessage('system',
                `Gates: ${failed}/${event.total} failed — ${(event.names_failed || []).join(', ')}`);
        } else if (event.total) {
            updateStatus(`Gates: ${event.total} passed`);
        }
    }

    else if (type === 'goal.budget_exceeded') {
        appendMessage('system', `Goal budget exceeded (${event.reason || 'budget'}) — ending turn.`);
    }

    else if (type === 'goal.continuation') {
        const gbudget = event.budget ? `/${event.budget}` : '';
        updateStatus(`Goal continuation ${event.ordinal || '?'}${gbudget}…`);
        if (state.sid) updateSessionActivity(state.sid, `goal continuation ${event.ordinal || ''}`);
    }

    else if (type === 'context.view_pruned') {
        // Budget-gated view pruning stubbed oversized tool results out of the
        // compiled view (the stored transcript is untouched).
        updateStatus(`Pruned ${event.stubbed} large tool result(s) from view`);
    }

    // Snooze (background idle-time consolidation), session.message_combined
    // and session.message_combine_skipped are intentionally silent — they're
    // informational only, but listing them in sse.js EVENT_TYPES keeps
    // _lastSeq advancing so gap detection stays honest.

    else if (type === 'turn.complete') {
        // Safety net: ensure button is always reset when turn finishes
        _dropEmptyStreamingBubble();
        _setStopPending(false);
        if (state.streaming) {
            state.streaming = false;
            _showSendButton();
        }
        // Clear any remaining queued indicators — turn is over
        for (const el of _injectedMessages) {
            el.classList.remove('queued');
            el.querySelector('.queued-remove')?.remove();
        }
        _injectedMessages = [];
        _clearToolStatus();
        // Restore model name in case scout routed to a different model this turn
        _renderModelBadge();
        if (state.sid) {
            updateSessionActivity(state.sid, '');
            loadContextInfo(state.sid);
            _reconcile();
        }
    }

    else if (type === 'session.cancelled') {
        if (_parseTimer) { clearTimeout(_parseTimer); _parseTimer = null; }
        _dropEmptyStreamingBubble();
        _setStopPending(false);
        appendMessage('system', 'Session cancelled by user.');
        updateStatus('');
        state.streaming = false;
        _showSendButton();
        if (state.sid) updateSessionActivity(state.sid, '');
    }

    else if (type === 'sse.reconnected') {
        // SSE reconnected after drop — sync button state with server
        _syncStreamingState();
    }

    else if (type === 'sse.session_gone') {
        // The session was deleted (possibly from another device) while this
        // tab had it open. Stop pretending it might come back.
        appendMessage('system', 'This session no longer exists on the server (deleted elsewhere). Pick another session or start a new one.');
        state.streaming = false;
        _showSendButton();
        loadSessions();
    }
}

// ---------------------------------------------------------------------------
// Sync streaming state with server (recovery after SSE reconnect or timeout)
// ---------------------------------------------------------------------------

async function _syncStreamingState() {
    if (!state.sid) return;
    try {
        const status = await get(`/api/sessions/${state.sid}/status`);
        // Server restart detection: the event_seq counter is in-memory and
        // restarts near 0. If the server's seq is *behind* ours, every future
        // event would be silently dropped by the `seq <= _lastSeq` dedup and
        // gap detection would never fire (seqs went down, not up). Reset and
        // reload so the UI doesn't go permanently dead.
        // A reaped-from-memory session reports a null seq; that is not a
        // restart, and treating it as 0 forced a reload and a scroll jump
        // every time a backgrounded tab came back.
        const serverSeq = status.in_memory === false ? null : status.event_seq;
        if (serverSeq != null && serverSeq < _lastSeq) {
            console.warn(`SSE: server seq went backwards (server=${serverSeq}, client=${_lastSeq}) — server restarted, resyncing`);
            _lastSeq = serverSeq;
            await _softReload();
            return;
        }
        _applyStateBadge(status.state || 'idle_ready', '');
        const serverActive = status.status === 'processing' || status.status === 'scouting';
        if (serverActive && !state.streaming) {
            state.streaming = true;
            _showStopButton();
        } else if (!serverActive && state.streaming) {
            state.streaming = false;
            _showSendButton();
            updateStatus('');
        }
        // The server is the authority on whether a cancel has landed: a stop
        // press against a turn that had already finished gets its button back
        // here rather than staying disabled until the next event.
        if (!serverActive) _setStopPending(false);
    } catch { /* ignore — next health check will retry */ }
}

// ---------------------------------------------------------------------------
// Soft reload — refresh messages from DB without full page refresh
// ---------------------------------------------------------------------------

function _isRlmView() {
    // RLM view sessions render the trace viewer, not a transcript. They have
    // no SSE stream and /status carries no event_seq for them, so the seq
    // reconciler would misread them as "server restarted" and a soft reload
    // would wipe the viewer mid-watch. The viewer polls its own endpoint.
    return (state.sessions || []).find(s => s.id === state.sid)?.session_type === 'rlm';
}

// Transient status-bar note that clears itself. Used for recoveries the user
// should know happened but must not have to dismiss.
let _noticeTimer = null;
function _showNotice(msg, ms = 6000) {
    // Its own span. A notice explains something that already happened and has
    // to stay readable for a few seconds; it used to share #status-info with
    // the live turn status, so the very next updateStatus('') — which fires on
    // stream.done, on every idle state change — wiped it mid-sentence.
    const noticeEl = document.getElementById('status-notice');
    if (!noticeEl) { updateStatus(msg); return; }
    noticeEl.textContent = msg;
    if (_noticeTimer) clearTimeout(_noticeTimer);
    _noticeTimer = setTimeout(() => {
        _noticeTimer = null;
        if (noticeEl.textContent === msg) noticeEl.textContent = '';
    }, ms);
}

/** True when the transcript ends on a user message that is not queued and
 *  was not rejected — i.e. one the agent never answered. */
function _lastMessageIsUnanswered() {
    const inner = _messagesInner();
    if (!inner) return false;
    const msgs = inner.querySelectorAll('.message');
    const last = msgs[msgs.length - 1];
    if (!last) return false;
    return last.classList.contains('user')
        && !last.classList.contains('queued')
        && !last.classList.contains('rejected');
}

async function _softReload() {
    if (!state.sid || _isRlmView()) return;
    if (_reloading) return;  // a second gap during the fetch is the same reload
    console.info('SSE: soft reload triggered (gap detected or reconciliation)');
    // loadMessages() clears and re-renders the DOM, which detaches any live
    // _streamingEl reference. Reset it unconditionally so the next stream.token
    // event creates a fresh element rather than updating a ghost node.
    //
    // _reloading holds the token handler off the DOM until the re-render
    // lands. Without it, tokens arriving during the fetch built a bubble in
    // the just-emptied container, the history was appended UNDER it, and the
    // status branch below then made a second empty bubble — so the answer's
    // tail ended up split across two bubbles with the history wedged between.
    _reloading = true;
    _streamingEl = null;
    _collected = '';
    state.streaming = false;
    try {
        await loadMessages(state.sid);
    } finally {
        _reloading = false;
    }
    // Re-fetch server state to re-wire streaming controls and badge.
    try {
        const status = await get(`/api/sessions/${state.sid}/status`);
        // Take the server's value even when it's 0/absent — after a server
        // restart keeping the old high _lastSeq would drop all future events.
        _lastSeq = status.event_seq || 0;
        _applyStateBadge(status.state || 'idle_ready', '');
        if (status.status === 'processing' || status.status === 'scouting') {
            state.streaming = true;
            _showStopButton();
            // Carry whatever arrived while the transcript was being fetched
            // into the one bubble, instead of discarding it and opening a
            // second empty one.
            _streamingEl = appendMessage('assistant', '');
            _collected = _bufferedDuringReload;
            if (_collected) {
                const contentEl = _streamingEl.querySelector('.content');
                if (contentEl) _renderStreamIncremental(contentEl);
            }
        } else {
            _showSendButton();
            updateStatus('');
            // A soft reload means events were dropped. If the transcript now
            // ends on the user's own message and the server is idle with an
            // empty queue, nothing is coming — the turn died inside the gap.
            // Saying nothing here is what made a lost turn look like an agent
            // that had simply ignored you.
            if (!status.pending_messages && _lastMessageIsUnanswered()) {
                appendMessage('system', 'Your last message was not answered — /retry to resend.');
            }
        }
    } catch {}
    _bufferedDuringReload = '';
    // Say so. The transcript is re-read from the database, so no *message* is
    // lost — but the live events that were dropped (tool chips, scout steps,
    // partial tokens) are gone for good, and the view visibly jumping without
    // explanation reads as a glitch. The server's replay buffer holds 2000
    // events and a reaped session comes back with an empty one, so this path
    // is reachable in normal operation, not just after an outage.
    _showNotice('reconnected — transcript refreshed');
}

async function _reconcile() {
    // Lightweight check: compare server event_seq with client _lastSeq.
    // Runs while streaming too — a stream that is silently dropping every
    // event looks exactly like a healthy one from here, and skipping the
    // check was why a deleted-then-recreated session or a mid-turn server
    // restart left the UI stuck with a stop button and no tokens.
    if (!state.sid || _isRlmView()) return;  // no transcript to reconcile
    try {
        const status = await get(`/api/sessions/${state.sid}/status`);
        // A session reaped from memory reports in_memory:false and a null
        // seq. That is "nothing to reconcile", not "the counter reset".
        if (status.in_memory === false || status.event_seq == null) return;
        const serverSeq = status.event_seq;
        if (state.streaming) {
            // Mid-stream, only the backwards case is actionable: a forward
            // gap resolves itself as the stream drains, while reloading the
            // transcript underneath a live turn splits the answer in two.
            if (serverSeq < _lastSeq) {
                console.warn(`SSE: server seq went backwards mid-stream (server=${serverSeq}, client=${_lastSeq}) — resyncing`);
                _lastSeq = serverSeq;
                await _syncStreamingState();
            }
            return;
        }
        if (serverSeq > _lastSeq + 5) {
            // Significant gap — soft reload
            console.warn(`SSE: reconciliation detected drift (server=${serverSeq}, client=${_lastSeq})`);
            await _softReload();
        } else if (serverSeq > _lastSeq) {
            // Small gap — just update seq to prevent future false alarms
            _lastSeq = serverSeq;
        } else if (serverSeq < _lastSeq) {
            // Server seq went backwards — in-memory counter reset after a
            // server restart. Without this branch the dedup guard drops every
            // future event and the UI silently freezes.
            console.warn(`SSE: server seq went backwards (server=${serverSeq}, client=${_lastSeq}) — server restarted, resyncing`);
            _lastSeq = serverSeq;
            await _softReload();
        }
    } catch {}
}

// Periodic reconciliation every 45 seconds (safety net)
setInterval(() => { if (state.sid) _reconcile(); }, 45000);

// Intervals are throttled while the tab is backgrounded — reconcile
// immediately on return (phone unlock) instead of waiting up to a minute.
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.sid) _reconcile();
});

// ---------------------------------------------------------------------------
// Stop / Send button toggle
// ---------------------------------------------------------------------------

// True between pressing stop and the server confirming the turn is over.
// Cancelling is cooperative — the agent finishes its current step first — so
// there is a real window in which the button used to look untouched and
// people pressed it again and again with no sign the first press landed.
let _stopPending = false;

function _setStopPending(pending) {
    _stopPending = pending;
    const btn = document.getElementById('send-btn');
    if (btn) btn.disabled = pending || !!document.getElementById('msg-input')?.disabled;
    if (pending) {
        updateStatus('Stopping…');
        _applyStateBadge('cancelling', 'stop pressed');
    } else {
        const infoEl = document.getElementById('status-info');
        if (infoEl && infoEl.textContent === 'Stopping…') infoEl.textContent = '';
    }
}

function _showStopButton() {
    const btn = document.getElementById('send-btn');
    btn.disabled = _stopPending;
    btn.title = 'Stop generation';
    btn.classList.add('stop-mode');
    btn.setAttribute('aria-label', 'Stop generation');
    clear(btn);
    btn.appendChild(icon('stop'));
}

function _showSendButton() {
    const btn = document.getElementById('send-btn');
    // Back in send mode: whatever the stop press was waiting for has happened.
    _stopPending = false;
    // A read-only session keeps its send button off through stop/send churn —
    // the composer input is the source of truth (_setComposerReadOnly).
    btn.disabled = !!document.getElementById('msg-input')?.disabled;
    btn.title = 'Send message';
    btn.classList.remove('stop-mode');
    btn.setAttribute('aria-label', 'Send message');
    clear(btn);
    btn.appendChild(icon('send'));
}

async function _cancelSession() {
    if (!state.sid) return;
    if (_stopPending) return;   // a second press is not a second cancel
    _setStopPending(true);
    try {
        const cancelHeaders = {};
        const _tc = getAuthToken();
        if (_tc) cancelHeaders['Authorization'] = `Bearer ${_tc}`;
        const resp = await fetch(`/api/sessions/${state.sid}/cancel`, { method: 'POST', headers: cancelHeaders });
        if (!resp.ok) {
            // This path keeps its own fetch because it branches on the
            // status (404 = nothing running), which api() flattens into an
            // Error. It still has to hand a 401 to the login flow, or an
            // expired session reads as "cancel failed" forever.
            if (resp.status === 401) {
                _setStopPending(false);
                window.dispatchEvent(new CustomEvent('pernix:auth-required'));
                return;
            }
            // 404 = session not in memory (nothing running) — silently fall
            // through to a state sync. Other failures get surfaced; either
            // way, don't leave the button stuck in stop-mode for up to 45s
            // until the reconcile loop notices.
            if (resp.status !== 404) {
                const err = await resp.json().catch(() => ({}));
                appendMessage('system', `Cancel failed: ${err.detail || resp.statusText}`);
            }
            await _syncStreamingState();
        }
    } catch (e) {
        appendMessage('system', `Cancel failed: ${e.message}`);
        await _syncStreamingState();
    }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

// Timestamp of the previously appended message — drives "resumed Nh later"
// gap dividers. Reset per session load.
let _lastMsgTs = 0;
const _MSG_GAP_MS = 10 * 60 * 1000;  // gaps over 10 minutes get a divider

function _msgTimestamp(ts) {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return null;
    const time = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    const full = d.toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit',
    });
    return el('span', { class: 'msg-time', title: full }, [text(time)]);
}

function _fmtGap(ms) {
    const mins = Math.round(ms / 60000);
    if (mins < 60) return `${mins}m`;
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    if (h < 24) return m ? `${h}h ${m}m` : `${h}h`;
    const days = Math.floor(h / 24);
    return `${days}d ${h % 24}h`;
}

function _parseMsgTs(createdAt) {
    if (createdAt == null) return 0;
    if (typeof createdAt === 'number') return createdAt;
    let s = String(createdAt);
    if (!/[Z+-]\d{2}/.test(s) && !s.endsWith('Z')) s += 'Z';  // DB times are UTC, no suffix
    const t = Date.parse(s);
    return isNaN(t) ? 0 : t;
}

/**
 * "qwen3-27b · 4.2s" under one assistant answer.
 *
 * Which model wrote a given reply is not a constant: a failover or an
 * in-turn switch_model changes it mid-conversation, and the status bar only
 * ever showed the CURRENT one. Same for cost in time. Both facts are saved
 * with the row now, so a reopened transcript can still answer the question.
 * Written by both paths — replay reads the row's metadata, the live stream
 * fills it in on stream.done.
 */
function _setMessageChip(msgEl, model, latencyMs) {
    if (!msgEl) return;
    const ms = Number(latencyMs) || 0;
    const parts = [];
    if (model) parts.push(String(model));
    if (ms > 0) parts.push(_humanizeMs(ms));
    let chip = msgEl.querySelector('.msg-chip');
    if (!parts.length) {
        if (chip) chip.remove();
        return;
    }
    if (!chip) {
        chip = el('div', { class: 'msg-chip' });
        // Before the action toolbar, which is the message's last child.
        msgEl.insertBefore(chip, msgEl.querySelector('.msg-actions'));
    }
    clear(chip);
    chip.appendChild(text(parts.join(' · ')));
    chip.title = model
        ? (ms > 0 ? `Answered by ${model} in ${_humanizeMs(ms)}` : `Answered by ${model}`)
        : `Answered in ${_humanizeMs(ms)}`;
    return chip;
}

/** Hover action toolbar: copy on every message, edit-&-resend on user messages. */
function _attachMessageActions(msgEl, role) {
    const actions = el('div', { class: 'msg-actions' });
    const copyBtn = el('button', {
        class: 'msg-action-btn', title: 'Copy message', 'aria-label': 'Copy message',
    }, [icon('copy', { size: 12 })]);
    copyBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const raw = msgEl._rawContent ?? msgEl.querySelector('.content')?.innerText ?? '';
        try {
            await navigator.clipboard.writeText(raw);
            clear(copyBtn);
        copyBtn.appendChild(icon('check', { size: 12 }));
            setTimeout(() => { clear(copyBtn); copyBtn.appendChild(icon('copy', { size: 12 })); }, 1200);
        } catch { /* clipboard unavailable (non-secure context) */ }
    });
    actions.appendChild(copyBtn);

    if (role === 'user') {
        // "Edit & resend" promised something this button does not do: it
        // neither edits the stored message nor resends anything, it only
        // puts the text back in the composer for you to change and send.
        const editBtn = el('button', {
            class: 'msg-action-btn', title: 'Copy to composer', 'aria-label': 'Copy to composer',
        }, [icon('edit', { size: 12 })]);
        editBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const textarea = document.getElementById('msg-input');
            if (!textarea) return;
            textarea.value = msgEl._rawContent ?? msgEl.querySelector('.content')?.innerText ?? '';
            textarea.dispatchEvent(new Event('input'));
            textarea.focus();
        });
        actions.appendChild(editBtn);
    }
    msgEl.appendChild(actions);
}

function appendMessage(role, content, meta = {}) {
    const inner = _messagesInner();
    const scroll = _messagesScroll();

    // Remove empty state if present
    const emptyEl = inner.querySelector('.empty-state');
    if (emptyEl) {
        emptyEl.remove();
        _renderSessionHeader();   // the hero is gone; the session has a name again
    }

    const ts = _parseMsgTs(meta.createdAt ?? Date.now());

    // Idle-gap divider — only between consecutive timestamped messages.
    if (ts && _lastMsgTs && ts - _lastMsgTs > _MSG_GAP_MS) {
        inner.appendChild(el('div', { class: 'msg-gap-divider' }, [
            text(`—— resumed ${_fmtGap(ts - _lastMsgTs)} later ——`),
        ]));
    }
    if (ts) _lastMsgTs = ts;

    const roleLabel = el('div', { class: 'role-label' }, [text(role)]);
    const timeEl = ts ? _msgTimestamp(ts) : null;
    if (timeEl) roleLabel.appendChild(timeEl);

    const msgEl = el('div', { class: `message ${role}` }, [
        roleLabel,
        el('div', { class: 'content' }),
    ]);
    msgEl._rawContent = content || '';
    if (meta.messageId != null) msgEl.dataset.messageId = String(meta.messageId);

    const contentEl = msgEl.querySelector('.content');
    if (role === 'assistant' && content) {
        contentEl.appendChild(renderMarkdown(content));
        addCopyButtons(contentEl);
        processFileRefs(contentEl);
    } else if (role === 'system' && content) {
        contentEl.appendChild(renderMarkdown(content));
        // Deliberately only for 'system'. The assistant bubble is the
        // streaming one; announcing it would narrate the answer twice.
        _announceSystem(content);
    } else if (content) {
        contentEl.appendChild(text(content));
    }
    // Nothing is appended for empty content — not even a zero-length text
    // node — so `.content:empty` can style the waiting streaming bubble.

    if (role === 'user' || role === 'assistant') _attachMessageActions(msgEl, role);

    if (role === 'assistant') {
        // When the live stream ends, stream.done fills this in against the
        // moment the bubble opened.
        msgEl._openedAt = Date.now();
        const row = _parseRowMetadata(meta.metadata);
        const model = meta.model || row.model || '';
        const latency = row.latency_ms ?? meta.latencyMs ?? 0;
        _setMessageChip(msgEl, model, latency);
        // On a phone the chip is one more line of clutter on every single
        // answer, so touch.css folds it away and the meta row is the handle
        // that brings it back.
        roleLabel.classList.add('role-label--toggle');
        roleLabel.addEventListener('click', () => msgEl.classList.toggle('show-meta'));
    }

    inner.appendChild(msgEl);
    scrollToBottom(role === 'user');
    return msgEl;
}

/**
 * Render a live ask_user question bubble in the chat.
 * The bubble can later be updated with an answer or marked dismissed.
 */
function appendQuestionBubble(questionId, questionText, context) {
    if (_questionBubbles.has(questionId)) return _questionBubbles.get(questionId);
    const inner = _messagesInner();
    const scroll = _messagesScroll();
    const emptyEl = inner.querySelector('.empty-state');
    if (emptyEl) emptyEl.remove();

    // Inline reply form — always present so the user can respond even if the
    // notification popup was closed or dismissed.
    const inlineInput = el('textarea', {
        class: 'q-inline-input',
        placeholder: 'Type your answer…',
        rows: '2',
    });
    const inlineStatus = el('span', { class: 'q-inline-status', role: 'status' });
    const inlineSend = el('button', { class: 'btn btn-primary q-inline-send', onClick: async () => {
        const answer = inlineInput.value.trim();
        if (!answer) return;
        inlineSend.disabled = true;
        try {
            await post(`/api/questions/${questionId}/answer`, { answer });
            markQuestionAnswered(questionId, answer);
        } catch {
            inlineStatus.textContent = 'Error sending';
            inlineSend.disabled = false;
        }
    }}, [text('Send')]);

    const answerArea = el('div', { class: 'q-answer-area' }, [
        el('div', { class: 'q-inline-form' }, [
            inlineInput,
            el('div', { class: 'q-inline-actions' }, [inlineStatus, inlineSend]),
        ]),
    ]);

    const msgEl = el('div', { class: 'message question-bubble' }, [
        el('div', { class: 'q-label' }, [text('Agent Question')]),
        el('div', { class: 'q-text' }, [text(questionText)]),
        ...(context ? [el('div', { class: 'q-context' }, [text(context)])] : []),
        answerArea,
    ]);

    _questionBubbles.set(questionId, msgEl);
    inner.appendChild(msgEl);
    scrollToBottom();
    return msgEl;
}

/** Update a question bubble to show the user's answer. */
function markQuestionAnswered(questionId, answer) {
    const bubble = _questionBubbles.get(questionId);
    if (!bubble) return;
    const area = bubble.querySelector('.q-answer-area');
    if (!area || area.querySelector('.q-answer')) return; // already marked
    // Replace inline form (if still present) with the final answer display.
    area.innerHTML = '';
    area.appendChild(
        el('div', { class: 'q-answer' }, [
            el('div', { class: 'q-answer-label' }, [text('Your answer')]),
            el('div', { class: 'q-answer-text' }, [text(answer)]),
        ])
    );
}

/** Update a question bubble to show it was dismissed (clears the inline form). */
function markQuestionDismissed(questionId) {
    const bubble = _questionBubbles.get(questionId);
    if (!bubble) return;
    const area = bubble.querySelector('.q-answer-area');
    if (!area || area.querySelector('.q-dismissed')) return;
    area.innerHTML = '';
    area.appendChild(el('div', { class: 'q-dismissed' }, [text('Dismissed')]));
}

/**
 * Render a persisted answered-question message (from loadMessages history).
 * Content format: "[User answered your question]\nQ: ...\nA: ..."
 */
function renderAnsweredQuestion(content) {
    const inner = _messagesInner();
    const scroll = _messagesScroll();

    // Parse Q: / A: lines
    const lines = content.split('\n');
    let questionText = '';
    let answerText = '';
    for (const line of lines) {
        if (line.startsWith('Q: ')) questionText = line.slice(3);
        else if (line.startsWith('A: ')) answerText = line.slice(3);
    }

    const msgEl = el('div', { class: 'message question-bubble' }, [
        el('div', { class: 'q-label' }, [text('Agent Question')]),
        el('div', { class: 'q-text' }, [text(questionText || content)]),
        el('div', { class: 'q-answer' }, [
            el('div', { class: 'q-answer-label' }, [text('Your answer')]),
            el('div', { class: 'q-answer-text' }, [text(answerText)]),
        ]),
    ]);

    inner.appendChild(msgEl);
    scrollToBottom();
}

/**
 * Render a persisted dismissed-question message (from loadMessages history).
 * Content format: "[User dismissed your question without answering]\nQ: ..."
 */
function renderDismissedQuestion(content) {
    const inner = _messagesInner();
    const scroll = _messagesScroll();

    const lines = content.split('\n');
    let questionText = '';
    for (const line of lines) {
        if (line.startsWith('Q: ')) { questionText = line.slice(3); break; }
    }

    const msgEl = el('div', { class: 'message question-bubble' }, [
        el('div', { class: 'q-label' }, [text('Agent Question')]),
        el('div', { class: 'q-text' }, [text(questionText || content)]),
        el('div', { class: 'q-answer-area' }, [
            el('div', { class: 'q-dismissed' }, [text('Dismissed')]),
        ]),
    ]);

    inner.appendChild(msgEl);
    scrollToBottom();
}

/**
 * Fetch pending questions for the given session and render question bubbles.
 * Called on session load so bubbles survive page refresh.
 */
async function loadPendingQuestions(sid) {
    try {
        const data = await get('/api/questions');
        for (const q of (data.questions || []).filter(q => q.session_id === sid)) {
            appendQuestionBubble(q.id, q.question, q.context || '');
        }
    } catch { /* non-fatal — bell panel will still show the question */ }
}

/** Render a compact command-result card. rows = [{key, value, badge?}], customBody = optional DOM node */
function appendCommandCard(title, rows, customBody) {
    const inner = _messagesInner();
    const scroll = _messagesScroll();
    const emptyEl = inner.querySelector('.empty-state');
    if (emptyEl) emptyEl.remove();

    const header = el('div', { class: 'cmd-card-header' }, [text(title)]);
    const body = el('div', { class: 'cmd-card-body' });

    if (customBody) {
        body.appendChild(customBody);
    } else if (rows) {
        for (const r of rows) {
            const valChildren = [];
            if (r.badge) {
                valChildren.push(el('span', { class: `cmd-badge cmd-badge-${r.badge}` }, [text(r.value)]));
            } else {
                valChildren.push(text(r.value));
            }
            body.appendChild(el('div', { class: 'cmd-card-row' }, [
                el('span', { class: 'cmd-card-key' }, [text(r.key)]),
                el('span', { class: 'cmd-card-val' }, valChildren),
            ]));
        }
    }

    const card = el('div', { class: 'cmd-card' }, [header, body]);
    inner.appendChild(card);
    scrollToBottom();
}

// ---------------------------------------------------------------------------
// Tool call grouping
// ---------------------------------------------------------------------------

/** One-line reading of a tool call's arguments. Shared by the live
 *  placeholder row and the finished row so a tool does not visibly change
 *  its own description the instant it returns. */
function _toolArgsSummary(name, args) {
    if (!args || typeof args !== 'object') return '';
    if (name === 'bash' && args.command) return '$ ' + args.command;
    if (args.path) return args.path;
    return Object.entries(args).map(([k, v]) => {
        const str = String(v);
        return `${k}: ${str.length > 60 ? str.slice(0, 60) + '...' : str}`;
    }).join(', ');
}

// Placeholder rows for tools that have STARTED but not returned. tool.start
// carries no call id today, so the fallback key is the tool name and the
// queue is FIFO — two concurrent calls to the same tool resolve in the order
// they were announced, which is the order the executor runs them in.
const _pendingToolItems = new Map();   // key -> [placeholder element, ...]

function _toolKey(name, callId) { return callId ? `id:${callId}` : `name:${name}`; }

/**
 * Append a live "running" row for a tool that just started. Until this
 * existed the transcript showed nothing at all for the whole runtime of a
 * slow call — a 40-second bash looked like a hung UI, with only a status-bar
 * word to say otherwise.
 */
function _appendRunningTool(name, args, callId) {
    const group = ensureToolGroup();
    const items = group.querySelector('.tool-group-items');
    const itemEl = el('div', { class: 'tool-item running' }, [
        el('div', { class: 'tool-item-header' }, [
            el('span', { class: 'tool-item-running-dot', 'aria-hidden': 'true' }),
            el('div', { class: 'tool-item-name' }, [text(name)]),
            el('span', { class: 'tool-item-summary' }, [text(_toolArgsSummary(name, args))]),
        ]),
    ]);
    items.appendChild(itemEl);
    const key = _toolKey(name, callId);
    if (!_pendingToolItems.has(key)) _pendingToolItems.set(key, []);
    _pendingToolItems.get(key).push(itemEl);
    _toolGroupRunning++;
    _updateToolGroupHeader(group);
    scrollToBottom();
    return itemEl;
}

/** The placeholder a finished call belongs to, if it is still on screen. */
function _takeRunningTool(name, callId) {
    const keys = callId ? [_toolKey(name, callId), `name:${name}`] : [`name:${name}`];
    for (const key of keys) {
        const queue = _pendingToolItems.get(key);
        if (!queue) continue;
        while (queue.length) {
            const node = queue.shift();
            if (!queue.length) _pendingToolItems.delete(key);
            if (node.isConnected) {
                _toolGroupRunning = Math.max(0, _toolGroupRunning - 1);
                return node;
            }
        }
        _pendingToolItems.delete(key);
    }
    return null;
}

/** Drop any placeholder whose result never arrived (cancelled turn, dropped
 *  stream) — a dot pulsing forever is worse than no row at all. */
function _clearRunningTools() {
    for (const queue of _pendingToolItems.values()) {
        for (const node of queue) node.remove();
    }
    _pendingToolItems.clear();
    _toolGroupRunning = 0;
}

/** ms → the shortest honest reading of it. A tool chip that says "44500ms"
 *  makes the reader do the division; the header summing a whole round would
 *  be worse still. */
function _humanizeMs(ms) {
    const n = Number(ms) || 0;
    if (n < 1000) return `${Math.round(n)}ms`;
    if (n < 10000) return `${(n / 1000).toFixed(1)}s`;
    if (n < 60000) return `${Math.round(n / 1000)}s`;
    const mins = Math.floor(n / 60000);
    const secs = Math.round((n % 60000) / 1000);
    return secs ? `${mins}m ${secs}s` : `${mins}m`;
}

/** "5 tool calls · 1 failed · 48s" — the header is the only thing visible
 *  when a group is collapsed, so it has to carry the two facts a reader
 *  actually needs: did anything fail, and how long did the round cost. */
function _updateToolGroupHeader(group) {
    const label = group.querySelector('.tg-label');
    if (!label) return;
    const parts = [_toolGroupCount === 1 ? '1 tool call' : `${_toolGroupCount} tool calls`];
    if (_toolGroupRunning) parts.push(`${_toolGroupRunning} running`);
    if (_toolGroupErrors) parts.push(`${_toolGroupErrors} failed`);
    if (_toolGroupLatency) parts.push(_humanizeMs(_toolGroupLatency));
    label.textContent = parts.join(' · ');
    group.classList.toggle('has-error', _toolGroupErrors > 0);
}

/**
 * Make a non-button element behave like a disclosure control: focusable,
 * operable with Enter and Space, and announcing whether it is open. The tool
 * group and tool item headers were plain divs with a click listener, so a
 * keyboard user could not open a single tool result in the transcript.
 *
 * @param {HTMLElement} headerEl
 * @param {boolean} expanded  its state right now
 * @param {function} toggle   flips the state and returns the new one
 */
function _makeDisclosure(headerEl, expanded, toggle) {
    headerEl.setAttribute('role', 'button');
    headerEl.setAttribute('tabindex', '0');
    headerEl.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    const fire = (e) => {
        e.preventDefault();
        e.stopPropagation();
        _syncDisclosure(headerEl, toggle());
    };
    headerEl.addEventListener('click', fire);
    headerEl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') fire(e);
    });
}

/** Keep aria-expanded honest when the state is changed by code rather than by
 *  the user — an auto-collapse, or a search hit forcing a group open. */
function _syncDisclosure(headerEl, expanded) {
    if (headerEl) headerEl.setAttribute('aria-expanded', expanded ? 'true' : 'false');
}

function ensureToolGroup() {
    if (_toolGroup) return _toolGroup;
    const inner = _messagesInner();
    const scroll = _messagesScroll();
    const emptyEl = inner.querySelector('.empty-state');
    if (emptyEl) emptyEl.remove();

    _toolGroupCount = 0;
    _toolGroupErrors = 0;
    _toolGroupLatency = 0;
    _toolGroupRunning = 0;
    const header = el('div', { class: 'tool-group-header' }, [
        el('span', { class: 'tg-toggle' }, [icon('chevron-down', { size: 10 })]),
        el('span', { class: 'tg-label' }, [text('0 tool calls')]),
    ]);
    _makeDisclosure(header, true, () => {
        const group = header.closest('.tool-group');
        if (!group) return true;
        group.classList.toggle('collapsed');
        const expanded = !group.classList.contains('collapsed');
        _rememberExpanded(group.dataset.groupKey || '', expanded);
        return expanded;
    });
    _toolGroup = el('div', { class: 'tool-group' }, [
        header,
        el('div', { class: 'tool-group-items' }),
    ]);
    inner.appendChild(_toolGroup);
    scrollToBottom();
    return _toolGroup;
}

function closeToolGroup() {
    if (!_toolGroup) return;
    // A round that contains a failure is never folded away: auto-collapsing
    // it is exactly how a red row goes unread. Neither is a round the reader
    // opened by hand — a soft reload used to shut every one of them, and the
    // reader had no way of knowing which had been open a second earlier.
    const groupKey = _toolGroup.dataset.groupKey || '';
    const remembered = _recallExpanded(groupKey);
    const collapse = remembered === null
        ? (_toolGroupCount > 2 && _toolGroupErrors === 0)
        : remembered === false;
    if (collapse) {
        _toolGroup.classList.add('collapsed');
        _syncDisclosure(_toolGroup.querySelector('.tool-group-header'), false);
    }
    _rememberExpanded(groupKey, !collapse);
    _clearRunningTools();
    _toolGroup = null;
    _toolGroupCount = 0;
    _toolGroupErrors = 0;
    _toolGroupLatency = 0;
}

function appendToolToGroup(name, preview, fullResult, isTruncated, wasError = false, latencyMs = 0, args = null, serverTruncated = false, replaceEl = null, itemKey = '') {
    const group = ensureToolGroup();
    const items = group.querySelector('.tool-group-items');
    // A group has no identity of its own, so it derives one from its first
    // row — the one row that cannot move out from under it on a re-render.
    // The 'grp:' prefix keeps it out of the row's own namespace: without it,
    // opening the group also marked its first row open.
    if (itemKey && !group.dataset.groupKey) group.dataset.groupKey = `grp:${itemKey}`;
    _toolGroupCount++;
    if (wasError) _toolGroupErrors++;
    _toolGroupLatency += Number(latencyMs) || 0;
    _updateToolGroupHeader(group);

    // --- Compact header row (always visible) ---
    const chevron = el('span', { class: 'tool-item-chevron' }, [icon('chevron-right', { size: 10 })]);

    const nameChildren = [text(name)];
    if (latencyMs) {
        const latencyClass = latencyMs < 500 ? 'fast' : latencyMs < 2000 ? 'medium' : 'slow';
        nameChildren.push(el('span', { class: `tool-latency ${latencyClass}` }, [text(_humanizeMs(latencyMs))]));
    }
    const nameEl = el('div', { class: 'tool-item-name' }, nameChildren);

    // Inline summary — single-line preview of args or output
    let summaryText = _toolArgsSummary(name, args);
    if (!summaryText && preview) {
        summaryText = preview.slice(0, 80).replace(/\n/g, ' ');
    }
    // A failure used to REPLACE the summary with a bare "(error)" — throwing
    // away the one thing that says which call failed. Keep the args and mark
    // them instead.
    if (wasError) summaryText = summaryText ? `${summaryText} — error` : 'error';
    const summaryEl = el('span', { class: 'tool-item-summary' }, [text(summaryText)]);

    const headerChildren = [chevron, nameEl, summaryEl];
    const headerEl = el('div', { class: 'tool-item-header' }, headerChildren);

    // "view" jumps to the file the call wrote. It used to sit INSIDE the
    // header — which is a role="button" — so it was a control nested in a
    // button: invalid ARIA, and unreachable from the keyboard because the
    // outer button owns the tab stop. It is a sibling now, in a flex row
    // that the header still fills. (A1)
    let headRowEl = headerEl;
    const fileMatch = (fullResult || preview || '').match(/Written \d+ chars to (.+)/);
    if (fileMatch) {
        const [, filePath] = fileMatch;
        const viewBtn = el('button', {
            class: 'file-view-btn',
            type: 'button',
            'aria-label': `Open ${filePath.trim()} in the Explorer`,
        }, [text('view')]);
        viewBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            openWorkspaceFile(filePath.trim());
        });
        headRowEl = el('div', { class: 'tool-item-headrow' }, [headerEl, viewBtn]);
    }

    // --- Expandable body (hidden until .expanded) ---
    const bodyChildren = [];

    // Full args display
    if (args && typeof args === 'object' && Object.keys(args).length > 0) {
        let argsText = '';
        if (name === 'bash' && args.command) {
            argsText = '$ ' + args.command;
        } else if (args.path) {
            argsText = args.path;
        } else {
            argsText = Object.entries(args).map(([k, v]) => `${k}: ${v}`).join(', ');
        }
        if (argsText) {
            bodyChildren.push(el('div', { class: 'tool-item-args' }, [text(argsText)]));
        }
    }

    // Browse web enrichment
    if (name === 'browse_web' && !wasError) {
        const match = (fullResult || preview || '').match(/^# (.+?)\n\*\*URL:\*\* (.+?)\n/);
        if (match) {
            const [, pageTitle, pageUrl] = match;
            bodyChildren.push(el('div', { class: 'tool-browse-header' }, [
                el('span', { class: 'tool-browse-icon' }, [text('\uD83C\uDF10')]),
                el('span', { class: 'tool-browse-title' }, [text(pageTitle)]),
                el('span', { class: 'tool-browse-url' }, [text(_extractHostname(pageUrl))]),
            ]));
        }
    }

    // Content/output
    const contentEl = el('div', { class: 'tool-item-content' }, [text(preview || '(no output)')]);
    bodyChildren.push(contentEl);

    // Expand toggle — always created; revealed after expansion if content overflows
    let fullyExpanded = false;
    let toggleRevealed = false;
    // The server truncates oversized tool output before it ever reaches the
    // client, so "show more" would otherwise promise a full result that does
    // not exist on this side. Say which kind of more it is.
    const truncNote = serverTruncated ? ' (truncated)' : '';
    const toggleBtn = el('button', { class: 'tool-toggle tool-toggle--hidden' }, [text('show more' + truncNote)]);
    toggleBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        fullyExpanded = !fullyExpanded;
        if (isTruncated) {
            clear(contentEl);
            contentEl.appendChild(text(fullyExpanded ? fullResult : preview));
        }
        contentEl.classList.toggle('fully-expanded', fullyExpanded);
        toggleBtn.textContent = (fullyExpanded ? 'show less' : 'show more') + truncNote;
    });
    bodyChildren.push(toggleBtn);

    const bodyEl = el('div', { class: 'tool-item-body' }, bodyChildren);

    // --- Assemble item ---
    const isBrowse = name === 'browse_web' && !wasError;
    // A row the reader opened stays open across a re-render or a soft reload.
    const startExpanded = _recallExpanded(itemKey) === true;
    const itemEl = el('div', {
        class: `tool-item${wasError ? ' error' : ''}${isBrowse ? ' browse' : ''}${startExpanded ? ' expanded' : ''}`
    }, [headRowEl, bodyEl]);

    const revealToggle = () => {
        if (toggleRevealed) return;
        requestAnimationFrame(() => {
            if (isTruncated || contentEl.scrollHeight > contentEl.clientHeight + 2) {
                contentEl.classList.add('overflows');
                toggleBtn.classList.remove('tool-toggle--hidden');
                toggleRevealed = true;
            }
        });
    };

    // Header toggles item expansion; the show-more button appears after the
    // first open if the content actually overflows.
    _makeDisclosure(headerEl, startExpanded, () => {
        itemEl.classList.toggle('expanded');
        const nowExpanded = itemEl.classList.contains('expanded');
        _rememberExpanded(itemKey, nowExpanded);
        if (nowExpanded) revealToggle();
        return nowExpanded;
    });
    if (startExpanded) revealToggle();

    // Replace the live placeholder in place when there is one, so a finished
    // row does not jump to the bottom of the group the moment it lands.
    if (replaceEl && replaceEl.isConnected) replaceEl.replaceWith(itemEl);
    else items.appendChild(itemEl);

    const scroll = _messagesScroll();
    scrollToBottom();
}

// ---------------------------------------------------------------------------
// Copy buttons for code blocks
// ---------------------------------------------------------------------------

function addCopyButtons(container) {
    const pres = container.querySelectorAll('pre');
    pres.forEach(pre => {
        if (pre.parentElement.classList.contains('code-block-wrap')) return;
        const wrap = document.createElement('div');
        wrap.className = 'code-block-wrap';
        pre.parentNode.insertBefore(wrap, pre);
        wrap.appendChild(pre);
        const btn = document.createElement('button');
        btn.className = 'copy-btn';
        btn.textContent = 'copy';
        btn.addEventListener('click', () => {
            const code = pre.querySelector('code') || pre;
            navigator.clipboard.writeText(code.textContent).then(() => {
                btn.textContent = 'copied';
                setTimeout(() => { btn.textContent = 'copy'; }, 1500);
            });
        });
        wrap.appendChild(btn);
    });
}

// ---------------------------------------------------------------------------
// File reference detection and viewing
// ---------------------------------------------------------------------------

function processFileRefs(container) {
    // Detect "Written N chars to {path}" patterns in tool results
    const textNodes = container.querySelectorAll('.tool-item-content, .content');
    textNodes.forEach(node => {
        const nodeText = node.textContent || '';
        const match = nodeText.match(/Written \d+ chars to (.+)/);
        if (match && !node.querySelector('.file-view-btn')) {
            const filePath = match[1].trim();
            const btn = el('button', { class: 'file-view-btn' }, [text('view')]);
            btn.addEventListener('click', () => {
                openWorkspaceFile(filePath);
            });
            node.parentNode.appendChild(btn);
        }
    });

    // Also detect workspace file links
    const links = container.querySelectorAll('a[href*="/workspace/"]');
    links.forEach(link => {
        const url = link.getAttribute('href');
        if (!url) return;
        const btn = el('button', { class: 'file-view-btn inline' }, [text('view')]);
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            const path = url.startsWith('/workspace/') ? url.slice('/workspace/'.length) : url;
            openWorkspaceFile(path);
        });
        link.parentNode.insertBefore(btn, link.nextSibling);
    });

    // Make inline code spans that look like file paths clickable
    const FILE_PATH_RE = /^[a-zA-Z0-9_.][a-zA-Z0-9_.\-]*(?:\/[a-zA-Z0-9_.][a-zA-Z0-9_.\-]*)+$/;
    const codeEls = container.querySelectorAll('.content code');
    codeEls.forEach(codeEl => {
        if (codeEl.closest('pre')) return;
        if (codeEl.classList.contains('file-path-link')) return;
        const pathText = codeEl.textContent.trim();
        if (pathText.length > 200 || pathText.includes('\n')) return;
        if (!FILE_PATH_RE.test(pathText)) return;
        codeEl.classList.add('file-path-link');
        codeEl.title = 'Open in Explorer';
        codeEl.addEventListener('click', () => openWorkspaceFile(pathText));
    });
}

function openWorkspaceFile(path) {
    openFilePanel({ tab: 'workspace', file: path });
}

// ---------------------------------------------------------------------------
// Scout report (ephemeral — not stored in DB or conversation context)
// ---------------------------------------------------------------------------

function renderScoutReport(event) {
    const inner = _messagesInner();
    const scroll = _messagesScroll();
    const emptyEl = inner.querySelector('.empty-state');
    if (emptyEl) emptyEl.remove();

    // Build summary line for header
    const count = (event.tools || []).length;
    const ms = event.latency_ms || 0;
    const cache = event.from_cache ? ' cached' : event.from_fallback ? ' fallback' : '';
    const headerText = `scout — ${count} tools, ${ms}ms${cache}`;

    const header = el('div', { class: 'scout-activity-header' }, [
        el('span', { class: 'scout-toggle' }, [icon('chevron-down', { size: 10 })]),
        el('span', {}, [text(headerText)]),
    ]);

    // Build report body
    const sections = [];

    if (event.approach) {
        sections.push(el('div', { class: 'scout-section' }, [
            el('div', { class: 'scout-section-label' }, [text('approach')]),
            el('div', { class: 'scout-section-body' }, [text(event.approach)]),
        ]));
    }

    if (event.tools && event.tools.length) {
        const toolText = event.tools.join(', ');
        const rationale = event.tool_rationale ? `\n${event.tool_rationale}` : '';
        sections.push(el('div', { class: 'scout-section' }, [
            el('div', { class: 'scout-section-label' }, [text('tools')]),
            el('div', { class: 'scout-section-body' }, [text(toolText + rationale)]),
        ]));
    }

    if (event.memory) {
        sections.push(el('div', { class: 'scout-section' }, [
            el('div', { class: 'scout-section-label' }, [text('memory')]),
            el('div', { class: 'scout-section-body' }, [text(event.memory)]),
        ]));
    }

    if (event.model) {
        const modelText = `${event.model}${event.model_rationale ? ' — ' + event.model_rationale : ''}`;
        sections.push(el('div', { class: 'scout-section' }, [
            el('div', { class: 'scout-section-label' }, [text('model')]),
            el('div', { class: 'scout-section-body' }, [text(modelText)]),
        ]));
    }

    if (event.identity) {
        sections.push(el('div', { class: 'scout-section' }, [
            el('div', { class: 'scout-section-label' }, [text('identity')]),
            el('div', { class: 'scout-section-body' }, [text(event.identity)]),
        ]));
    }

    if (event.scout_model) {
        sections.push(el('div', { class: 'scout-section' }, [
            el('div', { class: 'scout-section-label' }, [text('scout model')]),
            el('div', { class: 'scout-section-body' }, [text(event.scout_model)]),
        ]));
    }

    const body = el('div', { class: 'scout-body' }, sections);

    // Collapsed by default
    const container = el('div', { class: 'scout-activity scout-collapsed' }, [header, body]);
    _makeDisclosure(header, false, () => {
        container.classList.toggle('scout-collapsed');
        return !container.classList.contains('scout-collapsed');
    });

    inner.appendChild(container);
    scrollToBottom();
}

// ---------------------------------------------------------------------------
// Model divider — pill-with-rules row inserted into the chat stream when
// the effective model changes mid-turn. Visually marks "everything below
// this line was produced by <model>".
// ---------------------------------------------------------------------------

function renderModelDivider(info) {
    const inner = _messagesInner();
    const scroll = _messagesScroll();
    const emptyEl = inner.querySelector('.empty-state');
    if (emptyEl) emptyEl.remove();

    const to = info.to || '';
    const baseline = info.baseline || info.from || '';
    const isOverride = !!info.active;
    // "kind" — 'override' if jumping to a non-baseline model, 'restore' if
    // returning to baseline. Drives the chip color.
    const kind = isOverride ? 'override' : 'restore';

    const pill = el('span', { class: `model-divider-pill model-divider-${kind}` }, []);
    if (isOverride && baseline && baseline !== to) {
        pill.appendChild(document.createTextNode(baseline));
        const arrow = el('span', { class: 'model-divider-arrow' });
        arrow.textContent = ' ⇄ ';
        pill.appendChild(arrow);
    }
    pill.appendChild(document.createTextNode(to));

    const row = el('div', { class: 'model-divider' }, [
        el('span', { class: 'model-divider-rule' }),
        pill,
        el('span', { class: 'model-divider-rule' }),
    ]);

    inner.appendChild(row);
    scrollToBottom();
}

// ---------------------------------------------------------------------------
// Reflect card (post-execution verification verdict)
// ---------------------------------------------------------------------------

function renderReflectCard(event) {
    const inner = _messagesInner();
    const scroll = _messagesScroll();
    const emptyEl = inner.querySelector('.empty-state');
    if (emptyEl) emptyEl.remove();

    const verdict = event.verdict || 'pass';
    const ms = event.latency_ms || 0;
    const headerText = `reflect — ${verdict}${ms ? `, ${ms}ms` : ''}`;

    const header = el('div', { class: 'reflect-header' }, [
        el('span', { class: 'scout-toggle' }, [icon('chevron-down', { size: 10 })]),
        el('span', { class: `reflect-badge reflect-${verdict}` }, [text(verdict)]),
        el('span', {}, [text(headerText)]),
    ]);

    const sections = [];

    if (event.reasoning) {
        sections.push(el('div', { class: 'scout-section' }, [
            el('div', { class: 'scout-section-label' }, [text('reasoning')]),
            el('div', { class: 'scout-section-body' }, [text(event.reasoning)]),
        ]));
    }

    if (event.strategy) {
        sections.push(el('div', { class: 'scout-section' }, [
            el('div', { class: 'scout-section-label' }, [text('strategy')]),
            el('div', { class: 'scout-section-body' }, [text(event.strategy)]),
        ]));
    }

    if (event.missing) {
        sections.push(el('div', { class: 'scout-section' }, [
            el('div', { class: 'scout-section-label' }, [text('missing')]),
            el('div', { class: 'scout-section-body' }, [text(event.missing)]),
        ]));
    }

    if (event.reflect_model) {
        sections.push(el('div', { class: 'scout-section' }, [
            el('div', { class: 'scout-section-label' }, [text('reflect model')]),
            el('div', { class: 'scout-section-body' }, [text(event.reflect_model)]),
        ]));
    }

    const body = el('div', { class: 'scout-body' }, sections);

    // Collapsed by default for pass, expanded for retry/escalate
    const collapsed = verdict === 'pass' ? ' scout-collapsed' : '';
    const container = el('div', { class: `reflect-activity${collapsed}` }, [header, body]);
    _makeDisclosure(header, !collapsed, () => {
        container.classList.toggle('scout-collapsed');
        return !container.classList.contains('scout-collapsed');
    });

    inner.appendChild(container);
    scrollToBottom();
}

function renderEvalCard(event) {
    // event shape: {results: [{feature, title, passed, scores, feedback}], all_passed}
    const inner = _messagesInner();
    const scroll = _messagesScroll();
    const emptyEl = inner.querySelector('.empty-state');
    if (emptyEl) emptyEl.remove();

    const results = Array.isArray(event.results) ? event.results : [];
    const allPassed = !!event.all_passed;
    const passedCount = results.filter(r => r && r.passed).length;
    const verdict = allPassed ? 'pass' : 'fail';
    const headerText = `eval — ${passedCount}/${results.length} passed`;

    const reflectModel = event.reflect_model || '';
    const headerChildren = [
        el('span', { class: 'scout-toggle' }, [icon('chevron-down', { size: 10 })]),
        el('span', { class: `reflect-badge reflect-${verdict}` }, [text(verdict)]),
        el('span', {}, [text(headerText)]),
    ];
    if (reflectModel) {
        headerChildren.push(el('span', { class: 'reflect-model-chip', title: 'reflect model' }, [text(reflectModel)]));
    }
    const header = el('div', { class: 'reflect-header' }, headerChildren);

    const sections = [];
    for (const r of results) {
        if (!r) continue;
        const status = r.passed ? '✓ PASS' : '✗ FAIL';
        const titleLine = el('div', { class: 'scout-section-label' }, [
            text(`${status} — ${r.title || r.feature || '(untitled)'}`),
        ]);
        const bodyParts = [];
        const scores = r.scores && typeof r.scores === 'object' ? r.scores : {};
        for (const [crit, score] of Object.entries(scores)) {
            const scoreFmt = (typeof score === 'number') ? score.toFixed(2) : String(score);
            bodyParts.push(el('div', { class: 'scout-section-body' }, [
                text(`[${scoreFmt}] ${crit}`),
            ]));
        }
        if (r.feedback) {
            bodyParts.push(el('div', { class: 'scout-section-body' }, [
                el('strong', {}, [text('Feedback: ')]),
                text(r.feedback),
            ]));
        }
        sections.push(el('div', { class: 'scout-section' }, [titleLine, ...bodyParts]));
    }

    const body = el('div', { class: 'scout-body' }, sections);
    // Collapsed by default if everything passed; expanded if any failed.
    const collapsed = allPassed ? ' scout-collapsed' : '';
    const container = el('div', { class: `reflect-activity${collapsed}` }, [header, body]);
    _makeDisclosure(header, !collapsed, () => {
        container.classList.toggle('scout-collapsed');
        return !container.classList.contains('scout-collapsed');
    });

    inner.appendChild(container);
    scrollToBottom();
}

// Deterministic gate results, replayed from a persisted role='eval' row of
// shape {kind:'gate', attempt, gates:[{name, command, passed, exit_code,
// output_tail, reused, error}]}. Distinct from renderEvalCard, which renders
// the LLM feature judge — the two share a row role and nothing else.
function renderGateCard(event) {
    const inner = _messagesInner();
    const emptyEl = inner.querySelector('.empty-state');
    if (emptyEl) emptyEl.remove();

    const gates = Array.isArray(event.gates) ? event.gates : [];
    if (!gates.length) return;   // nothing ran; don't manufacture a verdict

    const passedCount = gates.filter(g => g && g.passed).length;
    const allPassed = passedCount === gates.length;
    const verdict = allPassed ? 'pass' : 'fail';
    const attempt = Number(event.attempt) || 0;
    const attemptSuffix = attempt > 1 ? ` · attempt ${attempt}` : '';

    const header = el('div', { class: 'reflect-header' }, [
        el('span', { class: 'scout-toggle' }, [icon('chevron-down', { size: 10 })]),
        el('span', { class: `reflect-badge reflect-${verdict}` }, [text(verdict)]),
        el('span', {}, [text(`gates — ${passedCount}/${gates.length} passed${attemptSuffix}`)]),
    ]);

    const sections = [];
    for (const g of gates) {
        if (!g) continue;
        const status = g.passed ? '✓ PASS' : '✗ FAIL';
        const marks = [];
        if (g.reused) marks.push('reused');
        if (g.exit_code !== null && g.exit_code !== undefined && !g.passed) marks.push(`exit ${g.exit_code}`);
        const suffix = marks.length ? ` (${marks.join(', ')})` : '';
        const parts = [
            el('div', { class: 'scout-section-label' }, [text(`${status} — ${g.name || '(unnamed)'}${suffix}`)]),
        ];
        if (g.command) {
            parts.push(el('div', { class: 'scout-section-body' }, [text(`$ ${g.command}`)]));
        }
        // Only surface output for failures — a passing gate's tail is noise.
        if (!g.passed && g.output_tail) {
            parts.push(el('pre', { class: 'scout-section-body' }, [text(g.output_tail)]));
        }
        if (g.error) {
            parts.push(el('div', { class: 'scout-section-body' }, [
                el('strong', {}, [text('Error: ')]),
                text(g.error),
            ]));
        }
        sections.push(el('div', { class: 'scout-section' }, parts));
    }

    const body = el('div', { class: 'scout-body' }, sections);
    const collapsed = allPassed ? ' scout-collapsed' : '';
    const container = el('div', { class: `reflect-activity${collapsed}` }, [header, body]);
    _makeDisclosure(header, !collapsed, () => {
        container.classList.toggle('scout-collapsed');
        return !container.classList.contains('scout-collapsed');
    });

    inner.appendChild(container);
    scrollToBottom();
}

function _extractHostname(url) {
    try { return new URL(url).hostname; } catch { return url.slice(0, 40); }
}

// ---------------------------------------------------------------------------
// In-session transcript search (Ctrl+F) — walks text nodes in the loaded
// transcript, wraps matches in <mark>, navigates with Enter / Shift+Enter.
// ---------------------------------------------------------------------------

let _searchBarEl = null;
let _searchMarks = [];
let _searchCurrent = -1;
let _searchDebounce = null;

function _clearSearchMarks() {
    for (const mark of _searchMarks) {
        const parent = mark.parentNode;
        if (!parent) continue;
        parent.replaceChild(document.createTextNode(mark.textContent), mark);
        parent.normalize();
    }
    _searchMarks = [];
    _searchCurrent = -1;
}

function _runTranscriptSearch(query) {
    _clearSearchMarks();
    const counter = _searchBarEl?.querySelector('.ts-counter');
    if (!query || query.length < 2) {
        if (counter) counter.textContent = '';
        return;
    }
    const inner = _messagesInner();
    if (!inner) return;

    const q = query.toLowerCase();
    // Collect first: mutating while walking breaks the TreeWalker.
    const textNodes = [];
    const walker = document.createTreeWalker(inner, NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
            if (!node.nodeValue || !node.nodeValue.toLowerCase().includes(q)) return NodeFilter.FILTER_REJECT;
            // Don't match inside collapsed tool bodies' hidden copy buttons etc.
            if (node.parentElement?.closest('.code-copy-btn, .msg-actions')) return NodeFilter.FILTER_REJECT;
            return NodeFilter.FILTER_ACCEPT;
        },
    });
    let n;
    while ((n = walker.nextNode())) textNodes.push(n);

    for (const node of textNodes) {
        let remaining = node;
        // A text node can hold several matches; split-and-wrap left to right.
        for (;;) {
            const idx = remaining.nodeValue.toLowerCase().indexOf(q);
            if (idx === -1) break;
            const matchNode = remaining.splitText(idx);
            remaining = matchNode.splitText(query.length);
            const mark = document.createElement('mark');
            mark.className = 'ts-mark';
            matchNode.parentNode.replaceChild(mark, matchNode);
            mark.appendChild(matchNode);
            _searchMarks.push(mark);
        }
    }

    if (counter) counter.textContent = _searchMarks.length ? `1/${_searchMarks.length}` : 'no matches';
    if (_searchMarks.length) _gotoSearchMatch(0);
}

function _gotoSearchMatch(idx) {
    if (!_searchMarks.length) return;
    if (_searchCurrent >= 0) _searchMarks[_searchCurrent]?.classList.remove('current');
    _searchCurrent = ((idx % _searchMarks.length) + _searchMarks.length) % _searchMarks.length;
    const mark = _searchMarks[_searchCurrent];
    mark.classList.add('current');
    // Expand a collapsed tool group / body so the hit is actually visible.
    const group = mark.closest('.tool-group.collapsed');
    if (group) {
        group.classList.remove('collapsed');
        _syncDisclosure(group.querySelector('.tool-group-header'), true);
    }
    const toolItem = mark.closest('.tool-item');
    if (toolItem && !toolItem.classList.contains('expanded')) {
        toolItem.classList.add('expanded');
        _syncDisclosure(toolItem.querySelector('.tool-item-header'), true);
    }
    mark.scrollIntoView({ block: 'center', behavior: 'smooth' });
    const counter = _searchBarEl?.querySelector('.ts-counter');
    if (counter) counter.textContent = `${_searchCurrent + 1}/${_searchMarks.length}`;
}

function openTranscriptSearch() {
    if (!state.sid) return;
    if (_searchBarEl) {
        _searchBarEl.querySelector('input')?.focus();
        return;
    }
    const input = el('input', {
        type: 'text',
        placeholder: 'Search transcript…',
        'aria-label': 'Search transcript',
    });
    const counter = el('span', { class: 'ts-counter', role: 'status', 'aria-live': 'polite' });
    const prevBtn = el('button', {
        class: 'ts-nav', title: 'Previous match (Shift+Enter)', 'aria-label': 'Previous match',
    }, [icon('arrow-up', { size: 12 })]);
    const nextBtn = el('button', {
        class: 'ts-nav', title: 'Next match (Enter)', 'aria-label': 'Next match',
    }, [icon('arrow-down', { size: 12 })]);
    const closeBtn = el('button', {
        class: 'ts-close', title: 'Close (Esc)', 'aria-label': 'Close transcript search',
    }, [icon('x', { size: 12 })]);

    input.addEventListener('input', () => {
        clearTimeout(_searchDebounce);
        _searchDebounce = setTimeout(() => _runTranscriptSearch(input.value.trim()), 200);
    });
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            _gotoSearchMatch(_searchCurrent + (e.shiftKey ? -1 : 1));
        } else if (e.key === 'Escape') {
            e.preventDefault();
            closeTranscriptSearch();
        }
    });
    prevBtn.addEventListener('click', () => _gotoSearchMatch(_searchCurrent - 1));
    nextBtn.addEventListener('click', () => _gotoSearchMatch(_searchCurrent + 1));
    closeBtn.addEventListener('click', closeTranscriptSearch);

    _searchBarEl = el('div', { id: 'transcript-search' }, [input, counter, prevBtn, nextBtn, closeBtn]);
    document.getElementById('main').appendChild(_searchBarEl);
    input.focus();
}

function closeTranscriptSearch() {
    _clearSearchMarks();
    if (_searchBarEl) {
        _searchBarEl.remove();
        _searchBarEl = null;
    }
}

// ---------------------------------------------------------------------------
// Ctrl+K session switcher — fuzzy-find palette over the loaded session list.
// ---------------------------------------------------------------------------

let _paletteEl = null;
let _closePaletteOverlay = null;   // teardown from a11y.js openOverlay()

// The Explorer's tabs, as things you can ask for by name. The KEYS are the
// ones openFilePanel has always taken and must not change; the labels are how
// the Explorer names them now, with the older name kept as a search alias so
// muscle memory ("memory", "skills", "jobs") still finds the right pane.
const PALETTE_EXPLORER_TABS = [
    { key: 'workspace', label: 'Files', alias: 'workspace files' },
    { key: 'memory', label: 'Knowledge', alias: 'memory notes recall' },
    { key: 'skills', label: 'Capabilities', alias: 'skills' },
    { key: 'tools', label: 'Tools', alias: 'capabilities' },
    { key: 'mcp', label: 'MCP', alias: 'servers capabilities' },
    { key: 'jobs', label: 'Automation', alias: 'jobs cron schedule' },
    { key: 'adaptive', label: 'Self-tuning', alias: 'adaptive learning' },
    { key: 'canary', label: 'Canary', alias: 'self-tuning tests' },
    { key: 'telos', label: 'Telos', alias: 'self-tuning goals purpose' },
];

/** The legend's hidden-type map, read straight from the sidebar's own store —
 *  a type switched off in the legend is switched off here too. */
function _paletteHiddenTypes() {
    try {
        return JSON.parse(localStorage.getItem('pernix:sidebar') || '{}').hiddenTypes || {};
    } catch { return {}; }
}

/** The sidebar's type key for one session, mirrored so the palette can honour
 *  the same legend without reaching into the sidebar's internals. */
function _paletteTypeKey(s) {
    if (s.session_type === 'worker') return 'worker';
    if (s.session_type === 'snooze') return 'snooze';
    if (s.session_type === 'rlm') return 'rlm';
    if (s.session_type === 'canary') return 'canary';
    if (s.title && s.title.startsWith('Cron:')) return 'cron';
    return 'chat';
}

/**
 * The things the palette can DO, as opposed to the places it can go.
 *
 * Ctrl+K was a session switcher and nothing else, so every other action in
 * the app was a hunt for the right button. These are the same actions the
 * buttons fire — no new code paths, just a keyboard-first way in.
 */
function _paletteVerbs() {
    const verbs = [];
    verbs.push({
        title: 'New session',
        hint: 'session',
        run: () => document.getElementById('new-session-btn')?.click(),
    });
    for (const sp of (state.spaces || [])) {
        verbs.push({
            title: `New session in ${sp.label}`,
            hint: 'space',
            color: sp.color,
            run: async () => {
                try {
                    const data = await post('/api/sessions', { title: 'New session', space_id: sp.id });
                    await loadSessions();
                    selectSession(data.session_id);
                } catch (e) {
                    notify('error', `Couldn't create the session — ${humanizeError(e)}`);
                }
            },
        });
    }
    for (const tab of PALETTE_EXPLORER_TABS) {
        verbs.push({
            title: `Open Explorer → ${tab.label}`,
            hint: 'explorer',
            alias: tab.alias,
            run: () => openFilePanel({ tab: tab.key }),
        });
    }
    verbs.push({ title: 'Settings', hint: 'app', alias: 'preferences models keys', run: openSettings });
    verbs.push({
        title: 'Clear conversation',
        hint: 'session',
        alias: 'delete messages transcript',
        run: () => SLASH_COMMANDS['/clear'](),
    });
    verbs.push({
        title: 'Toggle theme',
        hint: 'app',
        alias: 'dark light appearance',
        run: () => setTheme(isLight() ? 'dark' : 'light'),
    });
    return verbs;
}

/** Everything the palette can offer right now, verbs first. */
function _paletteEntries() {
    const hidden = _paletteHiddenTypes();
    const entries = [];
    let order = 0;
    for (const v of _paletteVerbs()) {
        entries.push({ ...v, isVerb: 1, recency: 0, order: order++ });
    }
    for (const s of (state.sessions || [])) {
        // Workers live under their parent, not in a flat jump list; the rest
        // answer to the legend the same way the sidebar does.
        if (s.session_type === 'worker') continue;
        if (hidden[_paletteTypeKey(s)]) continue;
        entries.push({
            title: s.title || 'New session',
            hint: _paletteTime(s.updated_at),
            alias: s.first_message || '',
            session: s,
            isVerb: 0,
            recency: _parseMsgTs(s.updated_at),
            order: order++,
            run: () => selectSession(s.id),
        });
    }
    return entries;
}

/** 3 = the query starts the text, 2 = it starts a word in it, 1 = it is in
 *  there somewhere, 0 = no match at all. */
function _matchScore(text, query) {
    if (!query) return 1;
    const t = (text || '').toLowerCase();
    if (!t) return 0;
    const i = t.indexOf(query);
    if (i < 0) return 0;
    if (i === 0) return 3;
    return /[\s\-_/·:.,(→]/.test(t[i - 1]) ? 2 : 1;
}

/**
 * Rank, do not filter-in-place.
 *
 * The palette used to show whatever the API happened to return first among
 * the substring matches, so typing the exact name of a session could still
 * leave it below three others that merely contained the word. Strength of
 * match first, then recency, with verbs winning a tie only once something has
 * actually been typed — with an empty box this is still a session switcher.
 */
function _rankPalette(entries, query) {
    const q = (query || '').trim().toLowerCase();
    const scored = [];
    for (const e of entries) {
        let score = _matchScore(e.title, q);
        if (q && score < 3) {
            // Secondary text (a session's first message, a verb's aliases)
            // can only ever earn the weakest kind of match.
            score = Math.max(score, Math.min(1, _matchScore(e.alias, q)));
        }
        if (!score) continue;
        scored.push({ e, score });
    }
    scored.sort((a, b) => (b.score - a.score)
        || (q ? (b.e.isVerb - a.e.isVerb) : 0)
        || (b.e.recency - a.e.recency)
        || (a.e.order - b.e.order));
    return scored.map((s) => s.e);
}

const PALETTE_MAX_ROWS = 20;

function openSessionPalette() {
    if (_paletteEl) { closeSessionPalette(); return; }

    const input = el('input', {
        type: 'text',
        placeholder: 'Jump to a session, or type a command…',
        'aria-label': 'Jump to a session or run a command',
    });
    const list = el('div', { class: 'palette-list' });
    const card = el('div', { class: 'palette-card' }, [input, list]);
    _paletteEl = el('div', { class: 'palette-overlay' }, [card]);
    _paletteEl.addEventListener('click', (e) => { if (e.target === _paletteEl) closeSessionPalette(); });
    document.body.appendChild(_paletteEl);
    // The palette has no heading of its own — name it from the input's own
    // placeholder text rather than inventing a visible title.
    card.setAttribute('aria-label', 'Jump to a session or run a command');
    _closePaletteOverlay = openOverlay(card, { initialFocus: input });

    // Snapshot once per open: the ten-second session poll must not reshuffle
    // the list under the reader's fingers between a keystroke and Enter.
    const all = _paletteEntries();
    let items = [];
    let selected = 0;

    const run = (entry) => {
        if (!entry) return;
        closeSessionPalette();
        entry.run();
    };

    const render = (q) => {
        clear(list);
        const matches = _rankPalette(all, q).slice(0, PALETTE_MAX_ROWS);
        items = matches;
        selected = 0;
        matches.forEach((entry, i) => {
            const titleKids = [];
            const sp = entry.session && entry.session.space_id
                ? (state.spaces || []).find((x) => x.id === entry.session.space_id)
                : null;
            if (sp) {
                titleKids.push(el('span', {
                    class: 'space-chip',
                    style: `--space-color: ${sp.color}`,
                    title: `Space: ${sp.label}`,
                }));
            } else if (entry.color) {
                titleKids.push(el('span', { class: 'space-chip', style: `--space-color: ${entry.color}` }));
            }
            titleKids.push(text(entry.title));
            const row = el('div', {
                class: `palette-item${entry.isVerb ? ' verb' : ''}${i === 0 ? ' selected' : ''}`,
            }, [
                el('span', { class: 'palette-title' }, titleKids),
                el('span', { class: 'palette-meta' }, [text(entry.hint || '')]),
            ]);
            row.addEventListener('click', () => run(entry));
            row.addEventListener('mousemove', () => {
                selected = i;
                list.querySelectorAll('.palette-item').forEach((r, j) => r.classList.toggle('selected', j === i));
            });
            list.appendChild(row);
        });
        if (!matches.length) list.appendChild(el('div', { class: 'palette-empty' }, [text('Nothing matches')]));
    };

    input.addEventListener('input', () => render(input.value));
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { e.preventDefault(); closeSessionPalette(); return; }
        if (e.key === 'Enter') {
            e.preventDefault();
            run(items[selected]);
            return;
        }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            if (!items.length) return;
            selected = (selected + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
            list.querySelectorAll('.palette-item').forEach((r, j) => r.classList.toggle('selected', j === selected));
            list.children[selected]?.scrollIntoView({ block: 'nearest' });
        }
    });

    render('');
    input.focus();
}

function closeSessionPalette() {
    if (_closePaletteOverlay) { _closePaletteOverlay(); _closePaletteOverlay = null; }
    if (_paletteEl) {
        _paletteEl.remove();
        _paletteEl = null;
    }
}

function _paletteTime(isoStr) {
    if (!isoStr) return '';
    let s = isoStr.replace(/\+00:00$/, 'Z');
    if (!/[Z+-]\d{2}/.test(s)) s += 'Z';
    const d = new Date(s);
    if (isNaN(d.getTime())) return '';
    const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (diffSec < 3600) return `${Math.max(1, Math.floor(diffSec / 60))}m ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// ---------------------------------------------------------------------------
// Export transcript as a Markdown download
// ---------------------------------------------------------------------------

async function exportTranscript() {
    if (!state.sid) return;
    try {
        const data = await get(`/api/sessions/${state.sid}?limit=100000`);
        const messages = data.messages || [];
        const sess = (state.sessions || []).find(s => s.id === state.sid);
        const title = (sess?.title || 'Pernix session').trim();

        const lines = [`# ${title}`, ''];
        lines.push(`> Exported ${new Date().toLocaleString()} · session ${state.sid}`, '');
        for (const m of messages) {
            if (m.role === 'user') {
                lines.push('## User', '', m.content || '', '');
            } else if (m.role === 'assistant') {
                if (m.content) lines.push('## Pernix', '', m.content, '');
            } else if (m.role === 'tool') {
                const head = (m.content || '').slice(0, 400);
                lines.push('<details><summary>tool output</summary>', '', '```', head, '```', '', '</details>', '');
            } else if (m.role === 'system' || m.role === 'notice') {
                lines.push(`*${(m.content || '').trim()}*`, '');
            }
            // scout/reflect/eval/compaction internals are noise in an export
        }

        const blob = new Blob([lines.join('\n')], { type: 'text/markdown' });
        const url = URL.createObjectURL(blob);
        const safeName = title.replace(/[^\w\d-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60) || 'session';
        const a = el('a', { href: url, download: `${safeName}.md` });
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
    } catch (e) {
        appendMessage('system', `Export failed: ${e.message}`);
    }
}

// ---------------------------------------------------------------------------
// Health / Status
// ---------------------------------------------------------------------------

async function loadHealth() {
    try {
        const data = await get('/api/health');
        state.model = data.model;
        _renderModelBadge();
    } catch {
        document.getElementById('status-model').textContent = 'offline';
    }
}

function updateStatus(msg) {
    const infoEl = document.getElementById('status-info');
    if (infoEl) infoEl.textContent = msg;
}

/** Clear the scout summary specifically — anything else in the status line
 *  (a retry, a compaction) is still current and must survive. */
function _clearScoutStatus() {
    const infoEl = document.getElementById('status-info');
    if (infoEl && /^Scout:/.test(infoEl.textContent || '')) infoEl.textContent = '';
}

// ---------------------------------------------------------------------------
// State badge + timeline drawer
// ---------------------------------------------------------------------------

const _STATE_LABELS = {
    idle_ready: 'idle',
    scouting: 'scouting',
    processing: 'processing',
    compacting: 'compacting',
    pause_requested: 'pausing\u2026',
    paused: 'paused',
    cancelling: 'cancelling',
    finalizing: 'finalizing',
    awaiting_user: 'awaiting user',
};

function _applyStateBadge(to, reason) {
    const el = document.getElementById('state-badge');
    if (!el) return;
    el.className = 'state-badge ' + to;
    el.textContent = _STATE_LABELS[to] || to;
    el.title = reason ? `state: ${to} (${reason})` : `state: ${to}`;
    el.setAttribute('aria-label',
        `Session state: ${_STATE_LABELS[to] || to}${reason ? ` (${reason})` : ''}. Open the timeline.`);
    _updatePauseButton(to);
}

function _updatePauseButton(stateStr) {
    const btn = document.getElementById('pause-btn');
    if (!btn) return;
    const paused = stateStr === 'paused' || stateStr === 'pause_requested';
    const active = stateStr === 'processing' || stateStr === 'scouting' || stateStr === 'compacting';
    btn.hidden = !(active || paused);
    btn._paused = paused;
    clear(btn);
    btn.appendChild(icon(paused ? 'play' : 'pause'));
    btn.title = paused ? 'Resume the session' : 'Pause after the current step';
    btn.setAttribute('aria-label', btn.title);
}

function _renderStateBadge(event) {
    const to = event.to || 'idle_ready';
    _applyStateBadge(to, event.reason || '');

    if (isTimelineOpen()) {
        appendTimelineRow({
            turn_id: event.turn_id,
            retry_index: event.retry_index,
            from_state: event.from,
            to_state: event.to,
            reason: event.reason,
            elapsed_ms: null,
            termination_reason: event.termination_reason,
        });
    }
}

// ---------------------------------------------------------------------------
// Session transcript copy
// ---------------------------------------------------------------------------

async function copyTranscript() {
    const inner = _messagesInner();
    if (!inner) return;
    const parts = [];
    for (const child of inner.children) {
        if (child.classList.contains('message')) {
            const role = (child.querySelector('.role-label')?.textContent || '').toUpperCase();
            const content = child.querySelector('.content')?.innerText || '';
            if (content.trim()) parts.push(`[${role}]\n${content}`);
        } else if (child.classList.contains('tool-group')) {
            const items = child.querySelectorAll('.tool-item');
            for (const item of items) {
                const name = item.querySelector('.tool-item-name')?.textContent || '';
                const result = item.querySelector('.tool-item-content')?.innerText || '';
                parts.push(`[TOOL: ${name.trim()}]\n${result}`);
            }
        }
    }
    if (parts.length === 0) return;
    const transcript = parts.join('\n\n---\n\n');
    try {
        await navigator.clipboard.writeText(transcript);
        const btn = document.getElementById('copy-transcript-btn');
        if (btn) {
            const orig = btn.textContent;
            btn.textContent = '\u2713';
            btn.title = 'Copied!';
            setTimeout(() => { btn.textContent = orig; btn.title = 'Copy transcript'; }, 1500);
        }
    } catch (e) {
        console.warn('Failed to copy transcript:', e);
    }
}


// ---------------------------------------------------------------------------
// Auth login screen (remote client onboarding)
// ---------------------------------------------------------------------------

function _showLoginScreen() {
    // Hide the main app behind the login overlay
    const overlay = document.createElement('div');
    overlay.className = 'auth-overlay';
    overlay.innerHTML = `
        <div class="auth-card">
            <div class="auth-logo">Pernix</div>
            <div class="auth-subtitle">Enter your access token to connect</div>
            <input class="auth-input" type="text" placeholder="Paste token here"
                   autocomplete="off" spellcheck="false" autofocus
                   aria-label="Access token"
                   aria-describedby="auth-hint auth-error" />
            <button class="auth-btn">Connect</button>
            <div class="auth-hint" id="auth-hint">Find this token in the server console output —
                or on a logged-in desktop browser, open Settings &rarr; Network and
                scan the QR code to sign this device in automatically.</div>
            <div class="auth-error" id="auth-error" role="alert" style="display:none"></div>
        </div>
    `;
    document.body.appendChild(overlay);

    const input = overlay.querySelector('.auth-input');
    const btn = overlay.querySelector('.auth-btn');
    const errorEl = overlay.querySelector('.auth-error');

    // No onClose: there is nothing behind this screen to go back to, so
    // Escape must not dismiss it. The trap keeps Tab on the three controls
    // that exist instead of walking the app underneath.
    const card = overlay.querySelector('.auth-card');
    card.setAttribute('aria-label', 'Sign in to Pernix');
    openOverlay(card, { initialFocus: input });

    async function submit() {
        const token = input.value.trim();
        if (!token) {
            errorEl.textContent = 'Token cannot be empty';
            errorEl.style.display = 'block';
            return;
        }
        btn.disabled = true;
        btn.textContent = 'Connecting\u2026';
        errorEl.style.display = 'none';

        // Test the token before committing
        try {
            const resp = await fetch('/api/settings', {
                headers: { 'Authorization': `Bearer ${token}` },
            });
            if (resp.status === 401) {
                errorEl.textContent = 'Invalid token. Check the server console (or re-scan the QR from Settings → Network) and try again.';
                errorEl.style.display = 'block';
                btn.disabled = false;
                btn.textContent = 'Connect';
                input.select();
                return;
            }
            setAuthToken(token);
            location.reload();
        } catch (e) {
            errorEl.textContent = `Connection failed: ${e.message}`;
            errorEl.style.display = 'block';
            btn.disabled = false;
            btn.textContent = 'Connect';
        }
    }

    btn.addEventListener('click', submit);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') submit();
    });
    // Focus after a tick (some browsers need this for autofocus in dynamic elements)
    requestAnimationFrame(() => input.focus());
}
