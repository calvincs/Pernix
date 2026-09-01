// Pernix — Spaces modal: create/edit (label, color, directive overrides)
// and the delete dialog with its cascade checkbox (v33).
import { el, text, clear } from '../../render.js';
import { get, post, del, patch, apiJson } from '../../api.js';
import { createCodeEditor } from '../file-panel.js';

const SWATCHES = ['#7c9cff', '#ff8a65', '#4db6ac', '#ba68c8', '#ffd54f', '#81c784', '#f06292', '#90a4ae'];
const DIRECTIVES = ['SOUL', 'RULES', 'SESSIONS'];
const DIRECTIVE_HINT = {
    SOUL: 'Identity — who the agent is in this space. Overrides data/agent/SOUL.md.',
    RULES: 'Binding rules for this space. Overrides data/agent/RULES.md.',
    SESSIONS: 'Deployment/config notes for this space. Overrides data/agent/SESSIONS.md.',
};

function _notifyChanged() {
    window.dispatchEvent(new CustomEvent('pernix:sessions-changed'));
}

// ---------------------------------------------------------------------------
// Create / edit modal
// ---------------------------------------------------------------------------

export function openSpaceModal(space) {
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
    const customColor = el('input', { type: 'color', class: 'space-color-custom', value: color, title: 'Custom color' });
    const paintSwatches = () => {
        clear(colorRow);
        for (const c of SWATCHES) {
            colorRow.appendChild(el('button', {
                class: 'space-swatch' + (c === color ? ' selected' : ''),
                style: `background:${c}`,
                title: c,
                onClick: (e) => { e.preventDefault(); color = c; customColor.value = c; paintSwatches(); },
            }));
        }
        colorRow.appendChild(customColor);
    };
    customColor.addEventListener('input', () => { color = customColor.value; paintSwatches(); });
    paintSwatches();

    const body = el('div', { class: 'modal-body space-modal-body' }, [
        el('label', { class: 'space-field-label' }, [text('Name')]),
        labelInput,
        el('label', { class: 'space-field-label' }, [text('Color')]),
        colorRow,
    ]);
    if (isEdit) {
        body.appendChild(el('div', { class: 'space-slug-note' }, [
            text(`slug: ${space.slug} · memory: pernix.space.${space.slug}.* · workspace: spaces/${space.slug}/`),
        ]));
        body.appendChild(_buildDirectivesSection(space, dirState, disposers));
    } else {
        body.appendChild(el('div', { class: 'space-slug-note' }, [
            text('Directive overrides (SOUL / RULES / SESSIONS) can be edited here after the space is created.'),
        ]));
    }

    const status = el('span', { class: 'space-modal-status' });

    const close = () => {
        for (const d of disposers) { try { d(); } catch { /* disposed */ } }
        overlay.remove();
        document.removeEventListener('keydown', onEsc);
    };
    const onEsc = (e) => { if (e.key === 'Escape') close(); };

    const save = async () => {
        const label = labelInput.value.trim();
        if (!label) { status.textContent = 'Name is required'; return; }
        status.textContent = 'Saving…';
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
                        }
                    } else if (st.revert && st.hadOverride) {
                        await del(`/api/spaces/${space.id}/directives/${name}`);
                    }
                }
            } else {
                await post('/api/spaces', { label, color });
            }
            _notifyChanged();
            close();
        } catch (e) {
            status.textContent = `Save failed: ${e.message || e}`;
        }
    };

    const card = el('div', { class: 'modal-card space-modal-card' }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text(isEdit ? `Space — ${space.label}` : 'New space')]),
            el('button', { class: 'modal-close', onClick: close }, [text('×')]),
        ]),
        body,
        el('div', { class: 'modal-footer' }, [
            status,
            el('button', { class: 'btn-secondary', onClick: close }, [text('Cancel')]),
            el('button', { class: 'btn-primary', onClick: save }, [text(isEdit ? 'Save' : 'Create space')]),
        ]),
    ]);

    const overlay = el('div', {
        class: 'modal-overlay space-modal-overlay',
        onClick: (e) => { if (e.target === overlay) close(); },
    }, [card]);
    document.body.appendChild(overlay);
    document.addEventListener('keydown', onEsc);
    labelInput.focus();
}

// ---------------------------------------------------------------------------
// Directive override editor — three tabs; default is read-only until
// "Customize" copies it into an editable buffer; "Revert" removes the
// override (applied on Save).
// ---------------------------------------------------------------------------

function _buildDirectivesSection(space, dirState, disposers) {
    const section = el('div', { class: 'space-directives' });
    section.appendChild(el('label', { class: 'space-field-label' }, [
        text('Directives (space overrides — undefined files fall back to the defaults)'),
    ]));

    const tabBar = el('div', { class: 'space-dir-tabs' });
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
                class: 'space-dir-tab' + (name === active ? ' active' : '') + (overridden ? ' overridden' : ''),
                onClick: () => { active = name; renderTabs(); renderPane(); },
            }, [text(name + (overridden ? ' •' : ''))]));
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
        pane.appendChild(el('div', { class: 'space-dir-hint' }, [text(DIRECTIVE_HINT[name])]));

        if (st.mode === 'default') {
            const label = st.revert && st.hadOverride
                ? 'Override will be removed on Save — showing the default:'
                : 'Using the default (read-only):';
            pane.appendChild(el('div', { class: 'space-dir-mode' }, [text(label)]));
            pane.appendChild(el('pre', { class: 'space-dir-default' }, [
                text(info.default || '(default file is empty or missing)'),
            ]));
            pane.appendChild(el('button', {
                class: 'btn-secondary space-dir-customize',
                onClick: () => {
                    st.mode = 'edit';
                    st.revert = false;
                    renderTabs();
                    renderPane();
                },
            }, [text('Customize for this space')]));
        } else {
            pane.appendChild(el('div', { class: 'space-dir-mode' }, [
                text(st.hadOverride ? 'Space override (editable):' : 'New override — seeded from the default:'),
            ]));
            const host = el('div', { class: 'space-dir-editor' });
            pane.appendChild(host);
            const seed = st.editor ? st.editor.getValue() : (info.override != null ? info.override : info.default);
            createCodeEditor(host, seed, 'markdown', () => { st.dirty = true; }).then(inst => {
                st.editor = inst;
                disposers.push(() => inst.dispose && inst.dispose());
            });
            pane.appendChild(el('button', {
                class: 'btn-secondary space-dir-revert',
                onClick: () => {
                    st.mode = 'default';
                    st.revert = true;
                    st.editor = null;
                    renderTabs();
                    renderPane();
                },
            }, [text('Revert to default')]));
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

export function openSpaceDeleteDialog(space) {
    document.querySelector('.space-delete-overlay')?.remove();
    const cascadeBox = el('input', { type: 'checkbox', id: 'space-cascade-box' });
    const status = el('span', { class: 'space-modal-status' });

    const close = () => { overlay.remove(); document.removeEventListener('keydown', onEsc); };
    const onEsc = (e) => { if (e.key === 'Escape') close(); };

    const doDelete = async () => {
        status.textContent = 'Deleting…';
        try {
            await del(`/api/spaces/${space.id}?cascade=${cascadeBox.checked}`);
            _notifyChanged();
            close();
        } catch (e) {
            status.textContent = `Delete failed: ${e.message || e}`;
        }
    };

    const card = el('div', { class: 'modal-card space-delete-card' }, [
        el('div', { class: 'modal-header' }, [
            el('h2', {}, [text(`Delete space “${space.label}”?`)]),
            el('button', { class: 'modal-close', onClick: close }, [text('×')]),
        ]),
        el('div', { class: 'modal-body' }, [
            el('p', {}, [text(
                'By default the space is removed but everything in it is kept: its sessions ' +
                'return to the session list, memory files and the workspace folder stay, and ' +
                'bound scheduled jobs keep running unbound.'
            )]),
            el('label', { class: 'space-cascade-label', for: 'space-cascade-box' }, [
                cascadeBox,
                text(' Also delete its sessions, memory files, workspace folder, and jobs'),
            ]),
        ]),
        el('div', { class: 'modal-footer' }, [
            status,
            el('button', { class: 'btn-secondary', onClick: close }, [text('Cancel')]),
            el('button', { class: 'btn-danger', onClick: doDelete }, [text('Delete space')]),
        ]),
    ]);

    const overlay = el('div', {
        class: 'modal-overlay space-delete-overlay',
        onClick: (e) => { if (e.target === overlay) close(); },
    }, [card]);
    document.body.appendChild(overlay);
    document.addEventListener('keydown', onEsc);
}
