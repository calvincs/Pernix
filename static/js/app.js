// Pernix — Main application entry point

import { state, subscribe } from './store.js';
import { get, post, del, getAuthToken, setAuthToken } from './api.js';
import { connectSSE, disconnectSSE } from './sse.js';
import { getPermission, requestPermission, connectGlobalNotifications, registerServiceWorker, subscribePush } from './notifications.js';
import { el, text, clear, initMarked, renderMarkdown } from './render.js';
import { initSigil } from './sigil.js';
import { openSettings } from './components/modals/settings.js';
import { openTimeline, appendTimelineRow, isTimelineOpen } from './components/modals/timeline.js';
import { initBell, openBellPanel, closeBellPanel, refreshBell } from './components/notification-bell.js';
import { initJobsIndicator } from './components/jobs-indicator.js';
import { initSidebar, renderSessionList as renderSidebar, updateSessionActivity } from './components/sidebar.js';
import { initFilePanel, toggleFilePanel, openFilePanel } from './components/file-panel.js';
import { initMobile, isMobile, closeSidebar } from './mobile.js';

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

    // Settings + bell + jobs + files + transcript buttons
    document.getElementById('settings-btn').addEventListener('click', openSettings);
    document.getElementById('state-badge')?.addEventListener('click', openTimeline);
    document.getElementById('files-btn').addEventListener('click', toggleFilePanel);
    document.getElementById('copy-transcript-btn').addEventListener('click', copyTranscript);
    initFilePanel({ selectSession });
    initBell();

    // Global notification SSE — connects immediately, no session required.
    // Handles browser notifications for dialog.notification and dialog.question events.
    connectGlobalNotifications();

    // Ask for notification permission on first load if not yet decided
    if (getPermission() === 'default') requestPermission();
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

    // Poll — guarded by isOnline() inside the functions / api layer
    setInterval(loadSessions, 10000);
    setInterval(loadHealth, 30000);

    // Connectivity overlay
    window.addEventListener('pernix:offline', _showOfflineOverlay);
    window.addEventListener('pernix:online', () => {
        _hideOfflineOverlay();
        loadSessions();
        loadHealth();
    });
});

function _showOfflineOverlay() {
    if (document.getElementById('offline-overlay')) return;
    const overlay = document.createElement('div');
    overlay.id = 'offline-overlay';
    overlay.className = 'offline-overlay';
    overlay.innerHTML = `
        <div class="offline-card">
            <div class="offline-spinner"></div>
            <div class="offline-title">Disconnected from server</div>
            <div class="offline-subtitle">Trying to reconnect every 10 seconds&hellip;</div>
        </div>
    `;
    document.body.appendChild(overlay);
}

function _hideOfflineOverlay() {
    const o = document.getElementById('offline-overlay');
    if (o) o.remove();
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

async function loadSessions() {
    try {
        const data = await get('/api/sessions?limit=500');
        state.sessions = data.items || [];
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
            showEmptyState();
        }
        await loadSessions();
    } catch (e) {
        console.error('Failed to delete session:', e);
    }
}

async function selectSession(sid) {
    if (isMobile()) closeSidebar();
    state.sid = sid;
    // Reset streaming state to prevent cross-session leakage
    _streamingEl = null;
    _collected = '';
    _toolGroup = null;
    _toolGroupCount = 0;
    if (_parseTimer) { clearTimeout(_parseTimer); _parseTimer = null; }
    renderSidebar(state.sessions, state.sid);
    await loadMessages(sid);
    await loadContextInfo(sid);

    // Fetch session status to get event_seq and streaming state BEFORE connecting SSE
    state.streaming = false;
    _showSendButton();
    updateStatus('');
    _clearToolStatus();
    _applyStateBadge('idle_ready', '');  // reset badge before fetching real state
    const _mEl = document.getElementById('status-model');
    if (_mEl) _mEl.textContent = state.model;
    try {
        const status = await get(`/api/sessions/${sid}/status`);
        // Set _lastSeq to server's current event_seq so SSE dedup skips
        // events already rendered from DB — prevents the load+replay race
        _lastSeq = status.event_seq || 0;
        _applyStateBadge(status.state || 'idle_ready', '');

        if (status.status === 'processing' || status.status === 'scouting') {
            state.streaming = true;
            _showStopButton();
            _streamingEl = appendMessage('assistant', '');
            _collected = '';
        }
    } catch {
        _lastSeq = 0;
    }

    connectSSE(sid, handleEvent);

}

async function loadMessages(sid) {
    const inner = _messagesInner();
    const scroll = _messagesScroll();
    clear(inner);
    try {
        const data = await get(`/api/sessions/${sid}`);
        const messages = data.messages || [];
        if (messages.length === 0) {
            showEmptyState();
            return;
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
                    renderEvalCard(evalData);
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
            } else if (m.role === 'notice') {
                // Persisted notices (cancellations, reflect-skipped, queue-dropped, etc.)
                // render with system-message styling so they're visible but unobtrusive,
                // matching how the live SSE handlers display the same events.
                closeToolGroup();
                appendMessage('system', m.content || '');
            } else {
                closeToolGroup();
                appendMessage(m.role, m.content);
            }
        }
        closeToolGroup();
        scroll.scrollTop = scroll.scrollHeight;
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
        el.title = `history: ${data.history_pct ?? 0}% of ${data.history_budget ?? 0} ` +
                   `(compacts at ${softPct}%, critical at ${critPct}%). ` +
                   `Compactions this session: ${compactions}.`;
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
        el('br'),
        text('Type '),
        el('kbd', {}, [text('/help')]),
        text(' for available commands.'),
    ]);
    const welcome = el('div', { class: 'empty-state' }, [figure, kicker, help]);
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
        appendCommandCard('Commands', null, list);
    },
};

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
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            send();
        }
    });

    textarea.addEventListener('input', () => {
        textarea.style.height = 'auto';
        textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
    });
}

async function send() {
    const textarea = document.getElementById('msg-input');
    const message = textarea.value.trim();
    if (!message && _pendingFiles.length === 0) return;

    // Slash commands — check before streaming guard so /cancel works mid-stream
    const cmd = Object.keys(SLASH_COMMANDS).find(c => message.startsWith(c));
    if (cmd) {
        textarea.value = '';
        textarea.style.height = 'auto';
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
        await _injectMessage(message);
        return;
    }

    textarea.value = '';
    textarea.style.height = 'auto';

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

    // Upload pending files first
    const uploadedFiles = [];
    if (_pendingFiles.length > 0) {
        for (const pf of _pendingFiles) {
            if (pf.uploaded && pf.serverName) {
                uploadedFiles.push(pf.serverName);
                continue;
            }
            try {
                const formData = new FormData();
                formData.append('file', pf.file);
                const uploadHeaders = {};
                const _t = getAuthToken();
                if (_t) uploadHeaders['Authorization'] = `Bearer ${_t}`;
                const resp = await fetch('/api/upload', { method: 'POST', body: formData, headers: uploadHeaders });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    throw new Error(err.detail || resp.statusText);
                }
                const result = await resp.json();
                uploadedFiles.push(result.filename);
            } catch (e) {
                appendMessage('system', `Upload failed: ${pf.name} — ${e.message}`);
            }
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

    appendMessage('user', finalMessage);
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

    // Also support input click-to-upload via hidden file input
    const fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.multiple = true;
    fileInput.style.display = 'none';
    document.body.appendChild(fileInput);
    fileInput.addEventListener('change', () => {
        if (fileInput.files.length > 0) {
            addPendingFiles(fileInput.files);
        }
        fileInput.value = '';
    });
}

function addPendingFiles(fileList) {
    for (const file of fileList) {
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

function _showToolStatus(name, args) {
    if (name === 'browse_web') return; // handled by #browser-status
    const statusEl = document.getElementById('tool-status');
    if (!statusEl) return;
    const [icon, labelFn] = _TOOL_ICONS[name] || ['⚙', () => name];
    const label = labelFn(args || {});
    statusEl.innerHTML = '';
    statusEl.appendChild(el('span', { class: 'tool-icon' }, [text(icon)]));
    statusEl.appendChild(text('\u00a0' + label));
    statusEl.classList.add('active');
    if (_toolStatusTimer) clearTimeout(_toolStatusTimer);
    _toolStatusTimer = setTimeout(_clearToolStatus, 2500);
}

function _clearToolStatus() {
    if (_toolStatusTimer) { clearTimeout(_toolStatusTimer); _toolStatusTimer = null; }
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
                    clear(contentEl);
                    contentEl.appendChild(renderMarkdown(_collected));
                    addCopyButtons(contentEl);
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
        if (state.sid) updateSessionActivity(state.sid, event.name);
        _showToolStatus(event.name, event.arguments || {});
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
    }

    else if (type === 'message.injected') {
        // Agent will see this message at the next tool round — clear queued indicator
        const msgEl = _injectedMessages.shift();
        if (msgEl) msgEl.classList.remove('queued');
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
    }

    else if (type === 'worker.done') {
        const reason = event.termination_reason ? ` (${event.termination_reason})` : '';
        const err = event.error ? ` error: ${event.error}` : '';
        appendMessage('system', `Worker done: ${event.worker_id}${reason}${err}`);
    }

    else if (type === 'worker.failed') {
        // Spawn-time failure: manager.prompt() raised before the worker
        // ever ran. Distinct from worker.done with an error — that's a
        // worker that ran and errored mid-turn. This is a worker that
        // never started, so there's no transcript or summary to inspect.
        const wid = event.worker_id || 'unknown';
        const err = event.error || '(no error message)';
        appendMessage('system', `⚠ Worker failed to start: ${wid} — ${err}`);
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
                mEl.classList.remove('has-override');
                mEl.textContent = state.model;
            }
        }
    }

    else if (type === 'turn.complete') {
        // Safety net: ensure button is always reset when turn finishes
        if (state.streaming) {
            state.streaming = false;
            _showSendButton();
        }
        // Clear any remaining queued indicators — turn is over
        for (const el of _injectedMessages) el.classList.remove('queued');
        _injectedMessages = [];
        _clearToolStatus();
        // Restore model name in case scout routed to a different model this turn
        const mEl = document.getElementById('status-model');
        if (mEl) {
            mEl.classList.remove('has-override');
            mEl.textContent = state.model;
        }
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
}

// ---------------------------------------------------------------------------
// Sync streaming state with server (recovery after SSE reconnect or timeout)
// ---------------------------------------------------------------------------

async function _syncStreamingState() {
    if (!state.sid) return;
    try {
        const status = await get(`/api/sessions/${state.sid}/status`);
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

async function _softReload() {
    if (!state.sid) return;
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
        _lastSeq = status.event_seq || _lastSeq;
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
}

async function _reconcile() {
    // Lightweight check: compare server event_seq with client _lastSeq
    if (!state.sid || state.streaming) return;  // Don't reconcile mid-stream
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
        }
    } catch {}
}

// Periodic reconciliation every 45 seconds (safety net)
setInterval(() => { if (state.sid && !state.streaming) _reconcile(); }, 45000);

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
    btn.disabled = false;
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
        await fetch(`/api/sessions/${state.sid}/cancel`, { method: 'POST', headers: cancelHeaders });
    } catch (e) {
        appendMessage('system', `Cancel failed: ${e.message}`);
    }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function appendMessage(role, content) {
    const inner = _messagesInner();
    const scroll = _messagesScroll();

    // Remove empty state if present
    const emptyEl = inner.querySelector('.empty-state');
    if (emptyEl) emptyEl.remove();

    const msgEl = el('div', { class: `message ${role}` }, [
        el('div', { class: 'role-label' }, [text(role)]),
        el('div', { class: 'content' }),
    ]);

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

    inner.appendChild(msgEl);
    scroll.scrollTop = scroll.scrollHeight;
    return msgEl;
}

/**
 * Render a live ask_user question bubble in the chat.
 * The bubble can later be updated with an answer or marked dismissed.
 */
function appendQuestionBubble(questionId, questionText, context) {
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
    scroll.scrollTop = scroll.scrollHeight;
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

/** Update a question bubble to show it was dismissed. */
function markQuestionDismissed(questionId) {
    const bubble = _questionBubbles.get(questionId);
    if (!bubble) return;
    const area = bubble.querySelector('.q-answer-area');
    if (!area || area.querySelector('.q-dismissed')) return;
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
    scroll.scrollTop = scroll.scrollHeight;
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
    scroll.scrollTop = scroll.scrollHeight;
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
    scroll.scrollTop = scroll.scrollHeight;
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
    scroll.scrollTop = scroll.scrollHeight;
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
    scroll.scrollTop = scroll.scrollHeight;
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
    scroll.scrollTop = scroll.scrollHeight;
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
    scroll.scrollTop = scroll.scrollHeight;
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
    scroll.scrollTop = scroll.scrollHeight;
}

function _extractHostname(url) {
    try { return new URL(url).hostname; } catch { return url.slice(0, 40); }
}

// ---------------------------------------------------------------------------
// Health / Status
// ---------------------------------------------------------------------------

async function loadHealth() {
    try {
        const data = await get('/api/health');
        state.model = data.model;
        document.getElementById('status-model').textContent = data.model;
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
            <div class="auth-hint">Find this token in the server console output</div>
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
                errorEl.textContent = 'Invalid token. Check the server console and try again.';
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
