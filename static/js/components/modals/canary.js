// Pernix — Canary tab (Explorer): suite table, pass rates, run triggers,
// recent runs. Closes the loop from the Adaptive tab's tripwire flags —
// a flagged batch cites canary regressions; this is where you read them.

import { el, text, clear } from '../../render.js';
import { get, post } from '../../api.js';

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

export async function renderCanaryTab(container) {
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

    const head = el('div', { class: 'adaptive-head' }, [
        badge(suite.enabled ? 'enabled' : 'disabled', suite.enabled ? 'ok' : 'off'),
        badge(`schedule ${suite.schedule}`),
        el('button', { class: 'adaptive-btn', onClick: refresh }, [text('↻ Refresh')]),
    ]);
    if (suite.enabled) {
        const runAll = el('button', { class: 'adaptive-btn' }, [text('▶ Run all')]);
        runAll.addEventListener('click', async () => {
            runAll.disabled = true;
            try { await post('/api/canary/run', { name: '*' }); } catch (e) { alert(`Run failed: ${e.message || e}`); }
            runAll.textContent = 'Queued ✓';
        });
        head.appendChild(runAll);
    }
    container.appendChild(head);

    // --- Suite table ---
    const canaries = suite.canaries || [];
    container.appendChild(el('div', { class: 'adaptive-section-title' }, [text(`Suite (${canaries.length} task${canaries.length === 1 ? '' : 's'})`)]));
    if (!canaries.length) {
        container.appendChild(el('div', { class: 'adaptive-empty' }, [text('No canaries in data/canaries/.')]));
    }
    for (const c of canaries) {
        const s = c.stats || { runs: 0, passed: 0, last_run: null };
        const rate = s.runs ? `${Math.round((s.passed / s.runs) * 100)}% of ${s.runs}` : 'no runs';
        const last = s.last_run
            ? `${s.last_run.passed ? 'PASS' : 'FAIL'} ${relTime(s.last_run.created_at)} (${s.last_run.trigger})`
            : '—';
        const row = el('div', { class: 'adaptive-card entry' });
        const headRow = el('div', { class: 'adaptive-card-head' }, [
            badge(rate, s.runs && s.passed === s.runs ? 'ok' : s.runs && s.passed < s.runs ? 'warn' : ''),
            ...(c.flaky ? [badge('flaky', 'warn')] : []),
            text(` ${c.name}`),
        ]);
        row.appendChild(headRow);
        row.appendChild(el('div', { class: 'adaptive-entry-content' }, [
            text(`gates: ${(c.gates || []).join(', ')} · tags: ${(c.tags || []).join(', ') || '—'} · last run: ${last} · reviewed: ${c.last_reviewed || '?'}`),
        ]));
        if (suite.enabled) {
            const btns = el('div', { class: 'adaptive-card-actions' });
            const runBtn = el('button', { class: 'adaptive-btn' }, [text('▶ Run')]);
            runBtn.addEventListener('click', async () => {
                runBtn.disabled = true;
                try { await post('/api/canary/run', { name: c.name }); runBtn.textContent = 'Queued ✓'; }
                catch (e) { alert(`Run failed: ${e.message || e}`); runBtn.disabled = false; }
            });
            btns.appendChild(runBtn);
            row.appendChild(btns);
        }
        container.appendChild(row);
    }

    // --- Recent runs ---
    const runs = runsRes.runs || [];
    container.appendChild(el('div', { class: 'adaptive-section-title' }, [text(`Recent runs (${runs.length})`)]));
    for (const r of runs) {
        const row = el('div', { class: 'adaptive-event-row' });
        const headEl = el('div', { class: 'adaptive-event-head' }, [
            badge(r.passed ? 'PASS' : 'FAIL', r.passed ? 'ok' : 'warn'),
            badge(r.trigger),
            text(` ${r.task} · ${relTime(r.created_at)} · ${Math.round(r.duration_s || 0)}s · ${(r.tokens || 0).toLocaleString()} tok${r.retries ? ` · ${r.retries} retr${r.retries === 1 ? 'y' : 'ies'}` : ''}${r.batch_id ? ` · ${r.batch_id}` : ''}`),
        ]);
        const detail = el('pre', { class: 'adaptive-diff', style: 'display:none' });
        let gates = [];
        try { gates = JSON.parse(r.gate_results_json || '[]'); } catch (_e) { /* leave empty */ }
        detail.textContent = gates.length
            ? gates.map(g => `${g.passed ? '✓' : '✗'} ${g.name}: ${g.command}\n${(g.output_tail || '').slice(-400)}`).join('\n\n')
            : '(no gate detail recorded)';
        headEl.addEventListener('click', () => {
            detail.style.display = detail.style.display === 'none' ? 'block' : 'none';
        });
        row.appendChild(headEl);
        row.appendChild(detail);
        container.appendChild(row);
    }
    if (!runs.length) container.appendChild(el('div', { class: 'adaptive-empty' }, [text('No recorded runs yet.')]));
}
