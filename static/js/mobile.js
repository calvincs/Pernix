// Pernix — the two device tiers: detection, the sidebar drawer, the edge
// swipes, the on-screen keyboard, and the three viewport custom properties
// touch.css sizes itself from (--vvh, --vv-top, --bottom-stack).
//
// The whole layer, including which stylesheet a rule belongs in and why the
// two gates differ, is written up in docs/internals/web-client.md.
//
// (openOverlay is not used here: the drawer is not a <div> dropped on top of
// the page but a permanent part of the layout that slides. Only overlayDepth()
// is borrowed, so Escape goes to whatever dialog is above it.)
//
// TWO QUESTIONS, TWO ANSWERS. They used to be one, and that is why a 1180px
// iPad in landscape got the phone layout.
//
//   isTouch()    is the pointer a finger? Decides target sizes, 16px inputs,
//                hover-revealed controls, safe-area insets, where the
//                hamburger lives, and the keyboard re-pin. Mirrors touch.css.
//   isCompact()  is the viewport narrower than 900px? Decides the drawer, the
//                scrim, the swipe gestures, the full-screen Explorer and the
//                bottom sheets. Mirrors compact.css.
//
// A phone is both. A landscape iPad is touch but NOT compact: it docks the
// sidebar and keeps the desktop shapes. A narrow desktop window is compact
// but not touch.
//
// The touch query alone does NOT see an iPad: iPadOS desktop-class browsing
// (the default in Safari and Chrome both) reports `hover: hover` and
// `pointer: fine`. touch-boot.js does the detection that works there and
// stamps <html data-touch-ui>; it is the single source of truth, and this ORs
// its verdict in. The attribute never changes after boot, so it is read once —
// which also means touch never flips OFF on a forced-touch device, so rotating
// an iPad cannot tear the touch UI down.

import { overlayDepth } from './a11y.js';

const TOUCH_BP = 768;
const COMPACT_BP = 899;

const touchMq = window.matchMedia(`(max-width: ${TOUCH_BP}px), (hover: none) and (pointer: coarse)`);
const compactMq = window.matchMedia(`(max-width: ${COMPACT_BP}px)`);
const FORCED_TOUCH = document.documentElement.hasAttribute('data-touch-ui');

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/** Input modality: finger, not mouse. Gate for touch.css's concerns. */
export function isTouch() { return touchMq.matches || FORCED_TOUCH; }

/** Layout tier: narrower than the 900px tablet line. Gate for compact.css. */
export function isCompact() { return compactMq.matches; }

export function initMobile() {
    _stamp();
    // Two independent listeners: crossing 900px must not disturb the touch
    // verdict, and a device that becomes touch (it can only ever become one,
    // see FORCED_TOUCH) must not disturb the layout tier.
    touchMq.addEventListener('change', () => { _stamp(); _applyTouch(); });
    compactMq.addEventListener('change', () => { _stamp(); _applyCompact(); _announceTier(); });

    _setupSidebarDrawer();
    _setupSwipeGesture();
    _setupFilePanelSwipe();
    _setupKeyboardHandler();
    _setupVisualViewport();
    _setupBottomStack();

    _applyTouch();
    _applyCompact();
}

// ---------------------------------------------------------------------------
// Tier detection
// ---------------------------------------------------------------------------

function _stamp() {
    document.body.toggleAttribute('data-touch', isTouch());
    document.body.toggleAttribute('data-compact', isCompact());
}

// The hamburger belongs in the status bar wherever the pointer is a finger —
// on a tablet too, where it collapses the docked sidebar instead of opening a
// drawer. The floating desktop position is only for a mouse.
function _applyTouch() {
    if (isTouch()) _moveToggleIntoHeader();
    else _restoreToggleFromHeader();
}

// Crossing 900px swaps one sidebar mechanism for the other, and each has to
// leave nothing of itself behind: a stale .mobile-open would show a docked
// sidebar as an off-canvas drawer, and a scrim that outlives the tier is an
// invisible sheet of glass over the whole app.
function _applyCompact() {
    if (isCompact()) {
        _ensureScrim();
        syncDrawerInert();
        return;
    }
    document.getElementById('sidebar')?.classList.remove('mobile-open');
    _removeScrim();
    // Neither the drawer's inert nor its hold on #main means anything above
    // 900px. The docked sidebar's own rule (collapsed, not closed) is app.js's,
    // and it re-runs on the same resize that got us here.
    document.getElementById('sidebar')?.removeAttribute('inert');
    setMainInert('drawer', false);
}

// Announced after every tier flip, in both directions. file-panel.js listens:
// an Explorer that was a full-screen overlay a moment ago is a sibling column
// now, and its hold on #main has to be given back.
function _announceTier() {
    window.dispatchEvent(new CustomEvent('pernix:tier-change', {
        detail: { compact: isCompact(), touch: isTouch() },
    }));
}

// ---------------------------------------------------------------------------
// Mobile header — move sidebar toggle into status bar
// ---------------------------------------------------------------------------

function _moveToggleIntoHeader() {
    const statusBar = document.getElementById('status-bar');
    const toggle = document.getElementById('sidebar-toggle');
    if (!statusBar || !toggle) return;
    // Only move if not already a child of status bar
    if (toggle.parentElement !== statusBar) {
        statusBar.insertBefore(toggle, statusBar.firstChild);
    }
}

function _restoreToggleFromHeader() {
    const main = document.getElementById('main');
    const toggle = document.getElementById('sidebar-toggle');
    if (!main || !toggle) return;
    // Only restore if not already a direct child of #main
    if (toggle.parentElement !== main) {
        main.insertBefore(toggle, main.firstChild);
    }
}

// ---------------------------------------------------------------------------
// Scrim (backdrop behind drawer)
//
// Exists only while the compact tier does. A docked sidebar has nothing to
// wash out behind it, and an element with `inset: 0` sitting in #app on a
// tablet is one CSS mistake away from swallowing every tap on the app.
// ---------------------------------------------------------------------------

let _scrim = null;

function _ensureScrim() {
    if (_scrim && _scrim.isConnected) return _scrim;
    _scrim = document.createElement('div');
    _scrim.className = 'mobile-scrim';
    document.getElementById('app')?.appendChild(_scrim);
    _scrim.addEventListener('click', () => closeSidebar({ restoreFocus: true }));
    return _scrim;
}

function _removeScrim() {
    _scrim?.remove();
    _scrim = null;
}

// ---------------------------------------------------------------------------
// #main inert — shared between the drawer and the Explorer
//
// Both cover the whole screen on compact, and either can open while the other
// is closing. A plain toggle let whichever finished last decide: opening the
// Explorer from the drawer un-inerted #main behind the Explorer. Counting the
// reasons instead means #main comes back only when nothing is covering it.
// ---------------------------------------------------------------------------

const _mainInertReasons = new Set();

/**
 * @param {string} reason  who is covering #main ('drawer', 'explorer').
 * @param {boolean} on
 */
export function setMainInert(reason, on) {
    if (on) _mainInertReasons.add(reason);
    else _mainInertReasons.delete(reason);
    document.getElementById('main')?.toggleAttribute('inert', _mainInertReasons.size > 0);
}

// ---------------------------------------------------------------------------
// Sidebar drawer
//
// A closed drawer is off-canvas, not gone: without inert, Tab walked the whole
// invisible session list and a screen reader read it out. An OPEN one is a
// modal surface, so #main goes inert under it and focus moves inside. (E5)
// ---------------------------------------------------------------------------

/** The drawer's half of #sidebar[inert]. app.js owns the docked half
 *  (collapsed, not closed) and delegates here below 900px so the two cannot
 *  clobber each other on the resize that crosses the line. */
export function syncDrawerInert() {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar || !isCompact()) return;
    sidebar.toggleAttribute('inert', !sidebar.classList.contains('mobile-open'));
}

function _firstFocusable(root) {
    return root.querySelector(
        'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]),'
        + ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])');
}

function openSidebar() {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar) return;
    sidebar.classList.add('mobile-open');
    _ensureScrim().classList.add('visible');
    // Un-inert BEFORE focusing: focus() on an inert subtree is a silent no-op.
    sidebar.removeAttribute('inert');
    setMainInert('drawer', true);

    // Mutual exclusion: close file panel if open
    const fp = document.getElementById('file-panel');
    if (fp?.classList.contains('open')) {
        document.getElementById('files-btn')?.click();
    }

    // The search field is what someone opening the session list came for, and
    // it is the first thing a screen reader should land on.
    (document.getElementById('session-search') || _firstFocusable(sidebar) || sidebar).focus();
}

/**
 * @param {{restoreFocus?: boolean}} [opts] restoreFocus puts focus back on the
 *        hamburger. True for a dismissal the user asked for (Escape, the
 *        scrim, a swipe); false when the drawer closes as a side effect of
 *        going somewhere else, which has its own idea of where focus belongs.
 */
export function closeSidebar({ restoreFocus = false } = {}) {
    const sidebar = document.getElementById('sidebar');
    const wasOpen = !!sidebar?.classList.contains('mobile-open');
    sidebar?.classList.remove('mobile-open');
    _scrim?.classList.remove('visible');
    // Clear #main's inert first — the hamburger lives inside it on touch.
    setMainInert('drawer', false);
    syncDrawerInert();
    if (wasOpen && restoreFocus) document.getElementById('sidebar-toggle')?.focus();
}

function _setupSidebarDrawer() {
    const toggle = document.getElementById('sidebar-toggle');
    if (!toggle) return;

    toggle.addEventListener('click', (e) => {
        // Only the compact tier has a drawer. On a docked sidebar — desktop or
        // wide touch — app.js's handler collapses it instead.
        if (!isCompact()) return;
        e.stopPropagation();
        const sidebar = document.getElementById('sidebar');
        if (sidebar?.classList.contains('mobile-open')) closeSidebar({ restoreFocus: true });
        else openSidebar();
    });

    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape' || e.defaultPrevented) return;
        // A dialog on top of the drawer owns the key; openOverlay's own handler
        // is scoped to the top of its stack and will have taken it already.
        if (overlayDepth() > 0) return;
        if (!isCompact()) return;
        if (!document.getElementById('sidebar')?.classList.contains('mobile-open')) return;
        e.preventDefault();
        closeSidebar({ restoreFocus: true });
    });
}

// ---------------------------------------------------------------------------
// Swipe gestures — sidebar (left edge)
// ---------------------------------------------------------------------------

function _insideScrollableCode(el) {
    // Returns true if el or any ancestor is a horizontally scrollable code block.
    // Used to avoid eating touchmove events that the user intends for code scrolling.
    while (el && el !== document.body) {
        if (el.classList?.contains('code-block-wrap') || el.tagName === 'PRE') {
            if (el.scrollWidth > el.clientWidth) return true;
        }
        el = el.parentElement;
    }
    return false;
}

function _setupSwipeGesture() {
    let startX = 0;
    let startY = 0;
    let tracking = false;
    let direction = null; // 'horizontal' | 'vertical'
    const EDGE_ZONE = 25;
    const THRESHOLD = 60;

    document.addEventListener('touchstart', (e) => {
        if (!isCompact()) return;
        const touch = e.touches[0];
        startX = touch.clientX;
        startY = touch.clientY;
        direction = null;

        const sidebar = document.getElementById('sidebar');
        const isOpen = sidebar?.classList.contains('mobile-open');

        // Don't track if file panel is open (let file panel swipe handle it)
        const fp = document.getElementById('file-panel');
        if (fp?.classList.contains('open') && !isOpen) return;

        // Start tracking if touch is near left edge (to open) or sidebar is open (to close)
        if (startX < EDGE_ZONE || isOpen) {
            tracking = true;
        }
    }, { passive: true });

    document.addEventListener('touchmove', (e) => {
        if (!tracking || !isCompact()) return;
        const touch = e.touches[0];
        const dx = touch.clientX - startX;
        const dy = touch.clientY - startY;

        // Lock direction on first significant movement
        if (!direction) {
            if (Math.abs(dx) > 10 || Math.abs(dy) > 10) {
                direction = Math.abs(dx) > Math.abs(dy) ? 'horizontal' : 'vertical';
            }
        }

        // Only handle horizontal swipes — but let code blocks scroll natively
        if (direction === 'horizontal' && !_insideScrollableCode(e.target)) {
            e.preventDefault();
        }
    }, { passive: false });

    document.addEventListener('touchend', (e) => {
        if (!tracking || !isCompact()) { tracking = false; return; }
        const touch = e.changedTouches[0];
        const dx = touch.clientX - startX;
        tracking = false;

        if (direction !== 'horizontal') return;

        const sidebar = document.getElementById('sidebar');
        const isOpen = sidebar?.classList.contains('mobile-open');

        if (!isOpen && dx > THRESHOLD && startX < EDGE_ZONE) {
            // Swipe right from edge -> open
            openSidebar();
        } else if (isOpen && dx < -THRESHOLD) {
            // Swipe left -> close
            closeSidebar({ restoreFocus: true });
        }
    }, { passive: true });
}

// ---------------------------------------------------------------------------
// Swipe gestures — file panel (right edge)
//
// Touch, not compact: the Explorer is a full-screen overlay below 900px and a
// docked column above it, but on a finger device the edge swipe is how you
// reach it either way. Nothing here is armed under a mouse.
// ---------------------------------------------------------------------------

function _setupFilePanelSwipe() {
    let startX = 0;
    let startY = 0;
    let tracking = false;
    let direction = null;
    const EDGE_ZONE = 25;
    const THRESHOLD = 60;

    document.addEventListener('touchstart', (e) => {
        if (!isTouch()) return;
        const touch = e.touches[0];
        startX = touch.clientX;
        startY = touch.clientY;
        direction = null;

        const fp = document.getElementById('file-panel');
        const isOpen = fp?.classList.contains('open');
        const rightEdge = window.innerWidth - EDGE_ZONE;

        // Track if near right edge (to open) or panel is open (to close)
        if (startX > rightEdge || isOpen) {
            // Don't conflict with sidebar swipe
            const sidebar = document.getElementById('sidebar');
            if (sidebar?.classList.contains('mobile-open')) return;
            tracking = true;
        }
    }, { passive: true });

    document.addEventListener('touchmove', (e) => {
        if (!tracking || !isTouch()) return;
        const touch = e.touches[0];
        const dx = touch.clientX - startX;
        const dy = touch.clientY - startY;

        if (!direction) {
            if (Math.abs(dx) > 10 || Math.abs(dy) > 10) {
                direction = Math.abs(dx) > Math.abs(dy) ? 'horizontal' : 'vertical';
            }
        }

        // With the panel open this handler tracks a swipe that started
        // anywhere, so on a tablet — where the transcript is still on screen
        // beside it — the same guard the drawer swipe uses has to apply, or a
        // wide code block could no longer be scrolled sideways.
        if (direction === 'horizontal' && !_insideScrollableCode(e.target)) {
            e.preventDefault();
        }
    }, { passive: false });

    document.addEventListener('touchend', (e) => {
        if (!tracking || !isTouch()) { tracking = false; return; }
        const touch = e.changedTouches[0];
        const dx = touch.clientX - startX;
        tracking = false;

        if (direction !== 'horizontal') return;

        const fp = document.getElementById('file-panel');
        const isOpen = fp?.classList.contains('open');
        const rightEdge = window.innerWidth - EDGE_ZONE;

        if (!isOpen && dx < -THRESHOLD && startX > rightEdge) {
            // Swipe left from right edge -> open file panel
            document.getElementById('files-btn')?.click();
        } else if (isOpen && dx > THRESHOLD) {
            // Swipe right anywhere -> close file panel
            document.getElementById('files-btn')?.click();
        }
    }, { passive: true });
}

// ---------------------------------------------------------------------------
// Virtual keyboard handling
// ---------------------------------------------------------------------------

// How close to the bottom still counts as "following the conversation".
const PIN_SLACK_PX = 48;

function _setupKeyboardHandler() {
    if (!window.visualViewport) return;

    const messages = document.getElementById('messages');
    if (!messages) return;

    // Track the pin continuously: once the keyboard opens, the viewport has
    // already shrunk and the pre-resize position is no longer recoverable.
    let pinned = true;
    messages.addEventListener('scroll', () => {
        pinned = messages.scrollHeight - messages.clientHeight - messages.scrollTop <= PIN_SLACK_PX;
    }, { passive: true });

    // interactive-widget=resizes-content shrinks the layout viewport when the
    // keyboard opens, which keeps the composer on screen but leaves #messages
    // scrolled where it was — the newest message ends up hidden behind the
    // keyboard for a reader who was at the bottom. Re-pin only in that case,
    // so someone scrolled back in history keeps their place.
    window.visualViewport.addEventListener('resize', () => {
        if (!isTouch() || !pinned) return;
        requestAnimationFrame(() => {
            messages.scrollTop = messages.scrollHeight;
        });
    });
}

// ---------------------------------------------------------------------------
// The shell is sized from the VISUAL viewport on touch
//
// touch.css pins #app to `height: 100dvh; overflow: hidden`, which is right
// everywhere except the one place it matters most: iOS ignores
// `interactive-widget=resizes-content` and does not shrink dvh when the
// on-screen keyboard opens. The shell keeps its full height, the keyboard
// covers the bottom of it, and the composer you are typing into is underneath
// — a known failure on iOS 15 through 18 that no headless browser reproduces.
//
// window.visualViewport is the measurement that does know about the keyboard.
// Its offsetTop matters too: a position:fixed shell is laid out against the
// LAYOUT viewport, so after a pinch-zoom scroll it drifts off the visible one
// unless it is offset back.
// ---------------------------------------------------------------------------

// The visual viewport jitters by a pixel while a page settles — address-bar
// animations, rubber-banding, a caret moving. Rewriting the shell's height for
// those relayouts the whole app for nothing anyone can see.
const VV_JITTER_PX = 2;

function _setupVisualViewport() {
    const root = document.documentElement;
    const vv = window.visualViewport;
    let lastH = -1;
    let lastTop = -1;

    const apply = (height, top) => {
        if (lastH < 0 || Math.abs(height - lastH) >= VV_JITTER_PX) {
            lastH = height;
            root.style.setProperty('--vvh', `${Math.round(height)}px`);
        }
        if (lastTop < 0 || Math.abs(top - lastTop) >= VV_JITTER_PX) {
            lastTop = top;
            root.style.setProperty('--vv-top', `${Math.round(top)}px`);
        }
    };

    const sync = () => {
        // Only touch.css reads these, and only a finger brings up a keyboard.
        if (!isTouch()) return;
        if (vv) apply(vv.height, vv.offsetTop);
        else apply(window.innerHeight, 0);
    };

    sync();
    if (vv) {
        vv.addEventListener('resize', sync);
        vv.addEventListener('scroll', sync);
    } else {
        // No visual viewport: innerHeight is the best available answer, and it
        // at least keeps the shell honest across a rotation.
        window.addEventListener('resize', sync);
    }
}

// ---------------------------------------------------------------------------
// --bottom-stack — how much bottom chrome the floating layer has to clear
//
// Five things float above the composer on touch: the status line, a recovery
// notice, toasts, the service-worker update pill and jump-to-bottom. All five
// carried the same hard-coded 76px (96px for the button), measured once
// against an empty composer. A composer with a long draft in it is 147px, and
// a worker strip adds another ~75px underneath the transcript — so the layer
// that is meant to sit ABOVE the composer sat on top of it the moment anyone
// typed a paragraph, and covered the workers whenever there were any.
//
// One measurement, five consumers. Runs on every device: a ResizeObserver on
// two elements costs nothing, and no desktop rule reads the token.
// ---------------------------------------------------------------------------

function _setupBottomStack() {
    const parts = ['input-wrapper', 'worker-strip']
        .map((id) => document.getElementById(id))
        .filter(Boolean);
    if (!parts.length) return;

    const measure = () => {
        // A hidden worker strip has no box at all, which is exactly 0 of the
        // stack — no special case needed for `hidden`.
        const total = parts.reduce((sum, el) => sum + el.getBoundingClientRect().height, 0);
        document.documentElement.style.setProperty('--bottom-stack', `${Math.round(total)}px`);
    };

    measure();
    if (typeof ResizeObserver !== 'function') return;
    const ro = new ResizeObserver(measure);
    for (const part of parts) ro.observe(part);
}
