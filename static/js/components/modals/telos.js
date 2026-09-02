// Pernix — Telos tab (Explorer): the question loop's state — root question,
// question/hypothesis pipeline, claims, alarms, and exploration
// temperature. Read-heavy by design: the only controls are the manual
// slow-loop trigger and alarm acknowledgement.

import { el, text, clear } from '../../render.js';
import { get, post } from '../../api.js';

function relTime(isoStr) {
    if (!isoStr) return '';
    let s = String(isoStr).replace(/\+00:00$/, 'Z');
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

function sectionTitle(label) {
    return el('div', { class: 'adaptive-section-title' }, [text(label)]);
}

/**
 * One plain-words line under a tab header, in the shape file-panel.js's
 * _buildTabDesc gives every other Explorer tab. Telos, Canary and Adaptive
 * opened straight into badges and vocabulary ("acedia signature", "EIG floor",
 * "tripwire") with nothing anywhere saying what the tab is FOR. Shared from
 * here because all three need the same treatment. (S11)
 */
export function tabGlossary(line) {
    return el('div', { class: 'fp-tab-desc' }, [
        el('div', { class: 'fp-tab-desc-brief' }, [el('span', {}, [text(line)])]),
    ]);
}

/**
 * Turn a <div> that toggles a detail block into a real disclosure control:
 * a tab stop, an announced role, and Enter/Space. Every expandable row on
 * these tabs was mouse-only. (A1)
 */
export function makeDisclosure(headerEl, isExpanded, toggle) {
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

// Inline result line, same shape as the MCP add form's — alert() steals focus,
// cannot be read next to the thing it is about, and is unreachable to anything
// that renders the tab in the background.
export function resultLine(message, isError = false) {
    return el('div', {
        class: `adaptive-result${isError ? ' err' : ''}`,
        role: isError ? 'alert' : 'status',
    }, [text(message)]);
}

function countLine(obj) {
    return Object.entries(obj || {})
        .map(([k, v]) => `${v} ${k}`)
        .join(' · ') || 'none';
}

export async function renderTelosTab(container) {
    clear(container);
    const refresh = () => renderTelosTab(container);

    let overview;
    try {
        overview = await get('/api/telos');
    } catch (e) {
        container.appendChild(el('div', { class: 'adaptive-empty' }, [text(`Telos unavailable: ${e.message || e}`)]));
        return;
    }

    container.appendChild(tabGlossary(
        'Telos is the question loop: what the agent noticed it cannot explain, '
        + 'the guesses it is testing about it, and what it has concluded.',
    ));

    const resultSlot = el('div');
    const head = el('div', { class: 'adaptive-head' }, [
        badge(overview.enabled ? 'enabled' : 'disabled', overview.enabled ? 'ok' : 'off'),
        el('button', {
            class: 'adaptive-btn',
            title: 'Reload the Telos state',
            'aria-label': 'Refresh the Telos tab',
            onClick: refresh,
        }, [text('↻ Refresh')]),
    ]);
    if (overview.enabled) {
        head.appendChild(badge(`slow loops ${overview.schedule}`));
        const runBtn = el('button', {
            class: 'adaptive-btn',
            title: 'Run the slow loops now instead of waiting for the schedule',
            'aria-label': 'Run the Telos slow loops now',
        }, [text('▶ Run slow loops')]);
        runBtn.addEventListener('click', async () => {
            runBtn.disabled = true;
            clear(resultSlot);
            try {
                await post('/api/telos/run', {});
                runBtn.textContent = 'Queued ✓';
            } catch (e) {
                resultSlot.appendChild(resultLine(`Run failed: ${e.message || e}`, true));
                runBtn.disabled = false;
            }
        });
        head.appendChild(runBtn);
    }
    container.appendChild(head);
    container.appendChild(resultSlot);

    if (!overview.enabled) {
        container.appendChild(el('div', { class: 'adaptive-empty' }, [
            text('Telos is off. Enable telos_enabled in Settings (tools need a restart) to start the teleological layer.'),
        ]));
        return;
    }

    // --- Root ---
    if (overview.root) {
        container.appendChild(sectionTitle('Root question (no satisfaction predicate)'));
        container.appendChild(el('div', { class: 'adaptive-entry' }, [
            el('div', { class: 'adaptive-entry-title' }, [text(overview.root.text || '')]),
            el('div', { class: 'adaptive-entry-meta' }, [
                text('re-expression requires operator co-sign · never completed, only re-expressed'),
            ]),
        ]));
    }

    // --- Alarms ---
    const alarms = overview.alarms_open || [];
    container.appendChild(sectionTitle(`Open alarms (${alarms.length})`));
    if (!alarms.length) {
        container.appendChild(el('div', { class: 'adaptive-empty' }, [text('No open alarms — no acedia signature holding (exploration entropy is above floor).')]));
    } else {
        alarms.forEach(a => {
            const row = el('div', { class: 'adaptive-entry' }, [
                el('div', { class: 'adaptive-entry-title' }, [
                    badge(`${a.type} L${a.level}`, 'warn'),
                    text(` ${a.target} `),
                    badge(relTime(a.updated_at)),
                ]),
                el('div', { class: 'adaptive-entry-meta' }, [text(JSON.stringify(a.evidence || {}))]),
            ]);
            const ack = el('button', {
                class: 'adaptive-btn',
                'aria-label': `Acknowledge the ${a.type} alarm on ${a.target}`,
            }, [text('Acknowledge')]);
            const ackResult = el('div');
            ack.addEventListener('click', async () => {
                ack.disabled = true;
                clear(ackResult);
                try {
                    await post(`/api/telos/alarms/${a.id}/ack`, {});
                    refresh();
                } catch (e) {
                    ackResult.appendChild(resultLine(`Ack failed: ${e.message || e}`, true));
                    ack.disabled = false;
                }
            });
            // Wrapped rather than appended bare, so the button picks up the
            // shared action-row spacing instead of butting against the meta line.
            row.appendChild(el('div', { class: 'adaptive-card-actions' }, [ack]));
            row.appendChild(ackResult);
            container.appendChild(row);
        });
    }

    // --- Pipeline counts ---
    container.appendChild(sectionTitle('Fast loop'));
    container.appendChild(el('div', { class: 'adaptive-entry' }, [
        el('div', { class: 'adaptive-entry-meta' }, [text(`Questions: ${countLine(overview.questions)} (${overview.serendipity_open || 0} serendipity open)`)]),
        // Live statuses only — terminal ones (untestable | expired) are moved
        // to soup/archive/ and out of every store scan, so they are counted
        // separately rather than silently missing from the pipeline line.
        el('div', { class: 'adaptive-entry-meta' }, [text(`Hypotheses: ${countLine(overview.hypotheses)}${overview.hypotheses_archived ? ` · ${overview.hypotheses_archived} archived` : ''}`)]),
        el('div', { class: 'adaptive-entry-meta' }, [text(`Claims committed: ${overview.claims || 0}`)]),
        el('div', { class: 'adaptive-entry-meta' }, [
            text(`Band mix near/mid/far: ${(overview.band_mix?.near ?? 0).toFixed(2)} / ${(overview.band_mix?.mid ?? 0).toFixed(2)} / ${(overview.band_mix?.far ?? 0).toFixed(2)} · serendipity budget ${(overview.serendipity_budget ?? 0).toFixed(2)}`),
        ]),
    ]));

    // --- Recent questions + hypotheses ---
    let qs, hs;
    try {
        [qs, hs] = await Promise.all([
            get('/api/telos/questions?limit=8'),
            get('/api/telos/hypotheses?limit=8'),
        ]);
    } catch (e) { qs = { questions: [] }; hs = { hypotheses: [] }; }

    container.appendChild(sectionTitle('Recent questions'));
    if (!(qs.questions || []).length) {
        container.appendChild(el('div', { class: 'adaptive-empty' }, [text('No questions yet — anomalies mint them at turn end; telos_ask mints them on demand.')]));
    } else {
        qs.questions.forEach(q => {
            container.appendChild(el('div', { class: 'adaptive-entry' }, [
                el('div', { class: 'adaptive-entry-title' }, [
                    badge(q.state, q.state === 'open' ? 'ok' : ''),
                    badge(q.origin || ''),
                    text(` ${q.text}`),
                ]),
                el('div', { class: 'adaptive-entry-meta' }, [text(`${q.id} · surprise ${q.surprise} · ${relTime(q.created_at)}`)]),
            ]));
        });
    }

    container.appendChild(sectionTitle('Recent hypotheses'));
    if (!(hs.hypotheses || []).length) {
        container.appendChild(el('div', { class: 'adaptive-empty' }, [text('No hypotheses yet — the SOUP generates them at idle.')]));
    } else {
        hs.hypotheses.forEach(h => {
            const cls = h.status === 'supported' ? 'ok' : (h.status === 'refuted' ? 'warn' : '');
            container.appendChild(el('div', { class: 'adaptive-entry' }, [
                el('div', { class: 'adaptive-entry-title' }, [
                    badge(h.status, cls),
                    badge(`${h.band}-band`),
                    text(` ${h.statement}`),
                ]),
                el('div', { class: 'adaptive-entry-meta' }, [
                    text(`${h.id} · q=${h.question} · eig ${h.eig} · ${(h.mapping && h.mapping.source_domain) ? 'from: ' + h.mapping.source_domain : ''}`),
                ]),
            ]));
        });
    }

}
