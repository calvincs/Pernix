// Pernix — the image viewer behind every picture in a transcript.
//
// render.js turns a workspace image in a message into a thumbnail button
// (.chat-image-btn). This module is what the button opens: the picture at
// full size, with the other pictures of the same message one arrow key away.
//
// One listener on the document rather than one per thumbnail: a streaming
// answer re-renders its tail many times a second, and every render makes new
// buttons. Delegation means none of them needs wiring or unwiring.
import { el, text } from '../render.js';
import { icon } from '../icons.js';
import { openOverlay } from '../a11y.js';

let _closeOpen = null;   // the live viewer — one at a time

function _fileName(src) {
    try { return decodeURIComponent(src.split('/').pop() || ''); } catch { return src.split('/').pop() || ''; }
}

/**
 * Show `items[index]` full size.
 *
 * @param {{src: string, alt: string}[]} items  the pictures of one message
 * @param {number} index  which one was clicked
 */
export function openLightbox(items, index = 0) {
    if (!items || !items.length) return;
    if (_closeOpen) _closeOpen();

    let i = Math.min(Math.max(index, 0), items.length - 1);
    let closeOverlay = null;
    const many = items.length > 1;

    const img = el('img', { class: 'lightbox-img', alt: '' });
    const title = el('h2', { class: 'lightbox-title' });
    const count = el('span', { class: 'lightbox-count', 'aria-live': 'polite' });
    const openLink = el('a', {
        class: 'modal-close lightbox-action', target: '_blank', rel: 'noopener noreferrer',
        title: 'Open in a new tab', 'aria-label': 'Open in a new tab',
    }, [icon('external', { size: 14 })]);
    const saveLink = el('a', {
        class: 'modal-close lightbox-action', title: 'Download', 'aria-label': 'Download',
    }, [icon('download', { size: 14 })]);

    const show = (n) => {
        i = (n + items.length) % items.length;
        const item = items[i];
        const name = _fileName(item.src);
        img.setAttribute('src', item.src);
        img.setAttribute('alt', item.alt || name);
        title.textContent = item.alt || name;
        count.textContent = many ? `${i + 1} / ${items.length}` : '';
        openLink.setAttribute('href', item.src);
        saveLink.setAttribute('href', item.src);
        saveLink.setAttribute('download', name);
    };

    const close = () => {
        if (!closeOverlay) return;
        const done = closeOverlay;
        closeOverlay = null;
        done();
        overlay.remove();
        if (_closeOpen === close) _closeOpen = null;
    };
    _closeOpen = close;

    const closeBtn = el('button', {
        class: 'modal-close', type: 'button', title: 'Close', 'aria-label': 'Close', onClick: close,
    }, [icon('x', { size: 14 })]);

    const stage = el('div', { class: 'lightbox-stage' }, [img]);
    if (many) {
        stage.appendChild(el('button', {
            class: 'lightbox-nav lightbox-prev', type: 'button', 'aria-label': 'Previous image',
            onClick: () => show(i - 1),
        }, [text('‹')]));
        stage.appendChild(el('button', {
            class: 'lightbox-nav lightbox-next', type: 'button', 'aria-label': 'Next image',
            onClick: () => show(i + 1),
        }, [text('›')]));
    }

    const card = el('div', { class: 'lightbox-card' }, [
        el('div', { class: 'lightbox-bar' }, [title, count, openLink, saveLink, closeBtn]),
        stage,
    ]);
    const overlay = el('div', { class: 'modal-overlay lightbox-overlay' }, [card]);

    // Only a press that STARTED on the backdrop closes — the same rule as the
    // other dialogs, so a drag that ends outside the picture does not.
    let downOnBackdrop = false;
    overlay.addEventListener('mousedown', (e) => { downOnBackdrop = e.target === overlay; });
    overlay.addEventListener('click', (e) => { if (downOnBackdrop && e.target === overlay) close(); });
    card.addEventListener('keydown', (e) => {
        if (!many) return;
        if (e.key === 'ArrowLeft') { e.preventDefault(); show(i - 1); }
        else if (e.key === 'ArrowRight') { e.preventDefault(); show(i + 1); }
    });

    show(i);
    document.body.appendChild(overlay);
    closeOverlay = openOverlay(card, { initialFocus: closeBtn, onClose: close });
}

/** Wire the transcript's thumbnails to the viewer. Call once at boot. */
export function initLightbox() {
    document.addEventListener('click', (e) => {
        const btn = e.target && e.target.closest ? e.target.closest('.chat-image-btn') : null;
        if (!btn || btn.disabled) return;
        e.preventDefault();
        const scope = btn.closest('.message') || btn.parentElement;
        const all = Array.from(scope.querySelectorAll('.chat-image-btn')).filter(b => !b.disabled);
        const items = all.map(b => {
            const im = b.querySelector('img');
            return { src: b.dataset.full, alt: (im && im.getAttribute('alt')) || '' };
        });
        openLightbox(items, Math.max(0, all.indexOf(btn)));
    });

    // A picture whose file is gone (the workspace was tidied, the path was
    // wrong). `error` does not bubble, hence the capture phase. The thumbnail
    // becomes a plain label instead of a broken-image icon that opens nothing.
    document.addEventListener('error', (e) => {
        const t = e.target;
        if (!t || t.tagName !== 'IMG' || !t.classList.contains('chat-image')) return;
        const btn = t.closest('.chat-image-btn');
        if (!btn || btn.disabled) return;
        const label = t.getAttribute('alt') || _fileName(btn.dataset.full || '');
        btn.disabled = true;
        btn.classList.add('chat-image-missing');
        btn.setAttribute('title', 'Image not found');
        btn.textContent = `image not found: ${label}`;
    }, true);
}
