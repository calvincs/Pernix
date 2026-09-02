// Pernix — theme preference, and reading the live palette from JS.
//
// Two jobs:
//
//   getTheme/setTheme   the System / Dark / Light preference behind
//                       Settings -> General -> Appearance. Stored in
//                       localStorage `pernix_theme`; applied by writing
//                       <html data-theme>, which is what tokens.css keys the
//                       light palette off. theme-boot.js does the same thing
//                       in <head> so the first paint is already correct; this
//                       module reuses that implementation when it is there.
//
//   readColor/isLight   the three places that paint colours in JS rather than
//                       CSS — the sigil canvas, Monaco's editor theme, and the
//                       Mermaid state diagram — need real rgb values, not
//                       token names. Each of them used to carry its own
//                       hard-coded copy of the dark palette, which is exactly
//                       why the sigil vanished on paper.
//
// Anything that paints from a token should also listen for `pernix:theme` on
// window and repaint: the theme can change without a reload, both from the
// Settings control and from the OS.

const KEY = 'pernix_theme';
const MODES = ['system', 'dark', 'light'];

const _boot = () => (typeof window !== 'undefined' ? window.__pernixTheme : null);

/** 'system' | 'dark' | 'light' — what the user chose, not what is showing. */
export function getTheme() {
    const boot = _boot();
    if (boot) return boot.read();
    try {
        const v = localStorage.getItem(KEY);
        return MODES.includes(v) ? v : 'system';
    } catch { return 'system'; }
}

/**
 * Store and apply a preference, then tell the rest of the app.
 * @param {'system'|'dark'|'light'} mode
 */
export function setTheme(mode) {
    const next = MODES.includes(mode) ? mode : 'system';
    try { localStorage.setItem(KEY, next); } catch { /* storage blocked */ }
    const boot = _boot();
    if (boot) {
        boot.apply(next);
    } else if (next === 'system') {
        document.documentElement.removeAttribute('data-theme');
    } else {
        document.documentElement.setAttribute('data-theme', next);
    }
    _announce();
    return next;
}

function _announce() {
    window.dispatchEvent(new CustomEvent('pernix:theme', {
        detail: { theme: getTheme(), light: isLight() },
    }));
}

// A "system" preference still changes when the OS does, and the canvas and the
// editor have to hear about it.
if (typeof window !== 'undefined' && window.matchMedia) {
    const mq = window.matchMedia('(prefers-color-scheme: light)');
    const onChange = () => { if (getTheme() === 'system') _announce(); };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
}

// ---------------------------------------------------------------------------
// Reading the live palette
// ---------------------------------------------------------------------------

// A custom property's value is its literal text — `#d4a843`, or a whole
// `color-mix(...)` expression — so getPropertyValue is not enough to get rgb
// out of it. Setting it on a probe element and reading back the COMPUTED
// `color` is: the engine resolves the token, the mix and any fallback for us.
let _probe = null;
function _probeEl() {
    if (_probe && _probe.isConnected) return _probe;
    _probe = document.createElement('span');
    _probe.setAttribute('aria-hidden', 'true');
    _probe.style.cssText = 'position:absolute;left:-9999px;top:0;width:0;height:0;pointer-events:none';
    document.documentElement.appendChild(_probe);
    return _probe;
}

/**
 * The current value of a colour token, as [r, g, b, a].
 *
 * @param {string} token e.g. '--accent'
 * @param {number[]} [fallback] used if the token is unset or unparseable.
 */
export function readColor(token, fallback = [0, 0, 0, 1]) {
    try {
        const el = _probeEl();
        el.style.color = '';
        el.style.color = `var(${token})`;
        const computed = getComputedStyle(el).color;
        const parts = computed.match(/[\d.]+/g);
        if (!parts || parts.length < 3) return fallback;
        return [
            Number(parts[0]), Number(parts[1]), Number(parts[2]),
            parts.length > 3 ? Number(parts[3]) : 1,
        ];
    } catch {
        return fallback;
    }
}

/** The same token as a CSS colour string, optionally at a different alpha. */
export function rgba(token, alpha = null, fallback = [0, 0, 0, 1]) {
    const [r, g, b, a] = readColor(token, fallback);
    return `rgba(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)}, ${alpha == null ? a : alpha})`;
}

/** The same token as `#rrggbb` — Monaco's theme API takes hex, not rgb(). */
export function hex(token, fallback = [0, 0, 0, 1]) {
    const [r, g, b] = readColor(token, fallback);
    const h = (n) => Math.round(n).toString(16).padStart(2, '0');
    return `#${h(r)}${h(g)}${h(b)}`;
}

/** Is the page currently painted on a light ground? */
export function isLight() {
    const [r, g, b] = readColor('--bg', [14, 14, 14]);
    const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b) > 0.5;
}
