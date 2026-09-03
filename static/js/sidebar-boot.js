// Pernix — sidebar width gate. Runs synchronously in <head>, before first paint.
//
// The sidebar's width is the token --sidebar-w, which layout.css, the 1024px
// media rule and touch.css's Explorer clamp all read. A user who has dragged
// the edge wider has a number in localStorage, and it has to be on the root
// element before anything is painted: `<script type="module">` is deferred, so
// applying it from app.js would show 270px, lay the whole app out against it,
// and then animate to 420px on every single load. Same reasoning as
// theme-boot.js next door, and it cannot be inline for the same reason —
// index.html ships `script-src 'self'` with no 'unsafe-inline'.
//
// sidebar.js (the module) owns the drag, the keyboard steps and the same
// clamp; it reads the copy this file publishes on window.__pernixSidebarWidth
// so the two can never drift apart at runtime.
(function () {
    var KEY = 'pernix:sidebar-width';   // integer px
    var MIN = 200;
    // The cap is whichever is tighter: 520px, or 45% of the window. A stored
    // width from a wide monitor must not eat half of a laptop screen, so the
    // proportional half of it is re-checked here at boot and again on resize.
    var HARD_MAX = 520;

    function max() {
        return Math.max(MIN, Math.min(HARD_MAX, Math.round(window.innerWidth * 0.45)));
    }

    // Anything that is not a plain integer in range is treated as absent
    // rather than repaired: a corrupt value should give the default layout,
    // not a nearby guess at one.
    function read() {
        try {
            var raw = localStorage.getItem(KEY);
            if (!raw || !/^\d+$/.test(raw)) return null;
            var v = parseInt(raw, 10);
            return (v >= MIN && v <= HARD_MAX) ? v : null;
        } catch (e) {
            // Private mode, or storage blocked for this origin. Default width.
            return null;
        }
    }

    function apply(w) {
        if (w == null) document.documentElement.style.removeProperty('--sidebar-w');
        else document.documentElement.style.setProperty('--sidebar-w', w + 'px');
    }

    function clear() {
        try { localStorage.removeItem(KEY); } catch (e) { /* nothing to clear */ }
        apply(null);
    }

    window.__pernixSidebarWidth = {
        KEY: KEY, MIN: MIN, HARD_MAX: HARD_MAX,
        max: max, read: read, apply: apply, clear: clear,
    };

    var stored = read();
    if (stored != null) apply(Math.min(stored, max()));
})();
