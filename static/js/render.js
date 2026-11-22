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

// GFM strikethrough accepts a SINGLE tilde. So "the ~256-byte vocabulary (vs
// ~100K tokens) is **the point**" renders everything between the two tildes
// struck through and the bold after it as raw asterisks. Approximate numbers
// are a normal way for a model to write — 182 of the 4 121 assistant messages
// in the month to 09-14 contain a lone tilde — and not one of them meant
// strikethrough, while ~~this~~ is rare and unambiguous. Double only.
//
// Requiring a non-space, non-tilde character on the inside of each fence is
// GFM's own rule, kept so `~~ x ~~` stays literal.
const DOUBLE_TILDE_DEL = /^~~(?=[^\s~])((?:\\.|[^\\])*?(?:\\.|[^\s~\\]))~~(?=[^~]|$)/;

export function initMarked() {
    if (typeof marked === 'undefined') return;
    _marked = marked;
    _marked.setOptions({ breaks: true, gfm: true });
    // Returning undefined rather than false is load-bearing: marked's use()
    // wraps a tokenizer override so that a `false` return falls back to the
    // ORIGINAL tokenizer — which is the one-tilde rule being replaced.
    _marked.use({
        tokenizer: {
            del(src) {
                const m = DOUBLE_TILDE_DEL.exec(src);
                if (!m) return undefined;
                return { type: 'del', raw: m[0], text: m[1], tokens: this.lexer.inlineTokens(m[1]) };
            },
        },
    });
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
    enhanceChatImages(container);
    return container;
}

// ── Pictures in a message ────────────────────────────────────────────────────
//
// A model that has made or edited a picture shows it the way it shows anything
// else: `![a dog on a beach](generated_images/dog_s7.png)`. Three things stood
// between that and a usable transcript. The path is relative to the WORKSPACE
// but an <img> resolves it against the page, so it 404'd unless the model
// happened to write the `/workspace/` prefix. The picture rendered at its
// natural size, and a 2048 px render is wider than the column. And there was no
// way to look at it properly.
//
// So a workspace image becomes a thumbnail button, and components/lightbox.js
// opens it full size. Anything that is not a workspace image is left exactly
// as the sanitizer returned it — the page CSP (`img-src 'self' data: blob:`)
// already refuses a remote one, which is what keeps a markdown image from
// being a way to send data out.

const CHAT_IMAGE_EXT = /\.(?:png|jpe?g|gif|webp|bmp)$/i;
const ATTACHED_REF = /\[attached:\s*([^\]]+)\]/g;

/**
 * The URL a workspace image is served from, or null when `src` is not one.
 *
 * Accepts the spellings a model actually writes: `dir/x.png`, `./dir/x.png`,
 * `/workspace/dir/x.png`, `workspace/dir/x.png`, `/app/data/workspace/dir/x.png`.
 * Refuses anything with a scheme, a protocol-relative URL, a `..` segment,
 * another absolute path of this app (`/static/…`, `/api/…`), and any file that
 * is not a raster image — SVG included, because it is a scripting context.
 */
export function workspaceImageSrc(src) {
    if (typeof src !== 'string') return null;
    let path = src.trim().split(/[?#]/)[0];
    if (!path || path.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(path)) return null;
    if (path.startsWith('/')) {
        const m = /^\/(?:app\/data\/workspace|data\/workspace|workspace)\/(.+)$/.exec(path);
        if (!m) return null;
        path = m[1];
    } else {
        path = path.replace(/^(?:\.\/)+/, '').replace(/^(?:data\/)?workspace\//, '');
    }
    if (!CHAT_IMAGE_EXT.test(path)) return null;
    const out = [];
    for (const raw of path.split('/')) {
        let seg = raw;
        try { seg = decodeURIComponent(raw); } catch { /* a literal % in a name */ }
        if (!seg || seg === '.' ) continue;
        if (seg === '..' || seg.includes('/') || seg.includes('\\')) return null;
        out.push(encodeURIComponent(seg));
    }
    return out.length ? '/workspace/' + out.join('/') : null;
}

/** The image files a user message carries as `[attached: name]` references,
 *  as workspace URLs, in order, without repeats. */
export function attachedImageSrcs(content) {
    const out = [];
    if (typeof content !== 'string') return out;
    for (const m of content.matchAll(ATTACHED_REF)) {
        const src = workspaceImageSrc(m[1].trim());
        if (src && !out.includes(src)) out.push(src);
    }
    return out;
}

function _chatImageButton(src, alt) {
    const img = el('img', { class: 'chat-image', src, alt: alt || '', loading: 'lazy', decoding: 'async' });
    return el('button', {
        class: 'chat-image-btn',
        type: 'button',
        'data-full': src,
        title: alt || 'View image',
        'aria-label': alt ? `View image: ${alt}` : 'View image',
    }, [img]);
}

/** Turn every workspace <img> under `container` into a thumbnail button, and
 *  lay a paragraph that holds nothing but two or more of them out as a row. */
export function enhanceChatImages(container) {
    container.querySelectorAll('img').forEach(img => {
        if (img.closest('a, button')) return;
        const src = workspaceImageSrc(img.getAttribute('src') || '');
        if (!src) return;
        img.replaceWith(_chatImageButton(src, (img.getAttribute('alt') || '').trim()));
    });
    container.querySelectorAll('p').forEach(p => {
        const kids = Array.from(p.childNodes).filter(n =>
            !(n.nodeType === 3 && !n.textContent.trim()) && n.nodeName !== 'BR');
        if (kids.length < 2) return;
        if (!kids.every(n => n.nodeType === 1 && n.classList.contains('chat-image-btn'))) return;
        p.querySelectorAll('br').forEach(br => br.remove());
        p.classList.add('chat-image-row');
    });
}

/** A row of thumbnails for the images attached to a user message, or null. */
export function attachedImageRow(content) {
    const srcs = attachedImageSrcs(content);
    if (!srcs.length) return null;
    return el('div', { class: 'chat-image-row chat-attachments' }, srcs.map(src => {
        let name = src.split('/').pop();
        try { name = decodeURIComponent(name); } catch { /* keep the encoded name */ }
        return _chatImageButton(src, name);
    }));
}
