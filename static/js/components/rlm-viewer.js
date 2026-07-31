// Pernix — RLM run viewer: read-only live/post-hoc rendering of a run's trace.
//
// Fills the chat area when a session_type='rlm' pseudo-session is selected
// (app.js routes here instead of loading messages). The pseudo-session is
// pure navigation chrome; everything shown comes from /api/rlm/runs/* which
// reads trace.jsonl + manifest.json off the run dir. While the run is live
// the viewer polls the trace endpoint with a byte offset (the engine flushes
// whole lines, so the file tails cleanly); once the run ends the same render
// path serves as the permanent inspection view.

import { el, text, clear, renderMarkdown } from '../render.js';
import { get } from '../api.js';

const POLL_MS = 2000;

// Single active viewer (mirrors the one-session-at-a-time chat area).
let _view = null; // { runId, offset, timer, root, traceEl, headerEl, answerEl, childrenEl, detail, stack }

export async function openRlmViewer(container, uiSessionId) {
    closeRlmViewer();
    clear(container);
    const root = el('div', { class: 'rlm-viewer' });
    container.appendChild(root);
    let detail;
    try {
        detail = await get(`/api/rlm/runs/by-session/${encodeURIComponent(uiSessionId)}`);
    } catch {
        root.appendChild(el('div', { class: 'rlm-viewer-empty' }, [
            text('No RLM run recorded for this session — the run may have been purged by retention.'),
        ]));
        return;
    }
    _mount(root, detail, []);
}

export function closeRlmViewer() {
    if (_view?.timer) clearTimeout(_view.timer);
    _view = null;
}

// ---------------------------------------------------------------------------
// Mount / navigation
// ---------------------------------------------------------------------------

async function _openRun(root, runId, stack) {
    let detail;
    try {
        detail = await get(`/api/rlm/runs/${encodeURIComponent(runId)}`);
    } catch {
        return; // keep the current view; the child row simply does nothing
    }
    _mount(root, detail, stack);
}

function _mount(root, detail, stack) {
    if (_view?.timer) clearTimeout(_view.timer);
    clear(root);

    const headerEl = el('div', { class: 'rlm-run-header' });
    const traceEl = el('div', { class: 'rlm-trace' });
    const answerEl = el('div', { class: 'rlm-answer' });
    const childrenEl = el('div', { class: 'rlm-children' });
    root.appendChild(headerEl);
    root.appendChild(traceEl);
    root.appendChild(answerEl);
    root.appendChild(childrenEl);

    _view = {
        runId: detail.run_id,
        offset: 0,
        timer: null,
        root,
        traceEl,
        headerEl,
        answerEl,
        childrenEl,
        detail,
        stack,
    };
    _renderHeader();
    _renderFinishedParts();
    _poll();
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

function _statusCls(status) {
    if (status === 'running') return 'running';
    if (status === 'completed') return 'completed';
    if (status === 'iteration_cap') return 'capped';
    return 'failed';
}

function _fmtElapsed(seconds) {
    seconds = Math.max(0, Math.round(seconds));
    const m = Math.floor(seconds / 60);
    return m > 0 ? `${m}m${String(seconds % 60).padStart(2, '0')}s` : `${seconds}s`;
}

function _elapsedSeconds(detail) {
    if (detail.manifest?.duration_seconds) return detail.manifest.duration_seconds;
    const started = Date.parse(detail.created_at || '');
    if (!started) return 0;
    const ended = detail.finished_at ? (Date.parse(detail.finished_at) || Date.now()) : Date.now();
    return (ended - started) / 1000;
}

function _bar(label, value, cap) {
    const wrap = el('span', { class: 'rlm-bar-wrap' }, [
        el('span', { class: 'rlm-bar-label' }, [text(cap ? `${label} ${value}/${cap}` : `${label} ${value}`)]),
    ]);
    if (cap) {
        const pct = Math.min(100, Math.round((value / cap) * 100));
        wrap.appendChild(el('span', { class: 'rlm-bar' }, [
            el('span', { class: 'rlm-bar-fill', style: `width:${pct}%` }),
        ]));
    }
    return wrap;
}

function _renderHeader() {
    const v = _view;
    if (!v) return;
    const d = v.detail;
    clear(v.headerEl);

    const caps = d.manifest?.caps || {};
    const titleRow = el('div', { class: 'rlm-run-title' });
    if (v.stack.length) {
        const parent = v.stack[v.stack.length - 1];
        titleRow.appendChild(el('button', {
            class: 'rlm-back',
            title: 'Back to parent run',
            onClick: () => {
                const stack = v.stack.slice(0, -1);
                _openRun(v.root, parent, stack);
            },
        }, [text('←')]));
    }
    titleRow.appendChild(el('span', { class: `rlm-status-badge ${_statusCls(d.status)}` }, [text(d.status)]));
    titleRow.appendChild(el('span', { class: 'rlm-run-id' }, [text(`RLM run ${d.run_id}`)]));
    if (d.root_model) {
        titleRow.appendChild(el('span', { class: 'rlm-run-models' }, [
            text(`${d.root_model} → ${d.sub_model}`),
        ]));
    }
    v.headerEl.appendChild(titleRow);
    v.headerEl.appendChild(el('div', { class: 'rlm-run-task' }, [text(d.task || '')]));

    const meta = [];
    if (d.source_desc) meta.push(`source: ${d.source_desc}`);
    if (d.input_chars) meta.push(`${d.input_chars.toLocaleString()} chars in`);
    if (d.trace_path) meta.push(d.trace_path);
    v.headerEl.appendChild(el('div', { class: 'rlm-run-meta' }, [text(meta.join(' · '))]));

    const progress = el('div', { class: 'rlm-run-progress' });
    progress.appendChild(_bar('iterations', d.iterations || 0, caps.max_iterations || 0));
    progress.appendChild(_bar('sub-calls', d.subcalls || 0, caps.max_subcalls || 0));
    const elapsed = _fmtElapsed(_elapsedSeconds(d));
    const budget = caps.timeout_seconds ? ` / ${_fmtElapsed(caps.timeout_seconds)}` : '';
    progress.appendChild(el('span', { class: 'rlm-bar-label' }, [
        text(`elapsed ${elapsed}${d.status === 'running' ? budget : ''}`),
    ]));
    if (d.status === 'running') {
        progress.appendChild(el('span', { class: 'rlm-live-dot', title: 'run in progress' }));
    }
    v.headerEl.appendChild(progress);

    if (d.error) {
        v.headerEl.appendChild(el('div', { class: 'rlm-run-error' }, [text(d.error)]));
    }
}

// ---------------------------------------------------------------------------
// Trace polling + event rendering
// ---------------------------------------------------------------------------

async function _poll() {
    const v = _view;
    if (!v || !v.root.isConnected) { closeRlmViewer(); return; }
    let page;
    try {
        page = await get(`/api/rlm/runs/${encodeURIComponent(v.runId)}/trace?after=${v.offset}`);
    } catch {
        page = null; // transient — retry on the next tick if still running
    }
    if (!_view || _view !== v || !v.root.isConnected) return;

    if (page) {
        const scroller = document.getElementById('messages');
        const pinned = scroller && (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80);
        for (const ev of (page.events || [])) {
            const node = _renderEvent(ev);
            if (node) v.traceEl.appendChild(node);
        }
        v.offset = page.next_offset ?? v.offset;
        const statusChanged = page.status !== v.detail.status;
        v.detail.status = page.status;
        v.detail.iterations = page.iterations;
        v.detail.subcalls = page.subcalls;
        _renderHeader();
        if (pinned && (page.events || []).length) scroller.scrollTop = scroller.scrollHeight;

        if (!page.running) {
            if (statusChanged || !v.detail.answer) {
                // Run just ended (or we started on a stale detail) — re-fetch
                // once for the answer text and final counters.
                try {
                    const fresh = await get(`/api/rlm/runs/${encodeURIComponent(v.runId)}`);
                    if (_view === v) { v.detail = fresh; _renderHeader(); }
                } catch { /* header already shows terminal status */ }
            }
            if (_view === v) _renderFinishedParts();
            return; // trace is complete — stop polling
        }
    }
    v.timer = setTimeout(_poll, POLL_MS);
}

function _renderEvent(ev) {
    switch (ev.type) {
        case 'root': {
            const body = el('div', { class: 'rlm-ev-body' });
            body.appendChild(renderMarkdown(ev.response_preview || ''));
            return el('div', { class: 'rlm-ev rlm-ev-root' }, [
                el('div', { class: 'rlm-ev-head' }, [text(`Iteration ${(ev.iteration ?? 0) + 1} — root model`)]),
                body,
            ]);
        }
        case 'synthesis': {
            const body = el('div', { class: 'rlm-ev-body' });
            body.appendChild(renderMarkdown(ev.response_preview || ''));
            return el('div', { class: 'rlm-ev rlm-ev-root rlm-ev-synthesis' }, [
                el('div', { class: 'rlm-ev-head' }, [text('Synthesis — out of turns, composing final answer')]),
                body,
            ]);
        }
        case 'cell': {
            const details = el('details', { class: 'rlm-ev rlm-ev-cell' });
            if (ev.final) details.setAttribute('open', '');
            const dur = typeof ev.duration === 'number' ? `${ev.duration.toFixed(1)}s` : '';
            details.appendChild(el('summary', {}, [
                el('span', { class: 'rlm-cell-tag' }, [text('repl')]),
                text(` ${_firstLine(ev.code)} · ${dur}${ev.final ? ' · ✔ final answer' : ''}`),
            ]));
            details.appendChild(el('pre', { class: 'rlm-code' }, [text(ev.code || '')]));
            if (ev.stdout_preview) {
                details.appendChild(el('div', { class: 'rlm-io-label' }, [text('stdout')]));
                details.appendChild(el('pre', { class: 'rlm-io' }, [text(ev.stdout_preview)]));
            }
            if (ev.stderr_preview) {
                details.appendChild(el('div', { class: 'rlm-io-label rlm-io-err' }, [text('stderr')]));
                details.appendChild(el('pre', { class: 'rlm-io rlm-io-err' }, [text(ev.stderr_preview)]));
            }
            return details;
        }
        case 'subcall': {
            const ok = !!ev.ok;
            const dur = typeof ev.duration === 'number' ? `${ev.duration.toFixed(1)}s` : '';
            return el('div', {
                class: `rlm-ev rlm-ev-subcall${ok ? '' : ' err'}`,
                title: ev.prompt_preview || '',
            }, [
                text(`→ sub-LLM ${ev.model || '(default)'} · ${dur}${ok ? '' : ` · ⚠ ${ev.error || 'failed'}`}`),
            ]);
        }
        case 'notice':
            return el('div', { class: 'rlm-ev rlm-ev-notice' }, [text(`notice: ${ev.notice || ''}`)]);
        case 'end': {
            const dur = typeof ev.duration === 'number' ? _fmtElapsed(ev.duration) : '';
            return el('div', { class: `rlm-ev rlm-ev-end ${_statusCls(ev.status)}` }, [
                text(`Run ${ev.status} — ${ev.iterations} iterations, ${ev.subcalls} sub-calls, ${dur}`),
            ]);
        }
        default:
            return null; // unknown event types render nothing rather than breaking the tail
    }
}

function _firstLine(s) {
    const line = (s || '').trim().split('\n')[0] || '';
    return line.length > 80 ? line.slice(0, 77) + '…' : line;
}

// ---------------------------------------------------------------------------
// Finished-run parts: answer + nested children
// ---------------------------------------------------------------------------

function _renderFinishedParts() {
    const v = _view;
    if (!v) return;
    const d = v.detail;
    clear(v.answerEl);
    clear(v.childrenEl);

    if (d.answer) {
        v.answerEl.appendChild(el('div', { class: 'rlm-section-head' }, [text('Answer')]));
        const body = el('div', { class: 'rlm-answer-body' });
        body.appendChild(renderMarkdown(d.answer));
        v.answerEl.appendChild(body);
    }

    const children = d.children || [];
    if (children.length) {
        v.childrenEl.appendChild(el('div', { class: 'rlm-section-head' }, [
            text(`Nested runs (${children.length})`),
        ]));
        for (const c of children) {
            v.childrenEl.appendChild(el('button', {
                class: 'rlm-child-row',
                onClick: () => _openRun(v.root, c.run_id, [...v.stack, d.run_id]),
            }, [
                el('span', { class: `rlm-status-badge ${_statusCls(c.status)}` }, [text(c.status)]),
                text(` ${c.run_id} · ${c.iterations} it · ${c.subcalls} calls · ${_firstLine(c.task)}`),
            ]));
        }
    }
}
