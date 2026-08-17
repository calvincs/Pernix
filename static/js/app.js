// Pernix — Main application entry point

import { state, subscribe } from './store.js';
import { get, post, del, patch, getAuthToken, setAuthToken } from './api.js';
import { connectSSE, disconnectSSE } from './sse.js';
import { getPermission, requestPermission, connectGlobalNotifications, registerServiceWorker, subscribePush } from './notifications.js';
import { el, text, clear, initMarked, renderMarkdown } from './render.js';
import { initSigil } from './sigil.js';
import { openSettings } from './components/modals/settings.js';
import { openTimeline, appendTimelineRow, appendTimelineToolRow, appendTimelineToolStart, isTimelineOpen } from './components/modals/timeline.js';
import { initBell, openBellPanel, closeBellPanel, refreshBell } from './components/notification-bell.js';
import { initJobsIndicator } from './components/jobs-indicator.js';
import { initSidebar, renderSessionList as renderSidebar, updateSessionActivity } from './components/sidebar.js';
import { initFilePanel, toggleFilePanel, openFilePanel } from './components/file-panel.js';
import { openRlmViewer, closeRlmViewer } from './components/rlm-viewer.js';
import { initMobile, isMobile, closeSidebar } from './mobile.js';
import { initVoice, stopVoice } from './voice.js';

// ---------------------------------------------------------------------------
// File uploads state
// ---------------------------------------------------------------------------

let _pendingFiles = []; // { file, name, uploading, uploaded, serverName, size }
let _scoutContainer = null;

// ---------------------------------------------------------------------------
// Message container helpers (inner for content, outer for scroll)
// ---------------------------------------------------------------------------

function _messagesInner() {
    return document.getElementById('messages-inner');
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
    // Auto-extract auth token from URL (shared link / QR code onboarding)
    const _urlParams = new URLSearchParams(window.location.search);
    const _urlToken = _urlParams.get('token');
    if (_urlToken) {
        setAuthToken(_urlToken);
        _urlParams.delete('token');
        const _cleanUrl = _urlParams.toString()
            ? `${window.location.pathname}?${_urlParams}`
            : window.location.pathname;
        history.replaceState(null, '', _cleanUrl);
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
    if (localStorage.getItem('pernix:sidebar-hidden') === '1') {
        sidebar.classList.add('collapsed');
    }
    sidebarToggle.addEventListener('click', () => {
        if (isMobile()) return;
        sidebar.classList.toggle('collapsed');
        localStorage.setItem('pernix:sidebar-hidden', sidebar.classList.contains('collapsed') ? '1' : '0');
    });

    // Restore the normal session list when the sidebar search box clears
    window.addEventListener('pernix:sidebar-refresh', () => renderSidebar(state.sessions, state.sid));

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
    initBell();

    // Keyboard shortcuts: Ctrl/Cmd+F → transcript search (with a session
    // open), Ctrl/Cmd+K → session switcher palette.
    document.addEventListener('keydown', (e) => {
        const mod = e.ctrlKey || e.metaKey;
        if (!mod) return;
        if (e.key === 'f' && state.sid) {
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
        renderSidebar(state.sessions, state.sid);
    });

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

async function loadSessions() {
    try {
        const data = await get('/api/sessions?limit=500');
        state.sessions = data.items || [];
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
        renderSidebar(state.sessions, state.sid);
    } catch (e) {
        if (!e.offline) console.warn('Failed to load sessions:', e);
    }
}

async function deleteSession(sid) {
    try {
        await del(`/api/sessions/${sid}`);
        if (state.sid === sid) {
            state.sid = null;
            disconnectSSE();
            closeRlmViewer();
            showEmptyState();
        }
        await loadSessions();
    } catch (e) {
        console.error('Failed to delete session:', e);
    }
}

function _setComposerReadOnly(readonly, reason) {
    const input = document.getElementById('msg-input');
    const btn = document.getElementById('send-btn');
    if (!input || !btn) return;
    input.disabled = readonly;
    btn.disabled = readonly;
    input.placeholder = readonly
        ? (reason || 'This session is read-only')
        : 'Message Pernix...';
}

async function selectSession(sid) {
    if (isMobile()) closeSidebar();
    state.sid = sid;
    _historyLimit = HISTORY_PAGE;  // fresh window per session
    _recentlyFinished.delete(sid);  // visiting clears the "done" attention tick
    _restoreDraft();
    // Reset streaming state to prevent cross-session leakage
    _streamingEl = null;
    _collected = '';
    _toolGroup = null;
    _toolGroupCount = 0;
    if (_parseTimer) { clearTimeout(_parseTimer); _parseTimer = null; }
    closeRlmViewer();
    renderSidebar(state.sessions, state.sid);

    // RLM run views have no transcript — the chat area renders the live
    // trace viewer instead, and the composer stays off (the server enforces
    // the same read-only policy via sessions.policy).
    const _sess = (state.sessions || []).find(s => s.id === sid);
    if (_sess?.session_type === 'rlm') {
        disconnectSSE();
        _activeWorkers.clear();
        _activeRlmRuns.clear();
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
    await loadContextInfo(sid);
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
        _lastSeq = 0;
    }

    await loadPendingQuestions(sid);
    connectSSE(sid, handleEvent);

}

const HISTORY_PAGE = 200;   // messages rendered initially; grows on demand
let _historyLimit = HISTORY_PAGE;

async function loadMessages(sid, { keepScroll = false } = {}) {
    const inner = _messagesInner();
    const scroll = _messagesScroll();
    // Anchor to distance-from-bottom so "load earlier" re-renders keep the
    // reader's place instead of dumping them at the end of the transcript.
    const prevBottomDist = keepScroll ? (scroll.scrollHeight - scroll.scrollTop) : null;
    clear(inner);
    _questionBubbles.clear();
    _lastMsgTs = 0;  // gap dividers restart per render
    try {
        const data = await get(`/api/sessions/${sid}?limit=${_historyLimit}`);
        const messages = data.messages || [];
        if (messages.length === 0) {
            showEmptyState();
            return;
        }
        if (data.has_more) {
            const remaining = (data.total_messages || 0) - messages.length;
            const loadBtn = el('button', { class: 'load-earlier-btn', onClick: async () => {
                loadBtn.disabled = true;
                loadBtn.textContent = 'Loading…';
                _historyLimit += HISTORY_PAGE;
                await loadMessages(sid, { keepScroll: true });
            }}, [text(`Load earlier messages (${remaining} more)`)]);
            inner.appendChild(loadBtn);
        }
        // Build tool_call_id → tool_name map from assistant messages
        const toolNameMap = {};
        for (const m of messages) {
            if (m.role === 'assistant' && m.tool_calls) {
                try {
                    const tcs = typeof m.tool_calls === 'string' ? JSON.parse(m.tool_calls) : m.tool_calls;
                    for (const tc of (Array.isArray(tcs) ? tcs : [])) {
                        const id = tc.id || '';
                        // Handle both formats: {name: "..."} and {function: {name: "..."}}
                        const name = tc.name || (tc.function || {}).name || '';
                        if (id && name) toolNameMap[id] = name;
                    }
                } catch { /* skip malformed tool_calls */ }
            }
        }

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
            if (m.role === 'tool') {
                const content = m.content || '';
                const preview = content.slice(0, 300);
                const toolName = toolNameMap[m.tool_call_id] || m.tool_call_id || '';
                appendToolToGroup(toolName, preview, content, content.length > 300);
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
                appendMessage(m.role, m.content, { createdAt: m.created_at, messageId: m.id });
            }
        }
        closeToolGroup();
        _markPendingQueued(sid);
        if (prevBottomDist !== null) {
            scroll.scrollTop = scroll.scrollHeight - prevBottomDist;
            _updateScrollPin();
        } else {
            scrollToBottom(true);
        }
    } catch (e) {
        appendMessage('system', `Error loading messages: ${e.message}`);
    }
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
// Empty state
// ---------------------------------------------------------------------------

let _sigilStop = null;
function showEmptyState() {
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
        if (!window.confirm('Clear all messages in this session? This cannot be undone.')) return;
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
let _histIdx = -1;        // -1 = not navigating history
let _histStash = '';      // text that was in the input when navigation started

function _draftKey() { return _DRAFT_PREFIX + (state.sid || 'new'); }

function _saveDraft(value) {
    clearTimeout(_draftTimer);
    _draftTimer = setTimeout(() => {
        try {
            if (value.trim()) localStorage.setItem(_draftKey(), value);
            else localStorage.removeItem(_draftKey());
        } catch { /* storage full/unavailable */ }
    }, 300);
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
        // On mobile, Enter inserts a newline — soft keyboards have no
        // Shift+Enter, so send-on-Enter made multi-line prompts impossible.
        // The send button is the submit action there.
        if (e.key === 'Enter' && !e.shiftKey && !isMobile()) {
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

    _restoreDraft();
}

async function send() {
    // A dictation session still running would keep writing into the input
    // after we clear it below.
    stopVoice();
    const textarea = document.getElementById('msg-input');
    const message = textarea.value.trim();
    if (!message && _pendingFiles.length === 0) return;

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
            await loadSessions();
            connectSSE(state.sid, handleEvent);
        } catch (e) {
            appendMessage('system', `Failed to create session: ${e.message}`);
            return;
        }
    }

    // Upload pending files first (XHR for per-chip progress — a 100MB file
    // on phone Wi-Fi used to look like a hang).
    const uploadedFiles = [];
    if (_pendingFiles.length > 0) {
        let failed = 0;
        for (const pf of _pendingFiles) {
            if (pf.uploaded && pf.serverName) {
                uploadedFiles.push(pf.serverName);
                continue;
            }
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
        const chatHeaders = { 'Content-Type': 'application/json' };
        const _tk = getAuthToken();
        if (_tk) chatHeaders['Authorization'] = `Bearer ${_tk}`;
        const resp = await fetch('/api/chat', {
            method: 'POST',
            headers: chatHeaders,
            body: JSON.stringify({ session_id: state.sid, message: finalMessage }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || resp.statusText);
        }
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
    }, [text('×')]);
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
// Event handling
// ---------------------------------------------------------------------------

let _streamingEl = null;
let _collected = '';
let _toolGroup = null;
let _toolGroupCount = 0;
let _parseTimer = null;
let _activityTimer = null;
let _lastSeq = 0;  // track last processed event seq for dedup on SSE reconnect
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
const _activeWorkers = new Map();  // worker_id → { title, startedAt }
// run_id → { uiSid, label, iterations, maxIterations, subcalls, startedAt }
const _activeRlmRuns = new Map();
let _workerTicker = null;

function _renderWorkerStrip() {
    const strip = document.getElementById('worker-strip');
    if (!strip) return;
    if (_activeWorkers.size === 0 && _activeRlmRuns.size === 0) {
        strip.hidden = true;
        strip.innerHTML = '';
        if (_workerTicker) { clearInterval(_workerTicker); _workerTicker = null; }
        return;
    }
    strip.hidden = false;
    strip.innerHTML = '';
    const labelParts = [];
    if (_activeWorkers.size) labelParts.push(`${_activeWorkers.size} worker${_activeWorkers.size === 1 ? '' : 's'}`);
    if (_activeRlmRuns.size) labelParts.push(`${_activeRlmRuns.size} RLM`);
    strip.appendChild(el('span', { class: 'worker-strip-label' }, [
        text(labelParts.join(' · ')),
    ]));
    for (const [wid, w] of _activeWorkers) {
        const elapsed = Math.max(0, Math.round((Date.now() - w.startedAt) / 1000));
        const mins = Math.floor(elapsed / 60);
        const elapsedStr = mins > 0 ? `${mins}m${elapsed % 60}s` : `${elapsed}s`;
        const chip = el('button', {
            class: `worker-chip${w.paused ? ' paused' : ''}`,
            title: `${w.title} — click to open transcript`,
            onClick: () => selectSession(wid),
        }, [
            el('span', { class: 'worker-chip-dot' }),
            text(` ${w.title.slice(0, 30)} · ${w.paused ? 'paused' : elapsedStr}`),
        ]);
        // Pause/resume the worker without leaving the parent session — the
        // endpoints exist per-worker; this is their first UI affordance.
        const ctlBtn = el('button', {
            class: 'worker-chip-ctl',
            title: w.paused ? 'Resume this worker' : 'Pause this worker after its current step',
        }, [text(w.paused ? '▶' : '❚❚')]);
        ctlBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            ctlBtn.disabled = true;
            const action = w.paused ? 'resume' : 'pause';
            try {
                await post(`/api/sessions/${state.sid}/workers/${wid}/${action}`, {});
                w.paused = !w.paused;
            } catch (err) {
                appendMessage('system', `Worker ${action} failed: ${err.message}`);
            }
            _renderWorkerStrip();
        });
        strip.appendChild(el('span', { class: 'worker-chip-wrap' }, [chip, ctlBtn]));
    }
    // RLM run chips — live iteration/sub-call counters; click opens the
    // run's read-only trace view (its sidebar pseudo-session).
    for (const [rid, r] of _activeRlmRuns) {
        const elapsed = Math.max(0, Math.round((Date.now() - r.startedAt) / 1000));
        const mins = Math.floor(elapsed / 60);
        const elapsedStr = mins > 0 ? `${mins}m${elapsed % 60}s` : `${elapsed}s`;
        const iter = r.maxIterations ? `it ${r.iterations}/${r.maxIterations}` : `it ${r.iterations}`;
        const chip = el('button', {
            class: 'worker-chip rlm-chip',
            title: `${r.label} — click to watch the run`,
            onClick: () => { if (r.uiSid) selectSession(r.uiSid); },
        }, [
            el('span', { class: 'worker-chip-dot rlm' }),
            text(` RLM · ${iter} · ${r.subcalls} calls · ${elapsedStr}`),
        ]);
        strip.appendChild(el('span', { class: 'worker-chip-wrap', 'data-run': rid }, [chip]));
    }
    if (!_workerTicker) _workerTicker = setInterval(_renderWorkerStrip, 5000);
}

async function _seedWorkerStrip(sid) {
    _activeWorkers.clear();
    _activeRlmRuns.clear();
    try {
        const data = await get(`/api/sessions/${sid}/workers`);
        const now = Date.now();
        for (const w of (data.workers || [])) {
            if (w.state === 'idle_ready') continue;  // finished
            const started = w.created_at ? Date.parse(w.created_at + 'Z') || now : now;
            _activeWorkers.set(w.id, {
                title: w.title,
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
    _renderWorkerStrip();
}

// ---------------------------------------------------------------------------
// Per-session model override — clicking the model badge opens a picker.
// Persistent for the session (unlike agent-initiated switch_model, which the
// manager reverts at turn end). Backed by PATCH /api/sessions/{sid}.
// ---------------------------------------------------------------------------

let _sessionModelOverride = null;
let _modelMenuEl = null;

function _renderModelBadge() {
    const mEl = document.getElementById('status-model');
    if (!mEl) return;
    mEl.classList.remove('has-override');
    mEl.textContent = '';
    if (_sessionModelOverride) {
        mEl.appendChild(text(_sessionModelOverride));
        const pin = document.createElement('span');
        pin.className = 'model-session-override';
        pin.textContent = ' ●';
        pin.title = `Session override (default: ${state.model})`;
        mEl.appendChild(pin);
        mEl.title = `This session runs on ${_sessionModelOverride} (default: ${state.model}). Click to change.`;
    } else {
        mEl.appendChild(text(state.model || '...'));
        mEl.title = 'Model for this session — click to override';
    }
}

function _closeModelMenu() {
    if (_modelMenuEl) {
        _modelMenuEl.remove();
        _modelMenuEl = null;
        document.removeEventListener('click', _closeModelMenu);
    }
}

async function _openModelMenu() {
    if (_modelMenuEl) { _closeModelMenu(); return; }
    if (!state.sid) return;

    const menu = el('div', { id: 'model-menu' }, [
        el('div', { class: 'model-menu-header' }, [text('Model for this session')]),
        el('div', { class: 'model-menu-loading' }, [text('Loading models…')]),
    ]);
    _modelMenuEl = menu;
    document.body.appendChild(menu);

    // Position above the status bar, anchored to the badge.
    const badge = document.getElementById('status-model');
    const rect = badge.getBoundingClientRect();
    menu.style.left = `${Math.max(8, rect.left)}px`;
    menu.style.bottom = `${window.innerHeight - rect.top + 6}px`;

    // Defer the outside-click closer so the opening click doesn't trigger it.
    setTimeout(() => document.addEventListener('click', _closeModelMenu), 0);
    menu.addEventListener('click', (e) => e.stopPropagation());

    let models = [];
    try {
        const data = await get('/api/models');
        models = data.models || [];
    } catch { /* render with just the default option */ }
    if (!_modelMenuEl) return;  // closed while loading

    menu.querySelector('.model-menu-loading')?.remove();
    const list = el('div', { class: 'model-menu-list' });

    const mkItem = (label, value, current) => {
        const item = el('button', { class: `model-menu-item${current ? ' current' : ''}` }, [text(label)]);
        item.addEventListener('click', async () => {
            try {
                await patch(`/api/sessions/${state.sid}`, { model_override: value });
                _sessionModelOverride = value || null;
                _renderModelBadge();
            } catch (e) {
                appendMessage('system', `Model override failed: ${e.message}`);
            }
            _closeModelMenu();
        });
        return item;
    };

    const providerLabels = { ollama: 'Ollama', openrouter: 'OpenRouter' };
    const labelFor = (p) => providerLabels[p] || p;

    const defaultProvider = models.find((m) => m.id === state.model)?.provider;
    const defaultLabel = `default (${state.model}${defaultProvider ? ` · ${labelFor(defaultProvider)}` : ''})`;
    list.appendChild(mkItem(defaultLabel, '', !_sessionModelOverride));

    // Group by provider, Ollama first (same ordering as the settings modal).
    const byProvider = {};
    for (const m of models) {
        const p = m.provider || 'unknown';
        (byProvider[p] ||= []).push(m);
    }
    const providerOrder = ['ollama', 'openrouter', ...Object.keys(byProvider).filter(p => p !== 'ollama' && p !== 'openrouter')];
    for (const provider of providerOrder) {
        const group = byProvider[provider];
        if (!group?.length) continue;
        list.appendChild(el('div', { class: 'model-menu-group' }, [text(labelFor(provider))]));
        for (const m of group) {
            list.appendChild(mkItem(m.id, m.id, _sessionModelOverride === m.id));
        }
    }
    if (!models.length) {
        list.appendChild(el('div', { class: 'model-menu-empty' }, [text('No models listed — check provider settings.')]));
    }
    menu.appendChild(list);
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
        closeToolGroup();
        // Recover streaming state if page was refreshed mid-stream
        if (!_streamingEl) {
            state.streaming = true;
            _showStopButton();
            _streamingEl = appendMessage('assistant', '');
            _collected = '';
        }
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
        _collected = '';
        _streamingEl = null;
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
        appendMessage('system', `Error: ${event.error || 'Unknown error'}`);
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
        appendToolToGroup(event.name, preview, full, isTruncated, event.was_error, event.latency_ms, event.arguments);
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
            renderSidebar(state.sessions, state.sid);
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
        const workerModel = event.model ? ` [${event.model}]` : '';
        appendMessage('system', `Worker started: ${event.title || event.worker_id}${workerModel}`);
        _activeWorkers.set(event.worker_id, { title: event.title || event.worker_id, startedAt: Date.now() });
        _renderWorkerStrip();
    }

    else if (type === 'worker.done') {
        const reason = event.termination_reason ? ` (${event.termination_reason})` : '';
        const err = event.error ? ` error: ${event.error}` : '';
        appendMessage('system', `Worker done: ${event.worker_id}${reason}${err}`);
        _activeWorkers.delete(event.worker_id);
        _renderWorkerStrip();
    }

    else if (type === 'worker.failed') {
        // Spawn-time failure: manager.prompt() raised before the worker
        // ever ran. Distinct from worker.done with an error — that's a
        // worker that ran and errored mid-turn. This is a worker that
        // never started, so there's no transcript or summary to inspect.
        const wid = event.worker_id || 'unknown';
        const err = event.error || '(no error message)';
        appendMessage('system', `⚠ Worker failed to start: ${wid} — ${err}`);
        _activeWorkers.delete(wid);
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
        appendMessage('system', `Reflect: max retries reached. ${event.reasoning || ''}`);
        updateStatus('');
        state.streaming = false;
        _showSendButton();
    }
    else if (type === 'reflect.escalate') {
        appendMessage('system', `Reflect: needs clarification \u2014 ${event.missing || event.reasoning || ''}`);
        updateStatus('');
        state.streaming = false;
        _showSendButton();
    }
    else if (type === 'reflect.circuit_breaker') {
        // Cross-retry breaker: reflect asked for another retry, but the last
        // two attempts failed identically. Retrying is refused rather than
        // burning the remaining budget on the same failure.
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

    else if (type === 'stream.fallback') {
        // Rate-limit / provider failover switched the model mid-stream.
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
        appendMessage('system', `LLM budget exhausted: ${event.message || 'no further retries'}`);
        updateStatus('');
        if (_parseTimer) { clearTimeout(_parseTimer); _parseTimer = null; }
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
        appendMessage('notice', `[${event.count} queued message(s) dropped${reason}]`);
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
        const serverSeq = status.event_seq || 0;
        if (serverSeq < _lastSeq) {
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
    updateStatus(msg);
    if (_noticeTimer) clearTimeout(_noticeTimer);
    _noticeTimer = setTimeout(() => {
        _noticeTimer = null;
        const infoEl = document.getElementById('status-info');
        if (infoEl && infoEl.textContent === msg) infoEl.textContent = '';
    }, ms);
}

async function _softReload() {
    if (!state.sid || _isRlmView()) return;
    console.info('SSE: soft reload triggered (gap detected or reconciliation)');
    // loadMessages() clears and re-renders the DOM, which detaches any live
    // _streamingEl reference. Reset it unconditionally so the next stream.token
    // event creates a fresh element rather than updating a ghost node.
    _streamingEl = null;
    _collected = '';
    state.streaming = false;
    await loadMessages(state.sid);
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
            _streamingEl = appendMessage('assistant', '');
            _collected = '';
        } else {
            _showSendButton();
            updateStatus('');
        }
    } catch {}
    // Say so. The transcript is re-read from the database, so no *message* is
    // lost — but the live events that were dropped (tool chips, scout steps,
    // partial tokens) are gone for good, and the view visibly jumping without
    // explanation reads as a glitch. The server's replay buffer holds 2000
    // events and a reaped session comes back with an empty one, so this path
    // is reachable in normal operation, not just after an outage.
    _showNotice('reconnected — transcript refreshed');
}

async function _reconcile() {
    // Lightweight check: compare server event_seq with client _lastSeq
    if (!state.sid || state.streaming || _isRlmView()) return;  // no stream (or no transcript) to reconcile
    try {
        const status = await get(`/api/sessions/${state.sid}/status`);
        const serverSeq = status.event_seq || 0;
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
setInterval(() => { if (state.sid && !state.streaming) _reconcile(); }, 45000);

// Intervals are throttled while the tab is backgrounded — reconcile
// immediately on return (phone unlock) instead of waiting up to a minute.
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.sid && !state.streaming) _reconcile();
});

// ---------------------------------------------------------------------------
// Stop / Send button toggle
// ---------------------------------------------------------------------------

function _showStopButton() {
    const btn = document.getElementById('send-btn');
    btn.disabled = false;
    btn.title = 'Stop generation';
    btn.classList.add('stop-mode');
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor" stroke="none">
        <rect x="6" y="6" width="12" height="12" rx="2"/>
    </svg>`;
    const statusBar = document.getElementById('status-bar');
    if (statusBar) statusBar.classList.add('processing');
}

function _showSendButton() {
    const btn = document.getElementById('send-btn');
    // A read-only session keeps its send button off through stop/send churn —
    // the composer input is the source of truth (_setComposerReadOnly).
    btn.disabled = !!document.getElementById('msg-input')?.disabled;
    btn.title = 'Send message';
    btn.classList.remove('stop-mode');
    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="22" y1="2" x2="11" y2="13"></line>
        <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
    </svg>`;
    const statusBar = document.getElementById('status-bar');
    if (statusBar) statusBar.classList.remove('processing');
}

async function _cancelSession() {
    if (!state.sid) return;
    try {
        const cancelHeaders = {};
        const _tc = getAuthToken();
        if (_tc) cancelHeaders['Authorization'] = `Bearer ${_tc}`;
        const resp = await fetch(`/api/sessions/${state.sid}/cancel`, { method: 'POST', headers: cancelHeaders });
        if (!resp.ok) {
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

/** Hover action toolbar: copy on every message, edit-&-resend on user messages. */
function _attachMessageActions(msgEl, role) {
    const actions = el('div', { class: 'msg-actions' });
    const copyBtn = el('button', { class: 'msg-action-btn', title: 'Copy message' }, [text('⧉')]);
    copyBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const raw = msgEl._rawContent ?? msgEl.querySelector('.content')?.innerText ?? '';
        try {
            await navigator.clipboard.writeText(raw);
            copyBtn.textContent = '✓';
            setTimeout(() => { copyBtn.textContent = '⧉'; }, 1200);
        } catch { /* clipboard unavailable (non-secure context) */ }
    });
    actions.appendChild(copyBtn);

    if (role === 'user') {
        const editBtn = el('button', { class: 'msg-action-btn', title: 'Edit & resend — copies this message into the input' }, [text('✎')]);
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
    if (emptyEl) emptyEl.remove();

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
    } else {
        contentEl.appendChild(text(content || ''));
    }

    if (role === 'user' || role === 'assistant') _attachMessageActions(msgEl, role);

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
    const inlineStatus = el('span', { class: 'q-inline-status' });
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

function ensureToolGroup() {
    if (_toolGroup) return _toolGroup;
    const inner = _messagesInner();
    const scroll = _messagesScroll();
    const emptyEl = inner.querySelector('.empty-state');
    if (emptyEl) emptyEl.remove();

    _toolGroupCount = 0;
    const header = el('div', { class: 'tool-group-header' }, [
        el('span', { class: 'tg-toggle' }, [text('\u25BC')]),
        el('span', { class: 'tg-label' }, [text('0 tool calls')]),
    ]);
    header.addEventListener('click', () => {
        const group = header.closest('.tool-group');
        if (group) group.classList.toggle('collapsed');
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
    if (_toolGroupCount > 2) {
        _toolGroup.classList.add('collapsed');
    }
    _toolGroup = null;
    _toolGroupCount = 0;
}

function appendToolToGroup(name, preview, fullResult, isTruncated, wasError = false, latencyMs = 0, args = null) {
    const group = ensureToolGroup();
    const items = group.querySelector('.tool-group-items');
    _toolGroupCount++;

    // Update header count
    const label = group.querySelector('.tg-label');
    label.textContent = _toolGroupCount === 1 ? '1 tool call' : `${_toolGroupCount} tool calls`;

    // --- Compact header row (always visible) ---
    const chevron = el('span', { class: 'tool-item-chevron' }, [text('\u25B6')]);

    const nameChildren = [text(name)];
    if (latencyMs) {
        const latencyClass = latencyMs < 500 ? 'fast' : latencyMs < 2000 ? 'medium' : 'slow';
        nameChildren.push(el('span', { class: `tool-latency ${latencyClass}` }, [text(`${latencyMs}ms`)]));
    }
    const nameEl = el('div', { class: 'tool-item-name' }, nameChildren);

    // Inline summary — single-line preview of args or output
    let summaryText = '';
    if (wasError) {
        summaryText = '(error)';
    } else if (args && typeof args === 'object') {
        if (name === 'bash' && args.command) {
            summaryText = '$ ' + args.command;
        } else if (args.path) {
            summaryText = args.path;
        } else {
            summaryText = Object.entries(args).map(([k, v]) => {
                const s = String(v);
                return `${k}: ${s.length > 60 ? s.slice(0, 60) + '...' : s}`;
            }).join(', ');
        }
    }
    if (!summaryText && preview) {
        summaryText = preview.slice(0, 80).replace(/\n/g, ' ');
    }
    const summaryEl = el('span', { class: 'tool-item-summary' }, [text(summaryText)]);

    const headerChildren = [chevron, nameEl, summaryEl];

    // File view button — in header for quick access
    const fileMatch = (fullResult || preview || '').match(/Written \d+ chars to (.+)/);
    if (fileMatch) {
        const [, filePath] = fileMatch;
        const viewBtn = el('button', { class: 'file-view-btn' }, [text('view')]);
        viewBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            openWorkspaceFile(filePath.trim());
        });
        headerChildren.push(viewBtn);
    }

    const headerEl = el('div', { class: 'tool-item-header' }, headerChildren);

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
    const toggleBtn = el('button', { class: 'tool-toggle tool-toggle--hidden' }, [text('show more')]);
    toggleBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        fullyExpanded = !fullyExpanded;
        if (isTruncated) {
            clear(contentEl);
            contentEl.appendChild(text(fullyExpanded ? fullResult : preview));
        }
        contentEl.classList.toggle('fully-expanded', fullyExpanded);
        toggleBtn.textContent = fullyExpanded ? 'show less' : 'show more';
    });
    bodyChildren.push(toggleBtn);

    const bodyEl = el('div', { class: 'tool-item-body' }, bodyChildren);

    // --- Assemble item ---
    const isBrowse = name === 'browse_web' && !wasError;
    const itemEl = el('div', {
        class: `tool-item${wasError ? ' error' : ''}${isBrowse ? ' browse' : ''}`
    }, [headerEl, bodyEl]);

    // Click header to toggle item expansion; reveal toggle button if content overflows
    headerEl.addEventListener('click', () => {
        itemEl.classList.toggle('expanded');
        if (itemEl.classList.contains('expanded') && !toggleRevealed) {
            requestAnimationFrame(() => {
                if (isTruncated || contentEl.scrollHeight > contentEl.clientHeight + 2) {
                    contentEl.classList.add('overflows');
                    toggleBtn.classList.remove('tool-toggle--hidden');
                    toggleRevealed = true;
                }
            });
        }
    });

    items.appendChild(itemEl);

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
        el('span', { class: 'scout-toggle' }, [text('\u25BC')]),
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
    header.addEventListener('click', () => {
        container.classList.toggle('scout-collapsed');
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
        el('span', { class: 'scout-toggle' }, [text('\u25BC')]),
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
    header.addEventListener('click', () => {
        container.classList.toggle('scout-collapsed');
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
        el('span', { class: 'scout-toggle' }, [text('▼')]),
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
    header.addEventListener('click', () => {
        container.classList.toggle('scout-collapsed');
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
        el('span', { class: 'scout-toggle' }, [text('▼')]),
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
    header.addEventListener('click', () => container.classList.toggle('scout-collapsed'));

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
    if (group) group.classList.remove('collapsed');
    const toolItem = mark.closest('.tool-item');
    if (toolItem && !toolItem.classList.contains('expanded')) toolItem.classList.add('expanded');
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
    const input = el('input', { type: 'text', placeholder: 'Search transcript…' });
    const counter = el('span', { class: 'ts-counter' });
    const prevBtn = el('button', { class: 'ts-nav', title: 'Previous match (Shift+Enter)' }, [text('↑')]);
    const nextBtn = el('button', { class: 'ts-nav', title: 'Next match (Enter)' }, [text('↓')]);
    const closeBtn = el('button', { class: 'ts-close', title: 'Close (Esc)' }, [text('×')]);

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

function openSessionPalette() {
    if (_paletteEl) { closeSessionPalette(); return; }

    const input = el('input', { type: 'text', placeholder: 'Jump to session…' });
    const list = el('div', { class: 'palette-list' });
    const card = el('div', { class: 'palette-card' }, [input, list]);
    _paletteEl = el('div', { class: 'palette-overlay' }, [card]);
    _paletteEl.addEventListener('click', (e) => { if (e.target === _paletteEl) closeSessionPalette(); });
    document.body.appendChild(_paletteEl);

    let items = [];
    let selected = 0;

    const render = (q) => {
        clear(list);
        const query = (q || '').toLowerCase();
        const matches = (state.sessions || [])
            .filter(s => s.session_type !== 'worker')
            .filter(s => {
                if (!query) return true;
                return (s.title || '').toLowerCase().includes(query)
                    || (s.first_message || '').toLowerCase().includes(query);
            })
            .slice(0, 15);
        items = matches;
        selected = 0;
        matches.forEach((s, i) => {
            const row = el('div', { class: `palette-item${i === 0 ? ' selected' : ''}` }, [
                el('span', { class: 'palette-title' }, [text(s.title || 'New session')]),
                el('span', { class: 'palette-meta' }, [text(_paletteTime(s.updated_at))]),
            ]);
            row.addEventListener('click', () => { closeSessionPalette(); selectSession(s.id); });
            row.addEventListener('mousemove', () => {
                selected = i;
                list.querySelectorAll('.palette-item').forEach((r, j) => r.classList.toggle('selected', j === i));
            });
            list.appendChild(row);
        });
        if (!matches.length) list.appendChild(el('div', { class: 'palette-empty' }, [text('No matching sessions')]));
    };

    input.addEventListener('input', () => render(input.value));
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { e.preventDefault(); closeSessionPalette(); return; }
        if (e.key === 'Enter') {
            e.preventDefault();
            const s = items[selected];
            if (s) { closeSessionPalette(); selectSession(s.id); }
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
    _updatePauseButton(to);
}

function _updatePauseButton(stateStr) {
    const btn = document.getElementById('pause-btn');
    if (!btn) return;
    const paused = stateStr === 'paused' || stateStr === 'pause_requested';
    const active = stateStr === 'processing' || stateStr === 'scouting' || stateStr === 'compacting';
    btn.hidden = !(active || paused);
    btn._paused = paused;
    btn.innerHTML = paused ? '&#9654;' : '&#10074;&#10074;';
    btn.title = paused ? 'Resume the session' : 'Pause after the current step';
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
                   autocomplete="off" spellcheck="false" autofocus />
            <button class="auth-btn">Connect</button>
            <div class="auth-hint">Find this token in the server console output —
                or on a logged-in desktop browser, open Settings &rarr; Network and
                scan the QR code to sign this device in automatically.</div>
            <div class="auth-error" style="display:none"></div>
        </div>
    `;
    document.body.appendChild(overlay);

    const input = overlay.querySelector('.auth-input');
    const btn = overlay.querySelector('.auth-btn');
    const errorEl = overlay.querySelector('.auth-error');

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
