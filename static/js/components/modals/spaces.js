// Pernix — Spaces modal: create/edit (label, color, directive overrides)
// and the delete dialog with its cascade checkbox (v33).
//
// Styling contract: buttons are `btn btn-*` (the bare variant classes carry
// only color — .btn carries shape), footer status is `.save-status`, and the
// directive tabs reuse the shared .tab-bar/.tab-btn look. The edit card is
// user-resizable (CSS resize: both); Monaco's automaticLayout tracks it.
import { el, text, clear } from '../../render.js';
import { get, post, del, patch, apiJson } from '../../api.js';
import { createCodeEditor } from '../file-panel.js';
import { announce, openOverlay } from '../../a11y.js';
import { notify } from '../../feedback.js';
import { confirmDanger } from './confirm.js';

const SWATCHES = ['#7c9cff', '#ff8a65', '#4db6ac', '#ba68c8', '#ffd54f', '#81c784', '#f06292', '#90a4ae'];
const DIRECTIVES = ['SOUL', 'RULES', 'SESSIONS'];
const DIRECTIVE_HINT = {
    SOUL: 'Identity — who the agent is in this space. Overrides data/agent/SOUL.md.',
    RULES: 'Binding rules for this space. Overrides data/agent/RULES.md.',
    SESSIONS: 'Deployment/config notes for this space. Overrides data/agent/SESSIONS.md.',
};
const MAX_DIRECTIVE_BYTES = 64000;  // mirrors the API cap

function _notifyChanged() {
    window.dispatchEvent(new CustomEvent('pernix:sessions-changed'));
}

// ---------------------------------------------------------------------------
// Create / edit modal
// ---------------------------------------------------------------------------

let _openSpaceModal = null;  // the live instance, so re-entry can close it properly

export function openSpaceModal(space) {
    // close(), not .remove(): the previous modal owns a document keydown
    // listener and (in the directives tab) Monaco instances, and tearing its
    // node out of the DOM left both behind.
    if (_openSpaceModal) {
        try { _openSpaceModal(); } catch {}
        _openSpaceModal = null;
    }
    document.querySelector('.space-modal-overlay')?.remove();
    const isEdit = !!space;
    let color = space?.color || SWATCHES[0];
    // Per-directive editor state: {mode: 'default'|'edit', editor, dirty,
    // hadOverride, revert}. Filled lazily when the tab loads.
    const dirState = {};
    const disposers = [];

    const labelInput = el('input', {
        class: 'space-label-input',
        type: 'text',
        placeholder: 'Space name',
        value: space?.label || '',
        maxlength: '120',
    });

    const colorRow = el('div', { class: 'space-color-row' });
    const customColor = el('input', {
        type: 'color',
        class: 'space-color-custom',
        value: color,
        title: 'Custom color',
        'aria-label': 'Custom color',
    });
    const paintSwatches = () => {
        clear(colorRow);
        for (const c of SWATCHES) {
            colorRow.appendChild(el('button', {
                class: 'space-swatch' + (c === color ? ' selected' : ''),
                type: 'button',
                style: `background:${c}`,
                title: c,
                'aria-label': `Color ${c}`,
                'aria-pressed': String(c === color),
                onClick: (e) => { e.preventDefault(); color = c; customColor.value = c; paintSwatches(); },
            }));
        }
        colorRow.appendChild(customColor);
    };
    customColor.addEventListener('input', () => { color = customColor.value; paintSwatches(); });
    paintSwatches();

    const body = el('div', { class: 'modal-body space-modal-body' }, [
        el('div', { class: 'space-form-grid' }, [
            el('div', { class: 'space-form-name' }, [
                el('label', { class: 'space-field-label' }, [text('Name')]),
                labelInput,
            ]),
            el('div', { class: 'space-form-color' }, [
                el('label', { class: 'space-field-label' }, [text('Color')]),
                colorRow,
            ]),
        ]),
    ]);
    if (isEdit) {
        body.appendChild(el('div', { class: 'space-slug-note' }, [
            text(`slug: ${space.slug} · memory: pernix.space.${space.slug}.* · workspace: spaces/${space.slug}/`),
        ]));
        body.appendChild(_buildDirectivesSection(space, dirState, disposers, () => save()));
    } else {
        body.appendChild(el('div', { class: 'space-slug-note' }, [
            text('Directive overrides (SOUL / RULES / SESSIONS) can be edited here after the space is created.'),
        ]));
    }

    const status = el('span', { class: 'save-status status-muted', role: 'status' });
    const setStatus = (msg, kind = 'muted') => {
        status.textContent = msg;
        status.className = `save-status status-${kind}`;
    };

    let closeOverlay = null;   // set once the overlay is in the DOM
    const close = () => {
        if (closeOverlay) { closeOverlay(); closeOverlay = null; }
        for (const d of disposers) { try { d(); } catch { /* disposed */ } }
        for (const st of Object.values(dirState)) {
            if (st && st.editor && st.editor.dispose) {
                try { st.editor.dispose(); } catch { /* already gone */ }
            }
            if (st) st.editor = null;
        }
        overlay.remove();
        if (_openSpaceModal === close) _openSpaceModal = null;
    };
    _openSpaceModal = close;

    const save = async () => {
        const label = labelInput.value.trim();
        if (!label) { setStatus('Name is required', 'error'); return; }
        setStatus('Saving…');
        try {
            if (isEdit) {
                await patch(`/api/spaces/${space.id}`, { label, color });
                // Persist any customized/reverted directives.
                for (const name of DIRECTIVES) {
                    const st = dirState[name];
                    if (!st) continue;
                    if (st.mode === 'edit' && st.editor && (st.dirty || !st.hadOverride)) {
                        const content = st.editor.getValue();
                        if (content.trim()) {
                            await apiJson('PUT', `/api/spaces/${space.id}/directives/${name}`, { content });
                            st.dirty = false;
                            st.hadOverride = true;
                        }
                    } else if (st.revert && st.hadOverride) {
                        await del(`/api/spaces/${space.id}/directives/${name}`);
                        st.hadOverride = false;
                        st.revert = false;
                    }
                }
                setStatus('Saved', 'muted');
                _notifyChanged();
            } else {
                await post('/api/spaces', { label, color });
                _notifyChanged();
                close();
            }
        } catch (e) {
            setStatus(`Save failed: ${e.message || e}`, 'error');
        }
    };

    const card = el('div', { class: 'modal-card space-modal-card' + (isEdit ? '' : ' compact') }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text(isEdit ? `Space — ${space.label}` : 'New space')]),
            el('button', {
                class: 'modal-close',
                title: 'Close',
                'aria-label': 'Close',
                onClick: close,
            }, [text('×')]),
        ]),
        body,
        el('div', { class: 'modal-footer' }, [
            status,
            el('button', { class: 'btn btn-secondary', onClick: close }, [text(isEdit ? 'Close' : 'Cancel')]),
            el('button', { class: 'btn btn-primary', onClick: save }, [text(isEdit ? 'Save' : 'Create space')]),
        ]),
    ]);

    const overlay = el('div', { class: 'modal-overlay space-modal-overlay' }, [card]);
    _armBackdropClose(overlay, close);
    document.body.appendChild(overlay);
    // openOverlay moves focus into the card and puts it back on the opener
    // when close() runs; the announcement is what tells a screen reader the
    // dialog arrived at all, since the focus move alone is not narrated
    // consistently.
    closeOverlay = openOverlay(card, { onClose: close, initialFocus: labelInput });
    announce(isEdit
        ? `Space settings for ${space.label}`
        : 'New space. Name it and pick a colour.');
}

// Backdrop close that only fires when the press STARTED on the backdrop.
// A naive click handler also fires after a resize-handle drag or a
// text-selection drag that ends off the card: the browser dispatches the
// synthesized click on the common ancestor of mousedown/mouseup — the
// overlay — and the modal closes mid-resize.
function _armBackdropClose(overlay, close) {
    let downOnBackdrop = false;
    overlay.addEventListener('mousedown', (e) => { downOnBackdrop = e.target === overlay; });
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay && downOnBackdrop) close();
        downOnBackdrop = false;
    });
}

// ---------------------------------------------------------------------------
// Directive override editor — shared .tab-bar tabs; the default is read-only
// until "Customize" copies it into an editable Monaco buffer that fills the
// (resizable) card. Ctrl+S in the editor saves the modal.
// ---------------------------------------------------------------------------

function _buildDirectivesSection(space, dirState, disposers, onSave) {
    const section = el('div', { class: 'space-directives' });
    section.appendChild(el('label', { class: 'space-field-label' }, [
        text('Directives — space overrides; undefined files fall back to the defaults'),
    ]));

    const tabBar = el('div', { class: 'tab-bar space-dir-tabbar' });
    const pane = el('div', { class: 'space-dir-pane' }, [text('Loading…')]);
    section.appendChild(tabBar);
    section.appendChild(pane);

    let files = null;
    let active = 'SOUL';

    const renderTabs = () => {
        clear(tabBar);
        for (const name of DIRECTIVES) {
            const st = dirState[name];
            const overridden = st ? (st.mode === 'edit' && !st.revert) : !!files?.[name]?.override;
            tabBar.appendChild(el('button', {
                class: 'tab-btn' + (name === active ? ' active' : ''),
                title: overridden ? `${name}.md — space override active` : `${name}.md — using the default`,
                onClick: () => { active = name; renderTabs(); renderPane(); },
            }, [
                text(name),
                overridden ? el('span', { class: 'space-dir-ovr-dot' }) : null,
            ]));
        }
    };

    const renderPane = () => {
        clear(pane);
        if (!files) { pane.appendChild(text('Loading…')); return; }
        const name = active;
        const info = files[name] || { default: '', override: null };
        let st = dirState[name];
        if (!st) {
            st = dirState[name] = {
                mode: info.override != null ? 'edit' : 'default',
                hadOverride: info.override != null,
                editor: null,
                dirty: false,
                revert: false,
            };
        }

        if (st.mode === 'default') {
            const modeLabel = st.revert && st.hadOverride
                ? 'Override will be removed on Save — showing the default:'
                : 'Using the default (read-only):';
            pane.appendChild(el('div', { class: 'space-dir-toolbar' }, [
                el('span', { class: 'space-dir-mode' }, [text(modeLabel)]),
                el('button', {
                    class: 'btn btn-primary space-dir-action',
                    onClick: () => { st.mode = 'edit'; st.revert = false; renderTabs(); renderPane(); },
                }, [text('Customize for this space')]),
            ]));
            pane.appendChild(el('pre', { class: 'space-dir-default' }, [
                text(info.default || '(default file is empty or missing)'),
            ]));
            pane.appendChild(el('div', { class: 'space-dir-hint' }, [text(DIRECTIVE_HINT[name])]));
        } else {
            const sizeInfo = el('span', { class: 'space-dir-size' });
            const updateSize = (v) => {
                const bytes = new TextEncoder().encode(v).length;
                sizeInfo.textContent = `${(bytes / 1024).toFixed(1)} KB / ${MAX_DIRECTIVE_BYTES / 1000} KB`;
                sizeInfo.classList.toggle('over', bytes > MAX_DIRECTIVE_BYTES);
            };
            pane.appendChild(el('div', { class: 'space-dir-toolbar' }, [
                el('span', { class: 'space-dir-mode' }, [
                    text(st.hadOverride ? 'Space override (editable)' : 'New override — seeded from the default'),
                ]),
                sizeInfo,
                el('button', {
                    class: 'btn btn-secondary space-dir-action',
                    title: 'Discard this override and go back to the default file',
                    onClick: () => {
                        st.mode = 'default';
                        st.revert = true;
                        st.editor = null;
                        renderTabs();
                        renderPane();
                    },
                }, [text('Revert to default')]),
            ]));
            const host = el('div', { class: 'space-dir-editor' });
            pane.appendChild(host);
            const seed = st.editor ? st.editor.getValue() : (info.override != null ? info.override : info.default);
            updateSize(seed);
            // Dispose the previous instance first. Switching SOUL -> RULES ->
            // SOUL re-rendered the pane and built a new Monaco each time,
            // leaving the old models and their listeners alive for as long as
            // the modal stayed open.
            if (st.editor && st.editor.dispose) {
                try { st.editor.dispose(); } catch {}
                st.editor = null;
            }
            createCodeEditor(host, seed, 'markdown', (v) => { st.dirty = true; updateSize(v); }).then(inst => {
                st.editor = inst;
                inst.addSaveCommand(() => onSave());
                disposers.push(() => inst.dispose && inst.dispose());
            });
            pane.appendChild(el('div', { class: 'space-dir-hint' }, [
                text(DIRECTIVE_HINT[name] + ' Ctrl+S saves. Applies on the next agent turn.'),
            ]));
        }
    };

    get(`/api/spaces/${space.id}/directives`).then(data => {
        files = data.files || {};
        renderTabs();
        renderPane();
    }).catch(() => {
        clear(pane);
        pane.appendChild(text('Could not load directives.'));
    });

    renderTabs();
    return section;
}

// ---------------------------------------------------------------------------
// Delete dialog — cascade checkbox OFF by default (detach & keep)
// ---------------------------------------------------------------------------

export async function openSpaceDeleteDialog(space) {
    const cascadeBox = el('input', { type: 'checkbox', id: 'space-cascade-box' });
    const ok = await confirmDanger({
        title: `Delete space “${space.label}”?`,
        body: 'By default the space is removed but everything in it is kept: its sessions '
            + 'return to the session list, memory files and the workspace folder stay, and '
            + 'bound scheduled jobs keep running unbound.',
        verb: 'Delete space',
        cancelLabel: 'Cancel',
        extra: el('label', { class: 'space-cascade-label', for: 'space-cascade-box' }, [
            cascadeBox,
            text(' Also delete its sessions, memory files, workspace folder, and jobs'),
        ]),
    });
    if (!ok) return;

    try {
        await del(`/api/spaces/${space.id}?cascade=${cascadeBox.checked}`);
        _notifyChanged();
    } catch (e) {
        // The dialog is gone by now, so the failure has to find the user
        // wherever they are looking.
        notify('error', `Could not delete “${space.label}”: ${e.message || e}`);
    }
}
