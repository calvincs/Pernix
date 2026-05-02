// Pernix — Mobile support: detection, sidebar drawer, swipe gestures, keyboard handling

const MOBILE_BP = 768;
// Match narrow viewports OR touch-primary devices (tablets like iPad that are wider than the
// width breakpoint but should still use the drawer-style mobile UI).
const mq = window.matchMedia(`(max-width: ${MOBILE_BP}px), (hover: none) and (pointer: coarse)`);
let _mobile = mq.matches;

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export function isMobile() { return _mobile; }

export function initMobile() {
    _apply(mq.matches);
    mq.addEventListener('change', (e) => {
        _apply(e.matches);
        if (e.matches) {
            _enterMobile();
        } else {
            _exitMobile();
        }
    });
    _createScrim();
    _setupSidebarDrawer();
    _setupSwipeGesture();
    _setupFilePanelSwipe();
    _setupKeyboardHandler();

    // Initial mobile setup if already at mobile width
    if (_mobile) _enterMobile();
}

// ---------------------------------------------------------------------------
// Mobile detection
// ---------------------------------------------------------------------------

function _apply(matches) {
    _mobile = matches;
    if (matches) document.body.setAttribute('data-mobile', '');
    else document.body.removeAttribute('data-mobile');
}

function _enterMobile() {
    _moveToggleIntoHeader();
}

function _exitMobile() {
    _restoreToggleFromHeader();
    // Close sidebar/scrim on transition to desktop
    const sidebar = document.getElementById('sidebar');
    const scrim = document.querySelector('.mobile-scrim');
    sidebar?.classList.remove('mobile-open');
    scrim?.classList.remove('visible');
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
        if (!_mobile) return; // let desktop handler proceed
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
        if (!_mobile) return;
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
        if (!tracking || !_mobile) return;
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
        if (!tracking || !_mobile) { tracking = false; return; }
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
        if (!_mobile) return;
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
        if (!tracking || !_mobile) return;
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
        if (!tracking || !_mobile) { tracking = false; return; }
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

function _setupKeyboardHandler() {
    if (!window.visualViewport) return;

    window.visualViewport.addEventListener('resize', () => {
        if (!_mobile) return;
        const active = document.activeElement;
        if (active?.tagName === 'TEXTAREA' || active?.tagName === 'INPUT') {
            requestAnimationFrame(() => {
                document.getElementById('input-wrapper')?.scrollIntoView({
                    block: 'end',
                    behavior: 'smooth',
                });
            });
        }
    });
}
