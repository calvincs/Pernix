// Pernix — Service worker (PWA installability + Web Push + notification click handling)
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
// No fetch handler — all requests pass through to network.

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
