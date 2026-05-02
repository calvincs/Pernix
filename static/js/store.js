// Pernix — Proxy-based reactive state store

const listeners = {};

export const state = new Proxy({
    sid: null,
    streaming: false,
    model: '(not set)',
    ctxPct: 0,
    sessions: [],
}, {
    set(target, key, value) {
        target[key] = value;
        (listeners[key] || []).forEach(fn => fn(value));
        return true;
    }
});

export function subscribe(key, fn) {
    if (!listeners[key]) listeners[key] = [];
    listeners[key].push(fn);
    return () => { listeners[key] = listeners[key].filter(f => f !== fn); };
}

// For deep updates: call notify() after mutating nested objects
export function notify(key) {
    (listeners[key] || []).forEach(fn => fn(state[key]));
}
