// Pernix — Trust tab (Explorer): is the learning loop measuring anything real?
//
// The Self-checks tab shows the canary suite doing its job. It does not answer
// the question underneath it — how much of what the grader concludes is
// grounded in something that actually happened, and how much is the model
// agreeing with itself.
//
// So this tab is deliberately the plainest surface in the app: counts, in
// sections, with no chart anywhere. A chart would invite reading a trend into
// four data points; what these numbers are for is "is the share of grounded
// outcomes going up". Everything comes
// from one GET, and every field is optional — an older server, a subsystem
// that is off, or a table that is empty all render as zeros rather than as an
// error.
//
// Written against the API contract in docs/dev/trust-loop-hardening-plan.md
// (workstream W2); the adaptive entries and trial-arm sections went with the
// adaptive layer in 3.2. On a server without the endpoint it is a 404, and a
// 404 here is one honest sentence rather than a broken panel.

import { el, text, clear } from '../../render.js';
import { icon } from '../../icons.js';
import { get } from '../../api.js';
import { tabGlossary } from './tab-kit.js';

const MISSING_BACKEND = 'Trust metrics need the 3.2 backend';

function section(title) {
    return el('div', { class: 'adaptive-section-title' }, [text(title)]);
}

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

/** A count, however the server spelled "nothing here". */
function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
}

/**
 * A share as a percentage. Agreement and hold-out accuracy are fractions
 * (0..1); a server that has already multiplied by 100 still reads correctly
 * because nothing above 1 can be a fraction.
 */
function pct(value) {
    if (value == null || value === '') return null;
    const n = Number(value);
    if (!Number.isFinite(n)) return null;
    return `${Math.round((n <= 1 ? n * 100 : n))}%`;
}

/**
 * One measurement: what it is on the left, the number on the right, and an
 * optional quiet line underneath saying what the number is OF. The sample
 * size lives in that line rather than in the value, because a percentage
 * printed without its n is the exact mistake this tab exists to stop making.
 */
function stat(label, value, note = '') {
    return el('div', { class: 'trust-stat' }, [
        el('div', { class: 'trust-stat-row' }, [
            el('span', { class: 'trust-stat-label' }, [text(label)]),
            el('span', { class: 'trust-stat-value' }, [text(String(value))]),
        ]),
        ...(note ? [el('div', { class: 'trust-stat-note' }, [text(note)])] : []),
    ]);
}

function plural(n, one, many) {
    return `${n} ${n === 1 ? one : many}`;
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

function graderSection(grader) {
    const out = [section('Grader agreement')];
    const n = num(grader.n);
    const agreement = pct(grader.agreement);
    out.push(stat(
        'Reflect agrees with the user',
        n ? (agreement ?? '—') : '—',
        n
            ? `over ${plural(n, 'turn', 'turns')} that carry both a verdict and a thumbs`
            : 'no turn carries both a verdict and a thumbs yet',
    ));

    const holdout = grader.holdout;
    if (holdout && typeof holdout === 'object') {
        const parts = [];
        if (holdout.n != null) parts.push(`${plural(num(holdout.n), 'fixture', 'fixtures')}`);
        if (holdout.model) parts.push(String(holdout.model));
        if (holdout.ran_at) parts.push(relTime(holdout.ran_at));
        out.push(stat('Hold-out accuracy', pct(holdout.accuracy) ?? '—', parts.join(' · ')));
    }
    return out;
}

function outcomesSection(outcomes) {
    const bySource = outcomes.by_source || {};
    const user = num(bySource.user);
    const nextTurn = num(bySource.next_turn);
    const llm = num(bySource.llm);
    const graded = num(outcomes.graded_7d);
    const userTurns = num(outcomes.user_turns_7d);

    return [
        section('Where outcomes come from'),
        // In the order the plan ranks them: a thumbs outranks the next
        // message, which outranks the model's own verdict.
        stat('You said so', user, 'an explicit thumbs on the answer'),
        stat('Your next message', nextTurn, 'read as a correction, a repeat, or moving on'),
        stat('Reflect said so', llm, 'the model grading its own turn'),
        stat('Turns graded (7d)', graded, `out of ${plural(userTurns, 'turn you sent', 'turns you sent')}`),
    ];
}

function canariesSection(canaries) {
    return [
        section('Self-checks (14 days)'),
        stat('Runs', num(canaries.runs_14d)),
        stat('Failures', num(canaries.fails_14d)),
        stat(
            'Contaminated',
            num(canaries.contaminated_14d),
            'a run that reached memory or a file outside its own workspace — excluded from every measurement',
        ),
    ];
}

// ---------------------------------------------------------------------------

export async function renderTrustTab(container) {
    if (!container) return;
    clear(container);
    const refresh = () => renderTrustTab(container);

    let data;
    try {
        data = await get('/api/trust');
    } catch (e) {
        // One line, and nothing else. A 404 is not a fault to report at
        // length — it is a server that predates the trust loop.
        container.appendChild(el('div', { class: 'adaptive-empty' }, [
            text(e && e.status === 404 ? MISSING_BACKEND : `Trust metrics unavailable: ${e.message || e}`),
        ]));
        return;
    }

    container.appendChild(tabGlossary(
        'How much of the grading is grounded in something that happened: how '
        + 'often the grader agrees with you, where each turn’s verdict came from, '
        + 'and whether the self-checks stayed clean.',
    ));

    container.appendChild(el('div', { class: 'adaptive-head' }, [
        el('button', {
            class: 'adaptive-btn',
            title: 'Re-read the trust metrics',
            'aria-label': 'Refresh the Trust tab',
            onClick: refresh,
        }, [icon('refresh', { size: 12 }), text('Refresh')]),
    ]));

    for (const node of graderSection(data.grader || {})) container.appendChild(node);
    for (const node of outcomesSection(data.outcomes || {})) container.appendChild(node);
    for (const node of canariesSection(data.canaries || {})) container.appendChild(node);
}
