// Pernix — shared helpers for the Explorer's self-tuning tabs (Self-checks,
// Trust): a plain-words header line, accessible disclosure rows,
// inline result lines, and action buttons whose outcome survives the
// refresh() that rebuilds the tab.

import { el, text } from '../../render.js';

/**
 * One plain-words line under a tab header, in the shape file-panel.js's
 * _buildTabDesc gives every other Explorer tab. Canary (and the Adaptive
 * tab retired in 3.2) opened straight into badges and vocabulary ("tripwire") with nothing
 * anywhere saying what the tab is FOR. Shared from here because every
 * self-tuning tab needs the same treatment. (S11)
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
