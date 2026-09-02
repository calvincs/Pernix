// Pernix — transient feedback (toasts).
//
// Promoted out of settings.js's private _showToast, which was the only place
// in the app that could tell the user something had happened without it
// having to be a modal or a line of text they had to already be looking at.
// Everything else either wrote into a status span in a panel that may be
// closed, or said nothing at all.
//
// Styling lives in layout.css (.toast-region / .toast) and mobile.css, and
// uses tokens only — no literals here.

import { el, text } from './render.js';
import { icon } from './icons.js';
import { announce } from './a11y.js';

const LEVELS = new Set(['info', 'success', 'warning', 'error']);
const DEFAULT_TTL = 5000;
const ERROR_TTL = 8000;

let _region = null;

function _regionEl() {
    if (_region && document.contains(_region)) return _region;
    _region = el('div', { class: 'toast-region', id: 'toast-region' });
    document.body.appendChild(_region);
    return _region;
}

/**
 * Show a transient message.
 *
 * @param {'info'|'success'|'warning'|'error'} level  anything else is treated
 *        as 'info'. Sets the accent stripe and, for 'error', role=alert plus
 *        the longer time-to-live.
 * @param {string} message
 * @param {object} [opts]
 * @param {function} [opts.action]      click handler for an inline button.
 * @param {string}   [opts.actionLabel] its label; defaults to 'Undo'. Ignored
 *        when `action` is absent.
 * @param {number}   [opts.ttl]         ms before auto-dismiss. Pass 0 to make
 *        the toast stay until dismissed. Defaults to 5000, or 8000 for errors.
 * @returns {function} dismiss() — idempotent.
 */
export function notify(level, message, { action, actionLabel, ttl } = {}) {
    const kind = LEVELS.has(level) ? level : 'info';
    const body = String(message == null ? '' : message).trim();
    if (!body) return () => {};

    const life = ttl == null ? (kind === 'error' ? ERROR_TTL : DEFAULT_TTL) : ttl;

    let timer = null;
    let gone = false;
    const dismiss = () => {
        if (gone) return;
        gone = true;
        clearTimeout(timer);
        card.remove();
        if (_region && !_region.firstChild) { _region.remove(); _region = null; }
    };

    const kids = [el('span', { class: 'toast-text' }, [text(body)])];
    if (typeof action === 'function') {
        kids.push(el('button', {
            class: 'toast-action',
            type: 'button',
            onClick: () => { dismiss(); action(); },
        }, [text(actionLabel || 'Undo')]));
    }
    kids.push(el('button', {
        class: 'toast-dismiss',
        type: 'button',
        'aria-label': 'Dismiss',
        onClick: dismiss,
    }, [icon('x', { size: 12 })]));

    const card = el('div', {
        class: `toast toast-${kind}`,
        // role on the node itself is what a live-region-aware AT reads when
        // the toast is the thing on screen; announce() below is the belt to
        // that braces, because a region created at the same moment as its
        // content is unreliable across screen readers.
        role: kind === 'error' ? 'alert' : 'status',
    }, kids);

    _regionEl().appendChild(card);
    announce(body, { assertive: kind === 'error' });

    // Hovering or focusing a toast you are still reading must not have it
    // vanish underneath you.
    if (life > 0) {
        const arm = () => { clearTimeout(timer); timer = setTimeout(dismiss, life); };
        card.addEventListener('mouseenter', () => clearTimeout(timer));
        card.addEventListener('mouseleave', arm);
        card.addEventListener('focusin', () => clearTimeout(timer));
        card.addEventListener('focusout', arm);
        arm();
    }

    return dismiss;
}
