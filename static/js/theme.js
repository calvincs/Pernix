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
    // `transition: none !important` is load-bearing, not tidiness. The
    // reduced-motion block in tokens.css puts `transition-duration: .01ms
    // !important` on `*` — deliberately near-zero rather than zero so
    // transitionend still fires — and the probe is a `*`. With a duration on
    // it, assigning `color` STARTS a transition, and the computed value read
    // back one statement later is the interpolated one: the colour the probe
    // held before, spelled `oklab(…)`. Every token then read back as the same
    // stale colour, so for anyone browsing with reduced motion the whole
    // palette collapsed onto whatever was read first. Inline `!important`
    // outranks the `*` rule, so the probe never animates and every read is the
    // settled value.
    _probe.style.cssText = 'position:absolute;left:-9999px;top:0;width:0;height:0;'
        + 'pointer-events:none;transition:none!important;animation:none!important';
    document.documentElement.appendChild(_probe);
    return _probe;
}

// …but a computed `color` is not always spelled `rgb(r, g, b)`. Chromium
// serialises a token whose value is a `color-mix()` in the modern syntax:
//
//     --accent               ->  rgb(138, 100, 16)              0..255 ints
//     --state-processing-bg  ->  color(srgb 0.807 0.845 0.861)  0..1 floats
//
// The old parser pulled every digit out with one regex and read them all as
// 0..255, so each color-mix token — which is every --state-*-fg/-bg pair in
// tokens.css — came back as [0.8, 0.84, 0.86] and `hex()` rounded that to
// #010101. The State timeline's Mermaid nodes, their labels and the
// time-in-state bar all painted black, in both themes.
//
// A 1×1 canvas resolves ANY colour string an engine can hand back — rgb(),
// color(srgb …), lab(), oklch(), a bare keyword — to 0..255 rgba, so that is
// the primary path. _parseColor() below is what runs where there is no 2d
// context to paint into.
let _ctx;
let _ctxTried = false;
function _canvas2d() {
    if (_ctxTried) return _ctx;
    _ctxTried = true;
    try {
        const c = document.createElement('canvas');
        c.width = 1;
        c.height = 1;
        _ctx = c.getContext('2d', { willReadFrequently: true }) || null;
    } catch { _ctx = null; }
    return _ctx;
}

/** Any CSS colour string as [r, g, b, a], by painting one pixel. Null if it can't. */
function _paintColor(css) {
    const ctx = _canvas2d();
    if (!ctx) return null;
    try {
        // An unrecognised value leaves fillStyle at whatever it already was,
        // which would quietly hand back the previous colour. Offer it two
        // different starting points: only a value the canvas actually
        // understood makes the two agree.
        ctx.fillStyle = '#000';
        ctx.fillStyle = css;
        const first = ctx.fillStyle;
        ctx.fillStyle = '#fff';
        ctx.fillStyle = css;
        if (ctx.fillStyle !== first) return null;
        ctx.clearRect(0, 0, 1, 1);
        ctx.fillRect(0, 0, 1, 1);
        const d = ctx.getImageData(0, 0, 1, 1).data;
        return [d[0], d[1], d[2], d[3] / 255];
    } catch {
        return null;   // no canvas, or reading the pixel back is blocked
    }
}

const _CHANNEL = /[-+]?(?:\d*\.\d+|\d+)%?/g;

/**
 * Fallback parser for the function syntaxes a computed `color` can take.
 * `rgb()/rgba()` channels are 0..255, `color(<space> …)` channels are 0..1,
 * either may be a percentage, and alpha is either the fourth channel or comes
 * after a `/`. srgb-linear and display-p3 are read as plain srgb: wrong in the
 * last few percent, right enough for a chart fill, and far better than black.
 *
 * Only those two spellings — a lab()/oklch() triple read as rgb would be
 * nonsense, so it returns null and readColor's caller-supplied fallback wins.
 */
function _parseColor(css) {
    const m = /^(rgba?|color)\(([^)]*)\)$/i.exec(String(css).trim());
    if (!m) return null;
    let body = m[2];
    let scale = 1;                                      // rgb() is already 0..255
    if (m[1].toLowerCase() === 'color') {
        body = body.replace(/^\s*[a-z0-9-]+\s+/i, '');  // drop `srgb`, `display-p3`
        scale = 255;                                    // whose channels are 0..1
    }
    const [head, tail] = body.split('/');
    const chans = head.match(_CHANNEL) || [];
    if (chans.length < 3) return null;
    const chan = (t) => (t.endsWith('%') ? parseFloat(t) * 2.55 : parseFloat(t) * scale);
    const alphaTok = tail ? (tail.match(_CHANNEL) || [])[0] : chans[3];
    const alpha = alphaTok == null ? 1
        : (alphaTok.endsWith('%') ? parseFloat(alphaTok) / 100 : parseFloat(alphaTok));
    const clamp = (n, hi) => Math.min(hi, Math.max(0, n));
    const out = [chan(chans[0]), chan(chans[1]), chan(chans[2])].map(n => clamp(n, 255));
    out.push(clamp(alpha, 1));
    return out.every(Number.isFinite) ? out : null;
}

/**
 * The current value of a colour token, as [r, g, b, a] — rgb 0..255, a 0..1.
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
        if (!computed) return fallback;
        return _paintColor(computed) || _parseColor(computed) || fallback;
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
