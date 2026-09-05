// Pernix — thumbs on an assistant answer.
//
// The reflection loop grades its own homework: a verdict written by the same
// family of model that produced the turn, about the turn it just produced.
// This is the one channel in the app that carries ground truth instead — a
// human saying "that was the answer" or "that was not" — and the backend
// treats a user signal as outranking anything reflect decided
// (docs/dev/trust-loop-hardening-plan.md, outcome precedence
// `user` > `next_turn` > `llm`).
//
// Two rules shape everything here:
//
//   1. IT MUST NEVER BE IN THE WAY. Rating an answer is optional and always
//      will be, so the controls live where the copy button already lives —
//      the hover toolbar on a mouse, the row's `⋯` sheet on a finger — and
//      the note on a thumbs-down is skippable in one keypress.
//   2. IT MUST NEVER BREAK THE TRANSCRIPT. The routes this calls arrive with
//      the 3.2 backend (workstream W2). Against an older server every one of
//      them is a 404, and a 404 here means the controls quietly are not
//      there — never a thrown error, never a broken message row.
//
// STATE, AND WHY IT IS LOADED ONCE. A session's ratings arrive in a single
// GET when the transcript loads, not one call per message: a 200-message
// transcript would otherwise open with two hundred requests. Everything after
// that is optimistic — the button flips before the write lands and rolls back
// if it does not.

import { el, text } from '../render.js';
import { icon } from '../icons.js';
import { get, post, humanizeError } from '../api.js';
import { notify } from '../feedback.js';
import { announce, openOverlay } from '../a11y.js';

// Sessions whose answers are not a human's to grade. A canary transcript is
// EVAL data — the whole point of the suite is that nothing it produces feeds
// the learning loop (plan, principle 5) — and a worker's answers are read by
// the parent agent, not by the person in the chat.
const UNGRADED_TYPES = new Set(['canary', 'worker']);

const NOTE_MAX = 280;

// null = never asked, true = the routes are there, false = this server does
// not have them. Sticky once false: one 404 per page load answers the
// question, and asking again on every session switch would only repeat it.
let _backend = null;
let _sid = null;
let _ungraded = false;

// message id (as a string) -> { signal, note }
const _signals = new Map();

// One note prompt at a time, on the confirm.js pattern.
let _openNote = null;

function _key(messageId) {
    return String(messageId);
}

/** Is the rating UI live for the session currently on screen? */
export function feedbackAvailable() {
    return _backend === true && !_ungraded;
}

/** The signal stored for one message: 'up', 'down', or null. */
export function feedbackSignal(messageId) {
    return _signals.get(_key(messageId))?.signal || null;
}

/**
 * Load one session's ratings. Never throws and never rejects: the transcript
 * load awaits this alongside its own fetch, and a rating store that is not
 * there must not cost the user their conversation.
 *
 * @param {string} sid
 * @param {{sessionType?: string}} [opts]
 */
export async function primeMessageFeedback(sid, { sessionType = '' } = {}) {
    _sid = sid;
    _signals.clear();
    _ungraded = UNGRADED_TYPES.has(sessionType);
    if (_backend === false || _ungraded || !sid) return;
    try {
        const data = await get(`/api/sessions/${sid}/feedback`);
        // A slower answer for a session the user has already left must not
        // paint its ratings onto the one now on screen.
        if (_sid !== sid) return;
        _backend = true;
        for (const item of data.items || []) {
            if (item && item.message_id != null) {
                _signals.set(_key(item.message_id), {
                    signal: item.signal || null,
                    note: item.note || '',
                });
            }
        }
    } catch (e) {
        // 404 is the answer "this server predates the trust loop", and it is
        // final. Anything else (offline, a 500) is this attempt failing:
        // leave the verdict alone so the next session can try again.
        if (e && e.status === 404) _backend = false;
    }
}

// ---------------------------------------------------------------------------
// Writing
// ---------------------------------------------------------------------------

function _repaint(messageId) {
    const key = _key(messageId);
    // Ids are database row numbers; anything else is not ours to query for.
    if (!/^\d+$/.test(key)) return;
    for (const btn of document.querySelectorAll(`.msg-feedback-btn[data-mid="${key}"]`)) {
        _paintButton(btn, btn.dataset.signal, feedbackSignal(key));
    }
}

/** Take every rating control off the page — the server said 404 mid-session. */
function _retire() {
    for (const btn of document.querySelectorAll('.msg-feedback-btn')) btn.remove();
}

/**
 * Write one signal, optimistically.
 *
 * @param {string|number} messageId
 * @param {'up'|'down'|null} next
 * @param {string|null} note  omitted from the body when null, so a plain
 *        toggle does not have an opinion about the note already stored.
 * @returns {Promise<boolean>} whether the write landed.
 */
async function _write(messageId, next, note) {
    const key = _key(messageId);
    const before = _signals.get(key) || null;
    if (next) _signals.set(key, { signal: next, note: note == null ? (before?.note || '') : note });
    else _signals.delete(key);
    _repaint(key);

    const body = { signal: next };
    if (note != null) body.note = note;
    try {
        await post(`/api/sessions/${_sid}/messages/${encodeURIComponent(key)}/feedback`, body);
        return true;
    } catch (e) {
        if (before) _signals.set(key, before); else _signals.delete(key);
        _repaint(key);
        if (e && e.status === 404) {
            // The store is not there after all. Say nothing and take the
            // controls away rather than offering a button that cannot work.
            _backend = false;
            _retire();
            return false;
        }
        notify('error', `Couldn't save that rating — ${humanizeError(e)}`);
        return false;
    }
}

const _SPOKEN = {
    up: 'Marked helpful',
    down: 'Marked not helpful',
    null: 'Rating removed',
};

/**
 * Press one of the two thumbs. Pressing the signal that is already set
 * removes it — the same button is the undo.
 */
async function _toggle(messageId, signal) {
    const next = feedbackSignal(messageId) === signal ? null : signal;
    const ok = await _write(messageId, next, null);
    if (!ok) return;
    announce(_SPOKEN[next === null ? 'null' : next]);
    if (next !== 'down') return;
    // The signal is already saved, so a skipped note costs nothing: this is
    // an offer to say more, not a form standing between the user and the
    // thumbs-down they just gave.
    const note = await notePrompt(_signals.get(_key(messageId))?.note || '');
    if (note == null) return;
    await _write(messageId, 'down', note);
}

// ---------------------------------------------------------------------------
// The optional one-line note
// ---------------------------------------------------------------------------

/**
 * Ask for one line about what went wrong. Skippable everywhere: Escape, the
 * ×, the backdrop and the Skip button all resolve null, and only the primary
 * button (or Enter in the field) resolves a string.
 *
 * @param {string} [initial]
 * @returns {Promise<string|null>}
 */
function notePrompt(initial = '') {
    if (_openNote) { try { _openNote(); } catch { /* already gone */ } }

    return new Promise((resolve) => {
        let settled = false;
        let closeOverlay = null;
        const finish = (value) => {
            if (settled) return;
            settled = true;
            if (closeOverlay) { closeOverlay(); closeOverlay = null; }
            overlay.remove();
            if (_openNote === skip) _openNote = null;
            resolve(value);
        };
        const skip = () => finish(null);
        _openNote = skip;

        const input = el('input', {
            class: 'msg-note-input',
            type: 'text',
            maxlength: String(NOTE_MAX),
            placeholder: 'it answered a different question',
            value: initial,
        });
        input.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter') return;
            e.preventDefault();
            finish(input.value.trim().slice(0, NOTE_MAX));
        });

        const card = el('div', { class: 'modal-card msg-note-card' }, [
            el('div', { class: 'modal-header' }, [
                el('h2', {}, [text('What was wrong with it?')]),
                el('button', {
                    class: 'modal-close',
                    type: 'button',
                    title: 'Skip',
                    'aria-label': 'Skip the note',
                    onClick: skip,
                }, [icon('x', { size: 14 })]),
            ]),
            el('div', { class: 'modal-body msg-note-body' }, [
                el('label', { class: 'msg-note-label' }, [
                    el('span', {}, [text('One line, optional. The thumbs-down is already saved.')]),
                    input,
                ]),
            ]),
            el('div', { class: 'modal-footer' }, [
                el('button', { class: 'btn btn--secondary', type: 'button', onClick: skip }, [text('Skip')]),
                el('button', {
                    class: 'btn btn--primary',
                    type: 'button',
                    onClick: () => finish(input.value.trim().slice(0, NOTE_MAX)),
                }, [text('Save note')]),
            ]),
        ]);

        const overlay = el('div', { class: 'modal-overlay msg-note-overlay' }, [card]);
        // Only a press that STARTED on the backdrop dismisses — the same rule
        // as confirm.js, so selecting the text of the prompt does not close it.
        let downOnBackdrop = false;
        overlay.addEventListener('mousedown', (e) => { downOnBackdrop = e.target === overlay; });
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay && downOnBackdrop) skip();
            downOnBackdrop = false;
        });

        document.body.appendChild(overlay);
        closeOverlay = openOverlay(card, { onClose: skip, initialFocus: input });
        announce('What was wrong with it? One line, optional.');
    });
}

// ---------------------------------------------------------------------------
// The controls
// ---------------------------------------------------------------------------

const LABELS = { up: 'Helpful', down: 'Not helpful' };

function _paintButton(btn, signal, current) {
    const on = current === signal;
    const label = LABELS[signal];
    btn.classList.toggle('on', on);
    btn.setAttribute('aria-pressed', String(on));
    // The name says the state, because a screen reader that does not surface
    // aria-pressed would otherwise read the same word for both states.
    btn.setAttribute('aria-label', on ? `${label} — remove this rating` : label);
    btn.title = on ? `${label} — click to undo` : label;
    while (btn.firstChild) btn.removeChild(btn.firstChild);
    btn.appendChild(icon(on ? `thumb-${signal}-filled` : `thumb-${signal}`, { size: 12 }));
}

function _button(messageId, signal) {
    const btn = el('button', {
        class: `msg-action-btn msg-feedback-btn msg-feedback-${signal}`,
        type: 'button',
        'data-mid': _key(messageId),
        'data-signal': signal,
    });
    _paintButton(btn, signal, feedbackSignal(messageId));
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        _toggle(messageId, signal);
    });
    return btn;
}

/**
 * The pair of hover-toolbar buttons for one assistant message, or nothing at
 * all when this server (or this session) has no ratings.
 *
 * @param {string|number} messageId
 * @returns {HTMLElement[]}
 */
export function feedbackButtons(messageId) {
    if (!feedbackAvailable() || messageId == null) return [];
    return [_button(messageId, 'up'), _button(messageId, 'down')];
}

/**
 * The same two controls as rows for an action sheet — the touch half, where
 * there is no hover to reveal a toolbar with.
 *
 * @param {string|number} messageId
 * @returns {Array<object>} items for actionSheet(), ids `feedback-up` /
 *          `feedback-down`, ready to hand back to applyFeedbackChoice().
 */
export function feedbackItems(messageId) {
    if (!feedbackAvailable() || messageId == null) return [];
    const current = feedbackSignal(messageId);
    return ['up', 'down'].map((signal) => ({
        id: `feedback-${signal}`,
        label: LABELS[signal],
        icon: current === signal ? `thumb-${signal}-filled` : `thumb-${signal}`,
        hint: current === signal ? 'your rating' : '',
    }));
}

/**
 * Act on a sheet choice. Anything that is not one of this module's ids is
 * ignored, so a caller can hand the whole sheet result straight through.
 *
 * @returns {Promise<boolean>} whether the choice belonged to this module.
 */
export async function applyFeedbackChoice(messageId, choice) {
    if (choice !== 'feedback-up' && choice !== 'feedback-down') return false;
    await _toggle(messageId, choice === 'feedback-up' ? 'up' : 'down');
    return true;
}
