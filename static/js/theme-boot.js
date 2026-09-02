// Pernix — theme gate. Runs synchronously in <head>, before first paint.
//
// The app is dark-first: dark lives on the bare :root in tokens.css and the
// light palette is an override, applied either because the OS asked for it
// (`@media (prefers-color-scheme: light)`) or because the user chose it
// (`:root[data-theme="light"]`). Only the second case needs script, and it
// needs to happen before anything is painted — a user who picked Light on a
// machine set to Dark would otherwise get a black flash on every load.
//
// Which is why this is a classic script in <head> and not the top of a
// module: `<script type="module">` is deferred, so app.js runs after the
// document is parsed and, on a slow machine, after the first paint. It also
// cannot be an inline script — index.html ships `script-src 'self'` with no
// 'unsafe-inline'.
//
// theme.js (the module) owns the same key and the same apply() logic for the
// Settings control; it prefers the copy this file publishes on
// window.__pernixTheme so the two can never drift apart at runtime.
(function () {
    var KEY = 'pernix_theme';   // 'system' | 'dark' | 'light'

    // The browser-chrome colour per theme. Kept in step with --bg-raised,
    // which is what the mobile header paints.
    var CHROME = { dark: '#151515', light: '#f4f0e6' };

    function read() {
        try {
            var v = localStorage.getItem(KEY);
            return (v === 'dark' || v === 'light') ? v : 'system';
        } catch (e) {
            // Private mode, or storage blocked for this origin. System it is.
            return 'system';
        }
    }

    // <meta name="theme-color"> ships twice, each gated on a media query, so
    // "system" is correct with no script at all. An explicit choice has to
    // override both: drop the media attributes and paint the chosen colour.
    function applyChrome(mode) {
        var metas = document.querySelectorAll('meta[name="theme-color"]');
        for (var i = 0; i < metas.length; i++) {
            var m = metas[i];
            var own = m.getAttribute('data-scheme');   // 'dark' | 'light'
            if (mode === 'system') {
                m.setAttribute('media', '(prefers-color-scheme: ' + own + ')');
                m.setAttribute('content', CHROME[own]);
            } else {
                m.removeAttribute('media');
                m.setAttribute('content', CHROME[mode]);
            }
        }
    }

    function apply(mode) {
        if (mode === 'dark' || mode === 'light') {
            document.documentElement.setAttribute('data-theme', mode);
        } else {
            document.documentElement.removeAttribute('data-theme');
        }
        applyChrome(mode);
    }

    window.__pernixTheme = { KEY: KEY, read: read, apply: apply };
    apply(read());
})();
