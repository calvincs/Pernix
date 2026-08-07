// Pernix — Service worker (PWA installability + Web Push + notification click handling)

// App-shell precache. CACHE_VERSION is stamped by the server at serve time
// (__BUILD__ → a hash of the shipped static assets), so every deploy is a
// new SW byte-wise: install → activate purges old caches → clients get a
// controllerchange and refresh (see index.html). Auth tokens live in
// localStorage and are untouched by cache purges. If served without the
// stamp (static hosting), the literal placeholder still works as a manual
// version. Strategy:
//   - static assets (/static/*): cache-first with background revalidate —
//     instant loads, survives server downtime, refreshes itself when online
//   - navigation (/): network-first, cached shell as offline fallback
//   - API/SSE requests and /sw.js itself: never intercepted (pass through)
const CACHE_VERSION = 'pernix-shell-__BUILD__';
const SHELL_ASSETS = [
    '/',
    '/static/css/tokens.css',
    '/static/css/layout.css',
    '/static/css/modals.css',
    '/static/css/jobs.css',
    '/static/css/file-panel.css',
    '/static/css/mobile.css',
    '/static/vendor/marked.min.js',
    '/static/js/app.js',
    '/static/js/store.js',
    '/static/js/render.js',
    '/static/js/api.js',
    '/static/js/sse.js',
    '/static/js/voice.js',
    '/static/js/mobile.js',
    '/static/js/sigil.js',
    '/static/js/notifications.js',
    '/static/img/favicon.png',
    '/static/img/app-icon-192.png',
    '/static/manifest.json',
];

self.addEventListener('install', (e) => {
    e.waitUntil(
        caches.open(CACHE_VERSION)
            .then((cache) => cache.addAll(SHELL_ASSETS))
            .catch(() => { /* partial precache is fine — fetch handler fills in */ })
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (e) => {
    e.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;

    // Never intercept API or SSE traffic.
    if (url.pathname.startsWith('/api/')) return;

    // /sw.js must always hit the network — a SW serving its own script from
    // cache blocks every future update.
    if (url.pathname === '/sw.js') return;

    // Static assets: cache-first + background revalidate.
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.open(CACHE_VERSION).then(async (cache) => {
                const cached = await cache.match(event.request);
                const refresh = fetch(event.request)
                    .then((resp) => {
                        if (resp && resp.ok) cache.put(event.request, resp.clone());
                        return resp;
                    })
                    .catch(() => cached);
                return cached || refresh;
            })
        );
        return;
    }

    // Navigations: network-first, cached shell when the server is down.
    if (event.request.mode === 'navigate') {
        event.respondWith(
            fetch(event.request)
                .then((resp) => {
                    if (resp && resp.ok) {
                        // Clone synchronously — the page starts consuming the
                        // body as soon as we return, after which clone() throws.
                        const copy = resp.clone();
                        caches.open(CACHE_VERSION).then((c) => c.put('/', copy)).catch(() => {});
                    }
                    return resp;
                })
                .catch(() => caches.match('/'))
        );
    }
});

// Web Push: show OS notification when a push message arrives (even with tab closed)
self.addEventListener('push', (event) => {
    const data = event.data?.json() || {};
    event.waitUntil(
        self.registration.showNotification(data.title || 'Pernix', {
            body: data.body || '',
            icon: '/static/img/app-icon-192.png',
            tag: data.session_id ? `pernix-session-${data.session_id}` : 'pernix-notify',
            data: { session_id: data.session_id || null },
        })
    );
});

// Notification click: focus existing window or open new one, then tell it to navigate
self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const sessionId = event.notification.data?.session_id || null;

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
            for (const client of clientList) {
                if ('focus' in client) {
                    if (sessionId) client.postMessage({ type: 'navigate-session', session_id: sessionId });
                    return client.focus();
                }
            }
            return clients.openWindow(sessionId ? `/?session=${sessionId}` : '/');
        })
    );
});
