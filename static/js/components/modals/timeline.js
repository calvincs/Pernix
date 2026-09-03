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
//   * Map      — the state machine itself, hand-drawn as inline SVG: the ten
//                states of sessions/state_v2.py and every edge of its
//                TRANSITIONS table. The edges this session actually took are
//                solid and carry their count; the rest stay faint. The state
//                the session is in is lit. Clicking a state filters the
//                Timeline tab to it. Under the map, the dwell-time bar and
//                the per-turn tool tally.
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
// Every colour in here is a --state-<name>-{fg,bg} custom property, referenced
// as a `var()` from an element's own class or inline style. Nothing resolves a
// token to a hex string in JavaScript any more, so a theme swap repaints the
// whole modal with no JS involved at all.

import { el, text } from '../../render.js';
import { icon } from '../../icons.js';
import { get } from '../../api.js';
import { state } from '../../store.js';
import { openOverlay } from '../../a11y.js';
import { bindStripScroll } from '../file-panel.js';

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
// tally, map) once the completed result arrives.
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
let _mapRefreshTimer = null;    // pending debounced map refresh (tool bursts)

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

    const mapPane = el('div', { class: 'tab-content', 'data-tab': 'map' }, [
        el('div', { class: 'timeline-map-status' }),
        el('div', { class: 'timeline-map-container', id: 'timeline-map' }),
        el('div', { class: 'timeline-map-caption', id: 'timeline-map-caption' }),
    ]);
    _bodyEl = el('div', { id: 'timeline-modal-body' });
    const listPane = el('div', { class: 'tab-content', 'data-tab': 'timeline' }, [
        _buildFilterBar(),
        _bodyEl,
    ]);

    _tabBtns = [
        el('button', { class: 'tab-btn active', 'data-tab': 'lane' }, [text('Lane')]),
        el('button', { class: 'tab-btn', 'data-tab': 'map' }, [text('Map')]),
        el('button', { class: 'tab-btn', 'data-tab': 'timeline' }, [text('Timeline')]),
    ];
    _panes = [lanePane, mapPane, listPane];
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

    const modalBody = el('div', { class: 'modal-body timeline-modal-content' }, [lanePane, mapPane, listPane]);
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
    // The Map is drawn on the first switch to it. It is cheap — one SVG of
    // fixed geometry — but it is also not the tab on screen.
}

export function closeTimeline() {
    if (_closeOverlay) { _closeOverlay(); _closeOverlay = null; }
    if (_overlay) {
        _overlay.remove();
        _overlay = null;
    }
    if (_mapRefreshTimer) {
        clearTimeout(_mapRefreshTimer);
        _mapRefreshTimer = null;
    }
    if (_laneRefreshTimer) {
        clearTimeout(_laneRefreshTimer);
        _laneRefreshTimer = null;
    }
    // Invalidate a lane fetch still in flight, so it cannot render into a
    // modal that is already gone.
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
    if (target === 'map') {
        const pane = _panes.find(p => p.getAttribute('data-tab') === 'map');
        if (pane) _renderMap(pane);
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
// tab (if currently rendered) and redraws the map.
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
    _refreshMapIfVisible();
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
        _refreshMapIfVisible();
        _scheduleLaneRefresh();
        return;
    }
    _data.liveTools.push(entry);
    _appendLiveEntry({ kind: 'tool', ts: entry.ts, turn: null, data: entry });
    _refreshMapIfVisible();
    _scheduleLaneRefresh();
}

// Coalesce map refreshes. A tool call moves nothing on the map itself — only
// the tally and the caption under it — and a tool-heavy round fires a dozen
// events whose intermediate frames are superseded before anyone reads them, so
// a trailing debounce collapses the burst into one redraw. A state change is
// the opposite: it is the whole point of the tab, so it redraws immediately.
const MAP_REFRESH_DEBOUNCE_MS = 250;

function _mapPane() {
    const pane = _panes.find(p => p.getAttribute('data-tab') === 'map');
    return pane && pane.classList.contains('active') ? pane : null;
}

function _refreshMapIfVisible() {
    if (_mapRefreshTimer) clearTimeout(_mapRefreshTimer);
    _mapRefreshTimer = setTimeout(() => {
        _mapRefreshTimer = null;
        const pane = _mapPane();
        if (pane) _renderMap(pane);
    }, MAP_REFRESH_DEBOUNCE_MS);
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

// Filter the Timeline tab to a single state — entry point for map state clicks.
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
// Map tab — the state machine, drawn once by hand
//
// The machine is fixed. Ten states and the 31 distinct edges of the
// TRANSITIONS table in sessions/state_v2.py, which changes about once a
// release: it does not need a layout engine, and it certainly did not need
// the 3.3 MB one that used to draw it. Mermaid cost a megabyte-plus parse on
// the first open of a tab; its classDef syntax is comma-separated and cannot
// carry a `color-mix()`, so every colour had to be resolved to #rrggbb through
// theme.js's hex() — twice the source of "every state painted black in both
// themes"; its default HTML labels live in a <foreignObject>, which the SVG
// sanitizer strips; and the diagram it laid out was wide enough to scroll the
// modal sideways on a phone.
//
// So the map is inline SVG, built with createElementNS — no markup string, so
// nothing to sanitize. Every coordinate is in the three tables below. Moving a
// state is editing two numbers and the `d` of the edges that touch it, and the
// viewBox is the drawing's own size, so the browser scales the whole thing to
// whatever width the modal has.
//
// The colours are CSS. A rect carries `tl-map-state tl-map-<state>` and takes
// its --state-<name>-{fg,bg} pair from layout.css, exactly the way the lane's
// segments do — which is why a theme swap repaints the map with no JS at all.
// ---------------------------------------------------------------------------

const SVG_NS = 'http://www.w3.org/2000/svg';

// The drawing, in user units. Wide rather than tall because the machine reads
// left to right: idle_ready → scouting → processing → finalizing → idle_ready,
// with compacting above processing, the four waits below it, and cancelling —
// which seven states can reach — at the end.
const MAP_VIEW = { w: 700, h: 292 };
const MAP_BOX = { w: 104, h: 34, r: 6 };

// state → [x, y] of the box's top-left corner.
const MAP_NODES = {
    idle_ready:       [40, 104],
    scouting:         [176, 104],
    processing:       [312, 104],
    finalizing:       [448, 104],
    cancelling:       [584, 104],
    compacting:       [312, 16],
    awaiting_user:    [40, 200],
    awaiting_workers: [176, 200],
    pause_requested:  [312, 200],
    paused:           [448, 200],
};

// Every (from, to) pair in TRANSITIONS, once each — 31 edges carrying 48
// reasons, because half that table is the reaper's and the cancel-finally
// handler's escape hatches back to idle_ready and they all draw as one line.
//
// `d` is the path. Long runs are routed through lanes rather than drawn
// straight, so the edges that cross do so at a right angle instead of
// disappearing into each other: y = 62/76/90 above the main row, 144…192
// between it and the waits, 244…280 below them, and x = 8/16/24 down the left
// margin for the three returns that come back around the outside.
//
// `l` is where the count sits when the session used the edge. Unused edges
// carry no label at all, so a collision needs two live edges sharing a lane,
// and the lanes were assigned so that cannot happen for the common ones. `a`
// is that label's text-anchor where `middle` is the wrong one.
const MAP_EDGES = [
    // The main flow, left to right, and the reaper's way back out of scouting.
    { from: 'idle_ready', to: 'scouting', d: 'M144,114 H176', l: [160, 110] },
    { from: 'scouting', to: 'idle_ready', d: 'M176,128 H144', l: [160, 140] },
    { from: 'scouting', to: 'processing', d: 'M280,121 H312', l: [296, 117] },
    { from: 'processing', to: 'finalizing', d: 'M416,121 H448', l: [432, 117] },
    // The compaction round trip, and the three ways out of it.
    { from: 'processing', to: 'compacting', d: 'M338,104 V50', l: [334, 80], a: 'end' },
    { from: 'compacting', to: 'processing', d: 'M390,50 V104', l: [394, 80], a: 'start' },
    { from: 'compacting', to: 'finalizing', d: 'M416,42 H466 V104', l: [441, 38] },
    { from: 'compacting', to: 'cancelling', d: 'M416,26 H648 V104', l: [532, 22] },
    { from: 'compacting', to: 'idle_ready', d: 'M312,33 H116 V104', l: [214, 29] },
    // The returns, over the top of the row they leave.
    { from: 'finalizing', to: 'idle_ready', d: 'M484,104 V62 H92 V104', l: [288, 58] },
    { from: 'finalizing', to: 'scouting', d: 'M516,104 V76 H228 V104', l: [372, 72] },
    { from: 'cancelling', to: 'idle_ready', d: 'M612,104 V90 H68 V104', l: [340, 86] },
    // The band between the main row and the waits.
    { from: 'scouting', to: 'finalizing', d: 'M264,138 V144 H460 V138', l: [362, 141] },
    { from: 'processing', to: 'cancelling', d: 'M392,138 V152 H624 V138', l: [508, 149] },
    { from: 'scouting', to: 'cancelling', d: 'M252,138 V160 H600 V138', l: [426, 157] },
    { from: 'processing', to: 'awaiting_workers', d: 'M324,138 V168 H240 V200', l: [282, 165] },
    { from: 'processing', to: 'awaiting_user', d: 'M336,138 V176 H104 V200', l: [220, 173] },
    { from: 'processing', to: 'idle_ready', d: 'M316,138 V184 H120 V138', l: [218, 181] },
    { from: 'awaiting_user', to: 'scouting', d: 'M124,200 V192 H202 V138', l: [163, 189] },
    { from: 'paused', to: 'processing', d: 'M476,200 V192 H380 V138', l: [428, 189] },
    // Straight down (or up) between a wait and the row above it.
    { from: 'processing', to: 'pause_requested', d: 'M352,138 V200', l: [356, 172], a: 'start' },
    { from: 'awaiting_workers', to: 'scouting', d: 'M216,200 V138', l: [212, 172], a: 'end' },
    { from: 'awaiting_user', to: 'idle_ready', d: 'M76,200 V138', l: [72, 172], a: 'end' },
    { from: 'pause_requested', to: 'paused', d: 'M416,217 H448', l: [432, 213] },
    // Under the waits: three returns around the left margin, four runs right
    // into cancelling.
    { from: 'awaiting_workers', to: 'idle_ready', d: 'M204,234 V244 H24 V112 H40', l: [114, 241] },
    { from: 'pause_requested', to: 'idle_ready', d: 'M340,234 V253 H16 V121 H40', l: [178, 250] },
    { from: 'paused', to: 'idle_ready', d: 'M476,234 V262 H8 V130 H40', l: [242, 259] },
    { from: 'pause_requested', to: 'cancelling', d: 'M388,234 V244 H648 V138', l: [518, 241] },
    { from: 'paused', to: 'cancelling', d: 'M524,234 V253 H636 V138', l: [580, 250] },
    { from: 'awaiting_user', to: 'cancelling', d: 'M92,234 V271 H660 V138', l: [376, 268] },
    { from: 'awaiting_workers', to: 'cancelling', d: 'M252,234 V280 H672 V138', l: [462, 277] },
];

// The states in which a turn is over. Everything else means the machine is
// still working, which is what pulses the lit state and the badge alike.
const RESTING_STATES = new Set(['idle_ready', 'awaiting_user', 'awaiting_workers']);

const MAP_FLASH_MS = 700;

function _svgEl(name, attrs, children) {
    const node = document.createElementNS(SVG_NS, name);
    for (const [k, v] of Object.entries(attrs || {})) {
        if (v != null) node.setAttribute(k, String(v));
    }
    for (const c of children || []) if (c) node.appendChild(c);
    return node;
}

function _svgTitle(str) {
    return _svgEl('title', {}, [document.createTextNode(str)]);
}

// The same aggregation the Mermaid source used to do: one entry per (from, to)
// pair, its count, the last reason seen on it, and whether any of them was an
// invariant violation. A row with no from_state is the session's very first
// and has no edge to draw.
function _mapStats(rows) {
    const edges = new Map();
    let current = null;
    let termination = null;
    const invariantViolations = [];

    for (const r of rows) {
        const to = r.to_state;
        if (!to) continue;
        current = to;
        if (r.termination_reason) termination = r.termination_reason;
        if (!r.from_state) continue;
        const key = `${r.from_state}||${to}`;
        const edge = edges.get(key) || { count: 0, reason: '', invariant: false };
        edge.count += 1;
        if (r.reason) edge.reason = r.reason;   // keep last reason
        if ((r.reason || '').startsWith('invariant-violation')) {
            edge.invariant = true;
            invariantViolations.push(r);
        }
        edges.set(key, edge);
    }
    return { edges, current, termination, invariantViolations };
}

function _buildMapSvg({ edges, current }) {
    const running = !!current && !RESTING_STATES.has(current);

    // Three arrowheads rather than one with `context-stroke`: the faint layer,
    // the edges this session took, and the ones it took illegally. Each is a
    // <path> with a class, so its fill comes from layout.css like everything
    // else here. userSpaceOnUse keeps the head one size whether the edge under
    // it is drawn at 1px or 2px.
    const marker = (id, cls) => _svgEl('marker', {
        id,
        markerWidth: 8,
        markerHeight: 8,
        refX: 8,
        refY: 4,
        orient: 'auto',
        markerUnits: 'userSpaceOnUse',
    }, [_svgEl('path', { class: cls, d: 'M0,0 L8,4 L0,8 Z' })]);

    const defs = _svgEl('defs', {}, [
        marker('tl-map-arrow', 'tl-map-arrowhead'),
        marker('tl-map-arrow-on', 'tl-map-arrowhead on'),
        marker('tl-map-arrow-bad', 'tl-map-arrowhead bad'),
    ]);

    const edgeLayer = _svgEl('g', { class: 'tl-map-edges' });
    for (const e of MAP_EDGES) {
        const key = `${e.from}||${e.to}`;
        const used = edges.get(key);
        const bad = !!(used && used.invariant);
        edgeLayer.appendChild(_svgEl('path', {
            class: `tl-map-edge${used ? ' used' : ''}${bad ? ' violation' : ''}`,
            'data-edge': key,
            d: e.d,
            'marker-end': `url(#${bad ? 'tl-map-arrow-bad' : used ? 'tl-map-arrow-on' : 'tl-map-arrow'})`,
        }, [_svgTitle(used
            ? `${e.from} → ${e.to} · ${used.count}× · ${used.reason || 'no reason recorded'}`
            : `${e.from} → ${e.to} · not taken in this session`)]));
        if (!used) continue;
        edgeLayer.appendChild(_svgEl('text', {
            class: 'tl-map-count',
            x: e.l[0],
            y: e.l[1],
            'text-anchor': e.a || 'middle',
        }, [document.createTextNode(`${used.count}×`)]));
    }

    const nodeLayer = _svgEl('g', { class: 'tl-map-nodes' });
    for (const [name, [x, y]] of Object.entries(MAP_NODES)) {
        const lit = name === current;
        const rect = _svgEl('rect', {
            class: `tl-map-state tl-map-${name}${lit ? ' current' : ''}${lit && running ? ' live' : ''}`,
            x,
            y,
            width: MAP_BOX.w,
            height: MAP_BOX.h,
            rx: MAP_BOX.r,
        });
        const label = _svgEl('text', {
            class: `tl-map-label tl-map-${name}`,
            x: x + MAP_BOX.w / 2,
            y: y + MAP_BOX.h / 2,
            'text-anchor': 'middle',
            'dominant-baseline': 'central',
        }, [document.createTextNode(name)]);
        // A bare SVG group is neither focusable nor announced, so the link
        // across to the Timeline tab was mouse-only until the Mermaid nodes
        // were given this treatment. The map keeps it.
        const node = _svgEl('g', {
            class: 'tl-map-node',
            'data-state': name,
            role: 'button',
            tabindex: '0',
            'aria-label': `Filter the timeline to the ${name} state`,
        }, [rect, label, _svgTitle(lit
            ? `${name} — where the session is now. Filter the timeline to it.`
            : `${name} — filter the timeline to it.`)]);
        const activate = () => _filterTimelineByState(name);
        node.addEventListener('click', activate);
        node.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
            e.preventDefault();
            activate();
        });
        nodeLayer.appendChild(node);
    }

    return _svgEl('svg', {
        class: 'tl-map',
        viewBox: `0 0 ${MAP_VIEW.w} ${MAP_VIEW.h}`,
        preserveAspectRatio: 'xMidYMid meet',
        role: 'group',
        'aria-label': 'The session state machine. Solid edges are the ones this session took.',
    }, [defs, edgeLayer, nodeLayer]);
}

// The edge a transition just travelled, briefly thickened. Called after the
// map has been redrawn, so the class always lands on a fresh element and there
// is no animation to restart.
function _flashMapEdge(container, key) {
    const path = container.querySelector(`.tl-map-edge[data-edge="${key}"]`);
    if (!path) return;
    path.classList.add('flash');
    setTimeout(() => path.classList.remove('flash'), MAP_FLASH_MS);
}

function _renderMap(pane, opts = {}) {
    const statusEl = pane.querySelector('.timeline-map-status');
    const container = pane.querySelector('.timeline-map-container');
    const caption = pane.querySelector('.timeline-map-caption');
    container.innerHTML = '';
    caption.innerHTML = '';

    // Remove any stale sections from a previous render.
    pane.querySelectorAll('.tl-tool-tally, .tl-dwell').forEach(n => n.remove());

    // The map is the machine, not the session, so an empty state log is not a
    // reason to draw nothing — it is a reason for every edge to stay faint.
    // (The old Graph tab answered "No state transitions yet" and rendered an
    // empty container, against which a colour check passes by drawing nothing.)
    statusEl.textContent = _data.stateLog.length
        ? ''
        : 'Nothing has run in this session yet — no edge is lit.';

    const stats = _mapStats(_data.stateLog);
    container.appendChild(_buildMapSvg(stats));
    if (opts.flash) _flashMapEdge(container, opts.flash);

    const entries = _allEntries();

    // Dwell-time breakdown + tool tally — inserted between map and caption.
    const dwellEl = _buildDwellEl(_data.stateLog);
    if (dwellEl) pane.insertBefore(dwellEl, caption);
    const tallyEl = _buildToolTallyEl(entries);
    if (tallyEl) pane.insertBefore(tallyEl, caption);

    const captionParts = [];
    if (stats.current) {
        captionParts.push(el('span', { class: 'tl-caption-current' }, [text(`Now: ${stats.current}`)]));
    }
    if (stats.termination) {
        captionParts.push(el('span', { class: 'tl-caption-term' }, [text(`Last termination: ${stats.termination}`)]));
    }
    if (stats.invariantViolations.length) {
        const n = stats.invariantViolations.length;
        captionParts.push(el('span', { class: 'tl-caption-warn' }, [
            text(`${n} invariant violation${n === 1 ? '' : 's'}`),
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

// Per-state dwell-time breakdown — elapsed_ms on a transition row is the time
// spent in from_state, so summing by from_state answers "where did the time
// go". The segments carry their --state-* pair as var() references, the way
// the lane's do: this used to hand a JS-resolved hex to a background, which is
// the bridge that painted the whole bar one black strip twice.

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
            'data-state': name,
            style: `width:${Math.max(pct, 1.5)}%;`
                + `background:var(--state-${name}-bg, var(--bg-surface));`
                + `border-bottom-color:var(--state-${name}-fg, var(--text-dim));`,
            title: `${name}: ${_fmtMs(ms)} (${pct.toFixed(0)}%)`,
        });
    });

    const chips = sorted.map(([name, ms]) => {
        const pct = ((ms / total) * 100).toFixed(0);
        return el('span', { class: 'tl-dwell-chip' }, [
            el('span', {
                class: 'tl-dwell-dot',
                style: `background:var(--state-${name}-fg, var(--text-dim))`,
            }),
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
