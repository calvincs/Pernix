// Pernix — Browser notification helpers + global notification SSE

import { api, isOnline } from './api.js';

let _globalSource = null;
let _wantsGlobalConnection = false;

window.addEventListener('pernix:offline', () => {
    if (_globalSource) { _globalSource.close(); _globalSource = null; }
});
window.addEventListener('pernix:online', () => {
    if (_wantsGlobalConnection && !_globalSource) connectGlobalNotifications();
});
// The global stream's server heartbeats are SSE comments — invisible to JS —
// so unlike sse.js there is no event-time signal to detect a half-dead
// connection after mobile sleep. Reconnecting on visibility return is the
// reliable fix: cheap, and EventSource teardown/re-setup is idempotent.
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && _wantsGlobalConnection) {
        connectGlobalNotifications();
    }
});

export function getPermission() {
    if (!('Notification' in window)) return 'unsupported';
    return Notification.permission; // 'default' | 'granted' | 'denied'
}

export async function requestPermission() {
    if (!('Notification' in window)) return false;
    if (Notification.permission === 'granted') return true;
    if (Notification.permission === 'denied') return false;
    const result = await Notification.requestPermission();
    if (result === 'granted') await subscribePush();
    return result === 'granted';
}

export function showNotification(title, body, { session_id = null, urgency = 'normal' } = {}) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;

    const opts = {
        body,
        icon: '/static/img/app-icon-192.png',
        tag: session_id ? `pernix-session-${session_id}` : 'pernix-notify',
        data: { session_id },
        // urgency was accepted and then thrown away. A high-urgency alert is
        // the agent blocked on an answer or a job that failed — exactly the
        // one that must not auto-dismiss into a notification centre while the
        // phone is in a pocket. Normal ones still behave normally.
        requireInteraction: urgency === 'high',
        // Explicit rather than implied: a silent alert for something that
        // needs a person is not an alert.
        silent: false,
    };

    // Prefer SW-backed notifications so the notificationclick handler fires correctly
    if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
        navigator.serviceWorker.ready
            .then(reg => reg.showNotification(title, opts))
            .catch(() => { new Notification(title, opts); });
    } else {
        try {
            const n = new Notification(title, opts);
            n.onclick = () => { window.focus(); n.close(); };
        } catch (e) {
            console.warn('[notify] Notification failed:', e);
        }
    }
}

/**
 * Register the service worker. Called once on page load.
 * Returns the registration or null if unsupported/failed.
 */
export async function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) return null;
    try {
        // /sw.js is served at root with Service-Worker-Allowed: / so it controls the full origin
        const reg = await navigator.serviceWorker.register('/sw.js');
        return reg;
    } catch (e) {
        console.warn('[sw] registration failed:', e);
        return null;
    }
}

/**
 * Subscribe to Web Push and send the subscription to the server.
 * Safe to call on every page load — the server upserts by endpoint.
 */
export async function subscribePush() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
    try {
        const reg = await navigator.serviceWorker.ready;
        const keyRes = await api('GET', '/api/push/vapid-public-key');
        const { publicKey } = await keyRes.json();

        // Check if existing subscription uses current VAPID key
        const existing = await reg.pushManager.getSubscription();
        if (existing) {
            const existingKey = existing.options?.applicationServerKey;
            if (existingKey && _b64Key(existingKey) !== publicKey) {
                // Server VAPID keys changed — old subscription is dead
                await existing.unsubscribe();
                console.info('[push] VAPID key changed, re-subscribing');
            } else {
                // Already subscribed with correct key — re-register with server (upsert)
                await api('POST', '/api/push/subscribe', existing.toJSON());
                return;
            }
        }

        const sub = await reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: publicKey,
        });
        await api('POST', '/api/push/subscribe', sub.toJSON());
    } catch (e) {
        console.warn('[push] subscribe failed:', e);
    }
}

/** ArrayBuffer → base64url (no padding) for VAPID key comparison. */
function _b64Key(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    bytes.forEach(b => binary += String.fromCharCode(b));
    return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/**
 * Connect to the global notification SSE stream.
 * This runs on page load — no session selection required.
 * Handles dialog.notification events and shows browser notifications.
 */
export function connectGlobalNotifications() {
    _wantsGlobalConnection = true;
    if (_globalSource) _globalSource.close();
    if (!isOnline()) return;

    _globalSource = new EventSource('/api/notifications/events');

    _globalSource.addEventListener('dialog.notification', (e) => {
        try {
            const data = JSON.parse(e.data);
            showNotification(data.title || 'Pernix', data.body || '', {
                session_id: data.source_session_id || null,
                urgency: data.urgency || 'normal',
            });
            window.dispatchEvent(new CustomEvent('pernix:bell-update'));
        } catch { /* ignore parse errors */ }
    });

    _globalSource.addEventListener('dialog.question', (e) => {
        try {
            const data = JSON.parse(e.data);
            showNotification(
                data.session_title ? `Question from: ${data.session_title}` : 'Agent Question',
                data.question || 'The agent needs your input',
                { session_id: data.source_session_id || null, urgency: data.urgency || 'normal' },
            );
            window.dispatchEvent(new CustomEvent('pernix:bell-update'));
        } catch { /* ignore parse errors */ }
    });

    _globalSource.onerror = () => {
        // EventSource auto-reconnects; nothing to do here
    };
}

export function disconnectGlobalNotifications() {
    _wantsGlobalConnection = false;
    if (_globalSource) {
        _globalSource.close();
        _globalSource = null;
    }
}
