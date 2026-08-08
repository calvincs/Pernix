// Pernix — Safe DOM rendering utilities (no innerHTML with user content)

export function el(tag, attrs = {}, children = []) {
    const elem = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === 'class') elem.className = v;
        else if (k === 'style' && typeof v === 'object') Object.assign(elem.style, v);
        else if (k.startsWith('on')) elem.addEventListener(k.slice(2).toLowerCase(), v);
        else elem.setAttribute(k, v);
    }
    for (const child of children) {
        if (typeof child === 'string') elem.appendChild(document.createTextNode(child));
        else if (child) elem.appendChild(child);
    }
    return elem;
}

export function text(str) {
    return document.createTextNode(str);
}

export function clear(elem) {
    while (elem.firstChild) elem.removeChild(elem.firstChild);
}

// SVG markup arriving as a string — Mermaid's rendered state diagram and the
// server-generated access QR — is the other place raw markup meets the DOM.
// Both are built from data we do not fully control (the QR encodes the auth
// URL; the Mermaid diagram is built from server-supplied state-log rows whose
// reason strings become node and edge labels), and SVG is a first-class
// scripting context: <script>, event-handler attributes and <use href> all
// execute inside an inline SVG exactly as they would in HTML.
//
// Replaces `host.innerHTML = svg` at those call sites. The `style` attribute
// is deliberately NOT stripped here (unlike the markdown path) because
// Mermaid colours its nodes and edges with inline styles — removing them
// renders the diagram unreadable. Inline CSS cannot execute script in any
// browser this app supports.
const _SVG_PURIFY_CONFIG = {
    RETURN_DOM_FRAGMENT: true,
    USE_PROFILES: { svg: true, svgFilters: true, html: true },
};

export function setSanitizedSvg(host, svgMarkup) {
    clear(host);
    if (typeof DOMPurify === 'undefined' || !DOMPurify.isSupported) {
        // Fail closed, same rule as the markdown path.
        console.error('DOMPurify unavailable — refusing to inject SVG markup');
        host.appendChild(el('div', { class: 'render-error' }, [text('Cannot render: sanitizer unavailable.')]));
        return false;
    }
    host.appendChild(DOMPurify.sanitize(svgMarkup, _SVG_PURIFY_CONFIG));
    return true;
}

// Markdown rendering (uses marked.js if available, plain text fallback)
let _marked = null;

export function initMarked() {
    if (typeof marked !== 'undefined') {
        _marked = marked;
        _marked.setOptions({ breaks: true, gfm: true });
    }
}

// The single markdown chokepoint for the whole app. Everything that renders
// model output, tool output, file contents, skill instructions or memory
// bodies goes through renderMarkdown(), so this is the one place that has to
// be right — all of those are attacker-influenced (a web page the agent read,
// a file in the workspace, a tool result).
//
// marked emits raw HTML by default (GFM allows inline HTML) and has no
// sanitizer of its own — `sanitize: true` was removed in marked v5 precisely
// because rolling your own is a losing game. So the output is passed through
// DOMPurify, vendored in static/vendor/ (no CDN — this app must work fully
// offline on a LAN).
//
// sanitize() runs with RETURN_DOM_FRAGMENT so the result is handed back as
// live nodes. Taking the string form and re-parsing it is what makes mutation
// XSS possible: a payload that is inert when parsed once can become live when
// the sanitized serialization is parsed a second time.
const _PURIFY_CONFIG = {
    RETURN_DOM_FRAGMENT: true,
    // Beyond DOMPurify's defaults. <style> can exfiltrate and spoof via CSS
    // selectors, and <form> plus a formaction turns a rendered message into a
    // credential-harvesting surface. None have a legitimate use in markdown.
    FORBID_TAGS: ['style', 'form', 'iframe', 'object', 'embed', 'base', 'meta', 'link'],
    // Inline styles survive markdown via raw HTML — a cosmetic spoofing vector
    // (invisible text, overlays on top of real UI) with no legitimate use in
    // model output.
    FORBID_ATTR: ['style'],
};

export function renderMarkdown(mdText) {
    const plain = () => el('pre', {}, [text(mdText)]);
    if (!_marked) return plain();
    // Fail closed. If DOMPurify did not load, degrade to plain text rather
    // than rendering unsanitized HTML — a broken-looking message is a far
    // better outcome than script execution on a page holding an auth token.
    if (typeof DOMPurify === 'undefined' || !DOMPurify.isSupported) {
        console.error('DOMPurify unavailable — rendering markdown as plain text');
        return plain();
    }

    const fragment = DOMPurify.sanitize(_marked.parse(mdText), _PURIFY_CONFIG);
    const container = document.createElement('div');
    container.appendChild(fragment);

    // Links open in a new tab — navigating the SPA away mid-turn loses the
    // live stream view. noopener blocks reverse-tabnabbing. Applied after
    // sanitization so a crafted rel/target in the source cannot survive.
    container.querySelectorAll('a[href]').forEach(a => {
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
    });
    return container;
}
