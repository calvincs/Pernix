// Pernix — the shared "are you sure?" dialog.
//
// Generalised out of the space-delete dialog, which was the only destructive
// action in the app that actually stopped and asked. Everything else either
// went straight through (the composer's /clear) or used a three-second
// arm-and-fire swap on the button itself — a pattern that cannot say WHAT is
// about to be destroyed, cannot be reached from the keyboard in any obvious
// way, and silently disarms while the user is reading it.
//
// The dialog is deliberately biased towards not destroying anything: the
// cancel button is what gets focus, Escape cancels, a backdrop click cancels,
// and the confirming button is the one you have to travel to.
//
// Styling reuses the shared .modal-* / .btn classes; the only rule of its own
// is the card width, which lives in layout.css.
import { el, text } from '../../render.js';
import { icon } from '../../icons.js';
import { announce, openOverlay } from '../../a11y.js';

let _openConfirm = null;   // the live instance — one at a time

/**
 * Ask before doing something destructive.
 *
 * @param {object}   opts
 * @param {string}   opts.title        the question, e.g. 'Delete this session?'
 * @param {string|string[]|Node} opts.body  what will happen. A string[] becomes
 *        one paragraph per entry; a Node is inserted as-is.
 * @param {string}   [opts.verb='Delete']   label of the destructive button.
 * @param {string}   [opts.cancelLabel='Keep'] label of the safe button, which
 *        is the one that receives focus.
 * @param {Node}     [opts.extra]      an extra control under the body — the
 *        space dialog's cascade checkbox. Its state is the caller's to read
 *        after the promise resolves.
 * @returns {Promise<boolean>} true if the user confirmed, false for cancel,
 *        Escape, the ×, or a backdrop click. Resolves exactly once.
 */
export function confirmDanger({ title, body, verb = 'Delete', cancelLabel = 'Keep', extra = null } = {}) {
    // A second call supersedes the first rather than stacking dialogs; the
    // superseded one resolves false so its caller is never left waiting.
    if (_openConfirm) { try { _openConfirm(); } catch { /* already gone */ } }

    return new Promise((resolve) => {
        let settled = false;
        let closeOverlay = null;

        const finish = (value) => {
            if (settled) return;
            settled = true;
            if (closeOverlay) { closeOverlay(); closeOverlay = null; }
            overlay.remove();
            if (_openConfirm === cancel) _openConfirm = null;
            resolve(value);
        };
        const cancel = () => finish(false);
        const confirm = () => finish(true);
        _openConfirm = cancel;

        const paragraphs = [];
        if (body instanceof Object && body.nodeType) paragraphs.push(body);
        else for (const line of [].concat(body || [])) paragraphs.push(el('p', {}, [text(line)]));
        if (extra) paragraphs.push(extra);

        const cancelBtn = el('button', {
            class: 'btn btn-secondary',
            type: 'button',
            onClick: cancel,
        }, [text(cancelLabel)]);

        const card = el('div', { class: 'modal-card confirm-card' }, [
            el('div', { class: 'modal-header' }, [
                el('h2', {}, [text(title)]),
                el('button', {
                    class: 'modal-close',
                    type: 'button',
                    title: cancelLabel,
                    'aria-label': cancelLabel,
                    onClick: cancel,
                }, [icon('x', { size: 14 })]),
            ]),
            el('div', { class: 'modal-body confirm-body' }, paragraphs),
            el('div', { class: 'modal-footer' }, [
                cancelBtn,
                el('button', { class: 'btn btn-danger', type: 'button', onClick: confirm }, [text(verb)]),
            ]),
        ]);

        const overlay = el('div', { class: 'modal-overlay confirm-overlay' }, [card]);

        // Only a press that STARTED on the backdrop counts. Otherwise a drag
        // that ends outside the card — selecting the text of the very warning
        // being read — dismisses the dialog.
        let downOnBackdrop = false;
        overlay.addEventListener('mousedown', (e) => { downOnBackdrop = e.target === overlay; });
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay && downOnBackdrop) cancel();
            downOnBackdrop = false;
        });

        document.body.appendChild(overlay);
        closeOverlay = openOverlay(card, { onClose: cancel, initialFocus: cancelBtn });

        // The dialog steals focus, but a screen reader is not guaranteed to
        // read the whole card on that move — say the question outright.
        const spoken = paragraphs
            .filter(p => p.tagName === 'P')
            .map(p => p.textContent)
            .join(' ');
        announce(`${title} ${spoken}`.trim(), { assertive: true });
    });
}
