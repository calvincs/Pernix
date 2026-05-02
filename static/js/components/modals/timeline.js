// Pernix — State timeline modal
//
// Two tabs:
//   * Graph    — Mermaid stateDiagram-v2 of visited states, edge counts, current
//                state highlighted, invariant-violation edges flagged.
//   * Timeline — state-log rows merged with tool-call rows by timestamp,
//                grouped by turn.
//
// Data sources:
//   GET /api/sessions/{sid}/state-log?limit=500
//   GET /api/sessions/{sid}                    (for messages → tool calls)
// Merge key: state row `timestamp_ms` vs message `created_at` (ISO) → ms.
//
// Mermaid is lazy-loaded from CDN on first open.

import { el, text } from '../../render.js';
import { get } from '../../api.js';
import { state } from '../../store.js';

// Mermaid 10 only ships an ESM build — use dynamic import, not a UMD <script>.
const MERMAID_SRC = 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';

let _overlay = null;
let _mermaid = null;
let _mermaidPromise = null;
let _data = { stateLog: [], messages: [] };

export async function openTimeline() {
    if (_overlay) return;
    if (!state.sid) return;

    const graphPane = el('div', { class: 'tab-content active', 'data-tab': 'graph' }, [
        el('div', { class: 'timeline-graph-status' }, [text('Loading\u2026')]),
        el('div', { class: 'timeline-graph-container', id: 'timeline-graph' }),
        el('div', { class: 'timeline-graph-caption', id: 'timeline-graph-caption' }),
    ]);
    const listPane = el('div', {
        class: 'tab-content',
        'data-tab': 'timeline',
        id: 'timeline-modal-body',
    });

    const tabBtns = [
        el('button', { class: 'tab-btn active', 'data-tab': 'graph' }, [text('Graph')]),
        el('button', { class: 'tab-btn', 'data-tab': 'timeline' }, [text('Timeline')]),
    ];
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const target = btn.getAttribute('data-tab');
            tabBtns.forEach(b => b.classList.toggle('active', b === btn));
            [graphPane, listPane].forEach(p => {
                p.classList.toggle('active', p.getAttribute('data-tab') === target);
            });
            if (target === 'graph') _renderGraph(graphPane);
        });
    });
    const tabBar = el('div', { class: 'tab-bar' }, tabBtns);

    const card = el('div', {
        id: 'timeline-modal',
        class: 'modal-card',
    }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text('State timeline')]),
            el('button', { class: 'modal-close', onClick: closeTimeline }, [text('\u00d7')]),
        ]),
        tabBar,
        el('div', { class: 'modal-body timeline-modal-content' }, [graphPane, listPane]),
    ]);

    _overlay = el('div', { class: 'modal-overlay' }, [card]);
    _overlay.addEventListener('click', (e) => {
        if (e.target === _overlay) closeTimeline();
    });
    document.body.append(_overlay);
    document.addEventListener('keydown', _onEsc);

    await _load();
    _renderTimeline(listPane);
    _renderGraph(graphPane);
}

export function closeTimeline() {
    if (_overlay) {
        _overlay.remove();
        _overlay = null;
    }
    document.removeEventListener('keydown', _onEsc);
}

export function isTimelineOpen() {
    return _overlay !== null;
}

// Live-append from _renderStateBadge. Appends a state row to the Timeline
// tab (if currently rendered) and invalidates the graph.
export function appendTimelineRow(row) {
    _data.stateLog.push({
        id: null,
        session_id: state.sid,
        turn_id: row.turn_id || 0,
        retry_index: row.retry_index || 0,
        from_state: row.from_state,
        to_state: row.to_state,
        reason: row.reason,
        termination_reason: row.termination_reason,
        elapsed_ms: row.elapsed_ms,
        timestamp_ms: Date.now(),
    });
    const body = document.getElementById('timeline-modal-body');
    if (body) {
        body.appendChild(_buildStateRow(row));
        body.scrollTop = body.scrollHeight;
    }
    // Re-render the graph if the graph tab is currently visible.
    const graphPane = document.querySelector('.tab-content[data-tab="graph"]');
    if (graphPane && graphPane.classList.contains('active')) {
        _renderGraph(graphPane);
    }
}

async function _load() {
    try {
        const [logRes, sessRes] = await Promise.all([
            get(`/api/sessions/${state.sid}/state-log?limit=500`),
            get(`/api/sessions/${state.sid}`),
        ]);
        _data.stateLog = logRes.entries || [];
        _data.messages = sessRes.messages || [];
    } catch (e) {
        console.error('Failed to load timeline data:', e);
        _data = { stateLog: [], messages: [] };
    }
}

// ---------------------------------------------------------------------------
// Graph tab
// ---------------------------------------------------------------------------

async function _renderGraph(pane) {
    const statusEl = pane.querySelector('.timeline-graph-status');
    const container = pane.querySelector('.timeline-graph-container');
    const caption = pane.querySelector('.timeline-graph-caption');
    container.innerHTML = '';
    caption.innerHTML = '';

    // Remove any stale tally from a previous render.
    const stale = pane.querySelector('.tl-tool-tally');
    if (stale) stale.remove();

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

    const { source, current, termination, invariantViolations } = _buildMermaidSource(_data.stateLog);

    try {
        // Unique id per render so Mermaid doesn't collide on re-entry.
        const id = `timeline-diagram-${Date.now()}`;
        const { svg } = await mermaid.render(id, source);
        container.innerHTML = svg;
    } catch (e) {
        statusEl.textContent = 'Diagram render error: ' + e.message;
        console.error('Mermaid render failed:', e, '\nSource:\n', source);
        return;
    }

    // Tool call tally — inserted between the diagram and the caption.
    const tallyEl = _buildToolTallyEl(_data.stateLog, _data.messages);
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

    // Highlight current state.
    if (current) {
        lines.push('    classDef current fill:#1e3a4f,stroke:#7ec4f0,color:#fff,stroke-width:2px');
        lines.push(`    class ${current} current`);
    }

    // Flag invariant-violation targets.
    const violationTargets = new Set();
    for (const [key, edge] of edges) {
        if (edge.invariant) violationTargets.add(key.split('||')[1]);
    }
    if (violationTargets.size) {
        lines.push('    classDef violation fill:#4a1f1a,stroke:#e88080,color:#fff');
        lines.push(`    class ${[...violationTargets].join(',')} violation`);
    }

    return {
        source: lines.join('\n'),
        current,
        termination,
        invariantViolations,
    };
}

function _edgeLabel(edge) {
    if (edge.count > 1) {
        return `${edge.count}\u00d7`;
    }
    const r = edge.reason || '';
    const short = r.length > 24 ? r.slice(0, 22) + '\u2026' : r;
    // Mermaid edge labels choke on : ; " \n — replace with safe chars.
    return short.replace(/[:;"\n]/g, ' ').trim();
}

// Build a DOM element showing per-turn tool call counts, or null if no tools.
function _buildToolTallyEl(stateLog, messages) {
    // Assign each tool-result message a turn using the same timestamp-order
    // logic as _mergeRows.
    const tcMeta = new Map(); // tool_call_id -> {name}
    for (const m of messages) {
        if (m.role === 'assistant' && m.tool_calls) {
            let parsed;
            try { parsed = JSON.parse(m.tool_calls); } catch { continue; }
            for (const tc of parsed || []) {
                tcMeta.set(tc.id, { name: tc.name || 'tool' });
            }
        }
    }

    // Build sorted [{ts, kind, data}] list from state log + tool messages.
    const entries = stateLog.map(s => ({ kind: 'state', ts: s.timestamp_ms || 0, turn: s.turn_id, data: s }));
    for (const m of messages) {
        if (m.role !== 'tool') continue;
        const meta = tcMeta.get(m.tool_call_id) || { name: 'tool' };
        entries.push({ kind: 'tool', ts: _isoToMs(m.created_at), turn: null, name: meta.name });
    }
    entries.sort((a, b) => (a.ts || 0) - (b.ts || 0));

    // Propagate turn labels to tool entries.
    let currentTurn = null;
    for (const e of entries) {
        if (e.kind === 'state' && e.turn != null) currentTurn = e.turn;
        else if (e.kind === 'tool') e.turn = currentTurn;
    }

    // Aggregate: Map<turn, Map<name, count>>
    const byTurn = new Map();
    for (const e of entries) {
        if (e.kind !== 'tool') continue;
        const t = e.turn ?? 0;
        if (!byTurn.has(t)) byTurn.set(t, new Map());
        const m = byTurn.get(t);
        m.set(e.name, (m.get(e.name) || 0) + 1);
    }
    if (!byTurn.size) return null;

    const multiTurn = byTurn.size > 1;
    const totalCalls = [...byTurn.values()].reduce((s, m) => s + [...m.values()].reduce((a, b) => a + b, 0), 0);

    const rows = [...byTurn.entries()]
        .sort((a, b) => (a[0] ?? 0) - (b[0] ?? 0))
        .map(([turn, counts]) => {
            const chips = [...counts.entries()]
                .sort((a, b) => b[1] - a[1])
                .map(([name, count]) =>
                    el('span', { class: 'tl-tally-chip' }, [text(`${name} (${count})`)])
                );
            const parts = [];
            if (multiTurn) {
                parts.push(el('span', { class: 'tl-tally-turn-label' }, [text(`T${turn}:`)]));
            }
            return el('div', { class: 'tl-tally-row' }, [...parts, ...chips]);
        });

    return el('div', { class: 'tl-tool-tally' }, [
        el('div', { class: 'tl-tally-header' }, [text(`Tool calls (${totalCalls})`)]),
        ...rows,
    ]);
}

async function _ensureMermaid() {
    if (_mermaid) return _mermaid;
    if (!_mermaidPromise) {
        _mermaidPromise = import(/* @vite-ignore */ MERMAID_SRC).then(mod => {
            _mermaid = mod.default;
            _mermaid.initialize({
                startOnLoad: false,
                theme: 'dark',
                securityLevel: 'strict',
            });
            return _mermaid;
        });
    }
    return _mermaidPromise;
}

// ---------------------------------------------------------------------------
// Timeline tab — state rows + tool call rows merged by timestamp
// ---------------------------------------------------------------------------

function _renderTimeline(body) {
    body.innerHTML = '';
    const rows = _mergeRows(_data.stateLog, _data.messages);
    if (!rows.length) {
        body.appendChild(el('div', { class: 'timeline-empty' }, [text('No transitions or tool calls yet.')]));
        return;
    }

    let lastTurn = null;
    for (const row of rows) {
        const turn = row.kind === 'state' ? row.data.turn_id : row.turnHint;
        if (turn != null && turn !== lastTurn) {
            body.appendChild(_buildTurnHeader(turn));
            lastTurn = turn;
        }
        if (row.kind === 'state') {
            body.appendChild(_buildStateRow({
                turn_id: row.data.turn_id,
                retry_index: row.data.retry_index,
                from_state: row.data.from_state,
                to_state: row.data.to_state,
                reason: row.data.reason,
                elapsed_ms: row.data.elapsed_ms,
            }));
        } else if (row.kind === 'tool') {
            body.appendChild(_buildToolRow(row.data));
        }
    }
    body.scrollTop = body.scrollHeight;
}

function _mergeRows(stateLog, messages) {
    const entries = [];

    for (const s of stateLog) {
        entries.push({
            kind: 'state',
            ts: s.timestamp_ms || 0,
            turnHint: s.turn_id,
            data: s,
        });
    }

    // Build a map of tool_call_id → {name, args} from assistant tool_calls.
    // Stored format (core/agent.py:482): [{id, name, arguments}] where
    // arguments is a JSON-encoded string.
    const toolCallMeta = new Map();
    for (const m of messages) {
        if (m.role === 'assistant' && m.tool_calls) {
            let parsed;
            try { parsed = JSON.parse(m.tool_calls); } catch { continue; }
            for (const tc of parsed || []) {
                let args = null;
                if (tc.arguments) {
                    try { args = JSON.parse(tc.arguments); } catch { args = null; }
                }
                toolCallMeta.set(tc.id, { name: tc.name || 'tool', args });
            }
        }
    }

    for (const m of messages) {
        if (m.role !== 'tool') continue;
        const meta = toolCallMeta.get(m.tool_call_id) || { name: 'tool', args: null };
        entries.push({
            kind: 'tool',
            ts: _isoToMs(m.created_at),
            turnHint: null,
            data: {
                id: m.id,
                name: meta.name,
                args: meta.args,
                content: m.content || '',
                latency_ms: m.latency_ms,
                was_error: _isErrorContent(m.content),
            },
        });
    }

    entries.sort((a, b) => (a.ts || 0) - (b.ts || 0));

    // Assign turn hints to tool rows based on the state row they follow.
    let currentTurn = null;
    for (const e of entries) {
        if (e.kind === 'state' && e.data.turn_id != null) {
            currentTurn = e.data.turn_id;
        } else if (e.kind === 'tool') {
            e.turnHint = currentTurn;
        }
    }
    return entries;
}

function _isoToMs(iso) {
    if (!iso) return 0;
    const t = Date.parse(iso);
    return isNaN(t) ? 0 : t;
}

function _isErrorContent(content) {
    if (!content) return false;
    const head = content.slice(0, 120).toLowerCase();
    return head.startsWith('error:') || head.includes('traceback');
}

function _buildTurnHeader(turn) {
    return el('div', { class: 'timeline-turn-header' }, [text(`Turn ${turn}`)]);
}

function _buildStateRow(row) {
    const div = document.createElement('div');
    div.className = 'timeline-row';
    if ((row.reason || '').startsWith('invariant-violation')) {
        div.classList.add('invariant-violation');
    }
    const turn = document.createElement('span');
    turn.className = 'tl-turn';
    turn.textContent = `T${row.turn_id || 0}.${row.retry_index || 0}`;
    const states = document.createElement('span');
    states.className = 'tl-states';
    const from = row.from_state || '\u2205';
    const to = row.to_state || '?';
    states.textContent = `${from} \u2192 ${to}`;
    const reasonEl = document.createElement('span');
    reasonEl.className = 'tl-reason';
    reasonEl.textContent = row.reason || '';
    const elapsed = document.createElement('span');
    elapsed.className = 'tl-elapsed';
    elapsed.textContent = row.elapsed_ms != null ? `${row.elapsed_ms}ms` : '';
    div.appendChild(turn);
    const combined = document.createElement('span');
    combined.appendChild(states);
    combined.appendChild(document.createElement('br'));
    combined.appendChild(reasonEl);
    div.appendChild(combined);
    div.appendChild(elapsed);
    return div;
}

function _buildToolRow(tool) {
    const chevron = el('span', { class: 'tool-item-chevron' }, [text('\u25B6')]);

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

    const headerEl = el('div', { class: 'tool-item-header' }, [chevron, nameEl, summaryEl]);

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

    headerEl.addEventListener('click', () => itemEl.classList.toggle('expanded'));
    return itemEl;
}

function _onEsc(e) {
    if (e.key === 'Escape') closeTimeline();
}
