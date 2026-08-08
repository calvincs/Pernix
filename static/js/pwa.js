// Pernix — PWA lifecycle: register the SW, check for new builds whenever the
// app is foregrounded (mobile PWAs otherwise poll at most daily), and refresh
// the UI when a new build takes control. Auth tokens and drafts live in
// localStorage — a refresh never touches them.
//
// Lives in its own file rather than inline in index.html so the page can ship
// a `script-src 'self'` CSP with no 'unsafe-inline' escape hatch. That CSP is
// what actually enforces the repo's "vendored assets only, no CDN" rule: with
// it, a stray <script src="https://cdn..."> fails to execute instead of
// silently reintroducing third-party code onto a page holding an auth cookie.

if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').then(function (reg) {
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') {
                reg.update().catch(function () { /* offline is fine */ });
            }
        });
    }).catch(function (e) {
        console.warn('SW registration failed:', e);
    });

    var hadController = !!navigator.serviceWorker.controller;
    var reloaded = false;
    navigator.serviceWorker.addEventListener('controllerchange', function () {
        // First controller on a fresh install is not an update.
        if (!hadController) { hadController = true; return; }
        if (reloaded) return;
        if (document.visibilityState === 'hidden') {
            // Backgrounded (typical mobile PWA): refresh silently.
            reloaded = true;
            location.reload();
            return;
        }
        // Visible: never yank the page mid-interaction — offer a tap.
        if (document.getElementById('sw-update-banner')) return;
        var banner = document.createElement('button');
        banner.id = 'sw-update-banner';
        banner.textContent = 'Pernix updated — tap to refresh';
        banner.style.cssText = 'position:fixed;bottom:calc(16px + env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);z-index:10000;padding:10px 18px;border-radius:999px;border:1px solid var(--border,#444);background:var(--surface,#1c1c1e);color:var(--text,#eee);font:inherit;box-shadow:0 4px 16px rgba(0,0,0,.35);cursor:pointer;';
        banner.addEventListener('click', function () {
            reloaded = true;
            location.reload();
        });
        document.body.appendChild(banner);
    });
}
