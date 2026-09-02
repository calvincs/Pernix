// Pernix — Touch and compact tiers: detection, sidebar drawer, swipe, keyboard.
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

/**
 * @deprecated Kept for one release so nothing breaks mid-refactor. Every
 * caller should say which of the two it means: isCompact() for anything about
 * the layout (drawer, sheets, full-screen panes), isTouch() for anything about
 * the pointer (target sizes, hover, gestures, tooltips).
 */
export function isMobile() { return isCompact(); }

export function initMobile() {
    _stamp();
    // Two independent listeners: crossing 900px must not disturb the touch
    // verdict, and a device that becomes touch (it can only ever become one,
    // see FORCED_TOUCH) must not disturb the layout tier.
    touchMq.addEventListener('change', () => { _stamp(); _applyTouch(); });
    compactMq.addEventListener('change', () => { _stamp(); _applyCompact(); });

    _createScrim();
    _setupSidebarDrawer();
    _setupSwipeGesture();
    _setupFilePanelSwipe();
    _setupKeyboardHandler();

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

// Leaving the compact tier turns the drawer back into a docked sidebar; the
// open state and its scrim mean nothing there and would leave a dead overlay.
function _applyCompact() {
    if (isCompact()) return;
    const sidebar = document.getElementById('sidebar');
    sidebar?.classList.remove('mobile-open');
    _scrim?.classList.remove('visible');
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
// ---------------------------------------------------------------------------

let _scrim = null;

function _createScrim() {
    _scrim = document.createElement('div');
    _scrim.className = 'mobile-scrim';
    document.getElementById('app').appendChild(_scrim);
    _scrim.addEventListener('click', () => closeSidebar());
}

// ---------------------------------------------------------------------------
// Sidebar drawer
// ---------------------------------------------------------------------------

export function closeSidebar() {
    const sidebar = document.getElementById('sidebar');
    sidebar?.classList.remove('mobile-open');
    _scrim?.classList.remove('visible');
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
        const isOpen = sidebar.classList.toggle('mobile-open');
        _scrim.classList.toggle('visible', isOpen);

        // Mutual exclusion: close file panel if open
        if (isOpen) {
            const fp = document.getElementById('file-panel');
            if (fp?.classList.contains('open')) {
                document.getElementById('files-btn')?.click();
            }
        }
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
            sidebar.classList.add('mobile-open');
            _scrim?.classList.add('visible');
        } else if (isOpen && dx < -THRESHOLD) {
            // Swipe left -> close
            closeSidebar();
        }
    }, { passive: true });
}

// ---------------------------------------------------------------------------
// Swipe gestures — file panel (right edge)
// ---------------------------------------------------------------------------

function _setupFilePanelSwipe() {
    let startX = 0;
    let startY = 0;
    let tracking = false;
    let direction = null;
    const EDGE_ZONE = 25;
    const THRESHOLD = 60;

    document.addEventListener('touchstart', (e) => {
        if (!isCompact()) return;
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
        if (!tracking || !isCompact()) return;
        const touch = e.touches[0];
        const dx = touch.clientX - startX;
        const dy = touch.clientY - startY;

        if (!direction) {
            if (Math.abs(dx) > 10 || Math.abs(dy) > 10) {
                direction = Math.abs(dx) > Math.abs(dy) ? 'horizontal' : 'vertical';
            }
        }

        if (direction === 'horizontal') {
            e.preventDefault();
        }
    }, { passive: false });

    document.addEventListener('touchend', (e) => {
        if (!tracking || !isCompact()) { tracking = false; return; }
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
