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

// Markdown rendering (uses marked.js if available, plain text fallback)
let _marked = null;

export function initMarked() {
    if (typeof marked !== 'undefined') {
        _marked = marked;
        _marked.setOptions({ breaks: true, gfm: true });
    }
}

export function renderMarkdown(mdText) {
    if (!_marked) return el('pre', {}, [text(mdText)]);
    // DOMParser-based sanitization for markdown output
    const raw = _marked.parse(mdText);
    const parser = new DOMParser();
    const doc = parser.parseFromString(raw, 'text/html');
    // Strip dangerous elements (XSS vectors beyond just <script>)
    const DANGEROUS = 'script,iframe,object,embed,form,base,meta,link,style';
    doc.querySelectorAll(DANGEROUS).forEach(s => s.remove());
    // Strip event handler attributes and dangerous URIs
    const URI_ATTRS = new Set(['href', 'src', 'action', 'formaction', 'xlink:href', 'poster', 'data']);
    const BLOCKED_SCHEMES = ['javascript:', 'vbscript:', 'data:text/html', 'data:application/javascript'];
    doc.querySelectorAll('*').forEach(node => {
        for (const attr of [...node.attributes]) {
            if (attr.name.startsWith('on')) node.removeAttribute(attr.name);
            if (URI_ATTRS.has(attr.name)) {
                const val = attr.value.replace(/[\x00-\x1f\s]+/g, '').toLowerCase();
                if (BLOCKED_SCHEMES.some(s => val.startsWith(s))) {
                    node.removeAttribute(attr.name);
                }
            }
        }
    });
    // Use appendChild instead of innerHTML to avoid re-parsing
    const container = document.createElement('div');
    while (doc.body.firstChild) {
        container.appendChild(doc.body.firstChild);
    }
    return container;
}
