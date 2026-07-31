// Pernix — Sidebar component: session list with grouping, dots, tooltips, legend
import { el, text, clear } from '../render.js';
import { isMobile } from '../mobile.js';
import { get, patch } from '../api.js';

// ---------------------------------------------------------------------------
// Session type definitions
// ---------------------------------------------------------------------------

const SESSION_TYPES = {
    chat:   { label: 'Session', cls: 'chat',   color: 'var(--accent)' },
    cron:   { label: 'Cron',   cls: 'cron',   color: 'var(--info)' },
    worker: { label: 'Worker', cls: 'worker', color: 'var(--teal-dim)' },
    snooze: { label: 'Dream',  cls: 'snooze', color: 'var(--dream)' },
};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const STORAGE_KEY = 'pernix:sidebar';
let _onSelect = null;
let _onDelete = null;
let _tooltipTimer = null;
let _lastJson = '';

// Live activity text per session (cleared on idle)
const _activity = new Map(); // sessionId → string

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export function initSidebar(onSelect, onDelete) {
    _onSelect = onSelect;
    _onDelete = onDelete;
    _createTooltip();
    _createLegend();
    _createSearchBox();
}

// ---------------------------------------------------------------------------
// Session search — FTS5 over all message content. The index has powered
// scout's cross-session lookups all along; this finally exposes it to the
// user ("find the session where we discussed X").
// ---------------------------------------------------------------------------

let _searchActive = false;
let _searchTimer = null;

function _createSearchBox() {
    const sidebar = document.getElementById('sidebar');
    const list = document.getElementById('session-list');
    if (!sidebar || !list || document.getElementById('session-search')) return;
    const input = el('input', {
        id: 'session-search',
        type: 'search',
        placeholder: 'Search sessions…',
        autocomplete: 'off',
    });
    sidebar.insertBefore(input, list);
    input.addEventListener('input', () => {
        clearTimeout(_searchTimer);
        const q = input.value.trim();
        if (q.length < 2) {
            if (_searchActive) {
                _searchActive = false;
                _lastJson = '';  // force the normal list to re-render
                window.dispatchEvent(new CustomEvent('pernix:sidebar-refresh'));
            }
            return;
        }
        _searchTimer = setTimeout(() => _runSearch(q), 300);
    });
}

async function _runSearch(q) {
    _searchActive = true;
    const list = document.getElementById('session-list');
    if (!list) return;
    try {
        const data = await get(`/api/sessions/search?q=${encodeURIComponent(q)}`);
        if (!_searchActive) return;  // user cleared the box while we fetched
        clear(list);
        const results = data.results || [];
        if (results.length === 0) {
            list.appendChild(el('div', { class: 'search-empty' }, [text('No matching sessions')]));
            return;
        }
        for (const r of results) {
            list.appendChild(el('div', {
                class: 'session-item search-hit',
                onClick: () => { if (_onSelect) _onSelect(r.session_id); },
            }, [
                el('div', { class: 'session-title' }, [text(r.title || 'untitled')]),
                el('div', { class: 'search-snippet' }, [text(r.snippet || '')]),
                el('div', { class: 'search-meta' }, [
                    text(`${r.matches} match${r.matches === 1 ? '' : 'es'}${r.updated_at ? ' · ' + r.updated_at : ''}`),
                ]),
            ]));
        }
    } catch {
        clear(list);
        list.appendChild(el('div', { class: 'search-empty' }, [text('Search failed')]));
    }
}

export function updateSessionActivity(sessionId, activityText) {
    if (activityText) {
        _activity.set(sessionId, activityText);
    } else {
        _activity.delete(sessionId);
    }
    // Update just the activity element — no full re-render
    const actEl = document.querySelector(`.session-item[data-sid="${sessionId}"] .session-activity-text`);
    if (!actEl) return;

    actEl.classList.remove('scrolling');

    // Update text and check overflow for scrolling
    actEl.textContent = activityText || '';
    if (activityText) {
        requestAnimationFrame(() => {
            if (actEl.scrollWidth > actEl.parentElement.clientWidth) {
                const duration = Math.max(8, actEl.scrollWidth / 25);
                actEl.style.setProperty('--ticker-duration', duration + 's');
                actEl.classList.add('scrolling');
            }
        });
    }
}

export function renderSessionList(sessions, activeSid) {
    if (_searchActive) return;  // search results own the list until cleared
    const json = JSON.stringify(sessions) + '|' + activeSid;
    if (json === _lastJson) return;
    _lastJson = json;

    const list = document.getElementById('session-list');
    const scrollTop = list.scrollTop;
    clear(list);

    if (!sessions || sessions.length === 0) {
        list.scrollTop = scrollTop;
        _updateLegendCounts({});
        return;
    }

    const sidebarState = _loadState();
    const hidden = sidebarState.hiddenTypes || {};

    // Separate top-level from workers, apply type filter
    const allTopLevel = sessions.filter(s => s.session_type !== 'worker');
    const allWorkers = sessions.filter(s => s.session_type === 'worker');

    // Count all types (before filtering) for legend
    const counts = { chat: 0, cron: 0, worker: 0, snooze: 0 };
    for (const s of sessions) counts[_getTypeKey(s)]++;
    _updateLegendCounts(counts);

    // Filter by hidden types
    const topLevel = allTopLevel.filter(s => !hidden[_getTypeKey(s)]);
    const workers = allWorkers.filter(s => !hidden.worker);

    const workersByParent = {};
    for (const w of workers) {
        const pid = w.parent_session_id || '_orphan';
        if (!workersByParent[pid]) workersByParent[pid] = [];
        workersByParent[pid].push(w);
    }

    // Bucket by time group — pinned sessions get their own group on top.
    const GROUP_ORDER = ['Pinned', 'Today', 'Yesterday', 'This Week', 'This Month', 'Older'];
    const DEFAULT_COLLAPSED = { Pinned: false, Today: false, Yesterday: false, 'This Week': true, 'This Month': true, Older: true };
    const buckets = {};
    for (const g of GROUP_ORDER) buckets[g] = [];
    for (const s of topLevel) buckets[s.pinned ? 'Pinned' : _timeGroup(s.updated_at)].push(s);
    // Within each group: real conversations first, auto-created cron
    // sessions after — a busy schedule otherwise crowds chats out of view.
    for (const g of GROUP_ORDER) {
        buckets[g].sort(
            (a, b) =>
                (['cron', 'snooze'].includes(_getTypeKey(a)) ? 1 : 0) -
                (['cron', 'snooze'].includes(_getTypeKey(b)) ? 1 : 0)
        );
    }

    for (const label of GROUP_ORDER) {
        const group = buckets[label];
        if (!group.length) continue;

        const hasActive = group.some(s => s.id === activeSid) ||
            group.some(s => (workersByParent[s.id] || []).some(w => w.id === activeSid));
        // User's saved choice wins over hasActive — otherwise clicking to
        // collapse the group containing the active session "un-toggles"
        // itself on the next SSE redraw because hasActive forces uncollapsed.
        // Fall back to hasActive (force-open) only when there's no saved
        // preference for this group.
        const savedCollapsed = sidebarState.collapsed?.[label];
        const collapsed = savedCollapsed !== undefined
            ? savedCollapsed
            : (hasActive ? false : DEFAULT_COLLAPSED[label]);

        const header = el('div', { class: 'session-group-header' + (collapsed ? ' collapsed' : '') }, [
            el('span', { class: 'sg-arrow' }, [text('\u25BC')]),
            text(label),
            el('span', { class: 'sg-count' }, [text(String(group.length))]),
        ]);

        const body = el('div', { class: 'session-group-body' + (collapsed ? ' collapsed' : '') });

        header.addEventListener('click', () => {
            const isCollapsed = header.classList.toggle('collapsed');
            body.classList.toggle('collapsed', isCollapsed);
            _saveCollapsed(label, isCollapsed);
        });

        for (const s of group) {
            _renderSessionWithWorkers(s, body, activeSid, workersByParent, sidebarState);
        }

        list.appendChild(header);
        list.appendChild(body);
    }

    // Orphaned workers
    const renderedParents = new Set(topLevel.map(s => s.id));
    for (const [pid, ws] of Object.entries(workersByParent)) {
        if (pid === '_orphan' || !renderedParents.has(pid)) {
            for (const w of ws) {
                _renderSessionItem(w, list, activeSid, true);
            }
        }
    }

    list.scrollTop = scrollTop;
}

// ---------------------------------------------------------------------------
// Type detection
// ---------------------------------------------------------------------------

function _getTypeKey(session) {
    if (session.session_type === 'worker') return 'worker';
    if (session.session_type === 'snooze') return 'snooze';
    if (session.title && session.title.startsWith('Cron:')) return 'cron';
    return 'chat';
}

// ---------------------------------------------------------------------------
// Rendering helpers
// ---------------------------------------------------------------------------

function _renderSessionWithWorkers(session, container, activeSid, workersByParent, sidebarState) {
    _renderSessionItem(session, container, activeSid, false);

    const children = workersByParent[session.id];
    if (!children || children.length === 0) return;

    delete workersByParent[session.id];

    const wasCollapsed = sidebarState.parentCollapsed?.[session.id];
    const collapsed = wasCollapsed ?? true;

    const summary = el('div', { class: 'worker-summary' + (collapsed ? ' collapsed' : '') }, [
        el('span', { class: 'ws-arrow' }, [text('\u25BC')]),
        text(`${children.length} worker${children.length > 1 ? 's' : ''}`),
    ]);

    const group = el('div', { class: 'worker-group' + (collapsed ? ' collapsed' : '') });

    summary.addEventListener('click', (e) => {
        e.stopPropagation();
        const isCollapsed = summary.classList.toggle('collapsed');
        group.classList.toggle('collapsed', isCollapsed);
        _saveParentCollapsed(session.id, isCollapsed);
    });

    container.appendChild(summary);
    for (const w of children) {
        _renderSessionItem(w, group, activeSid, true);
    }
    container.appendChild(group);
}

function _renderSessionItem(session, container, activeSid, isWorker) {
    const typeKey = _getTypeKey(session);
    const typeDef = SESSION_TYPES[typeKey];

    const meta = [];

    // Time
    if (session.updated_at) {
        meta.push(el('span', { class: 'session-time' }, [text(_relativeTime(session.updated_at))]));
    }

    // Session ID badge — hover shows full id, click copies to clipboard
    meta.push(el('button', {
        class: 'session-id-badge',
        title: session.id,
        onClick: (e) => {
            e.stopPropagation();
            _copySessionId(e.currentTarget, session.id);
        },
    }, [text('#')]));

    // Pin toggle — pinned sessions live in their own group at the top.
    if (!isWorker) {
        meta.push(el('button', {
            class: `session-pin${session.pinned ? ' pinned' : ''}`,
            title: session.pinned ? 'Unpin session' : 'Pin session to top',
            onClick: async (e) => {
                e.stopPropagation();
                const next = !session.pinned;
                try {
                    await patch(`/api/sessions/${session.id}`, { pinned: next });
                    session.pinned = next ? 1 : 0;
                    _lastJson = '';  // force re-render with new grouping
                    window.dispatchEvent(new CustomEvent('pernix:sidebar-refresh'));
                } catch { /* leave as-is on failure */ }
            },
        }, [text('⚲')]));

        // Rename — swaps the title for an inline editor.
        meta.push(el('button', {
            class: 'session-rename',
            title: 'Rename session',
            onClick: (e) => {
                e.stopPropagation();
                _startRename(session);
            },
        }, [text('✎')]));
    }

    // Delete button \u2014 two-tap confirm: the \u00d7 is always visible on touch
    // devices and sits next to the copy-id button, so a single stray tap
    // must not permanently destroy a conversation (there is no undo).
    meta.push(el('button', {
        class: 'session-delete',
        title: 'Delete session',
        onClick: (e) => {
            e.stopPropagation();
            const btn = e.currentTarget;
            if (!btn.classList.contains('confirm')) {
                btn.classList.add('confirm');
                btn.textContent = 'sure?';
                btn._disarmTimer = setTimeout(() => {
                    btn.classList.remove('confirm');
                    btn.textContent = '\u00d7';
                }, 3000);
                return;
            }
            clearTimeout(btn._disarmTimer);
            if (_onDelete) _onDelete(session.id);
        },
    }, [text('\u00d7')]));

    // Build title text
    let titleText = session.title || 'New session';
    // Strip thinking model garbage from title display
    if (/^(Thinking Process|<think|Thought Process)/i.test(titleText)) {
        titleText = 'New session';
    }
    // Strip "Cron: " prefix — the dot indicates type
    if (titleText.startsWith('Cron: ') && typeKey === 'cron') {
        titleText = titleText.slice(6);
    }
    // Journal titles read as "Dream Jul 31" — the shorthand carries the
    // meaning, the dot carries the color.
    if (typeKey === 'snooze' && titleText.startsWith('Dream journal — ')) {
        const d = new Date(titleText.slice(16) + 'T00:00:00');
        titleText = isNaN(d)
            ? titleText
            : `Dream ${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`;
    }

    const classes = ['session-item'];
    if (session.id === activeSid) classes.push('active');
    if (isWorker) classes.push('worker');
    if (typeKey === 'cron') classes.push('cron-session');
    if (typeKey === 'snooze') classes.push('snooze-session');

    // Title with colored dot prefix
    const titleChildren = [];
    if (isWorker) {
        titleChildren.push(el('span', { class: 'worker-prefix' }, [text('\u21B3')]));
    }
    const isActive = session.state && session.state !== 'idle';
    const dotCls = `session-dot ${typeDef.cls}${isActive ? ' active-pulse' : ''}`;
    titleChildren.push(el('span', { class: dotCls }));
    titleChildren.push(el('span', { class: 'session-title-text' }, [text(titleText)]));

    // Needs-attention badges: "?" = blocked waiting for your input,
    // "✓" = a background turn finished since you last looked.
    if (session._attention === 'input') {
        titleChildren.push(el('span', {
            class: 'session-attn attn-input',
            title: 'Waiting for your input',
        }, [text('?')]));
    } else if (session._attention === 'done') {
        titleChildren.push(el('span', {
            class: 'session-attn attn-done',
            title: 'Finished while you were away',
        }, [text('✓')]));
    }

    // Activity ticker line: live activity when processing, subtitle/preview when idle
    let liveActivity = _activity.get(session.id);
    // Clear stale live activity for sessions that have returned to idle
    // (e.g. cron sessions whose turn.complete wasn't received because SSE was on another session)
    if (liveActivity && !isActive) {
        _activity.delete(session.id);
        liveActivity = null;
    }
    const idlePreview = session.subtitle || _cleanPreview(session.first_message);
    const activityStr = liveActivity || idlePreview;
    const actTextEl = el('span', { class: 'session-activity-text' }, activityStr ? [text(activityStr)] : []);
    const actLine = el('div', { class: 'session-activity' }, [actTextEl]);

    const item = el('div', {
        class: classes.join(' '),
        'data-sid': session.id,
        onClick: () => { if (_onSelect) _onSelect(session.id); },
    }, [
        el('div', { class: 'session-top' }, [
            el('span', { class: 'session-title' }, titleChildren),
            el('span', { class: 'session-meta' }, meta),
        ]),
        actLine,
    ]);

    // Tooltip events (desktop only — suppressed on mobile)
    if (!isMobile()) {
        item.addEventListener('mouseenter', (e) => _showTooltip(e, session, typeDef));
        item.addEventListener('mouseleave', () => _hideTooltip());
        item.addEventListener('mousemove', (e) => _positionTooltip(e));
    }

    container.appendChild(item);

    // Scroll only live activity text (not idle previews which look like stuck status)
    if (liveActivity) {
        requestAnimationFrame(() => {
            if (actTextEl.scrollWidth > actTextEl.parentElement.clientWidth) {
                const duration = Math.max(8, actTextEl.scrollWidth / 25);
                actTextEl.style.setProperty('--ticker-duration', duration + 's');
                actTextEl.classList.add('scrolling');
            }
        });
    }
}

// ---------------------------------------------------------------------------
// Inline rename — replaces the title span with an input; Enter/blur saves,
// Esc cancels. Rename does not bump recency (server contract).
// ---------------------------------------------------------------------------

function _startRename(session) {
    const item = document.querySelector(`.session-item[data-sid="${session.id}"]`);
    const titleSpan = item?.querySelector('.session-title-text');
    if (!titleSpan || item.querySelector('.session-rename-input')) return;

    const input = el('input', {
        class: 'session-rename-input',
        type: 'text',
        value: session.title || '',
    });
    input.addEventListener('click', (e) => e.stopPropagation());

    let finished = false;
    const finish = async (save) => {
        if (finished) return;
        finished = true;
        const newTitle = input.value.trim();
        if (save && newTitle && newTitle !== session.title) {
            try {
                const res = await patch(`/api/sessions/${session.id}`, { title: newTitle });
                session.title = res.title || newTitle;
            } catch { /* keep old title */ }
        }
        _lastJson = '';
        window.dispatchEvent(new CustomEvent('pernix:sidebar-refresh'));
    };

    input.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter') { e.preventDefault(); finish(true); }
        else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));

    titleSpan.replaceWith(input);
    input.focus();
    input.select();
}

function _cleanPreview(msg) {
    if (!msg) return '';
    let s = msg.trim();
    // Truncate to a concise preview of the user's request
    if (s.length > 60) s = s.slice(0, 57) + '\u2026';
    return s;
}

// ---------------------------------------------------------------------------
// Legend footer
// ---------------------------------------------------------------------------

function _createLegend() {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar || document.getElementById('sidebar-legend')) return;

    const sidebarState = _loadState();
    const hidden = sidebarState.hiddenTypes || {};

    const legend = el('div', { id: 'sidebar-legend' });

    for (const [key, def] of Object.entries(SESSION_TYPES)) {
        const isHidden = !!hidden[key];
        const item = el('button', {
            class: `legend-item ${def.cls}${isHidden ? ' dimmed' : ''}`,
            title: `${isHidden ? 'Show' : 'Hide'} ${def.label} sessions`,
            'data-type': key,
            onClick: () => _toggleType(key),
        }, [
            el('span', { class: `legend-dot ${def.cls}` }),
            el('span', { class: 'legend-label' }, [text(def.label)]),
            el('span', { class: 'legend-count', 'data-count-type': key }, [text('0')]),
        ]);
        legend.appendChild(item);
    }

    sidebar.appendChild(legend);
}

function _toggleType(typeKey) {
    const state = _loadState();
    if (!state.hiddenTypes) state.hiddenTypes = {};
    state.hiddenTypes[typeKey] = !state.hiddenTypes[typeKey];
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));

    // Update legend UI
    const btn = document.querySelector(`.legend-item[data-type="${typeKey}"]`);
    if (btn) {
        btn.classList.toggle('dimmed', !!state.hiddenTypes[typeKey]);
        btn.title = `${state.hiddenTypes[typeKey] ? 'Show' : 'Hide'} ${SESSION_TYPES[typeKey].label} sessions`;
    }

    // Force re-render
    _lastJson = '';
    // Dispatch custom event so app.js can trigger re-render
    document.dispatchEvent(new CustomEvent('sidebar:filter-changed'));
}

function _updateLegendCounts(counts) {
    for (const key of Object.keys(SESSION_TYPES)) {
        const span = document.querySelector(`.legend-count[data-count-type="${key}"]`);
        if (span) span.textContent = String(counts[key] || 0);
    }
}

// ---------------------------------------------------------------------------
// Copy session id
// ---------------------------------------------------------------------------

async function _copySessionId(btn, sessionId) {
    try {
        await navigator.clipboard.writeText(sessionId);
    } catch {
        // Fallback for non-secure contexts
        const ta = document.createElement('textarea');
        ta.value = sessionId;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch {}
        document.body.removeChild(ta);
    }
    btn.classList.add('copied');
    setTimeout(() => btn.classList.remove('copied'), 900);
}

// ---------------------------------------------------------------------------
// Tooltip
// ---------------------------------------------------------------------------

function _createTooltip() {
    if (document.getElementById('session-tooltip')) return;
    const tt = el('div', { id: 'session-tooltip' });
    document.body.appendChild(tt);
}

function _showTooltip(event, session, typeDef) {
    clearTimeout(_tooltipTimer);
    _tooltipTimer = setTimeout(() => {
        const tt = document.getElementById('session-tooltip');
        if (!tt) return;

        clear(tt);

        // Type + Title
        const titleRow = el('div', { class: 'tt-title' }, [
            el('span', { class: `session-dot ${typeDef.cls}` }),
            text(session.title || 'New session'),
        ]);
        tt.appendChild(titleRow);
        tt.appendChild(el('div', { class: 'tt-divider' }));

        // First message preview
        if (session.first_message) {
            const preview = session.first_message.length > 120
                ? session.first_message.slice(0, 120) + '...'
                : session.first_message;
            tt.appendChild(el('div', { class: 'tt-preview' }, [text(preview)]));
        }

        // Stats
        const stats = [];
        if (session.message_count != null) {
            stats.push(el('span', {}, [text(`${session.message_count} msgs`)]));
        }
        if (session.total_tokens) {
            stats.push(el('span', {}, [text(`${_formatTokens(session.total_tokens)} tokens`)]));
        }
        // Money builds trust for an agent that burns tokens autonomously —
        // cron jobs run while the user sleeps; they should see the bill.
        if (session.total_cost && session.total_cost >= 0.005) {
            stats.push(el('span', {}, [text(`$${session.total_cost.toFixed(2)}`)]));
        }
        if (stats.length) {
            tt.appendChild(el('div', { class: 'tt-stats' }, stats));
        }

        // Timestamps
        const times = [];
        if (session.created_at) times.push(`Created ${_formatDate(session.created_at)}`);
        if (session.updated_at) times.push(`Updated ${_relativeTime(session.updated_at)}`);
        if (times.length) {
            tt.appendChild(el('div', { class: 'tt-stats' }, [
                el('span', {}, [text(times.join(' \u00b7 '))]),
            ]));
        }

        // State
        if (session.state && session.state !== 'idle') {
            tt.appendChild(el('div', { class: 'tt-stats' }, [
                el('span', {}, [text(`State: ${session.state}`)]),
            ]));
        }

        _positionTooltip(event);
        tt.classList.add('visible');
    }, 350);
}

function _hideTooltip() {
    clearTimeout(_tooltipTimer);
    const tt = document.getElementById('session-tooltip');
    if (tt) tt.classList.remove('visible');
}

function _positionTooltip(event) {
    const tt = document.getElementById('session-tooltip');
    if (!tt || !tt.classList.contains('visible')) return;

    const sidebar = document.getElementById('sidebar');
    const sidebarRect = sidebar.getBoundingClientRect();
    const ttRect = tt.getBoundingClientRect();

    let left = sidebarRect.right + 8;
    let top = event.clientY - 20;

    if (left + ttRect.width > window.innerWidth) {
        left = sidebarRect.left - ttRect.width - 8;
    }
    if (top + ttRect.height > window.innerHeight) {
        top = window.innerHeight - ttRect.height - 8;
    }
    if (top < 8) top = 8;

    tt.style.left = left + 'px';
    tt.style.top = top + 'px';
}

// ---------------------------------------------------------------------------
// Time helpers
// ---------------------------------------------------------------------------

function _timeGroup(isoStr) {
    if (!isoStr) return 'Older';
    const now = new Date();
    const d = new Date(isoStr.replace(/\+00:00$/, 'Z'));
    if (isNaN(d.getTime())) return 'Older';
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const dStart = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const diffDays = Math.floor((todayStart - dStart) / 86400000);
    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    if (diffDays <= 7) return 'This Week';
    if (diffDays <= 30) return 'This Month';
    return 'Older';
}

function _relativeTime(isoStr) {
    if (!isoStr) return '';
    let s = isoStr.replace(/\+00:00$/, 'Z');
    if (!/[Z+-]\d{2}/.test(s)) s += 'Z';
    const date = new Date(s);
    if (isNaN(date.getTime())) return '';
    const diffSec = Math.floor((Date.now() - date.getTime()) / 1000);
    if (diffSec < 60) return 'now';
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`;
    if (diffSec < 604800) return `${Math.floor(diffSec / 86400)}d`;
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function _formatDate(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr.replace(/\+00:00$/, 'Z'));
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function _formatTokens(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
    return String(n);
}

// ---------------------------------------------------------------------------
// localStorage persistence
// ---------------------------------------------------------------------------

function _loadState() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); }
    catch { return {}; }
}

function _saveCollapsed(label, isCollapsed) {
    const state = _loadState();
    if (!state.collapsed) state.collapsed = {};
    state.collapsed[label] = isCollapsed;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function _saveParentCollapsed(orchId, isCollapsed) {
    const state = _loadState();
    if (!state.parentCollapsed) state.parentCollapsed = {};
    state.parentCollapsed[orchId] = isCollapsed;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}
