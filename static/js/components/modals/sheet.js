// Pernix — the action sheet.
//
// A phone has no hover, so every "reveal the controls when the pointer is
// near" pattern in the app is either invisible or permanently in the way. The
// answer everywhere else on a touch device is one overflow control per row
// that opens a list of everything that row can do — one 44px target instead of
// three 32px ones, every action named in words rather than a glyph, and a
// screen reader reading a plain list of buttons.
//
// Deliberately NOT a menu: role=menu/menuitem asks a screen reader for arrow
// -key navigation the app does not implement, and openOverlay already gives a
// dialog everything this needs — a name, a focus trap, Escape, and focus put
// back where it came from. It is a .modal-card, so compact.css turns it into a
// bottom sheet on a narrow screen and modals.css centres it as a card
// everywhere else, with no work here.
//
// Callers: the session and space row menus and the move picker (sidebar.js),
// the Explorer's file and directory rows (file-panel.js), the worker strip's
// summary line and the session-header title on compact (app.js). The model
// switcher builds its own sheet rather than calling this one: its rows are a
// live, grouped, async list, not a fixed set of actions.
import { el, text } from '../../render.js';
import { icon } from '../../icons.js';
import { openOverlay } from '../../a11y.js';

let _openSheet = null;   // the live instance — one at a time

/**
 * Ask which of several things to do.
 *
 * @param {object}   opts
 * @param {string}   opts.title       what the actions are about, e.g. the
 *        session's name. Becomes the sheet's accessible name.
 * @param {Array<{id: string, label: string, icon?: string, danger?: boolean,
 *                disabled?: boolean, hint?: string}>} opts.items
 *        `icon` is a name from icons.js; `hint` is a quiet right-aligned note
 *        (a shortcut, a count); `danger` colours the row as destructive;
 *        `disabled` renders it greyed and unselectable rather than hiding it,
 *        so the list does not change shape between rows.
 * @param {string}   [opts.cancelLabel='Cancel']
 * @returns {Promise<string|null>} the chosen item's `id`, or null for cancel,
 *        Escape, or a backdrop tap. Resolves exactly once.
 */
export function actionSheet({ title, items = [], cancelLabel = 'Cancel' } = {}) {
    // A second call supersedes the first rather than stacking sheets; the
    // superseded one resolves null so its caller is never left waiting.
    if (_openSheet) { try { _openSheet(); } catch { /* already gone */ } }

    return new Promise((resolve) => {
        let settled = false;
        let closeOverlay = null;

        const finish = (value) => {
            if (settled) return;
            settled = true;
            if (closeOverlay) { closeOverlay(); closeOverlay = null; }
            overlay.remove();
            if (_openSheet === cancel) _openSheet = null;
            resolve(value);
        };
        const cancel = () => finish(null);
        _openSheet = cancel;

        const rows = [];
        let firstEnabled = null;
        for (const item of items) {
            if (!item) continue;
            const children = [];
            if (item.icon) children.push(icon(item.icon, { size: 18 }));
            children.push(el('span', { class: 'sheet-item-label' }, [text(item.label ?? item.id)]));
            if (item.hint) children.push(el('span', { class: 'sheet-item-hint' }, [text(item.hint)]));

            const row = el('button', {
                class: `sheet-item${item.danger ? ' sheet-item--danger' : ''}`,
                type: 'button',
            }, children);
            if (item.disabled) {
                row.disabled = true;
                row.setAttribute('aria-disabled', 'true');
            } else {
                row.addEventListener('click', () => finish(item.id));
                if (!firstEnabled) firstEnabled = row;
            }
            rows.push(row);
        }

        const cancelBtn = el('button', {
            class: 'btn sheet-cancel',
            type: 'button',
            onClick: cancel,
        }, [text(cancelLabel)]);

        const card = el('div', { class: 'modal-card sheet-card' }, [
            ...(title ? [el('h2', { class: 'sheet-title' }, [text(title)])] : []),
            el('div', { class: 'sheet-items' }, rows),
            cancelBtn,
        ]);
        // openOverlay names a dialog from its first heading; a title-less sheet
        // has none, and an unnamed dialog is announced as just "dialog".
        if (!title) card.setAttribute('aria-label', 'Actions');

        const overlay = el('div', { class: 'modal-overlay sheet-overlay' }, [card]);

        // Only a press that STARTED on the backdrop counts, so a drag that ends
        // outside the card does not dismiss it.
        let downOnBackdrop = false;
        overlay.addEventListener('mousedown', (e) => { downOnBackdrop = e.target === overlay; });
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay && downOnBackdrop) cancel();
            downOnBackdrop = false;
        });

        document.body.appendChild(overlay);
        // The first thing you can actually pick, not the way out: a sheet is
        // opened to choose something. Cancel is one Shift+Tab (or Escape) away.
        closeOverlay = openOverlay(card, {
            onClose: cancel,
            initialFocus: firstEnabled || cancelBtn,
        });
    });
}
