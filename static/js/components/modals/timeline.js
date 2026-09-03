// Pernix — State timeline modal
//
// Three tabs:
//   * Lane     — one row per TURN, oldest at the top. Each row is the turn's
//                phases drawn as proportional segments of one bar, its tool
//                calls as ticks placed by start time, and its duration at the
//                right edge. Selecting a row opens its Story below the lane:
//                Plan (the scout report), Act (the tool calls and the token
//                bill), Verify (the reflect chain and the eval gates) and
//                Remembers (compactions and notices).
//   * Graph    — Mermaid stateDiagram-v2 of visited states, edge counts, current
//                state highlighted, invariant-violation edges flagged, per-state
//                dwell-time breakdown, per-turn tool tally. Clicking a state
//                node filters the Timeline tab to that state.
//   * Timeline — state-log rows merged with tool-call rows by timestamp,
//                grouped into collapsible turns, with wall-clock times, idle-gap
//                dividers, a filter bar (all/states/tools/errors + text), and
//                backward pagination ("load older").
//
// Data sources:
//   GET /api/sessions/{sid}/turns?limit=20                  (the lane + story)
//   GET /api/sessions/{sid}/turns?before_turn=N             (older turn pages)
//   GET /api/sessions/{sid}/state-log?limit=500&tail=true   (newest window)
//   GET /api/sessions/{sid}/state-log?before_id=N           (older pages)
//   GET /api/sessions/{sid}                                 (messages → tool calls)
// Merge key for the two list-tab sources: state row `timestamp_ms` vs message
// `created_at` (ISO) → ms. The lane needs no merge at all — /turns is the read
// model that already did the join, server-side (see docs/api.md, "Get Turns").
//
// Lane colours come straight from the --state-<name>-{fg,bg} custom properties
// as `var()` references, not through theme.js's hex() bridge: these are HTML
// elements, so the browser resolves the token itself and a theme swap repaints
// them with no JS involved. The Mermaid classDefs below still need real hex,
// because a comma-separated classDef cannot carry a color-mix() token stream.
//
// Mermaid is vendored and lazy-loaded from disk on first open.

import { el, text, setSanitizedSvg } from '../../render.js';
import { icon } from '../../icons.js';
import { get } from '../../api.js';
import { state } from '../../store.js';
import { openOverlay } from '../../a11y.js';
import { hex } from '../../theme.js';
import { bindStripScroll } from '../file-panel.js';

// Vendored rather than CDN-loaded: the page ships a `script-src 'self'` CSP
// (see index.html) and has to work offline on a LAN. Mermaid 10's ESM build
// is code-split across ~120 chunks, so the single-file UMD bundle is what is
// vendored — hence a <script> tag and window.mermaid rather than import().
const MERMAID_SRC = '/static/vendor/mermaid.min.js';

const PAGE_LIMIT = 500;
const GAP_MS = 30_000; // idle gap worth flagging between adjacent rows

// Lane paging. 20 turns is the endpoint's own default and about two screens
// of rows; "Load older turns" walks backward from there with before_turn.
const TURN_PAGE_LIMIT = 20;
// A phase this short is invisible at any realistic bar width, so it is drawn
// at a floor instead of at its true fraction. 3px is the narrowest strip that
// still reads as a segment and still takes a hover.
const LANE_MIN_SEG_PCT = 1.5;

let _overlay = null;
let _closeOverlay = null;  // teardown from a11y.js openOverlay()
let _mermaid = null;
let _mermaidPromise = null;
let _data = { stateLog: [], messages: [], liveTools: [], hasOlder: false };

// The lane's own data: turn records from /turns, OLDEST FIRST (the endpoint
// answers newest first; the lane reads top-to-bottom like the transcript).
let _turns = [];
let _turnsHasOlder = false;
let _selectedTurn = null;
let _laneListEl = null;
let _storyEl = null;
let _laneRefreshTimer = null;   // pending debounced refetch of the newest page
let _laneLoadToken = 0;         // newest lane fetch wins

// In-flight tool rows: appended on tool.start, upgraded in place on tool.call.
// Without these, a multi-minute tool run (rlm_process, bash, spawn_worker)
// is invisible until it completes and then reads as an unexplained idle gap.
// Purely a live-DOM affordance: entries only join _data.liveTools (export,
// tally, graph) once the completed result arrives.
let _pendingToolRows = [];
let _filter = { mode: 'all', q: '' };
let _scroller = null;   // .modal-body — the actual scroll container
let _revealTab = () => {};   // set by bindStripScroll when the strip is built
let _bodyEl = null;     // #timeline-modal-body — rebuilt by _renderTimeline
let _filterInput = null;
let _filterBtns = [];
let _tabBtns = [];
let _panes = [];
let _lastRowTs = 0;     // last rendered row timestamp, for live gap dividers
let _graphRefreshTimer = null;  // pending debounced graph refresh
let _graphRenderToken = 0;      // newest _renderGraph call wins
let _graphRenderSeq = 0;        // unique mermaid render ids

export async function openTimeline() {
    if (_overlay) return;
    if (!state.sid) return;

    _filter = { mode: 'all', q: '' };
    _lastRowTs = 0;
    _turns = [];
    _turnsHasOlder = false;
    _selectedTurn = null;

    _laneListEl = el('div', { class: 'tl-lane-list', id: 'timeline-lane' });
    _storyEl = el('div', { class: 'tl-story', id: 'timeline-story' });
    const lanePane = el('div', { class: 'tab-content active', 'data-tab': 'lane' }, [
        _laneListEl,
        _storyEl,
    ]);

    const graphPane = el('div', { class: 'tab-content', 'data-tab': 'graph' }, [
        el('div', { class: 'timeline-graph-status' }, [text('Loading…')]),
        el('div', { class: 'timeline-graph-container', id: 'timeline-graph' }),
        el('div', { class: 'timeline-graph-caption', id: 'timeline-graph-caption' }),
    ]);
    _bodyEl = el('div', { id: 'timeline-modal-body' });
    const listPane = el('div', { class: 'tab-content', 'data-tab': 'timeline' }, [
        _buildFilterBar(),
        _bodyEl,
    ]);

    _tabBtns = [
        el('button', { class: 'tab-btn active', 'data-tab': 'lane' }, [text('Lane')]),
        el('button', { class: 'tab-btn', 'data-tab': 'graph' }, [text('Graph')]),
        el('button', { class: 'tab-btn', 'data-tab': 'timeline' }, [text('Timeline')]),
    ];
    _panes = [lanePane, graphPane, listPane];
    _tabBtns.forEach(btn => {
        btn.addEventListener('click', () => _switchTab(btn.getAttribute('data-tab')));
    });
    const tabBar = el('div', { class: 'tab-bar' }, _tabBtns);
    _revealTab = bindStripScroll(tabBar, _tabBtns[0]);

    const copyBtn = el('button', {
        class: 'tl-copy-btn',
        title: 'Copy state log + tool calls as JSON',
        'aria-label': 'Copy the state log and tool calls as JSON',
    }, [text('Copy JSON')]);
    copyBtn.addEventListener('click', () => _copyExport(copyBtn));

    const modalBody = el('div', { class: 'modal-body timeline-modal-content' }, [lanePane, graphPane, listPane]);
    _scroller = modalBody;

    const card = el('div', {
        id: 'timeline-modal',
        class: 'modal-card',
    }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text('State timeline')]),
            copyBtn,
            el('button', {
                class: 'modal-close',
                title: 'Close',
                'aria-label': 'Close the state timeline',
                onClick: closeTimeline,
            }, [icon('x', { size: 14 })]),
        ]),
        tabBar,
        modalBody,
    ]);

    _overlay = el('div', { class: 'modal-overlay' }, [card]);
    _overlay.addEventListener('click', (e) => {
        if (e.target === _overlay) closeTimeline();
    });
    document.body.append(_overlay);
    _closeOverlay = openOverlay(card, { onClose: _onEsc });

    await _load();
    _renderTimeline();
    _renderLane();
    // The Graph tab is no longer the one on screen, and mermaid.min.js is a
    // ~1MB parse. It loads on the first switch to it instead of on every open.
}

export function closeTimeline() {
    if (_closeOverlay) { _closeOverlay(); _closeOverlay = null; }
    if (_overlay) {
        _overlay.remove();
        _overlay = null;
    }
    if (_graphRefreshTimer) {
        clearTimeout(_graphRefreshTimer);
        _graphRefreshTimer = null;
    }
    if (_laneRefreshTimer) {
        clearTimeout(_laneRefreshTimer);
        _laneRefreshTimer = null;
    }
    // Invalidate any _renderGraph still parked on an await, so it cannot
    // touch a pane belonging to a modal that is already gone. Same for a
    // lane fetch in flight.
    _graphRenderToken++;
    _laneLoadToken++;
    _scroller = null;
    _bodyEl = null;
    _filterInput = null;
    _filterBtns = [];
    _tabBtns = [];
    _panes = [];
    _laneListEl = null;
    _storyEl = null;
    _turns = [];
    _selectedTurn = null;
}

export function isTimelineOpen() {
    return _overlay !== null;
}

function _switchTab(target) {
    _tabBtns.forEach(b => b.classList.toggle('active', b.getAttribute('data-tab') === target));
    _revealTab(_tabBtns.find(b => b.getAttribute('data-tab') === target));
    _panes.forEach(p => p.classList.toggle('active', p.getAttribute('data-tab') === target));
    if (target === 'graph') {
        const pane = _panes.find(p => p.getAttribute('data-tab') === 'graph');
        if (pane) _renderGraph(pane);
    } else if (target === 'lane' && _scroller) {
        // The lane reads top-down and its "Load older turns" control is at
        // the top, so entering it lands at the top — the opposite of the
        // Timeline tab, whose newest rows are at the bottom.
        _scroller.scrollTop = 0;
    } else if (target === 'timeline' && _scroller) {
        // The .modal-body scroller is shared between tabs — land on the
        // newest rows when entering the timeline.
        _scroller.scrollTop = _scroller.scrollHeight;
    }
}

// True when the Timeline tab is the visible one — scrolling the shared
// .modal-body scroller only makes sense then.
function _timelineActive() {
    const pane = _panes.find(p => p.getAttribute('data-tab') === 'timeline');
    return !!pane && pane.classList.contains('active');
}

// Live-append from _renderStateBadge. Appends a state row to the Timeline
// tab (if currently rendered) and invalidates the graph.
export function appendTimelineRow(row) {
    const ts = Date.now();
    const data = {
        id: null,
        session_id: state.sid,
        turn_id: row.turn_id || 0,
        retry_index: row.retry_index || 0,
        from_state: row.from_state,
        to_state: row.to_state,
        reason: row.reason,
        termination_reason: row.termination_reason,
        elapsed_ms: row.elapsed_ms,
        timestamp_ms: ts,
    };
    _data.stateLog.push(data);
    _appendLiveEntry({ kind: 'state', ts, turn: data.turn_id, data });
    _refreshGraphIfVisible();
    _scheduleLaneRefresh();
}

// Live-append from the tool.start SSE handler in app.js: a "running" row
// that appendTimelineToolRow upgrades in place when the result arrives.
export function appendTimelineToolStart(tool) {
    if (!_bodyEl) return;
    const entry = {
        name: tool.name || 'tool',
        args: tool.args || null,
        content: '',
        latency_ms: null,
        was_error: false,
        running: true,
        ts: Date.now(),
    };
    const rowEl = _appendLiveEntry({ kind: 'tool', ts: entry.ts, turn: null, data: entry });
    if (rowEl) _pendingToolRows.push({ name: entry.name, startTs: entry.ts, el: rowEl });
    _scheduleLaneRefresh();
}

// Live-append from the tool.call SSE handler in app.js. Mirrors what the
// reopen path reconstructs from session messages. If a tool.start row for
// this tool is on screen, upgrade it in place instead of appending.
export function appendTimelineToolRow(tool) {
    const entry = {
        name: tool.name || 'tool',
        args: tool.args || null,
        content: tool.content || '',
        latency_ms: tool.latency_ms || null,
        was_error: !!tool.was_error,
        ts: Date.now(),
    };
    // Oldest matching pending row wins (parallel same-name calls complete FIFO).
    _pendingToolRows = _pendingToolRows.filter(p => p.el.isConnected);
    const idx = _pendingToolRows.findIndex(p => p.name === entry.name);
    if (idx !== -1) {
        const pending = _pendingToolRows.splice(idx, 1)[0];
        entry.ts = pending.startTs; // keep the row anchored where the run began
        pending.el.replaceWith(_buildToolRow(entry, entry.ts));
        _data.liveTools.push(entry);
        _refreshGraphIfVisible();
        _scheduleLaneRefresh();
        return;
    }
    _data.liveTools.push(entry);
    _appendLiveEntry({ kind: 'tool', ts: entry.ts, turn: null, data: entry });
    _refreshGraphIfVisible();
    _scheduleLaneRefresh();
}

// Coalesce graph refreshes. Every live tool.start/tool.call invalidates the
// diagram, and mermaid.render() is a full layout pass — re-running it once per
// event through a tool-heavy turn pins the main thread for no visible gain,
// since the intermediate frames are superseded before anyone reads them. A
// trailing debounce collapses a burst into a single render.
const GRAPH_REFRESH_DEBOUNCE_MS = 250;

function _refreshGraphIfVisible() {
    if (_graphRefreshTimer) clearTimeout(_graphRefreshTimer);
    _graphRefreshTimer = setTimeout(() => {
        _graphRefreshTimer = null;
        const graphPane = document.querySelector('#timeline-modal .tab-content[data-tab="graph"]');
        if (graphPane && graphPane.classList.contains('active')) {
            _renderGraph(graphPane);
        }
    }, GRAPH_REFRESH_DEBOUNCE_MS);
}

function _appendLiveEntry(entry) {
    if (!_bodyEl) return null;
    if (!_entryMatches(entry)) return null;

    const placeholder = _bodyEl.querySelector('.timeline-empty');
    if (placeholder) placeholder.remove();

    // A state row that starts a new turn gets a fresh group; everything else
    // lands in the last group on screen.
    let group = _bodyEl.querySelector('.tl-turn-group:last-child');
    if (entry.kind === 'state' && entry.turn != null &&
        (!group || group.dataset.turn !== String(entry.turn))) {
        group = null;
    }
    if (!group) {
        group = _buildTurnGroup(entry.turn, {});
        _bodyEl.appendChild(group);
    }
    const groupBody = group.querySelector('.tl-turn-body');
    if (_lastRowTs && entry.ts - _lastRowTs > GAP_MS) {
        groupBody.appendChild(_buildGapRow(entry.ts - _lastRowTs));
    }
    _lastRowTs = entry.ts;
    const rowEl = entry.kind === 'state'
        ? _buildStateRow(entry.data, entry.ts)
        : _buildToolRow(entry.data, entry.ts);
    groupBody.appendChild(rowEl);
    group.classList.remove('collapsed');
    if (_scroller && _timelineActive()) _scroller.scrollTop = _scroller.scrollHeight;
    return rowEl;
}

async function _load() {
    // Three requests, one round trip. /turns is its own read model rather
    // than a slice of the other two — it is the only one the Lane tab reads.
    const [logRes, sessRes, turnRes] = await Promise.all([
        get(`/api/sessions/${state.sid}/state-log?limit=${PAGE_LIMIT}&tail=true`)
            .catch(e => { console.error('Failed to load the state log:', e); return null; }),
        get(`/api/sessions/${state.sid}`)
            .catch(e => { console.error('Failed to load the transcript:', e); return null; }),
        get(`/api/sessions/${state.sid}/turns?limit=${TURN_PAGE_LIMIT}`)
            .catch(e => { console.error('Failed to load the turn lane:', e); return null; }),
    ]);
    const entries = (logRes && logRes.entries) || [];
    _data = {
        stateLog: entries,
        messages: (sessRes && sessRes.messages) || [],
        liveTools: [],
        hasOlder: entries.length === PAGE_LIMIT,
    };
    _turns = _turnsOldestFirst(turnRes);
    _turnsHasOlder = !!(turnRes && turnRes.has_more);
    // The newest turn is the one anybody opening the timeline came to read.
    _selectedTurn = _turns.length ? _turns[_turns.length - 1].turn_id : null;
    _pendingToolRows = [];
}

async function _loadOlder(btn) {
    const oldest = _data.stateLog.find(r => r.id != null);
    if (!oldest) return;
    btn.disabled = true;
    btn.textContent = 'Loading…';
    try {
        const res = await get(`/api/sessions/${state.sid}/state-log?before_id=${oldest.id}&limit=${PAGE_LIMIT}`);
        const older = res.entries || [];
        _data.stateLog = older.concat(_data.stateLog);
        _data.hasOlder = older.length === PAGE_LIMIT;
        const prevHeight = _scroller ? _scroller.scrollHeight : 0;
        const prevTop = _scroller ? _scroller.scrollTop : 0;
        _renderTimeline({ scroll: 'none' });
        if (_scroller) _scroller.scrollTop = _scroller.scrollHeight - prevHeight + prevTop;
    } catch (e) {
        console.error('Failed to load older state log:', e);
        btn.disabled = false;
        btn.textContent = 'Load older';
    }
}

// ---------------------------------------------------------------------------
// Lane tab — one row per turn, oldest at the top
//
// Everything here is read straight off GET /api/sessions/{sid}/turns. The lane
// does no joining and no arithmetic beyond turning a phase's elapsed_ms into a
// percentage of its own turn: each row is normalised to ITSELF, so a 4-second
// turn and a 40-minute turn are both a full-width bar and the shape of the
// work inside them is comparable at a glance. Comparing their lengths is what
// the duration at the right edge is for.
// ---------------------------------------------------------------------------

function _turnsOldestFirst(res) {
    // The endpoint answers newest first (it pages backward); the lane reads
    // top-to-bottom like the transcript does.
    return ((res && res.turns) || []).slice().reverse();
}

function _selectedTurnRecord() {
    return _turns.find(t => t.turn_id === _selectedTurn) || null;
}

// A debounced refetch of the NEWEST page, armed by every live row app.js
// appends. The live SSE row lands in the Timeline tab immediately; the lane
// is a server-side read model, so what it needs is the page again — once,
// after the burst, not once per event.
const LANE_REFRESH_DEBOUNCE_MS = 700;

function _scheduleLaneRefresh() {
    if (!_overlay || !state.sid) return;
    if (_laneRefreshTimer) clearTimeout(_laneRefreshTimer);
    _laneRefreshTimer = setTimeout(async () => {
        _laneRefreshTimer = null;
        const token = ++_laneLoadToken;
        let res;
        try {
            res = await get(`/api/sessions/${state.sid}/turns?limit=${TURN_PAGE_LIMIT}`);
        } catch (e) {
            console.error('Failed to refresh the turn lane:', e);
            return;
        }
        // The modal may have closed, or a newer refresh started, while this
        // one was in flight.
        if (token !== _laneLoadToken || !_laneListEl) return;
        const head = _turnsOldestFirst(res);
        if (!head.length) return;
        // Older pages the reader already asked for stay; only the window the
        // refetch covers is replaced.
        const older = _turns.filter(t => t.turn_id < head[0].turn_id);
        _turns = older.concat(head);
        if (!older.length) _turnsHasOlder = !!res.has_more;
        if (!_turns.some(t => t.turn_id === _selectedTurn)) {
            _selectedTurn = _turns[_turns.length - 1].turn_id;
        }
        _renderLane();
    }, LANE_REFRESH_DEBOUNCE_MS);
}

async function _loadOlderTurns(btn) {
    if (!_turns.length) return;
    btn.disabled = true;
    btn.textContent = 'Loading…';
    try {
        const res = await get(
            `/api/sessions/${state.sid}/turns?before_turn=${_turns[0].turn_id}&limit=${TURN_PAGE_LIMIT}`);
        _turns = _turnsOldestFirst(res).concat(_turns);
        _turnsHasOlder = !!res.has_more;
        _renderLane();
    } catch (e) {
        console.error('Failed to load older turns:', e);
        btn.disabled = false;
        btn.textContent = 'Load older turns';
    }
}

function _renderLane() {
    if (!_laneListEl) return;
    _laneListEl.innerHTML = '';

    if (!_turns.length) {
        _laneListEl.appendChild(el('div', { class: 'timeline-empty' }, [
            text('No turns yet — this session has not run one.'),
        ]));
        if (_storyEl) _storyEl.innerHTML = '';
        return;
    }

    if (_turnsHasOlder) {
        const btn = el('button', { class: 'tl-lane-older-btn' }, [text('Load older turns')]);
        btn.addEventListener('click', () => _loadOlderTurns(btn));
        _laneListEl.appendChild(el('div', { class: 'tl-lane-older' }, [btn]));
    }

    for (const turn of _turns) _laneListEl.appendChild(_buildLaneRow(turn));
    _syncLaneSelection();
    _renderStory();
}

function _buildLaneRow(turn) {
    const label = el('span', { class: 'tl-lane-label' }, [
        el('span', { class: 'tl-lane-id' }, [text(`T${turn.turn_id}`)]),
    ]);
    if (turn.parent_turn_id != null) {
        label.appendChild(el('span', {
            class: 'tl-lane-parent',
            title: `Continues turn ${turn.parent_turn_id}`,
        }, [text(`↳ T${turn.parent_turn_id}`)]));
    }
    if (turn.retry_index > 0) {
        label.appendChild(el('span', {
            class: 'tl-lane-retry',
            title: `${turn.retry_index} retr${turn.retry_index === 1 ? 'y' : 'ies'}`,
        }, [text(`·${turn.retry_index}`)]));
    }
    const violations = turn.invariant_violations || [];
    if (violations.length) {
        label.appendChild(el('span', {
            class: 'tl-lane-warn',
            title: violations.join(', '),
        }, [icon('warning', { size: 11 })]));
    }

    const elapsed = el('span', { class: 'tl-lane-elapsed' }, [
        text(turn.running ? `${_fmtMs(turn.elapsed_ms) || '0ms'}…` : (_fmtMs(turn.elapsed_ms) || '—')),
    ]);

    const row = el('div', {
        class: `tl-lane-row${turn.running ? ' running' : ''}${violations.length ? ' warned' : ''}`,
        role: 'button',
        tabindex: '0',
        'data-turn': String(turn.turn_id),
        'aria-label': _laneRowLabel(turn),
    }, [label, _buildLaneBar(turn), elapsed]);

    row.addEventListener('click', () => _selectTurn(turn.turn_id));
    row.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
            e.preventDefault();
            _selectTurn(turn.turn_id);
            return;
        }
        if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
        e.preventDefault();
        const rows = [..._laneListEl.querySelectorAll('.tl-lane-row')];
        const at = rows.indexOf(row);
        const next = rows[at + (e.key === 'ArrowUp' ? -1 : 1)];
        if (!next) return;
        _selectTurn(Number(next.dataset.turn), { focus: true });
    });
    return row;
}

function _laneRowLabel(turn) {
    const parts = [`Turn ${turn.turn_id}`];
    if (turn.parent_turn_id != null) parts.push(`continuing turn ${turn.parent_turn_id}`);
    if (turn.retry_index > 0) parts.push(`retry ${turn.retry_index}`);
    parts.push(turn.running ? 'still running' : (_fmtMs(turn.elapsed_ms) || 'no elapsed time'));
    const tools = (turn.tool_calls || []).length;
    if (tools) parts.push(`${tools} tool call${tools === 1 ? '' : 's'}`);
    if (turn.termination_reason) parts.push(turn.termination_reason);
    return parts.join(', ');
}

// One bar per turn: the phases as proportional segments, the tool calls as
// ticks positioned by when they were issued, and — while the turn is live —
// a pulsing marker at the leading edge.
function _buildLaneBar(turn) {
    const phases = turn.phases || [];
    const total = turn.elapsed_ms || phases.reduce((s, p) => s + (p.elapsed_ms || 0), 0);
    const bar = el('div', { class: 'tl-lane-bar' });

    if (!phases.length || !total) {
        bar.appendChild(el('div', { class: 'tl-lane-seg tl-lane-seg-empty', style: 'width:100%' }));
    }
    for (const phase of phases) {
        const pct = total ? ((phase.elapsed_ms || 0) / total) * 100 : 0;
        const thin = pct < LANE_MIN_SEG_PCT;
        const state_ = phase.state || 'unknown';
        // var() with a fallback, so an unrecognised state paints a real
        // surface colour rather than dropping to transparent.
        bar.appendChild(el('div', {
            class: `tl-lane-seg${thin ? ' tl-lane-seg-thin' : ''}`,
            'data-state': state_,
            style: `width:${pct.toFixed(4)}%;`
                + `background:var(--state-${state_}-bg, var(--bg-surface));`
                + `border-bottom-color:var(--state-${state_}-fg, var(--text-dim));`,
            title: _phaseTitle(phase),
        }));
    }

    const startMs = _isoToMs(turn.started_at);
    for (const call of turn.tool_calls || []) {
        const at = _isoToMs(call.started_at);
        if (!at || !startMs || !total) continue;
        const pct = Math.min(100, Math.max(0, ((at - startMs) / total) * 100));
        bar.appendChild(el('div', {
            class: `tl-lane-tick${call.was_error ? ' error' : ''}`,
            style: `left:${pct.toFixed(4)}%`,
            title: `${call.name || 'tool'} · ${_fmtMs(call.latency_ms) || '—'}`
                + (call.was_error ? ' · error' : ''),
        }));
    }

    if (turn.running) bar.appendChild(el('div', { class: 'tl-lane-live', title: 'Still running' }));
    return bar;
}

function _phaseTitle(phase) {
    const flow = phase.reason_out
        ? `${phase.reason_in || '—'} → ${phase.reason_out}`
        : `${phase.reason_in || '—'} → (still in this phase)`;
    return `${phase.state} · ${_fmtMs(phase.elapsed_ms) || '0ms'} · ${flow}`;
}

function _syncLaneSelection() {
    if (!_laneListEl) return;
    for (const row of _laneListEl.querySelectorAll('.tl-lane-row')) {
        const on = row.dataset.turn === String(_selectedTurn);
        row.classList.toggle('selected', on);
        if (on) row.setAttribute('aria-current', 'true');
        else row.removeAttribute('aria-current');
    }
}

function _selectTurn(turnId, { focus = false } = {}) {
    if (!_turns.some(t => t.turn_id === turnId)) return;
    _selectedTurn = turnId;
    _syncLaneSelection();
    _renderStory();
    if (!focus || !_laneListEl) return;
    const row = _laneListEl.querySelector(`.tl-lane-row[data-turn="${turnId}"]`);
    if (row) row.focus();
}

// Entry point for the Timeline tab's per-turn "Story" affordance.
function _showStoryFor(turnId) {
    _switchTab('lane');
    _selectTurn(turnId, { focus: true });
}

// ---------------------------------------------------------------------------
// Story — the selected turn, in the order the agent lived it
// ---------------------------------------------------------------------------

function _renderStory() {
    if (!_storyEl) return;
    _storyEl.innerHTML = '';
    const turn = _selectedTurnRecord();
    if (!turn) return;

    _storyEl.appendChild(el('div', { class: 'tl-story-head' }, [
        el('span', { class: 'tl-story-turn' }, [text(`T${turn.turn_id}`)]),
        el('span', { class: 'tl-story-when' }, [text(_fmtStamp(turn.started_at))]),
        el('span', { class: 'tl-story-when' }, [
            text(turn.running ? 'running' : (_fmtMs(turn.elapsed_ms) || '—')),
        ]),
        ...(turn.termination_reason
            ? [el('span', { class: `tl-turn-term tl-turn-term-${turn.termination_reason.replace(/_/g, '-')}` },
                [text(turn.termination_reason)])]
            : []),
    ]));

    _storyEl.appendChild(_planCard(turn));
    _storyEl.appendChild(_actCard(turn));
    _storyEl.appendChild(_verifyCard(turn));
    const remembers = _remembersCard(turn);
    if (remembers) _storyEl.appendChild(remembers);
}

// A story card: a mono eyebrow that is also the disclosure control.
function _storyCard(eyebrow, children, { open = true, cls = '' } = {}) {
    const head = el('div', { class: 'tl-card-head' }, [
        el('span', { class: 'tl-card-chevron' }, [icon('chevron-down', { size: 10 })]),
        el('span', { class: 'tl-card-eyebrow' }, [text(eyebrow)]),
    ]);
    const body = el('div', { class: 'tl-card-body' }, children.filter(Boolean));
    const card = el('div', { class: `tl-card${cls ? ` ${cls}` : ''}${open ? '' : ' collapsed'}` }, [head, body]);
    _makeDisclosure(
        head,
        () => !card.classList.contains('collapsed'),
        () => card.classList.toggle('collapsed'),
    );
    head.setAttribute('aria-label', `${eyebrow} — this turn`);
    return card;
}

function _storyEmpty(msg) {
    return el('div', { class: 'tl-story-empty' }, [text(msg)]);
}

function _storyField(label, value) {
    if (value == null || value === '') return null;
    return el('div', { class: 'tl-story-field' }, [
        el('span', { class: 'tl-story-key' }, [text(label)]),
        el('span', { class: 'tl-story-val' }, [text(String(value))]),
    ]);
}

// A small nested disclosure for the bulky parts — recalled memory, a gate's
// output tail — that are worth keeping but not worth opening on.
function _storyFold(label, body) {
    const head = el('div', { class: 'tl-fold-head' }, [
        el('span', { class: 'tl-card-chevron' }, [icon('chevron-down', { size: 9 })]),
        el('span', {}, [text(label)]),
    ]);
    const wrap = el('div', { class: 'tl-fold collapsed' }, [head, el('div', { class: 'tl-fold-body' }, [body])]);
    _makeDisclosure(head, () => !wrap.classList.contains('collapsed'), () => wrap.classList.toggle('collapsed'));
    head.setAttribute('aria-label', label);
    return wrap;
}

function _plainText(value) {
    if (value == null) return '';
    if (typeof value === 'string') return value;
    try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

// --- Plan --------------------------------------------------------------

function _planCard(turn) {
    const scout = turn.scout;
    if (!scout) return _storyCard('Plan', [_storyEmpty('No scout report for this turn.')]);
    if (typeof scout.raw === 'string' && scout.approach === undefined) {
        return _storyCard('Plan', [
            _storyEmpty('The scout report did not parse. Its raw head:'),
            el('pre', { class: 'tl-story-raw' }, [text(scout.raw)]),
        ]);
    }

    const parts = [];
    if (scout.approach) parts.push(el('p', { class: 'tl-story-prose' }, [text(String(scout.approach))]));

    const tools = Array.isArray(scout.tools) ? scout.tools.filter(Boolean) : [];
    if (tools.length) {
        parts.push(el('div', { class: 'tl-chip-row' },
            tools.map(t => el('span', { class: 'tl-chip' }, [text(String(t))]))));
    }
    if (scout.tool_rationale) {
        parts.push(el('p', { class: 'tl-story-note' }, [text(String(scout.tool_rationale))]));
    }
    const memory = _plainText(scout.memory).trim();
    if (memory) parts.push(_storyFold('Memory recalled', el('pre', { class: 'tl-story-raw' }, [text(memory)])));

    const meta = [];
    const model = scout.scout_model || scout.model;
    if (model) meta.push(el('span', {}, [text(`scout: ${model}`)]));
    if (scout.latency_ms) meta.push(el('span', {}, [text(_fmtMs(scout.latency_ms))]));
    for (const [key, label] of [['from_cache', 'cached'], ['from_fallback', 'fallback'], ['reused_prior', 'reused']]) {
        if (scout[key]) meta.push(el('span', { class: 'tl-badge tl-badge-plan' }, [text(label)]));
    }
    if (meta.length) parts.push(el('div', { class: 'tl-story-meta' }, meta));

    if (!parts.length) parts.push(_storyEmpty('The scout report is empty.'));
    return _storyCard('Plan', parts);
}

// --- Act ---------------------------------------------------------------

function _actCard(turn) {
    const parts = [];
    // Errors first: the reason anyone opens this card is to find the call
    // that went wrong, and it is rarely the first one made.
    const calls = (turn.tool_calls || []).slice()
        .sort((a, b) => (b.was_error ? 1 : 0) - (a.was_error ? 1 : 0));
    if (!calls.length) {
        parts.push(_storyEmpty('No tool calls in this turn.'));
    } else {
        parts.push(el('div', { class: 'tl-act-list' }, calls.map(_buildActRow)));
    }

    const tokens = turn.tokens || {};
    const meta = [];
    if (tokens.total) {
        meta.push(el('span', { class: 'tl-act-tokens' }, [
            text(`${_fmtNum(tokens.prompt)} / ${_fmtNum(tokens.completion)} / ${_fmtNum(tokens.total)} tok`),
        ]));
    }
    if (tokens.calls) meta.push(el('span', {}, [text(`${tokens.calls} call${tokens.calls === 1 ? '' : 's'}`)]));
    const model = turn.model || (Array.isArray(tokens.models) && tokens.models.length ? tokens.models[0] : null);
    if (model) meta.push(el('span', {}, [text(model)]));
    // Null cost is "nobody priced it", which is not "it was free" — an
    // unpriced local model must never read as $0.00.
    if (typeof tokens.cost_estimate === 'number') {
        meta.push(el('span', {}, [text(`$${tokens.cost_estimate.toFixed(4)}`)]));
    }
    if (meta.length) parts.push(el('div', { class: 'tl-story-meta' }, meta));
    return _storyCard('Act', parts);
}

function _buildActRow(call) {
    const head = [
        el('span', { class: 'tl-act-name' }, [text(call.name || 'tool')]),
        el('span', { class: 'tl-act-args' }, [text(call.args_summary || '')]),
    ];
    if (call.latency_ms != null) {
        const cls = call.latency_ms < 500 ? 'fast' : call.latency_ms < 2000 ? 'medium' : 'slow';
        head.push(el('span', { class: `tool-latency ${cls}` }, [text(`${call.latency_ms}ms`)]));
    }
    if (call.was_error) head.push(el('span', { class: 'tl-chip tl-chip-fail' }, [text('error')]));
    return el('div', { class: `tl-act-row${call.was_error ? ' error' : ''}` }, head);
}

// --- Verify ------------------------------------------------------------

const _VERDICT_CLASS = { pass: 'tl-chip-pass', retry: 'tl-chip-retry', escalate: 'tl-chip-escalate' };

function _verifyCard(turn) {
    const reflects = turn.reflect || [];
    const evals = turn.eval || [];
    if (!reflects.length && !evals.length) {
        return _storyCard('Verify', [_storyEmpty('No verification ran.')]);
    }

    const parts = [];
    for (const entry of reflects) {
        const head = [
            el('span', { class: 'tl-verify-attempt' }, [text(`reflect ${entry.attempt}`)]),
        ];
        if (entry.verdict) {
            head.push(el('span', {
                class: `tl-chip ${_VERDICT_CLASS[entry.verdict] || 'tl-chip-retry'}`,
            }, [text(entry.verdict)]));
        }
        if (typeof entry.raw === 'string') head.push(el('span', { class: 'tl-chip' }, [text('unparsed')]));
        const block = [el('div', { class: 'tl-verify-head' }, head)];
        if (entry.reasoning) block.push(el('p', { class: 'tl-story-prose' }, [text(String(entry.reasoning))]));
        if (entry.diagnostic) block.push(_storyField('diagnostic', entry.diagnostic));
        if (entry.what_worked) block.push(_storyField('what worked', entry.what_worked));
        if (typeof entry.raw === 'string') block.push(el('pre', { class: 'tl-story-raw' }, [text(entry.raw)]));
        parts.push(el('div', { class: 'tl-verify-block' }, block.filter(Boolean)));
    }

    for (const attempt of evals) {
        for (const gate of attempt.gates || []) {
            const head = [
                el('span', { class: 'tl-verify-attempt' }, [text(`gate ${gate.name || '—'}`)]),
                el('span', {
                    class: `tl-chip ${gate.passed ? 'tl-chip-pass' : 'tl-chip-fail'}`,
                }, [text(gate.passed ? 'passed' : 'failed')]),
            ];
            if (gate.exit_code != null) head.push(el('span', { class: 'tl-verify-exit' }, [text(`exit ${gate.exit_code}`)]));
            const block = [el('div', { class: 'tl-verify-head' }, head)];
            if (gate.command) block.push(el('code', { class: 'tl-verify-cmd' }, [text(String(gate.command))]));
            if (gate.output_tail) {
                block.push(_storyFold('Output tail', el('pre', { class: 'tl-story-raw' }, [text(String(gate.output_tail))])));
            }
            parts.push(el('div', { class: 'tl-verify-block' }, block));
        }
        if (typeof attempt.raw === 'string') {
            parts.push(el('div', { class: 'tl-verify-block' }, [
                el('div', { class: 'tl-verify-head' }, [
                    el('span', { class: 'tl-verify-attempt' }, [text(`eval ${attempt.attempt}`)]),
                    el('span', { class: 'tl-chip' }, [text('unparsed')]),
                ]),
                el('pre', { class: 'tl-story-raw' }, [text(attempt.raw)]),
            ]));
        }
    }
    return _storyCard('Verify', parts);
}

// --- Remembers ---------------------------------------------------------

function _remembersCard(turn) {
    const compactions = turn.compactions || [];
    const notices = turn.notices || [];
    // Nothing to remember is not a state worth a card saying so.
    if (!compactions.length && !notices.length) return null;

    const parts = [];
    for (const c of compactions) {
        const block = [el('div', { class: 'tl-verify-head' }, [
            el('span', { class: 'tl-verify-attempt' }, [text('compaction')]),
            el('span', { class: 'tl-story-when' }, [text(_fmtStamp(c.at))]),
        ])];
        if (c.summary && typeof c.summary === 'object' && !Array.isArray(c.summary)) {
            for (const [k, v] of Object.entries(c.summary)) {
                block.push(_storyField(k, Array.isArray(v) ? v.map(String).join(' · ') : _plainText(v)));
            }
        } else if (c.summary) {
            block.push(el('p', { class: 'tl-story-prose' }, [text(_plainText(c.summary))]));
        }
        const counts = [];
        if (c.compacted_up_to != null) counts.push(`up to message ${c.compacted_up_to}`);
        if (c.original_count != null) counts.push(`${c.original_count} messages before`);
        if (counts.length) block.push(el('div', { class: 'tl-story-meta' }, [el('span', {}, [text(counts.join(' · '))])]));
        parts.push(el('div', { class: 'tl-verify-block' }, block.filter(Boolean)));
    }
    for (const n of notices) {
        parts.push(el('div', { class: 'tl-verify-block' }, [
            el('div', { class: 'tl-verify-head' }, [
                el('span', { class: 'tl-verify-attempt' }, [text('notice')]),
                el('span', { class: 'tl-story-when' }, [text(_fmtStamp(n.at))]),
            ]),
            el('p', { class: 'tl-story-prose' }, [text(n.text || '')]),
        ]));
    }
    return _storyCard('Remembers', parts);
}

function _fmtNum(n) {
    return Number(n || 0).toLocaleString();
}

function _fmtStamp(iso) {
    const ms = _isoToMs(iso);
    return ms ? _fmtClock(ms) : '';
}

// ---------------------------------------------------------------------------
// Unified entry list — single merge used by the timeline, the tool tally,
// and the JSON export.
// ---------------------------------------------------------------------------

// Build a map of tool_call_id → {name, args} from assistant tool_calls.
// Stored format (core/agent.py:482): [{id, name, arguments}] where
// arguments is a JSON-encoded string.
function _toolCallMetaMap(messages) {
    const meta = new Map();
    for (const m of messages) {
        if (m.role !== 'assistant' || !m.tool_calls) continue;
        let parsed;
        try { parsed = JSON.parse(m.tool_calls); } catch { continue; }
        for (const tc of parsed || []) {
            let args = null;
            if (tc.arguments) {
                try { args = JSON.parse(tc.arguments); } catch { args = null; }
            }
            meta.set(tc.id, { name: tc.name || 'tool', args });
        }
    }
    return meta;
}

function _allEntries() {
    const entries = [];
    for (const s of _data.stateLog) {
        entries.push({ kind: 'state', ts: s.timestamp_ms || 0, turn: s.turn_id, data: s });
    }
    const tcMeta = _toolCallMetaMap(_data.messages);
    for (const m of _data.messages) {
        if (m.role !== 'tool') continue;
        const meta = tcMeta.get(m.tool_call_id) || { name: 'tool', args: null };
        entries.push({
            kind: 'tool',
            ts: _isoToMs(m.created_at),
            turn: null,
            data: {
                name: meta.name,
                args: meta.args,
                content: m.content || '',
                latency_ms: m.latency_ms || null,
                was_error: _isErrorContent(m.content),
            },
        });
    }
    for (const t of _data.liveTools) {
        entries.push({ kind: 'tool', ts: t.ts, turn: null, data: t });
    }
    entries.sort((a, b) => (a.ts || 0) - (b.ts || 0));

    // Assign turn hints to tool rows based on the state row they follow.
    let currentTurn = null;
    for (const e of entries) {
        if (e.kind === 'state' && e.data.turn_id != null) currentTurn = e.data.turn_id;
        else if (e.kind === 'tool') e.turn = currentTurn;
    }
    return entries;
}

// ---------------------------------------------------------------------------
// Filtering
// ---------------------------------------------------------------------------

function _isErrorEntry(e) {
    if (e.kind === 'tool') return !!e.data.was_error;
    const reason = e.data.reason || '';
    const term = e.data.termination_reason || '';
    return reason.startsWith('invariant-violation') || term.includes('error');
}

function _entrySearchText(e) {
    if (e.kind === 'state') {
        const d = e.data;
        return `${d.from_state || ''} ${d.to_state || ''} ${d.reason || ''} ${d.termination_reason || ''}`.toLowerCase();
    }
    const d = e.data;
    let argsStr = '';
    if (d.args && typeof d.args === 'object') {
        try { argsStr = JSON.stringify(d.args).slice(0, 500); } catch { argsStr = ''; }
    }
    return `${d.name || ''} ${argsStr}`.toLowerCase();
}

function _entryMatches(e) {
    if (_filter.mode === 'state' && e.kind !== 'state') return false;
    if (_filter.mode === 'tool' && e.kind !== 'tool') return false;
    if (_filter.mode === 'error' && !_isErrorEntry(e)) return false;
    const q = _filter.q.trim().toLowerCase();
    if (q && !_entrySearchText(e).includes(q)) return false;
    return true;
}

function _filterActive() {
    return _filter.mode !== 'all' || _filter.q.trim() !== '';
}

function _buildFilterBar() {
    const modes = [['all', 'All'], ['state', 'States'], ['tool', 'Tools'], ['error', 'Errors']];
    _filterBtns = modes.map(([mode, label]) => {
        const btn = el('button', { class: `tl-filter-btn${mode === _filter.mode ? ' active' : ''}`, 'data-mode': mode }, [text(label)]);
        btn.addEventListener('click', () => {
            _filter.mode = mode;
            _syncFilterBar();
            _renderTimeline();
        });
        return btn;
    });

    _filterInput = el('input', { class: 'tl-filter-input', type: 'text', placeholder: 'filter…' });
    let debounce = null;
    _filterInput.addEventListener('input', () => {
        clearTimeout(debounce);
        debounce = setTimeout(() => {
            _filter.q = _filterInput.value;
            _renderTimeline();
        }, 150);
    });

    const nextErrBtn = el('button', { class: 'tl-next-error', title: 'Jump to next error' }, [
        icon('warning', { size: 11 }), text('next'),
    ]);
    nextErrBtn.addEventListener('click', _jumpToNextError);

    return el('div', { class: 'tl-filter-bar' }, [..._filterBtns, _filterInput, nextErrBtn]);
}

function _syncFilterBar() {
    _filterBtns.forEach(b => b.classList.toggle('active', b.getAttribute('data-mode') === _filter.mode));
    if (_filterInput) _filterInput.value = _filter.q;
}

// Filter the Timeline tab to a single state — entry point for graph node clicks.
function _filterTimelineByState(name) {
    _filter.mode = 'state';
    _filter.q = name;
    _syncFilterBar();
    _switchTab('timeline');
    _renderTimeline();
}

function _jumpToNextError() {
    if (!_bodyEl || !_scroller) return;
    const errs = [..._bodyEl.querySelectorAll('.timeline-row.tl-error, .timeline-tool-row.error')];
    if (!errs.length) return;
    const scrollerTop = _scroller.getBoundingClientRect().top;
    // First error strictly below the current viewport top, else wrap to the first.
    let target = errs.find(e => {
        const group = e.closest('.tl-turn-group');
        const top = (group && group.classList.contains('collapsed') ? group : e).getBoundingClientRect().top;
        return top - scrollerTop > 40;
    }) || errs[0];
    const group = target.closest('.tl-turn-group');
    if (group) group.classList.remove('collapsed');
    const top = target.getBoundingClientRect().top - scrollerTop + _scroller.scrollTop;
    _scroller.scrollTo({ top: Math.max(0, top - 60), behavior: 'smooth' });
    target.classList.add('tl-flash');
    setTimeout(() => target.classList.remove('tl-flash'), 1200);
}

// ---------------------------------------------------------------------------
// Graph tab
// ---------------------------------------------------------------------------

async function _renderGraph(pane) {
    // This function awaits twice (library load, mermaid.render), so a second
    // call can interleave with one already in flight. Both would then run the
    // stale-section sweep below and both would insert their own dwell/tally
    // block, leaving duplicates on screen. Newest call wins; older ones bail
    // at their next resumption point.
    const token = ++_graphRenderToken;

    const statusEl = pane.querySelector('.timeline-graph-status');
    const container = pane.querySelector('.timeline-graph-container');
    const caption = pane.querySelector('.timeline-graph-caption');
    container.innerHTML = '';
    caption.innerHTML = '';

    // Remove any stale sections from a previous render.
    pane.querySelectorAll('.tl-tool-tally, .tl-dwell').forEach(n => n.remove());

    if (!_data.stateLog.length) {
        statusEl.textContent = 'No state transitions yet.';
        return;
    }
    statusEl.textContent = '';

    let mermaid;
    try {
        mermaid = await _ensureMermaid();
    } catch (e) {
        statusEl.textContent = 'Failed to load diagram library: ' + e.message;
        return;
    }
    if (token !== _graphRenderToken) return;

    const { source, current, termination, invariantViolations, nodeNames } = _buildMermaidSource(_data.stateLog);

    try {
        // Unique id per render so Mermaid doesn't collide on re-entry. A
        // counter, not Date.now() — two renders in the same millisecond are
        // reachable from the debounce above plus a tab switch, and mermaid
        // keys its internal style block on this id.
        const id = `timeline-diagram-${++_graphRenderSeq}`;
        const { svg } = await mermaid.render(id, source);
        if (token !== _graphRenderToken) return;
        // Node and edge labels come from server-supplied state-log rows
        // (to_state, reason, termination_reason), so this markup is not
        // developer-controlled. Inline SVG is a scripting context — sanitize
        // rather than assigning innerHTML directly.
        setSanitizedSvg(container, svg);
    } catch (e) {
        statusEl.textContent = 'Diagram render error: ' + e.message;
        console.error('Mermaid render failed:', e, '\nSource:\n', source);
        return;
    }

    // Cross-tab linking: activating a state node filters the Timeline tab.
    // These are SVG <g> elements, so nothing about them was focusable or
    // announced — the link between the two tabs was mouse-only. (A1)
    container.querySelectorAll('g.node').forEach(node => {
        const label = (node.textContent || '').trim();
        if (!nodeNames.has(label)) return;
        node.style.cursor = 'pointer';
        node.setAttribute('role', 'button');
        node.setAttribute('tabindex', '0');
        node.setAttribute('aria-label', `Filter the timeline to the ${label} state`);
        const activate = () => _filterTimelineByState(label);
        node.addEventListener('click', activate);
        node.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
            e.preventDefault();
            activate();
        });
        const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
        title.textContent = 'Filter timeline to this state';
        node.appendChild(title);
    });

    const entries = _allEntries();

    // Dwell-time breakdown + tool tally — inserted between diagram and caption.
    const dwellEl = _buildDwellEl(_data.stateLog);
    if (dwellEl) pane.insertBefore(dwellEl, caption);
    const tallyEl = _buildToolTallyEl(entries);
    if (tallyEl) pane.insertBefore(tallyEl, caption);

    const captionParts = [];
    if (current) {
        captionParts.push(el('span', { class: 'tl-caption-current' }, [text(`Now: ${current}`)]));
    }
    if (termination) {
        captionParts.push(el('span', { class: 'tl-caption-term' }, [text(`Last termination: ${termination}`)]));
    }
    if (invariantViolations.length) {
        captionParts.push(el('span', { class: 'tl-caption-warn' }, [
            text(`${invariantViolations.length} invariant violation${invariantViolations.length === 1 ? '' : 's'}`),
        ]));
    }

    // Summary stat chips: turns, tool calls, compactions, retries, total elapsed.
    const stateLog = _data.stateLog;
    const distinctTurns = new Set(stateLog.map(r => r.turn_id).filter(t => t != null)).size;
    const totalToolCalls = entries.filter(e => e.kind === 'tool').length;
    const compactions = stateLog.filter(r =>
        r.reason === 'compact-proactive' || r.reason === 'compact-critical' || r.reason === 'compact-overflow'
    ).length;
    const retries = stateLog.filter(r => r.reason === 'reflect-retry' || r.reason === 'eval-retry').length;
    const totalElapsed = stateLog.reduce((s, r) => s + (r.elapsed_ms || 0), 0);

    if (distinctTurns > 0) captionParts.push(el('span', { class: 'tl-caption-stat' }, [text(`${distinctTurns} turn${distinctTurns === 1 ? '' : 's'}`)]));
    if (totalToolCalls > 0) captionParts.push(el('span', { class: 'tl-caption-stat' }, [text(`${totalToolCalls} tool call${totalToolCalls === 1 ? '' : 's'}`)]));
    if (compactions > 0) captionParts.push(el('span', { class: 'tl-caption-stat' }, [text(`${compactions} compaction${compactions === 1 ? '' : 's'}`)]));
    if (retries > 0) captionParts.push(el('span', { class: 'tl-caption-stat' }, [text(`${retries} retr${retries === 1 ? 'y' : 'ies'}`)]));
    if (totalElapsed > 0) captionParts.push(el('span', { class: 'tl-caption-stat' }, [text(_fmtMs(totalElapsed))]));

    captionParts.forEach(p => caption.appendChild(p));
}

function _buildMermaidSource(rows) {
    // Aggregate edges: key = `${from}||${to}`, value = {count, reasons[], invariant}
    const edges = new Map();
    const nodes = new Set();
    let current = null;
    let termination = null;
    const invariantViolations = [];

    for (const r of rows) {
        const from = r.from_state || '__start__';
        const to = r.to_state;
        if (!to) continue;
        nodes.add(to);
        if (from !== '__start__') nodes.add(from);
        const key = `${from}||${to}`;
        const edge = edges.get(key) || { count: 0, reason: '', invariant: false };
        edge.count += 1;
        if (r.reason) edge.reason = r.reason;   // keep last reason
        if ((r.reason || '').startsWith('invariant-violation')) {
            edge.invariant = true;
            invariantViolations.push(r);
        }
        edges.set(key, edge);
        current = to;
        if (r.termination_reason) termination = r.termination_reason;
    }

    const lines = ['stateDiagram-v2', '    direction LR'];
    for (const [key, edge] of edges) {
        const [from, to] = key.split('||');
        const fromNode = from === '__start__' ? '[*]' : from;
        const label = _edgeLabel(edge);
        lines.push(`    ${fromNode} --> ${to}${label ? `: ${label}` : ''}`);
    }

    // Phase color hints — applied before current/violation so they can always override.
    const phaseGroups = {
        work:  ['processing', 'compacting'],
        // scouting is a real, frequently-visited state (it has its own dwell
        // colour below) and was the one phase with no hint here, so it drew
        // in the default grey next to colour-coded neighbours.
        scout: ['scouting'],
        wait:  ['awaiting_user', 'awaiting_workers', 'paused', 'pause_requested'],
        end:   ['finalizing', 'cancelling'],
    };
    // One state = one colour everywhere. The Mermaid classDefs, the dwell bars
    // and the .state-badge in the status bar all read the same --state-*
    // tokens (tokens.css) instead of each carrying its own hand-picked hex.
    const phaseStyles = {
        work:  _mermaidStyle('processing'),
        scout: _mermaidStyle('scouting'),
        wait:  _mermaidStyle('paused'),
        end:   _mermaidStyle('cancelling'),
    };
    for (const [phaseName, stateList] of Object.entries(phaseGroups)) {
        const present = stateList.filter(s => nodes.has(s));
        if (!present.length) continue;
        lines.push(`    classDef phase_${phaseName} ${phaseStyles[phaseName]}`);
        lines.push(`    class ${present.join(',')} phase_${phaseName}`);
    }

    // Highlight current state (applied after phase hints — wins on any overlap).
    if (current) {
        lines.push(`    classDef current ${_mermaidStyle('processing')},stroke-width:2px`);
        lines.push(`    class ${current} current`);
    }

    // Flag invariant-violation targets.
    const violationTargets = new Set();
    for (const [key, edge] of edges) {
        if (edge.invariant) violationTargets.add(key.split('||')[1]);
    }
    if (violationTargets.size) {
        lines.push(`    classDef violation ${_mermaidStyle('cancelling')}`);
        lines.push(`    class ${[...violationTargets].join(',')} violation`);
    }

    return {
        source: lines.join('\n'),
        current,
        termination,
        invariantViolations,
        nodeNames: nodes,
    };
}

function _edgeLabel(edge) {
    if (edge.count > 1) {
        return `${edge.count}×`;
    }
    const r = edge.reason || '';
    const short = r.length > 24 ? r.slice(0, 22) + '…' : r;
    // Mermaid edge labels choke on : ; " \n — replace with safe chars.
    return short.replace(/[:;"\n]/g, ' ').trim();
}

// ---------------------------------------------------------------------------
// State colours — single source of truth is the --state-<name>-{fg,bg} pairs
// in tokens.css. Mermaid classDefs are comma-separated, so a raw
// `color-mix(in srgb, …)` token stream (what getPropertyValue hands back for
// an unregistered custom property) would break the parser. Resolve it to a
// plain #rrggbb through a probe element, whose `color` IS a real colour
// property and therefore computes color-mix() for us.
// ---------------------------------------------------------------------------
const _STATE_COLOR_CACHE = new Map();

function _stateColor(state, part) {
    const key = `${state}|${part}`;
    if (_STATE_COLOR_CACHE.has(key)) return _STATE_COLOR_CACHE.get(key);
    // The fallback is a token too — an unknown state must not paint a dark
    // node onto a light diagram. Check the property exists first: `color:
    // var(--nope)` is invalid at computed-value time, so the probe would
    // quietly hand back the inherited text colour instead of nothing.
    const token = `--state-${state}-${part}`;
    const defined = getComputedStyle(document.documentElement)
        .getPropertyValue(token).trim();
    const out = hex(defined ? token : (part === 'bg' ? '--bg-surface' : '--text-dim'));
    _STATE_COLOR_CACHE.set(key, out);
    return out;
}

// The cache holds resolved hex, so it has to die when the palette changes.
window.addEventListener('pernix:theme', () => _STATE_COLOR_CACHE.clear());

// `fill:…,stroke:…,color:…` for one Mermaid classDef.
function _mermaidStyle(state) {
    return `fill:${_stateColor(state, 'bg')},stroke:${_stateColor(state, 'fg')},color:${_stateColor(state, 'fg')}`;
}

// Per-state dwell-time breakdown — elapsed_ms on a transition row is the time
// spent in from_state, so summing by from_state answers "where did the time go".

function _buildDwellEl(stateLog) {
    const byState = new Map();
    for (const r of stateLog) {
        if (!r.from_state || r.elapsed_ms == null) continue;
        byState.set(r.from_state, (byState.get(r.from_state) || 0) + r.elapsed_ms);
    }
    const total = [...byState.values()].reduce((a, b) => a + b, 0);
    if (!total) return null;

    const sorted = [...byState.entries()].sort((a, b) => b[1] - a[1]);

    const segments = sorted.map(([name, ms]) => {
        const pct = (ms / total) * 100;
        return el('div', {
            class: 'tl-dwell-seg',
            style: `width:${Math.max(pct, 1.5)}%;background:${_stateColor(name, 'fg')}`,
            title: `${name}: ${_fmtMs(ms)} (${pct.toFixed(0)}%)`,
        });
    });

    const chips = sorted.map(([name, ms]) => {
        const pct = ((ms / total) * 100).toFixed(0);
        return el('span', { class: 'tl-dwell-chip' }, [
            el('span', { class: 'tl-dwell-dot', style: `background:${_stateColor(name, 'fg')}` }),
            text(`${name} ${_fmtMs(ms)} (${pct}%)`),
        ]);
    });

    return el('div', { class: 'tl-dwell' }, [
        el('div', { class: 'tl-dwell-header' }, [text(`Time in state (${_fmtMs(total)})`)]),
        el('div', { class: 'tl-dwell-bar' }, segments),
        el('div', { class: 'tl-dwell-chips' }, chips),
    ]);
}

// Build a DOM element showing per-turn tool call counts, or null if no tools.
function _buildToolTallyEl(entries) {
    // Aggregate: Map<turn, Map<name, count>>, plus per-name error tracking.
    const byTurn = new Map();
    const nameErrors = new Map(); // tool name -> error count
    let totalErrors = 0;
    let totalSlow = 0;
    for (const e of entries) {
        if (e.kind !== 'tool') continue;
        const t = e.turn ?? 0;
        if (!byTurn.has(t)) byTurn.set(t, new Map());
        const m = byTurn.get(t);
        m.set(e.data.name, (m.get(e.data.name) || 0) + 1);
        if (e.data.was_error) {
            nameErrors.set(e.data.name, (nameErrors.get(e.data.name) || 0) + 1);
            totalErrors++;
        }
        if (e.data.latency_ms >= 2000) totalSlow++;
    }
    if (!byTurn.size) return null;

    const multiTurn = byTurn.size > 1;
    const totalCalls = [...byTurn.values()].reduce((s, m) => s + [...m.values()].reduce((a, b) => a + b, 0), 0);

    let headerText = `Tool calls (${totalCalls}`;
    if (totalErrors) headerText += ` · ${totalErrors} error${totalErrors === 1 ? '' : 's'}`;
    if (totalSlow) headerText += ` · ${totalSlow} slow`;
    headerText += ')';

    const rows = [...byTurn.entries()]
        .sort((a, b) => (a[0] ?? 0) - (b[0] ?? 0))
        .map(([turn, counts]) => {
            const chips = [...counts.entries()]
                .sort((a, b) => b[1] - a[1])
                .map(([name, count]) => {
                    const hasError = nameErrors.has(name);
                    return el('span', { class: `tl-tally-chip${hasError ? ' error' : ''}` }, [text(`${name} (${count})`)]);
                });
            const parts = [];
            if (multiTurn) {
                parts.push(el('span', { class: 'tl-tally-turn-label' }, [text(`T${turn}:`)]));
            }
            return el('div', { class: 'tl-tally-row' }, [...parts, ...chips]);
        });

    return el('div', { class: 'tl-tool-tally' }, [
        el('div', { class: 'tl-tally-header' }, [text(headerText)]),
        ...rows,
    ]);
}

async function _ensureMermaid() {
    if (_mermaid) return _mermaid;
    if (!_mermaidPromise) {
        _mermaidPromise = new Promise((resolve, reject) => {
            if (window.mermaid) { resolve(window.mermaid); return; }
            // index.html loads Monaco's AMD loader, which installs a global
            // define() with define.amd set. Mermaid's UMD wrapper checks for
            // that first and registers itself as an anonymous AMD module
            // instead of assigning window.mermaid — which nothing ever
            // require()s, so the global stays undefined and the graph tab dies
            // with "Failed to load diagram library". Hide define() for the
            // duration of the load so the UMD wrapper takes its browser-global
            // branch, then put it back for Monaco.
            const prevDefine = window.define;
            const restore = () => { if (prevDefine !== undefined) window.define = prevDefine; };
            if (prevDefine && prevDefine.amd) window.define = undefined;

            const tag = document.createElement('script');
            tag.src = MERMAID_SRC;
            tag.onload = () => { restore(); resolve(window.mermaid); };
            tag.onerror = () => {
                restore();
                _mermaidPromise = null;  // let a later open retry
                reject(new Error('Failed to load diagram library'));
            };
            document.head.appendChild(tag);
        }).then(m => {
            if (!m) throw new Error('Diagram library loaded but exported nothing');
            _mermaid = m;
            _mermaid.initialize({
                startOnLoad: false,
                theme: 'dark',
                securityLevel: 'strict',
                // Native <text> labels, not HTML-in-<foreignObject>.
                //
                // Mermaid's default label mode wraps every node and edge label
                // in a <foreignObject>, and DOMPurify ships `foreignobject` in
                // its svgDisallowed list — so setSanitizedSvg() stripped all of
                // them and the graph rendered as correctly-coloured, correctly
                // -connected, completely UNLABELLED boxes. It also broke the
                // click-a-node-to-filter wiring below, which matches on
                // node.textContent (empty once the labels are gone).
                //
                // Widening the sanitizer does NOT fix this: allowing
                // foreignObject through keeps the element but DOMPurify's
                // namespace check still strips its HTML children, so the
                // labels stay empty. Turning htmlLabels off is what actually
                // works — mermaid then emits plain SVG <text>, which survives
                // sanitization untouched and needs no loosening of the CSP or
                // the purifier. Both keys are needed: the state renderer reads
                // the flowchart config for label construction.
                flowchart: { htmlLabels: false },
                state: { htmlLabels: false },
            });
            return _mermaid;
        });
    }
    return _mermaidPromise;
}

// ---------------------------------------------------------------------------
// Timeline tab — state rows + tool call rows merged by timestamp, grouped
// into collapsible turns
// ---------------------------------------------------------------------------

function _renderTimeline(opts = {}) {
    if (!_bodyEl) return;
    _bodyEl.innerHTML = '';
    _lastRowTs = 0;

    if (_data.hasOlder) {
        const btn = el('button', {}, [text('Load older')]);
        btn.addEventListener('click', () => _loadOlder(btn));
        _bodyEl.appendChild(el('div', { class: 'tl-load-older' }, [
            el('span', {}, [text(`Showing last ${_data.stateLog.length} transitions`)]),
            btn,
        ]));
    }

    const all = _allEntries();
    const entries = all.filter(_entryMatches);
    if (!entries.length) {
        _bodyEl.appendChild(el('div', { class: 'timeline-empty' }, [
            text(all.length ? 'Nothing matches the current filter.' : 'No transitions or tool calls yet.'),
        ]));
        return;
    }

    const turnMeta = _computeTurnMeta(all); // meta over ALL entries so counts stay truthful under filters
    const groups = [];
    let lastTurn;
    let group = null;
    let groupBody = null;
    for (const entry of entries) {
        const turn = entry.turn;
        if (group === null || turn !== lastTurn) {
            group = _buildTurnGroup(turn, turnMeta.get(turn) || {});
            groupBody = group.querySelector('.tl-turn-body');
            groups.push(group);
            _bodyEl.appendChild(group);
            lastTurn = turn;
        }
        if (_lastRowTs && entry.ts && entry.ts - _lastRowTs > GAP_MS) {
            groupBody.appendChild(_buildGapRow(entry.ts - _lastRowTs));
        }
        if (entry.ts) _lastRowTs = entry.ts;
        groupBody.appendChild(entry.kind === 'state'
            ? _buildStateRow(entry.data, entry.ts)
            : _buildToolRow(entry.data, entry.ts));
    }

    // Collapse everything but the latest turn — unless a filter is active,
    // in which case all matches stay visible.
    if (!_filterActive()) {
        groups.slice(0, -1).forEach(g => g.classList.add('collapsed'));
    }

    if (opts.scroll !== 'none' && _scroller && _timelineActive()) {
        _scroller.scrollTop = _scroller.scrollHeight;
    }
}

function _buildGapRow(deltaMs) {
    return el('div', { class: 'tl-gap' }, [text(`· · · ${_fmtMs(deltaMs)} gap · · ·`)]);
}

function _isoToMs(iso) {
    if (!iso) return 0;
    const t = Date.parse(iso);
    return isNaN(t) ? 0 : t;
}

function _fmtMs(ms) {
    if (!ms) return '';
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
    const m = Math.floor(ms / 60_000);
    const s = Math.round((ms % 60_000) / 1000);
    if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return rm ? `${h}h ${rm}m` : `${h}h`;
}

function _fmtClock(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    const p = n => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function _isErrorContent(content) {
    if (!content) return false;
    const head = content.slice(0, 120).toLowerCase();
    return head.startsWith('error:') || head.includes('traceback');
}

/**
 * Make a header <div> that toggles a detail block behave like the control it
 * already looks like: focusable, announced as a button, and driven by
 * Enter/Space as well as a click. (A1)
 */
function _makeDisclosure(headerEl, isExpanded, toggle) {
    headerEl.setAttribute('role', 'button');
    headerEl.setAttribute('tabindex', '0');
    const sync = () => headerEl.setAttribute('aria-expanded', String(!!isExpanded()));
    const activate = () => { toggle(); sync(); };
    headerEl.addEventListener('click', activate);
    headerEl.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
        e.preventDefault();
        activate();
    });
    sync();
    return headerEl;
}

function _computeTurnMeta(entries) {
    const meta = new Map();
    for (const entry of entries) {
        const turn = entry.turn;
        if (turn == null) continue;
        if (!meta.has(turn)) {
            meta.set(turn, {
                elapsedMs: 0, toolCount: 0, termination: null, parentTurnId: null,
                compactions: 0, reflectRetries: 0, evalRetries: 0, errors: 0,
            });
        }
        const m = meta.get(turn);
        if (_isErrorEntry(entry)) m.errors++;
        if (entry.kind === 'state') {
            const d = entry.data;
            if (d.elapsed_ms != null) m.elapsedMs += d.elapsed_ms;
            if (d.termination_reason) m.termination = d.termination_reason;
            if (d.parent_turn_id != null && m.parentTurnId == null) m.parentTurnId = d.parent_turn_id;
            const reason = d.reason || '';
            if (reason === 'compact-proactive' || reason === 'compact-critical' || reason === 'compact-overflow') m.compactions++;
            if (reason === 'reflect-retry') m.reflectRetries++;
            if (reason === 'eval-retry') m.evalRetries++;
        } else {
            m.toolCount++;
        }
    }
    return meta;
}

function _buildTurnGroup(turn, meta) {
    const header = _buildTurnHeader(turn, meta);
    const body = el('div', { class: 'tl-turn-body' });
    const group = el('div', { class: 'tl-turn-group', 'data-turn': String(turn ?? '') }, [header, body]);
    // A bare <div> with a click handler: no tab stop, no role, no state. (A1)
    _makeDisclosure(
        header,
        () => !group.classList.contains('collapsed'),
        () => group.classList.toggle('collapsed'),
    );
    header.setAttribute('aria-label', `Turn ${turn ?? 'unknown'}`);
    return group;
}

function _buildTurnHeader(turn, meta = {}) {
    const parts = [
        el('span', { class: 'tl-turn-chevron' }, [icon('chevron-down', { size: 10 })]),
        el('span', {}, [text(`Turn ${turn ?? '—'}`)]),
    ];
    if (meta.elapsedMs) parts.push(el('span', { class: 'tl-turn-meta' }, [text(_fmtMs(meta.elapsedMs))]));
    if (meta.toolCount) parts.push(el('span', { class: 'tl-turn-meta' }, [text(`${meta.toolCount} tool${meta.toolCount === 1 ? '' : 's'}`)]));
    if (meta.compactions) parts.push(el('span', { class: 'tl-turn-meta' }, [text(`${meta.compactions} compaction${meta.compactions === 1 ? '' : 's'}`)]));
    if (meta.reflectRetries) parts.push(el('span', { class: 'tl-turn-meta' }, [icon('refresh', { size: 10 }), text(`${meta.reflectRetries} reflect`)]));
    if (meta.evalRetries) parts.push(el('span', { class: 'tl-turn-meta' }, [icon('refresh', { size: 10 }), text(`${meta.evalRetries} eval`)]));
    if (meta.errors) parts.push(el('span', { class: 'tl-turn-meta tl-turn-errors' }, [text(`${meta.errors} error${meta.errors === 1 ? '' : 's'}`)]));
    if (meta.termination) {
        const slug = meta.termination.replace(/_/g, '-');
        parts.push(el('span', { class: `tl-turn-term tl-turn-term-${slug}` }, [text(meta.termination)]));
    }
    if (meta.parentTurnId != null) parts.push(el('span', { class: 'tl-turn-parent' }, [text(`↳ T${meta.parentTurnId}`)]));
    if (turn != null) parts.push(_buildStoryLink(turn));
    return el('div', { class: 'timeline-turn-header' }, parts);
}

// The list tab and the lane are two readings of the same turn, so each turn
// header carries the way across. It is a real <button> nested inside a
// role="button" header, so both activations have to be kept off the header:
// the click by stopPropagation, the key by stopping the keydown BEFORE the
// header's own handler sees it (that handler calls preventDefault, which
// would suppress the button's own click).
function _buildStoryLink(turn) {
    const btn = el('button', {
        class: 'tl-turn-story',
        title: `Show turn ${turn} in the lane`,
        'aria-label': `Show turn ${turn} in the lane`,
    }, [text('Story')]);
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        _showStoryFor(turn);
    });
    btn.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') e.stopPropagation();
    });
    return btn;
}

function _buildStateRow(row, ts) {
    const div = document.createElement('div');
    div.className = 'timeline-row';
    if ((row.reason || '').startsWith('invariant-violation')) {
        div.classList.add('invariant-violation');
    }
    if (_isErrorEntry({ kind: 'state', data: row })) {
        div.classList.add('tl-error');
    }
    const time = document.createElement('span');
    time.className = 'tl-time';
    time.textContent = _fmtClock(ts != null ? ts : row.timestamp_ms);
    const turn = document.createElement('span');
    turn.className = 'tl-turn';
    turn.textContent = `T${row.turn_id || 0}.${row.retry_index || 0}`;
    const states = document.createElement('span');
    states.className = 'tl-states';
    const from = row.from_state || '∅';
    const to = row.to_state || '?';
    states.textContent = `${from} → ${to}`;
    const reasonEl = document.createElement('span');
    reasonEl.className = 'tl-reason';
    reasonEl.textContent = row.reason || '';
    const elapsed = document.createElement('span');
    elapsed.className = 'tl-elapsed';
    elapsed.textContent = row.elapsed_ms != null ? _fmtMs(row.elapsed_ms) : '';
    div.appendChild(time);
    div.appendChild(turn);
    const combined = document.createElement('span');
    combined.appendChild(states);
    combined.appendChild(document.createElement('br'));
    combined.appendChild(reasonEl);

    // Small inline badges for notable transition types.
    const reason = row.reason || '';
    const toState = row.to_state || '';
    const badges = [];
    if (reason === 'compact-proactive' || reason === 'compact-critical' || reason === 'compact-overflow') {
        badges.push(el('span', { class: 'tl-badge tl-badge-compact' }, [text('compact')]));
    } else if (reason === 'compact-done') {
        badges.push(el('span', { class: 'tl-badge tl-badge-compact' }, [text('compact done')]));
    }
    // ⏳ and ⏸ carry EMOJI presentation in most fonts — they rendered in
    // colour, at emoji size, inside an 11px monochrome pill.
    const badge = (cls, name, label) => el('span', { class: `tl-badge ${cls}` }, [
        icon(name, { size: 10 }), text(label),
    ]);
    if (reason === 'reflect-retry') badges.push(badge('tl-badge-retry', 'refresh', 'reflect'));
    else if (reason === 'eval-retry') badges.push(badge('tl-badge-retry', 'refresh', 'eval'));
    if (toState === 'awaiting_user') badges.push(badge('tl-badge-await', 'clock', 'awaiting'));
    else if (toState === 'awaiting_workers') badges.push(badge('tl-badge-await', 'clock', 'workers'));
    if (toState === 'paused' || toState === 'pause_requested') badges.push(badge('tl-badge-pause', 'pause', 'paused'));
    if (badges.length) {
        const badgeRow = document.createElement('span');
        badgeRow.className = 'tl-badge-row';
        badges.forEach(b => badgeRow.appendChild(b));
        combined.appendChild(badgeRow);
    }

    div.appendChild(combined);
    div.appendChild(elapsed);
    return div;
}

function _buildToolRow(tool, ts) {
    const timeEl = el('span', { class: 'tl-time' }, [text(_fmtClock(ts))]);

    if (tool.running) {
        // In-flight row: no body, no chevron — upgraded in place on completion.
        const spinner = el('span', { class: 'tool-item-running-dot' });
        const nameEl = el('div', { class: 'tool-item-name' }, [text(tool.name || 'tool')]);
        let summary = '';
        if (tool.args && typeof tool.args === 'object') {
            summary = tool.name === 'bash' && tool.args.command
                ? '$ ' + tool.args.command
                : (tool.args.path || tool.args.task || Object.values(tool.args).map(v => String(v).slice(0, 40)).join(', '));
        }
        const summaryEl = el('span', { class: 'tool-item-summary' }, [text(String(summary).slice(0, 120))]);
        const headerEl = el('div', { class: 'tool-item-header' }, [timeEl, spinner, nameEl, summaryEl]);
        return el('div', { class: 'tool-item timeline-tool-row running' }, [headerEl]);
    }

    const chevron = el('span', { class: 'tool-item-chevron' }, [icon('chevron-right', { size: 10 })]);

    const nameChildren = [text(tool.name || 'tool')];
    if (tool.latency_ms) {
        const cls = tool.latency_ms < 500 ? 'fast' : tool.latency_ms < 2000 ? 'medium' : 'slow';
        nameChildren.push(el('span', { class: `tool-latency ${cls}` }, [text(`${tool.latency_ms}ms`)]));
    }
    const nameEl = el('div', { class: 'tool-item-name' }, nameChildren);

    let summary = '';
    if (tool.was_error) {
        summary = '(error)';
    } else if (tool.args && typeof tool.args === 'object') {
        if (tool.name === 'bash' && tool.args.command) {
            summary = '$ ' + tool.args.command;
        } else if (tool.args.path) {
            summary = tool.args.path;
        } else {
            summary = Object.entries(tool.args)
                .map(([k, v]) => `${k}: ${String(v).slice(0, 40)}`)
                .join(', ');
        }
    }
    if (!summary && tool.content) {
        summary = tool.content.slice(0, 80).replace(/\n/g, ' ');
    }
    const summaryEl = el('span', { class: 'tool-item-summary' }, [text(summary)]);

    const headerEl = el('div', { class: 'tool-item-header' }, [timeEl, chevron, nameEl, summaryEl]);

    const bodyChildren = [];
    if (tool.args && Object.keys(tool.args || {}).length) {
        bodyChildren.push(el('pre', { class: 'tool-item-args' }, [
            text(JSON.stringify(tool.args, null, 2)),
        ]));
    }
    bodyChildren.push(el('div', { class: 'tool-item-content' }, [text(tool.content || '(no output)')]));

    const bodyEl = el('div', { class: 'tool-item-body' }, bodyChildren);
    const itemEl = el('div', {
        class: `tool-item timeline-tool-row${tool.was_error ? ' error' : ''}`,
    }, [headerEl, bodyEl]);

    _makeDisclosure(
        headerEl,
        () => itemEl.classList.contains('expanded'),
        () => itemEl.classList.toggle('expanded'),
    );
    headerEl.setAttribute('aria-label', `${tool.name || 'tool'} call details`);
    return itemEl;
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

async function _copyExport(btn) {
    const toolCalls = _allEntries()
        .filter(e => e.kind === 'tool')
        .map(e => ({
            ts: e.ts,
            turn: e.turn,
            name: e.data.name,
            args: e.data.args,
            latency_ms: e.data.latency_ms,
            was_error: e.data.was_error,
            content: (e.data.content || '').slice(0, 4000),
        }));
    const payload = {
        session_id: state.sid,
        exported_at: new Date().toISOString(),
        // The turn records as the lane received them: the phases, the scout
        // report, the reflect chain, the gates and the token bill, already
        // grouped. Pasted into an issue this is the whole story of a turn.
        turns: _turns,
        state_log: _data.stateLog,
        tool_calls: toolCalls,
    };
    try {
        await navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
        const prev = btn.textContent;
        btn.textContent = 'Copied ✓';
        setTimeout(() => { btn.textContent = prev; }, 1500);
    } catch (e) {
        console.error('Clipboard write failed:', e);
        btn.textContent = 'Copy failed';
        setTimeout(() => { btn.textContent = 'Copy JSON'; }, 1500);
    }
}

// Called by openOverlay() on Escape.
function _onEsc() {
    // Esc inside the filter input clears it instead of closing the modal.
    if (_filterInput && document.activeElement === _filterInput) {
        if (_filterInput.value) {
            _filterInput.value = '';
            _filter.q = '';
            _renderTimeline();
        }
        _filterInput.blur();
        return;
    }
    closeTimeline();
}
