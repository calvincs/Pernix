// Pernix — Canary tab (Explorer): suite table, pass rates, run triggers,
// recent runs, and full lifecycle control — create, edit, park/unpark,
// retire, probes. Closes the loop from the Adaptive tab's tripwire flags —
// a flagged batch cites canary regressions; this is where you read them.

import { el, text, clear } from '../../render.js';
import { get, post, put, del, patch } from '../../api.js';
import { actionBtn, setActionNotice, takeActionNotice } from './adaptive.js';
import { resultLine, tabGlossary } from './telos.js';
import { createCodeEditor } from '../file-panel.js';

// The open raw-CANARY.md editor, if any. Refresh and Cancel both tear the tab
// down, so they have to be able to see an unsaved edit — and the Monaco
// instance has to be disposed rather than left leaking behind a cleared
// container. (S11)
let _editor = null;
let _editorDirty = false;

function disposeEditor() {
    if (_editor) { _editor.dispose(); _editor = null; }
    _editorDirty = false;
}

function guardEditor() {
    if (!_editorDirty) return true;
    if (!confirm('Discard unsaved changes?')) return false;
    _editorDirty = false;
    return true;
}

function relTime(isoStr) {
    if (!isoStr) return '';
    let s = isoStr.replace(/\+00:00$/, 'Z');
    if (!/[Z+-]\d{2}/.test(s)) s += 'Z';
    const d = new Date(s);
    if (isNaN(d.getTime())) return isoStr;
    const sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 60) return 'just now';
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    return `${Math.floor(sec / 86400)}d ago`;
}

function badge(label, cls = '') {
    return el('span', { class: `adaptive-badge ${cls}` }, [text(label)]);
}

function outcomeBadge(r) {
    if (r.passed) return badge('PASS', 'ok');
    const oc = r.outcome || 'FAIL';
    // gate_fail is the honest capability failure; the rest are harness noise
    // and must read differently at a glance.
    return badge(oc === 'gate_fail' ? 'FAIL' : oc.toUpperCase(), oc === 'gate_fail' ? 'warn' : 'off');
}

const PROBE_TEMPLATE = `---
name: probe-my-check
prompt: |
  Describe the one-off task to test here.
gates:
  - name: check
    command: test -f expected.txt
max_runs: 3
tags: [probe]
---
One-off probe: runs 3 times, reports a summary notification, then retires
itself to .retired/. Edit name, prompt and gates before creating.
`;

const CANARY_TEMPLATE = `---
name: my-canary
prompt: |
  Describe the task to test here.
gates:
  - name: check
    command: test -f expected.txt
tags: []
covers: []
last_reviewed: ${new Date().toISOString().slice(0, 10)}
---
What this canary checks, for the humans who review it.
`;

// Editor panel: raw CANARY.md in the SAME editor the Workspace, Skills and
// Jobs tabs use — a bare <textarea> here meant no line numbers, no YAML
// highlighting and no bracket matching for the one document in the app that
// is nothing but hand-written YAML and shell. Used for both create (POST) and
// edit (PUT) — jobs.js's view/edit closure pattern. (S11)
function editorPanel(title, initial, onSave, refresh) {
    const wrap = el('div', { class: 'adaptive-card' });
    wrap.appendChild(el('div', { class: 'adaptive-card-head' }, [text(title)]));

    const host = el('div', { class: 'adaptive-editor-host' });
    wrap.appendChild(host);

    const result = el('div');
    const setResult = (msg, isErr) => {
        clear(result);
        if (msg) result.appendChild(resultLine(msg, isErr));
    };

    const actions = el('div', { class: 'adaptive-card-actions' });
    const save = el('button', { class: 'adaptive-btn' }, [text('Save')]);
    const doSave = async () => {
        if (!_editor) return;
        save.disabled = true;
        setResult('Saving\u2026', false);
        try {
            const res = await onSave(_editor.getValue());
            _editorDirty = false;
            if (res && res.warnings && res.warnings.length) {
                setActionNotice(`Saved, with warnings: ${res.warnings.join(' \u00b7 ')}`, false);
            }
            await refresh();
        } catch (e) {
            setResult(`Save failed: ${e.message || e}`, true);
            save.disabled = false;
        }
    };
    save.addEventListener('click', doSave);

    const cancel = el('button', { class: 'adaptive-btn' }, [text('Cancel')]);
    cancel.addEventListener('click', () => { if (guardEditor()) refresh(); });
    actions.appendChild(save);
    actions.appendChild(cancel);
    wrap.appendChild(actions);
    wrap.appendChild(result);

    disposeEditor();
    createCodeEditor(host, initial, 'markdown', (value) => {
        _editorDirty = value !== initial;
    }).then(inst => {
        _editor = inst;
        inst.addSaveCommand(doSave);
        inst.focus();
    });

    return wrap;
}

export async function renderCanaryTab(container) {
    disposeEditor();
    clear(container);
    const refresh = () => renderCanaryTab(container);

    let suite, runsRes;
    try {
        [suite, runsRes] = await Promise.all([
            get('/api/canary'),
            get('/api/canary/runs?limit=30'),
        ]);
    } catch (e) {
        container.appendChild(el('div', { class: 'adaptive-empty' }, [text(`Canary suite unavailable: ${e.message || e}`)]));
        return;
    }

    container.appendChild(tabGlossary(
        'Canaries are fixed tasks with pass/fail checks the agent runs headlessly '
        + '\u2014 the suite that catches it quietly getting worse.',
    ));

    const head = el('div', { class: 'adaptive-head' }, [
        badge(suite.enabled ? 'enabled' : 'disabled', suite.enabled ? 'ok' : 'off'),
        badge(`heartbeat ${suite.heartbeat_per_night || 2}/night · ${suite.schedule}`),
        el('button', {
            class: 'adaptive-btn',
            title: 'Reload the suite and its recent runs',
            'aria-label': 'Refresh the Canary tab',
            // Refresh rebuilds the tab, which throws away an open editor.
            onClick: () => { if (guardEditor()) refresh(); },
        }, [text('↻ Refresh')]),
    ]);
    if (suite.enabled) {
        head.appendChild(await actionBtn('▶ Run all (incl. parked)', () => post('/api/canary/run', { name: '*' }), refresh));
    }
    const newBtn = el('button', { class: 'adaptive-btn' }, [text('+ New canary')]);
    const probeBtn = el('button', { class: 'adaptive-btn' }, [text('+ One-off probe')]);
    head.appendChild(newBtn);
    head.appendChild(probeBtn);
    container.appendChild(head);
    const notice = takeActionNotice();
    if (notice) container.appendChild(notice);

    const editorSlot = el('div');
    container.appendChild(editorSlot);
    const openCreate = (template, title) => {
        if (!guardEditor()) return;
        clear(editorSlot);
        editorSlot.appendChild(editorPanel(title, template, (raw) => post('/api/canary', { raw }), refresh));
    };
    newBtn.addEventListener('click', () => openCreate(CANARY_TEMPLATE, 'New canary (raw CANARY.md)'));
    probeBtn.addEventListener('click', () => openCreate(PROBE_TEMPLATE, 'One-off probe — runs, reports, retires itself'));

    // --- Suite table ---
    const canaries = suite.canaries || [];
    const parkedCount = canaries.filter(c => c.parked).length;
    container.appendChild(el('div', { class: 'adaptive-section-title' }, [
        text(`Suite (${canaries.length} task${canaries.length === 1 ? '' : 's'}${parkedCount ? `, ${parkedCount} parked` : ''})`),
    ]));
    if (!canaries.length) {
        container.appendChild(el('div', { class: 'adaptive-empty' }, [text('No canaries in data/canaries/.')]));
    }
    for (const c of canaries) {
        const s = c.stats || { runs: 0, passed: 0, last_run: null };
        const rate = s.runs ? `${Math.round((s.passed / s.runs) * 100)}% of ${s.runs}` : 'no runs';
        const last = s.last_run
            ? `${s.last_run.passed ? 'PASS' : (s.last_run.outcome || 'FAIL')} ${relTime(s.last_run.created_at)} (${s.last_run.trigger})`
            : '—';
        const isProbe = c.max_runs > 0 || c.expires;
        const row = el('div', { class: 'adaptive-card entry' });
        const headRow = el('div', { class: 'adaptive-card-head' }, [
            badge(rate, s.runs && s.passed === s.runs ? 'ok' : s.runs && s.passed < s.runs ? 'warn' : ''),
            ...(c.parked ? [badge('parked', 'off')] : []),
            ...(c.flaky ? [badge('flaky', 'warn')] : []),
            ...(c.tags && c.tags.includes('sentinel') ? [badge('sentinel')] : []),
            ...(isProbe ? [badge(c.max_runs > 0 ? `probe ${Math.min(s.runs, c.max_runs)}/${c.max_runs}` : `probe until ${c.expires}`)] : []),
            text(` ${c.name}`),
        ]);
        row.appendChild(headRow);
        row.appendChild(el('div', { class: 'adaptive-entry-content' }, [
            text(`gates: ${(c.gates || []).join(', ')} · tags: ${(c.tags || []).join(', ') || '—'}`
                + `${(c.covers || []).length ? ` · covers: ${c.covers.join(', ')}` : ''}`
                + ` · last run: ${last} · reviewed: ${c.last_reviewed || '?'}`),
        ]));
        const btns = el('div', { class: 'adaptive-card-actions' });
        if (suite.enabled) {
            btns.appendChild(await actionBtn('▶ Run', () => post('/api/canary/run', { name: c.name }), refresh));
        }
        btns.appendChild(await actionBtn(
            c.parked ? 'Unpark' : 'Park',
            () => patch(`/api/canary/${encodeURIComponent(c.name)}`, { parked: !c.parked }),
            refresh,
        ));
        const editBtn = el('button', {
            class: 'adaptive-btn',
            'aria-label': `Edit the canary ${c.name}`,
        }, [text('Edit')]);
        editBtn.addEventListener('click', async () => {
            if (!guardEditor()) return;
            editBtn.disabled = true;
            try {
                const full = await get(`/api/canary/${encodeURIComponent(c.name)}`);
                clear(editorSlot);
                editorSlot.appendChild(editorPanel(
                    `Edit ${c.name}`,
                    full.raw_content || '',
                    (raw) => put(`/api/canary/${encodeURIComponent(c.name)}`, { raw }),
                    refresh,
                ));
                editorSlot.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            } catch (e) {
                clear(editorSlot);
                editorSlot.appendChild(resultLine(`Could not load ${c.name}: ${e.message || e}`, true));
            }
            editBtn.disabled = false;
        });
        btns.appendChild(editBtn);
        btns.appendChild(await actionBtn('Reviewed ✓', () => post(`/api/canary/${encodeURIComponent(c.name)}/reviewed`), refresh));
        const delBtn = el('button', {
            class: 'adaptive-btn',
            'aria-label': `Retire the canary ${c.name}`,
        }, [text('Retire')]);
        delBtn.addEventListener('click', async () => {
            if (!confirm(`Retire the canary "${c.name}" \u2014 it moves to .retired/ for a grace window, so this can be rolled back.`)) return;
            delBtn.disabled = true;
            try {
                await del(`/api/canary/${encodeURIComponent(c.name)}`);
            } catch (e) {
                setActionNotice(`Retire failed: ${e.message || e}`, true);
            }
            await refresh();
        });
        btns.appendChild(delBtn);
        row.appendChild(btns);
        container.appendChild(row);
    }

    // --- Recent runs ---
    const runs = runsRes.runs || [];
    container.appendChild(el('div', { class: 'adaptive-section-title' }, [text(`Recent runs (${runs.length})`)]));
    for (const r of runs) {
        const row = el('div', { class: 'adaptive-event-row' });
        const headEl = el('div', { class: 'adaptive-event-head' }, [
            outcomeBadge(r),
            badge(r.trigger),
            text(` ${r.task} · ${relTime(r.created_at)} · ${Math.round(r.duration_s || 0)}s · ${(r.tokens || 0).toLocaleString()} tok${r.retries ? ` · ${r.retries} retr${r.retries === 1 ? 'y' : 'ies'}` : ''}${r.batch_id ? ` · ${r.batch_id}` : ''}`),
        ]);
        const detail = el('pre', { class: 'adaptive-diff', style: 'display:none' });
        let gates = [];
        try { gates = JSON.parse(r.gate_results_json || '[]'); } catch (_e) { /* leave empty */ }
        const gateText = gates.length
            ? gates.map(g => `${g.passed ? '✓' : '✗'} ${g.name}: ${g.command}\n${(g.output_tail || '').slice(-400)}`).join('\n\n')
            : '(no gate detail recorded)';
        detail.textContent = r.error ? `error: ${r.error}\n\n${gateText}` : gateText;
        headEl.addEventListener('click', () => {
            detail.style.display = detail.style.display === 'none' ? 'block' : 'none';
        });
        row.appendChild(headEl);
        row.appendChild(detail);
        container.appendChild(row);
    }
    if (!runs.length) container.appendChild(el('div', { class: 'adaptive-empty' }, [text('No recorded runs yet.')]));
}
