// Pernix — Accessibility primitives.
//
// Two things the app had no way to do:
//
//   announce()    — say something to a screen reader that is not visible in
//                   the DOM at the moment it matters (a save result that
//                   lands in a footer the user is not looking at, a new
//                   notification arriving while focus is elsewhere).
//   openOverlay() — make a <div> that merely LOOKS like a modal behave like
//                   one: named, modal to assistive tech, focus moved into it
//                   on open, Tab kept inside it, focus put back where it came
//                   from on close.
//
// Both are deliberately small and free of app state so every overlay in the
// app can adopt them without a rewrite. Nothing here imports anything.

// ---------------------------------------------------------------------------
// Live-region announcements
// ---------------------------------------------------------------------------

const POLITE_ID = 'a11y-live';
const ASSERTIVE_ID = 'a11y-live-assertive';

// Bursts: more than BURST_MAX calls inside BURST_WINDOW and only the last one
// is spoken. Without this, a poll that resolves five statuses at once turns
// into five interruptions the user cannot read fast enough anyway.
const BURST_WINDOW = 500;
const BURST_MAX = 3;

const _calls = [];
let _burstTimer = null;
let _pending = null;

function _region(assertive) {
    const id = assertive ? ASSERTIVE_ID : POLITE_ID;
    let node = document.getElementById(id);
    if (!node) {
        // index.html ships both regions; this is the fallback for a page (or a
        // test harness) that does not, so a missing element never silently
        // swallows announcements.
        node = document.createElement('div');
        node.id = id;
        node.className = 'visually-hidden';
        node.setAttribute('aria-live', assertive ? 'assertive' : 'polite');
        node.setAttribute('aria-atomic', 'true');
        document.body.appendChild(node);
    }
    return node;
}

function _write(message, assertive) {
    const node = _region(assertive);
    // Clear-then-set on the next frame. Assigning the SAME string a second
    // time is not a text change, so a screen reader says nothing — which is
    // exactly the case that matters ("Saved" twice in a row).
    node.textContent = '';
    const paint = () => { node.textContent = message; };
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(paint);
    else setTimeout(paint, 0);
}

function _flushBurst() {
    _burstTimer = null;
    if (!_pending) return;
    const { message, assertive } = _pending;
    _pending = null;
    _write(message, assertive);
}

/**
 * Speak `message` through a visually-hidden live region.
 *
 * @param {string} message
 * @param {{assertive?: boolean}} [opts] assertive interrupts whatever the
 *        screen reader is saying; use it only for errors and for things the
 *        user must act on. Default polite.
 */
export function announce(message, { assertive = false } = {}) {
    const text = String(message == null ? '' : message).trim();
    if (!text) return;

    const now = Date.now();
    while (_calls.length && now - _calls[0] > BURST_WINDOW) _calls.shift();
    _calls.push(now);

    if (_calls.length > BURST_MAX) {
        _pending = { message: text, assertive };
        clearTimeout(_burstTimer);
        _burstTimer = setTimeout(_flushBurst, BURST_WINDOW);
        return;
    }
    _write(text, assertive);
}

// ---------------------------------------------------------------------------
// Modal overlays
// ---------------------------------------------------------------------------

const FOCUSABLE = [
    'a[href]',
    'button:not([disabled])',
    'input:not([disabled]):not([type="hidden"])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    'summary',
    '[contenteditable=""]',
    '[contenteditable="true"]',
    '[tabindex]:not([tabindex="-1"])',
].join(',');

let _uid = 0;
const _stack = [];

function _visible(elem) {
    if (elem.hasAttribute('inert') || elem.closest('[inert]')) return false;
    if (elem.getAttribute('aria-hidden') === 'true') return false;
    // getClientRects() is empty for display:none and for a collapsed
    // ancestor; offsetParent alone misses position:fixed subtrees.
    return elem.getClientRects().length > 0;
}

function _focusables(card) {
    return Array.from(card.querySelectorAll(FOCUSABLE)).filter(_visible);
}

// The direct child of <body> that contains `node` — the thing a sibling
// overlay has to make inert.
function _bodyLevel(node) {
    let n = node;
    while (n && n.parentNode && n.parentNode !== document.body) n = n.parentNode;
    return n && n.parentNode === document.body ? n : null;
}

function _labelFor(card, labelledBy) {
    if (labelledBy) return labelledBy;
    const heading = card.querySelector('h1, h2, h3');
    if (!heading) return null;
    if (!heading.id) heading.id = `a11y-dlg-title-${++_uid}`;
    return heading.id;
}

/**
 * Turn `cardEl` into a real modal dialog.
 *
 * @param {HTMLElement} cardEl the dialog CARD (not the backdrop) — the box
 *        that holds the header and the controls.
 * @param {object}  [opts]
 * @param {string}  [opts.labelledBy] id of the element naming the dialog. If
 *        omitted, the first h1/h2/h3 inside the card is used (and given an id
 *        if it has none).
 * @param {HTMLElement|function} [opts.initialFocus] element (or a function
 *        returning one) to focus on open; defaults to the first focusable
 *        element in the card, or the card itself.
 * @param {function} [opts.onClose] called when Escape is pressed. Escape is
 *        ignored entirely when this is omitted, which is how a dialog opts out
 *        of dismissal.
 * @returns {function} close() — idempotent. Removes the key handler, clears
 *        the inert flags this overlay set, and restores focus to whatever had
 *        it before the overlay opened (if that element is still in the
 *        document). It does NOT remove the overlay from the DOM; the caller
 *        owns its own node.
 */
export function openOverlay(cardEl, { labelledBy, initialFocus, onClose } = {}) {
    if (!cardEl) return () => {};

    cardEl.setAttribute('role', 'dialog');
    cardEl.setAttribute('aria-modal', 'true');
    const labelId = _labelFor(cardEl, labelledBy);
    if (labelId) cardEl.setAttribute('aria-labelledby', labelId);
    if (!cardEl.hasAttribute('tabindex')) cardEl.setAttribute('tabindex', '-1');

    const root = _bodyLevel(cardEl) || cardEl;
    const prevFocus = document.activeElement;

    // Everything else at the top level goes inert — #app and, for a nested
    // overlay, the overlay underneath it. Elements that were ALREADY inert
    // are left out of the list so closing this overlay does not un-inert
    // something another overlay is holding.
    const inerted = [];
    for (const sibling of Array.from(document.body.children)) {
        if (sibling === root) continue;
        if (sibling.hasAttribute('inert')) continue;
        if (sibling.id === POLITE_ID || sibling.id === ASSERTIVE_ID) continue;
        if (sibling.tagName === 'SCRIPT' || sibling.tagName === 'TEMPLATE') continue;
        sibling.setAttribute('inert', '');
        inerted.push(sibling);
    }

    const entry = { card: cardEl, root, prevFocus, inerted, closed: false, handler: null };

    entry.handler = (e) => {
        // Monaco (and any control that means something by Tab) calls
        // preventDefault first; do not steal the key from it.
        if (e.defaultPrevented) return;
        if (_stack[_stack.length - 1] !== entry) return;

        if (e.key === 'Escape') {
            if (!onClose) return;
            e.preventDefault();
            onClose();
            return;
        }
        if (e.key !== 'Tab') return;

        // A dialog opened on top of this one that did NOT register here is a
        // later child of <body>; leave its Tab presses alone.
        if (!cardEl.contains(e.target)) {
            const other = _bodyLevel(e.target);
            if (other && other !== root
                && (other.compareDocumentPosition(root) & Node.DOCUMENT_POSITION_PRECEDING)) {
                return;
            }
        }

        const items = _focusables(cardEl);
        if (!items.length) {
            e.preventDefault();
            cardEl.focus();
            return;
        }
        const first = items[0];
        const last = items[items.length - 1];
        const active = document.activeElement;
        if (!cardEl.contains(active)) {
            e.preventDefault();
            (e.shiftKey ? last : first).focus();
        } else if (e.shiftKey && active === first) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && active === last) {
            e.preventDefault();
            first.focus();
        }
    };

    document.addEventListener('keydown', entry.handler);
    _stack.push(entry);

    const target = typeof initialFocus === 'function' ? initialFocus() : initialFocus;
    const fallback = _focusables(cardEl)[0] || cardEl;
    (target && document.contains(target) ? target : fallback).focus();

    return function close() {
        if (entry.closed) return;
        entry.closed = true;
        document.removeEventListener('keydown', entry.handler);
        const at = _stack.indexOf(entry);
        if (at !== -1) _stack.splice(at, 1);
        // Clear inert BEFORE restoring focus — focus() on an inert subtree is
        // a no-op, which would silently drop the user at the top of the page.
        for (const node of entry.inerted) node.removeAttribute('inert');
        if (prevFocus && document.contains(prevFocus) && typeof prevFocus.focus === 'function') {
            prevFocus.focus();
        }
    };
}

/** How many overlays openOverlay currently has open. Exposed for callers that
 *  need to know whether a global key binding should fire. */
export function overlayDepth() {
    return _stack.length;
}
