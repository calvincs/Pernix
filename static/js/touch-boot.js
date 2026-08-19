// Pernix — touch UI gate. Runs synchronously in <head>, before first paint.
//
// THE PROBLEM
// iPadOS defaults to desktop-class browsing, in Safari and in Chrome alike
// (Chrome on iOS is a WebKit wrapper — Apple requires it, so both browsers
// behave identically here). In that mode the browser reports:
//
//     hover: hover      pointer: fine
//     navigator.userAgent  ->  "...(Macintosh; Intel Mac OS X 10_15_7)..."
//     layout width         ->  ~1024px, above the 768px breakpoint
//
// So `(max-width: 768px), (hover: none) and (pointer: coarse)` — the gate every
// touch rule in this app used to sit behind — was false on an iPad. The tablet
// got the desktop layout: 13px inputs that make iOS zoom in on focus and never
// zoom back, hover-only rename/pin/delete controls that are invisible but still
// tappable, no drawer, no swipe gestures, and no safe-area insets.
//
// THE SIGNAL
// navigator.maxTouchPoints is the one thing desktop mode does not fake, because
// faking it would break touch input. No Mac has a touchscreen, so "claims to be
// a Mac AND reports touch points" identifies an iPad with no false positives.
//
// Deliberately narrower than `(any-pointer: coarse)`, the other common answer:
// that also matches touchscreen Windows laptops and Chromebooks, where the
// mouse is the primary input and the desktop layout is the correct one. Android
// tablets need nothing here — they report hover/pointer honestly already.
//
// WHAT IT TOUCHES
//   1. <html data-touch-ui>   — read by mobile.js (drawer + swipe), sigil.js
//                               (animation power tier) and notification-bell.js
//                               (iOS Home Screen instructions). Set once at
//                               boot; nothing clears it.
//   2. mobile.css's link      — flipped to media="all". That stylesheet keeps
//                               its gate on the <link> rather than in an
//                               internal @media precisely so this line can
//                               reach it.
//
// A classic script in its own file, not an inline one: index.html ships a
// `script-src 'self'` CSP with no 'unsafe-inline' escape hatch.
(function () {
    var ua = navigator.userAgent || '';
    var platform = navigator.platform || '';
    var touchPoints = navigator.maxTouchPoints || 0;

    var isIOS = /iPad|iPhone|iPod/.test(ua) ||          // mobile-mode UA
                (/Mac/.test(platform) && touchPoints > 0);  // desktop-mode iPad
    if (!isIOS) return;

    document.documentElement.setAttribute('data-touch-ui', '');

    var link = document.querySelector('link[href*="/css/mobile.css"]');
    if (link) link.media = 'all';
})();
