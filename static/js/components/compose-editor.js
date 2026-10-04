// Pernix — the expand editor: the composer's "write something long" surface.
//
// The composer at the bottom of a chat rests at two lines and grows to 40dvh.
// That is right for the message you dash off and wrong for the brief you
// actually came here to write: a multi-paragraph prompt edited through a
// nine-line letterbox is why people draft Pernix messages somewhere else and
// paste them in. Slack, Linear and Claude.ai all answer this the same way —
// one control that opens the SAME text in a room-sized editor and hands it
// straight back.
//
// The draft is never copied. `onChange` fires on every keystroke (throttled,
// and flushed before anything that could read it), so the composer's textarea
// and its localStorage draft stay the single source of truth. Nothing here
// writes back on close, which is why "Escape keeps the draft" needs no special
// case at all: there is no second copy that could win.
//
// Two shapes, one component. Under a fine pointer it is a centred dialog
// (70vw x 70vh) with the key bindings spelled out along the footer. Under a
// finger it is a full-screen sheet with a top bar — Cancel · Compose · Send —
// because a phone has no room for a dialog and no Escape key to leave one
// with. The verdict is `body[data-touch]` (stamped by mobile.js), ORed with
// the same two fallbacks mobile.js itself uses so the answer is right even if
// the editor is somehow reached before initMobile() has run.
//
// Published on `window.PernixComposeEditor` from its own module <script> in
// index.html rather than imported by app.js: the composer calls the editor and
// the editor never calls the composer, and a global keeps that arrow one-way.
// The three named exports are the same functions for anything that would
// rather import them.
import { el, text } from '../render.js';
import { icon } from '../icons.js';
import { openOverlay } from '../a11y.js';

// Mirrors app.js. localStorage is the store; the event exists so a flip in
// Settings lands in an editor that is already open, and so a browser that
// refuses the write still shows the right hint.
const ENTER_SENDS_KEY = 'pernix:enter-sends';

// Mirrors mobile.js's touchMq. Duplicated rather than imported because this
// module loads on its own and must not drag app.js's graph in behind it.
const TOUCH_MQ = '(max-width: 768px), (hover: none) and (pointer: coarse)';

// At most one onChange per this many ms while typing, and always one within
// this many ms of the last keystroke. The brief's ceiling is 100.
const CHANGE_THROTTLE_MS = 80;

// How long "Draft saved" stays lit after the last change lands.
const SAVED_FLASH_MS = 1400;

// A window drag that crosses 768px swaps which stylesheet is under the editor.
const REFLOW_DEBOUNCE_MS = 150;

/** The live editor, or null. One at a time, by construction. */
let _live = null;

// ---------------------------------------------------------------------------
// The two things this component has to agree with the composer about
// ---------------------------------------------------------------------------

/** Input modality: finger, not mouse. Same verdict as mobile.js's isTouch(). */
function _isTouch() {
    if (document.body && document.body.hasAttribute('data-touch')) return true;
    if (document.documentElement.hasAttribute('data-touch-ui')) return true;
    try { return window.matchMedia(TOUCH_MQ).matches; } catch { return false; }
}

// Set by the preference event, exactly as app.js does it — so a flip lands
// even in a browser that refused the localStorage write.
let _enterSendsCache = null;

/** Does a bare Enter send? Same resolution order as app.js's enterSends(). */
function _enterSends() {
    if (_enterSendsCache !== null) return _enterSendsCache;
    let stored = null;
    try { stored = localStorage.getItem(ENTER_SENDS_KEY); } catch { /* private mode */ }
    if (stored === '1') return true;
    if (stored === '0') return false;
    return !_isTouch();
}

/**
 * Does this keypress mean "send"? A copy of app.js's shouldSendOnKey, and it
 * has to stay one: an editor that sends on a key the composer treats as a new
 * line is worse than an editor with no bindings at all.
 */
function _sendsOnKey(e, enterSends) {
    if (!e || e.key !== 'Enter') return false;
    // An Enter that commits an IME candidate is the user choosing a character,
    // not asking to send half a sentence.
    if (e.isComposing || e.keyCode === 229) return false;
    // The binding that always means send, on every device, in either setting.
    if (e.ctrlKey || e.metaKey) return true;
    if (e.shiftKey || e.altKey) return false;
    return enterSends;
}

/** The footer line. Says what the keys do NOW, never what they usually do. */
function _hintText(enterSends) {
    return enterSends
        ? 'Enter to send · Shift+Enter new line · Esc closes and keeps the draft'
        : 'Ctrl+Enter to send · Enter new line · Esc closes and keeps the draft';
}

function _countLabel(n) {
    return `${n.toLocaleString()} character${n === 1 ? '' : 's'}`;
}

// ---------------------------------------------------------------------------
// The draft channel
// ---------------------------------------------------------------------------

function _flashSaved() {
    if (!_live || !_live.saved) return;
    _live.saved.classList.add('is-flash');
    clearTimeout(_live.savedTimer);
    _live.savedTimer = setTimeout(() => {
        if (_live && _live.saved) _live.saved.classList.remove('is-flash');
    }, SAVED_FLASH_MS);
}

/**
 * Hand the current text to the caller, now. Runs on the throttle, and
 * unconditionally before send and before close — a send fired 20ms after the
 * last keystroke must not post the text as it was 20ms ago.
 */
function _flushChange() {
    if (!_live) return;
    clearTimeout(_live.changeTimer);
    _live.changeTimer = null;
    const value = _live.input.value;
    if (value === _live.lastSent) return;
    _live.lastSent = value;
    _flashSaved();
    if (typeof _live.onChange !== 'function') return;
    try {
        _live.onChange(value);
    } catch (err) {
        // A throwing caller must not take the editor's typing down with it.
        console.error('compose editor: onChange failed', err);
    }
}

// A throttle with a guaranteed trailing flush rather than a plain debounce: a
// long paragraph typed without pause still reaches the composer's draft every
// 80ms instead of only when the typing stops.
function _scheduleChange() {
    if (!_live || _live.changeTimer) return;
    _live.changeTimer = setTimeout(_flushChange, CHANGE_THROTTLE_MS);
}

// ---------------------------------------------------------------------------
// Building the two shapes
// ---------------------------------------------------------------------------

function _buildInput(touch) {
    const input = el('textarea', {
        id: 'compose-editor-input',
        class: 'compose-editor-input',
        // A placeholder is not a label — the same reason index.html gives the
        // composer's textarea one.
        'aria-label': 'Message Pernix',
        placeholder: 'Message Pernix…',
        spellcheck: 'true',
    });
    if (touch) {
        input.setAttribute('autocapitalize', 'sentences');
        input.setAttribute('autocorrect', 'on');
    }
    return input;
}

function _button(id, label, { primary = false, iconName = null } = {}) {
    const children = [];
    if (iconName) children.push(icon(iconName, { size: 14 }));
    children.push(el('span', { class: 'compose-editor-btn-label' }, [text(label)]));
    return el('button', {
        id,
        type: 'button',
        class: `btn ${primary ? 'btn-primary' : 'btn-secondary'} compose-editor-btn`,
    }, children);
}

/** Fine pointer: a centred dialog. Bindings and count run along the footer. */
function _buildDialog(parts) {
    return el('div', { id: 'compose-editor', class: 'compose-editor' }, [
        el('div', { class: 'compose-editor-top' }, [
            el('h2', { id: 'compose-editor-title', class: 'compose-editor-eyebrow' }, [text('Compose')]),
            parts.saved,
        ]),
        el('div', { class: 'compose-editor-body' }, [parts.input]),
        el('div', { class: 'compose-editor-foot' }, [
            parts.hint,
            el('span', { class: 'compose-editor-spacer' }),
            parts.count,
            parts.close,
            parts.send,
        ]),
    ]);
}

/** Touch: a full-screen sheet. Cancel · Compose · Send, count on its own strip. */
function _buildSheet(parts) {
    return el('div', { id: 'compose-editor', class: 'compose-editor compose-editor--sheet' }, [
        el('div', { class: 'compose-editor-top' }, [
            parts.close,
            el('h2', { id: 'compose-editor-title', class: 'compose-editor-eyebrow' }, [text('Compose')]),
            parts.send,
        ]),
        el('div', { class: 'compose-editor-body' }, [parts.input]),
        el('div', { class: 'compose-editor-foot' }, [
            parts.saved,
            el('span', { class: 'compose-editor-spacer' }),
            parts.count,
        ]),
    ]);
}

// Everything that depends on the shape, built once per open (and once more if
// the window is dragged across the touch line while the editor is up).
function _buildCard(touch) {
    const parts = {
        input: _buildInput(touch),
        count: el('span', {
            id: 'compose-editor-count',
            class: 'compose-editor-count',
            'aria-hidden': 'true',
        }),
        // Reassurance, not information: a live region saying "Draft saved" on
        // every keystroke would make the editor unusable with a screen reader,
        // and the draft is saved whether or not anyone is told.
        saved: el('span', { class: 'compose-editor-saved', 'aria-hidden': 'true' }, [
            text('Draft saved'),
        ]),
        // Deliberately not a button. The composer's own #composer-hint is the
        // one that flips the preference; a second interactive element here
        // would sit in the focus trap between the text and the way out.
        hint: touch ? null : el('span', {
            id: 'compose-editor-hint',
            class: 'compose-editor-hint',
        }),
        close: _button('compose-editor-close', touch ? 'Cancel' : 'Close'),
        send: _button('compose-editor-send', 'Send', {
            primary: true,
            iconName: touch ? null : 'send',
        }),
    };
    return { card: touch ? _buildSheet(parts) : _buildDialog(parts), parts };
}

// ---------------------------------------------------------------------------
// State the shape does not own
// ---------------------------------------------------------------------------

function _syncCount() {
    if (!_live) return;
    _live.count.textContent = _countLabel(_live.input.value.length);
}

function _syncBindings() {
    if (!_live) return;
    const sends = _enterSends();
    // The soft keyboard's return key should say what it actually does — the
    // same rule the composer uses, so the two never disagree mid-draft.
    _live.input.setAttribute('enterkeyhint', sends ? 'send' : 'enter');
    // The binding the footer has no room for on touch, and the one a screen
    // reader has no other way to hear.
    _live.input.setAttribute('aria-description', _hintText(sends));
    _live.input.title = _hintText(sends);
    if (_live.hint) _live.hint.textContent = _hintText(sends);
}

// One module-level listener rather than one per instance: the cache has to
// track the preference whether or not an editor happens to be open.
window.addEventListener(ENTER_SENDS_KEY, (e) => {
    _enterSendsCache = !!(e && e.detail);
    _syncBindings();
});

// ---------------------------------------------------------------------------
// Open / close
// ---------------------------------------------------------------------------

function _detach(inst) {
    clearTimeout(inst.changeTimer);
    clearTimeout(inst.savedTimer);
    clearTimeout(inst.reflowTimer);
    window.removeEventListener('resize', inst.onResize);
    // Close the overlay BEFORE removing the node: closing clears the inert
    // flags and puts focus back where it came from, and focus cannot be
    // restored out of a subtree that is no longer in the document.
    try { inst.closeOverlay(); } catch { /* already gone */ }
    inst.overlay.remove();
}

function _teardown(notify) {
    if (!_live || _live.closing) return;
    const inst = _live;
    // Escape, the Close button and a caller's own close() can all arrive for
    // the same instance; the first one wins and the rest are no-ops.
    inst.closing = true;

    // The last keystroke reaches the composer before anything else does.
    _flushChange();
    _live = null;

    _detach(inst);

    // Last, so a caller that wants focus in the composer's own textarea wins
    // it back from the element openOverlay just restored it to.
    if (notify && typeof inst.onClose === 'function') {
        try {
            inst.onClose();
        } catch (err) {
            console.error('compose editor: onClose failed', err);
        }
    }
}

function _send() {
    if (!_live || _live.closing) return;
    // Flush first: onSend's implementation reads the composer's textarea, and
    // that has to already hold what is on screen here.
    _flushChange();
    const value = _live.input.value;
    const onSend = _live.onSend;
    try {
        if (typeof onSend === 'function') onSend(value);
    } catch (err) {
        console.error('compose editor: onSend failed', err);
    } finally {
        // A throwing onSend still closes: leaving the editor up over a failed
        // send with no error in it is the one state nothing can act on.
        _teardown(true);
    }
}

function _mount(touch, carry, callbacks) {
    const { value, selectionStart, selectionEnd, scrollTop } = carry;
    const { card, parts } = _buildCard(touch);
    const overlay = el('div', {
        id: 'compose-editor-overlay',
        class: 'modal-overlay compose-editor-overlay',
    }, [card]);

    parts.input.value = value;

    _live = {
        overlay,
        card,
        touch,
        input: parts.input,
        count: parts.count,
        saved: parts.saved,
        hint: parts.hint,
        onChange: callbacks.onChange,
        onSend: callbacks.onSend,
        onClose: callbacks.onClose,
        lastSent: value,
        closing: false,
        changeTimer: null,
        savedTimer: null,
        reflowTimer: null,
        closeOverlay: () => {},
        onResize: null,
    };

    parts.input.addEventListener('input', () => {
        _syncCount();
        _scheduleChange();
    });
    parts.input.addEventListener('keydown', (e) => {
        if (!_sendsOnKey(e, _enterSends())) return;
        e.preventDefault();
        _send();
    });
    // A blur that is not a close — clicking Send, tabbing to Close — still
    // ends a typing burst; land the draft rather than waiting out the throttle.
    parts.input.addEventListener('blur', _flushChange);

    parts.close.addEventListener('click', () => _teardown(true));
    parts.send.addEventListener('click', _send);

    _live.onResize = () => {
        if (!_live) return;
        clearTimeout(_live.reflowTimer);
        _live.reflowTimer = setTimeout(_reflow, REFLOW_DEBOUNCE_MS);
    };
    window.addEventListener('resize', _live.onResize);

    _syncCount();
    _syncBindings();

    document.body.appendChild(overlay);

    _live.closeOverlay = openOverlay(card, {
        labelledBy: 'compose-editor-title',
        initialFocus: parts.input,
        onClose: () => _teardown(true),
    });

    // Caret at the end, not at the top: the editor is opened to keep writing.
    const end = value.length;
    const start = selectionStart == null ? end : Math.min(selectionStart, end);
    const stop = selectionEnd == null ? end : Math.min(selectionEnd, end);
    try { parts.input.setSelectionRange(start, stop); } catch { /* detached */ }
    parts.input.scrollTop = scrollTop == null ? parts.input.scrollHeight : scrollTop;
}

// A window dragged across 768px swaps which stylesheet is loaded under the
// editor, and the DOM has to swap with it — otherwise a desktop dialog ends up
// wearing the full-screen sheet's rules. Text, selection and scroll survive.
function _reflow() {
    if (!_live || _live.closing) return;
    const touch = _isTouch();
    if (touch === _live.touch) return;

    const inst = _live;
    const carry = {
        value: inst.input.value,
        selectionStart: inst.input.selectionStart,
        selectionEnd: inst.input.selectionEnd,
        scrollTop: inst.input.scrollTop,
    };
    const callbacks = { onChange: inst.onChange, onSend: inst.onSend, onClose: inst.onClose };
    const lastSent = inst.lastSent;

    _live = null;
    _detach(inst);
    _mount(touch, carry, callbacks);
    // Nothing changed for the caller, so the next flush must not re-announce
    // a value it already has.
    _live.lastSent = lastSent;
}

/**
 * Open the large editor over the composer.
 *
 * @param {object}   [opts]
 * @param {string}   [opts.value='']  the draft to edit. The editor never reads
 *        the composer itself; whatever is passed here is what it shows.
 * @param {function(string)} [opts.onChange]  the ONLY channel by which text
 *        leaves the editor. Fires on every input, throttled to one call per
 *        80ms, and flushed before onSend and before onClose — so the value the
 *        caller holds is never stale when either of those runs.
 * @param {function(string)} [opts.onSend]  the user asked to send. Receives the
 *        final text; the editor closes immediately afterwards, onClose included,
 *        so focus handling has one path rather than two.
 * @param {function} [opts.onClose]  the editor is gone and focus has been
 *        restored to whatever opened it. Move focus to the composer's textarea
 *        here if that is where it belongs. It must NOT write text back: the
 *        caller already has every keystroke through onChange, and writing here
 *        is how a sent message reappears in the composer.
 */
export function openComposeEditor({ value = '', onChange, onSend, onClose } = {}) {
    // A second open supersedes the first rather than stacking dialogs, and the
    // superseded one's onClose still runs so its caller is never left thinking
    // an editor is up.
    if (_live) _teardown(true);
    _mount(_isTouch(), { value: String(value == null ? '' : value) }, { onChange, onSend, onClose });
}

/** Close the editor, keeping the draft. Fires onClose. Idempotent. */
export function closeComposeEditor() {
    _teardown(true);
}

/** Is the editor up? */
export function isComposeEditorOpen() {
    return _live !== null;
}

window.PernixComposeEditor = {
    open: openComposeEditor,
    close: closeComposeEditor,
    isOpen: isComposeEditorOpen,
};
