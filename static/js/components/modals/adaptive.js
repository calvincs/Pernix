// Pernix — Adaptive tab (Explorer): entries, proposals, batches, event journal.
// Read/govern surface for the machine-curated policy store (plan 4f):
// approve/reject proposals (approve = apply), roll back batches/events,
// dismiss tripwire flags.

import { el, text, clear } from '../../render.js';
import { del, get, post } from '../../api.js';
import { makeDisclosure, resultLine, tabGlossary } from './telos.js';

// Every action here ends in a refresh() that rebuilds the whole tab, so an
// inline line written before it would be wiped a frame later. Park the message
// and render it at the top of the next pass instead of firing an alert(). (S11)
let _pendingNotice = null;

export function setActionNotice(message, isError = false) {
    _pendingNotice = message ? { message, isError } : null;
}

export function takeActionNotice() {
    const notice = _pendingNotice;
    _pendingNotice = null;
    return notice ? resultLine(notice.message, notice.isError) : null;
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

function section(title) {
    return el('div', { class: 'adaptive-section-title' }, [text(title)]);
}

function badge(label, cls = '') {
    return el('span', { class: `adaptive-badge ${cls}` }, [text(label)]);
}

export async function actionBtn(label, fn, refresh) {
    const btn = el('button', { class: 'adaptive-btn' }, [text(label)]);
    btn.addEventListener('click', async () => {
        btn.disabled = true;
        try {
            await fn();
        } catch (e) {
            setActionNotice(`Action failed: ${e.message || e}`, true);
        }
        await refresh();
    });
    return btn;
}

export async function renderAdaptiveTab(container) {
    clear(container);
    const refresh = () => renderAdaptiveTab(container);

    let entriesRes, proposalsRes, batchesRes, eventsRes;
    try {
        [entriesRes, proposalsRes, batchesRes, eventsRes] = await Promise.all([
            get('/api/adaptive/entries?status='),
            get('/api/adaptive/proposals?status=pending'),
            get('/api/adaptive/batches'),
            get('/api/adaptive/events?limit=50'),
        ]);
    } catch (e) {
        container.appendChild(el('div', { class: 'adaptive-empty' }, [text(`Adaptive layer unavailable: ${e.message || e}`)]));
        return;
    }

    container.appendChild(tabGlossary(
        'Rules the agent writes about itself — routing hints and prompt notes it '
        + 'may apply on its own, everything else waiting for your approval, and a '
        + 'one-click rollback for all of it.',
    ));

    const head = el('div', { class: 'adaptive-head' }, [
        badge(entriesRes.enabled ? 'enabled' : 'disabled', entriesRes.enabled ? 'ok' : 'off'),
        badge(entriesRes.auto_apply ? 'auto-apply on' : 'auto-apply off', entriesRes.auto_apply ? 'ok' : 'warn'),
        el('button', {
            class: 'adaptive-btn',
            title: 'Reload entries, proposals, batches and the journal',
            'aria-label': 'Refresh the Adaptive tab',
            onClick: refresh,
        }, [text('↻ Refresh')]),
    ]);
    container.appendChild(head);
    const notice = takeActionNotice();
    if (notice) container.appendChild(notice);

    // --- Pending proposals (approve = apply) ---
    const proposals = proposalsRes.proposals || [];
    container.appendChild(section(`Proposals awaiting review (${proposals.length})`));
    if (!proposals.length) {
        container.appendChild(el('div', { class: 'adaptive-empty' }, [text('No pending proposals.')]));
    }
    for (const p of proposals) {
        let edits = [];
        try { edits = JSON.parse(p.payload_json || '[]'); } catch (_e) { /* render rationale only */ }
        const row = el('div', { class: 'adaptive-card proposal' });
        row.appendChild(el('div', { class: 'adaptive-card-head' }, [
            badge(p.producer), text(` #${p.id} · ${relTime(p.created_at)} — ${p.rationale || ''}`),
        ]));
        for (const ed of edits) {
            row.appendChild(el('div', { class: 'adaptive-edit-line' }, [
                badge(ed.kind), badge(ed.action),
                text(` ${ed.title || ed.entry_id || ''}: ${ed.content || ''}`),
            ]));
        }
        const btns = el('div', { class: 'adaptive-card-actions' });
        btns.appendChild(await actionBtn(edits.length ? 'Approve & apply' : 'Acknowledge', async () => {
            await post(`/api/adaptive/proposals/${p.id}/approve`, {});
        }, refresh));
        btns.appendChild(await actionBtn('Reject', async () => {
            await post(`/api/adaptive/proposals/${p.id}/reject`, {});
        }, refresh));
        row.appendChild(btns);
        container.appendChild(row);
    }

    // --- Active entries by kind ---
    const entries = (entriesRes.entries || []).filter(e => e.status === 'active');
    const entriesHead = el('div', { class: 'adaptive-head' }, []);
    const addBtn = el('button', { class: 'adaptive-btn' }, [text('+ New entry')]);
    entriesHead.appendChild(addBtn);
    container.appendChild(section(`Active entries (${entries.length})`));
    container.appendChild(entriesHead);
    const formSlot = el('div');
    container.appendChild(formSlot);
    addBtn.addEventListener('click', () => {
        clear(formSlot);
        const kindSel = el('select', { class: 'adaptive-input' }, []);
        for (const k of ['prompt_note', 'routing_hint', 'policy']) {
            kindSel.appendChild(el('option', { value: k }, [text(k)]));
        }
        const titleIn = el('input', { class: 'adaptive-input', placeholder: 'short stable title (becomes the id)' });
        const contentIn = el('textarea', {
            class: 'adaptive-input',
            placeholder: 'the instruction — what to do and when',
            style: { width: '100%', minHeight: '80px' },
        });
        // The form stays open on failure, so its result belongs IN the form —
        // where the text the user has to fix still is. (S11)
        const formResult = el('div');
        const save = el('button', { class: 'adaptive-btn' }, [text('Create')]);
        save.addEventListener('click', async () => {
            save.disabled = true;
            clear(formResult);
            try {
                await post('/api/adaptive/entries', { kind: kindSel.value, title: titleIn.value, content: contentIn.value });
                setActionNotice(`Entry "${titleIn.value}" created`);
                await refresh();
            } catch (err) {
                formResult.appendChild(resultLine(`Create failed: ${err.message || err}`, true));
                save.disabled = false;
            }
        });
        const cancel = el('button', { class: 'adaptive-btn' }, [text('Cancel')]);
        cancel.addEventListener('click', () => {
            const typed = titleIn.value.trim() || contentIn.value.trim();
            if (typed && !confirm('Discard this unsaved entry?')) return;
            refresh();
        });
        formSlot.appendChild(el('div', { class: 'adaptive-card' }, [
            el('div', { class: 'adaptive-card-head' }, [text('New adaptive entry (yours — applies immediately, journaled)')]),
            kindSel, titleIn, contentIn,
            el('div', { class: 'adaptive-card-actions' }, [save, cancel]),
            formResult,
        ]));
    });
    const byKind = {};
    for (const e of entries) (byKind[e.kind] = byKind[e.kind] || []).push(e);
    for (const kind of Object.keys(byKind).sort()) {
        container.appendChild(el('div', { class: 'adaptive-kind-head' }, [text(kind)]));
        for (const e of byKind[kind]) {
            // Release valve: a soft delete frees the per-kind cap that
            // producers can only ever fill. Journaled, so it rolls back.
            const rm = await actionBtn('Delete', async () => {
                if (!confirm(`Delete the adaptive entry "${e.title}" \u2014 it is journaled, so this can be rolled back.`)) return;
                await del(`/api/adaptive/entries/${encodeURIComponent(e.id)}`);
            }, refresh);
            // Usage badge: the per-entry usefulness signal. Zero-use is the
            // highlighted state — those are the retirement sweep's targets.
            const u = e.usage;
            const usageBadge = u
                ? badge(`used ${u.uses}${u.successes ? ` · ✓${u.successes}` : ''}${u.failures ? ` · ✗${u.failures}` : ''}`, 'ok')
                : badge('unused', 'off');
            container.appendChild(el('div', { class: 'adaptive-card entry' }, [
                el('div', { class: 'adaptive-card-head' }, [
                    badge(`v${e.version}`), badge(e.risk, e.risk === 'high' ? 'warn' : ''), badge(e.source),
                    usageBadge,
                    text(` ${e.title}`),
                ]),
                el('div', { class: 'adaptive-entry-content' }, [text(e.content)]),
                el('div', { class: 'adaptive-card-actions' }, [rm]),
            ]));
        }
    }
    if (!entries.length) container.appendChild(el('div', { class: 'adaptive-empty' }, [text('No active entries.')]));

    // --- Batches ---
    const batches = (batchesRes.batches || []).filter(b => b.status !== 'pending');
    const pendingBatches = (batchesRes.batches || []).filter(b => b.status === 'pending');
    container.appendChild(section(`Batches (${batches.length} settled, ${pendingBatches.length} pending idle apply)`));
    for (const b of batches.slice(0, 20)) {
        const row = el('div', { class: `adaptive-card batch ${b.status}` });
        row.appendChild(el('div', { class: 'adaptive-card-head' }, [
            badge(b.status, b.status === 'suspect' ? 'warn' : (b.status === 'rolled_back' || b.status === 'rejected') ? 'off' : 'ok'),
            badge(b.producer),
            text(` ${b.batch_id} · ${relTime(b.created_at)}`),
        ]));
        if (b.flagged_reason) {
            row.appendChild(el('div', { class: 'adaptive-flag-reason' }, [text(`⚠ ${b.flagged_reason}`)]));
        }
        const btns = el('div', { class: 'adaptive-card-actions' });
        if (b.status === 'applied' || b.status === 'suspect') {
            btns.appendChild(await actionBtn('Roll back', async () => {
                if (!confirm(`Roll back batch ${b.batch_id} \u2014 its entries restore to their pre-batch snapshots, and this is itself journaled.`)) return;
                await post('/api/adaptive/rollback', { batch_id: b.batch_id });
            }, refresh));
        }
        if (b.status === 'suspect') {
            btns.appendChild(await actionBtn('Dismiss flag', async () => {
                await post(`/api/adaptive/batches/${b.batch_id}/dismiss`, {});
            }, refresh));
        }
        row.appendChild(btns);
        container.appendChild(row);
    }

    // --- Event journal (before/after on expand) ---
    const events = eventsRes.events || [];
    container.appendChild(section(`Event journal (last ${events.length})`));
    for (const ev of events) {
        const row = el('div', { class: 'adaptive-event-row' });
        const head = el('div', { class: 'adaptive-event-head' }, [
            badge(`#${ev.id}`), badge(ev.action, ev.action === 'rollback' ? 'warn' : ''), badge(ev.actor || '?'),
            text(` ${ev.entry_id} · ${relTime(ev.created_at)}${ev.batch_id ? ` · ${ev.batch_id}` : ''}`),
        ]);
        const diff = el('pre', { class: 'adaptive-diff', style: 'display:none' });
        const fmt = (j) => { try { return JSON.stringify(JSON.parse(j), null, 1); } catch (_e) { return j || '(none)'; } };
        diff.textContent = `BEFORE:\n${fmt(ev.before_json)}\n\nAFTER:\n${fmt(ev.after_json)}\n\nEVIDENCE: ${ev.evidence_json || '[]'}`;
        makeDisclosure(
            head,
            () => diff.style.display !== 'none',
            () => { diff.style.display = diff.style.display === 'none' ? 'block' : 'none'; },
        );
        head.setAttribute('aria-label', `Event ${ev.id}: ${ev.action} on ${ev.entry_id}`);
        row.appendChild(head);
        row.appendChild(diff);
        container.appendChild(row);
    }
}
