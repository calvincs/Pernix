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
        // visibilitychange alone never fires for a desktop tab left open for
        // days, which is exactly the session that ends up several builds
        // behind. Hourly is cheap: an unchanged /sw.js is a 304.
        setInterval(function () {
            reg.update().catch(function () { /* offline is fine */ });
        }, 60 * 60 * 1000);
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
        // Styling lives in layout.css / touch.css (#sw-update-banner) so it
        // uses the real tokens and can be anchored above the composer on a
        // phone, where a centred pill sat on top of the send button.
        var banner = document.createElement('div');
        banner.id = 'sw-update-banner';

        var refresh = document.createElement('button');
        refresh.type = 'button';
        refresh.className = 'sw-update-action';
        refresh.textContent = 'Pernix updated — tap to refresh';
        refresh.addEventListener('click', function () {
            reloaded = true;
            location.reload();
        });

        // Without this the pill is unremovable: the only way out was to take
        // the update you were in the middle of something else to avoid.
        var dismiss = document.createElement('button');
        dismiss.type = 'button';
        dismiss.className = 'sw-update-dismiss';
        dismiss.setAttribute('aria-label', 'Dismiss');
        dismiss.textContent = '\u00d7';
        dismiss.addEventListener('click', function () { banner.remove(); });

        banner.appendChild(refresh);
        banner.appendChild(dismiss);
        document.body.appendChild(banner);
    });
}
