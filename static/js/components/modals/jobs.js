// Pernix — Jobs tab builders (used by Explorer file-panel)

import { el, text, clear } from '../../render.js';
import { get, post, del, apiJson } from '../../api.js';
import { createCodeEditor } from '../file-panel.js';

let _refreshCallback = null;
let _setSubTabCallback = null;
let _selectSessionCallback = null;
let _elapsedTimers = [];
let _activeEditors = [];

// ---------------------------------------------------------------------------
// Configuration — set by host (file-panel)
// ---------------------------------------------------------------------------

export function setJobsCallbacks({ refresh, setSubTab, selectSession }) {
    _refreshCallback = refresh || null;
    _setSubTabCallback = setSubTab || null;
    _selectSessionCallback = selectSession || null;
}

export function clearElapsedTimers() {
    for (const t of _elapsedTimers) clearInterval(t);
    _elapsedTimers = [];
    for (const ed of _activeEditors) ed.dispose();
    _activeEditors = [];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(isoStr) {
    if (!isoStr) return '';
    let s = isoStr.replace(/\+00:00$/, 'Z');
    if (!/[Z+-]\d{2}/.test(s)) s += 'Z';
    const d = new Date(s);
    if (isNaN(d.getTime())) return isoStr;
    const sec = Math.floor((Date.now() - d.getTime()) / 1000);
    if (sec < 60) return 'just now';
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    return `${Math.floor(sec / 86400)}d ago`;
}

function formatDuration(ms) {
    if (!ms) return '-';
    const sec = Math.floor(ms / 1000);
    if (sec < 60) return `${sec}s`;
    return `${Math.floor(sec / 60)}m ${sec % 60}s`;
}

function elapsed(startedAt) {
    if (!startedAt) return '0s';
    let s = startedAt.replace(/\+00:00$/, 'Z');
    if (!/[Z+-]\d{2}/.test(s)) s += 'Z';
    const d = new Date(s);
    const sec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
    if (sec < 60) return `${sec}s`;
    return `${Math.floor(sec / 60)}m ${sec % 60}s`;
}

function statusBadge(status) {
    return el('span', { class: `jobs-status ${status}` }, [text(status)]);
}

function _refreshPanel() {
    if (_refreshCallback) _refreshCallback();
}

function _goToSession(sessionId) {
    if (_selectSessionCallback) _selectSessionCallback(sessionId);
}

// ---------------------------------------------------------------------------
// RLM runs — rendered as regular job rows in Active/History so they follow
// the sub-tab model and the jobs text filter. Root runs only: nested child
// runs are visible in the RLM viewer for the run's session.
// ---------------------------------------------------------------------------

async function fetchRlmRuns(limit = 20) {
    try {
        const data = await get(`/api/rlm/runs?limit=${limit}`);
        return (data.runs || []).filter(r => !r.parent_run_id);
    } catch {
        return []; // endpoint unavailable — old server / RLM add-on absent
    }
}

function rlmRunItem(run, { live = false } = {}) {
    const meta = [];
    if (live) {
        const elapsedEl = el('span', { class: 'jobs-elapsed' }, [text(elapsed(run.created_at))]);
        const timer = setInterval(() => {
            elapsedEl.textContent = elapsed(run.created_at);
        }, 1000);
        _elapsedTimers.push(timer);
        meta.push(elapsedEl);
    } else {
        if (run.created_at) meta.push(el('span', {}, [text(relativeTime(run.created_at))]));
        if (run.finished_at && run.created_at) {
            meta.push(el('span', {}, [text(formatDuration(
                new Date(run.finished_at).getTime() - new Date(run.created_at).getTime()
            ))]));
        }
    }
    meta.push(el('span', {}, [text(`${run.iterations || 0} it · ${run.subcalls || 0} calls`)]));
    if (run.session_id) {
        meta.push(el('span', {
            class: 'jobs-session-link',
            onClick: () => _goToSession(run.session_id),
        }, [text(run.session_id.slice(0, 8))]));
    }
    const task = (run.task || '').trim();
    const name = `RLM: ${task ? (task.length > 70 ? task.slice(0, 70) + '…' : task) : run.run_id}`;
    return el('div', { class: 'jobs-item' }, [
        statusBadge(run.status === 'failed' ? 'error' : run.status),
        el('div', { class: 'jobs-item-main' }, [
            el('div', { class: 'jobs-item-name' }, [text(name)]),
            el('div', { class: 'jobs-item-meta' }, meta),
            run.error ? el('div', { class: 'jobs-error-detail' }, [text(run.error)]) : null,
        ]),
    ]);
}

// ---------------------------------------------------------------------------
// Active tab
// ---------------------------------------------------------------------------

export async function buildActiveTab() {
    const container = el('div', { class: 'jobs-tab-content' });

    try {
        const status = await get('/api/jobs/status');
        const runs = await get('/api/jobs/runs?limit=10');
        const rlmRunning = (await fetchRlmRuns(10)).filter(r => r.status === 'running');

        const running = (runs.items || []).filter(r => r.status === 'running');
        const snoozing = status.snooze?.running;

        if (running.length === 0 && rlmRunning.length === 0 && !snoozing) {
            container.appendChild(el('div', { class: 'jobs-empty' }, [text('No active tasks')]));
            return container;
        }

        for (const run of running) {
            const elapsedEl = el('span', { class: 'jobs-elapsed' }, [text(elapsed(run.started_at))]);
            const timer = setInterval(() => {
                elapsedEl.textContent = elapsed(run.started_at);
            }, 1000);
            _elapsedTimers.push(timer);

            const item = el('div', { class: 'jobs-item' }, [
                statusBadge('running'),
                el('div', { class: 'jobs-item-main' }, [
                    el('div', { class: 'jobs-item-name' }, [text(run.job_name)]),
                    el('div', { class: 'jobs-item-meta' }, [
                        elapsedEl,
                        run.session_id ? el('span', {
                            class: 'jobs-session-link',
                            onClick: () => _goToSession(run.session_id),
                        }, [text(run.session_id.slice(0, 8))]) : null,
                    ]),
                ]),
            ]);
            container.appendChild(item);
        }

        for (const run of rlmRunning) {
            container.appendChild(rlmRunItem(run, { live: true }));
        }

        if (snoozing) {
            container.appendChild(el('div', { class: 'jobs-item' }, [
                statusBadge('snooze'),
                el('div', { class: 'jobs-item-main' }, [
                    el('div', { class: 'jobs-item-name' }, [text('Snooze cycle')]),
                    el('div', { class: 'jobs-item-meta' }, [
                        text('Memory consolidation & maintenance'),
                    ]),
                ]),
            ]));
        }
    } catch (e) {
        container.appendChild(el('div', { class: 'jobs-empty' }, [text(`Error: ${e.message}`)]));
    }

    return container;
}

// ---------------------------------------------------------------------------
// Scheduled tab
// ---------------------------------------------------------------------------

export async function buildScheduledTab() {
    const container = el('div', { class: 'jobs-tab-content' });

    try {
        const [data, modelsData] = await Promise.all([
            get('/api/jobs'),
            get('/api/models').catch(() => ({ models: [], current: '' })),
        ]);
        const jobs = data.items || [];
        const models = modelsData;

        container.appendChild(el('div', {
            style: 'font-size:var(--text-xs); color:var(--text-faint); margin-bottom:var(--sp-2); padding:0 var(--sp-3);',
        }, [text('Cron expressions and next-run times are in UTC.')]));

        if (jobs.length === 0) {
            container.appendChild(el('div', { class: 'jobs-empty' }, [text('No scheduled jobs')]));
        } else {
            for (const job of jobs) {
                container.appendChild(_buildJobRow(job, models));
            }
        }

        container.appendChild(_buildAddSection(models));
    } catch (e) {
        container.appendChild(el('div', { class: 'jobs-empty' }, [text(`Error: ${e.message}`)]));
    }

    return container;
}

function _buildJobRow(job, models) {
    const wrapper = el('div');

    function renderView() {
        clear(wrapper);
        const item = el('div', { class: 'jobs-item' }, [
            statusBadge(job.status),
            el('div', { class: 'jobs-item-main' }, [
                el('div', { class: 'jobs-item-name' }, [text(job.name)]),
                el('div', { class: 'jobs-item-meta' }, [
                    el('span', {}, [text(`${job.cron_expr} (UTC)`)]),
                    job.next_run ? el('span', {}, [text(`next: ${relativeTime(job.next_run)}`)]) : null,
                    job.model ? el('span', {}, [text(`model: ${job.model}`)]) : null,
                    el('span', {}, [text(`runs: ${job.run_count}`)]),
                    job.last_run_at ? el('span', {}, [text(`last: ${relativeTime(job.last_run_at)}`)]) : null,
                ]),
                el('div', { class: 'jobs-item-meta', style: { marginTop: '2px' } }, [
                    el('span', { style: { color: 'var(--text-faint)' } },
                        [text(job.prompt.length > 80 ? job.prompt.slice(0, 80) + '...' : job.prompt)]),
                ]),
            ]),
            el('div', { class: 'jobs-item-actions' }, [
                el('button', {
                    class: 'jobs-btn',
                    onClick: () => renderEdit(),
                }, [text('edit')]),
                el('button', {
                    class: 'jobs-btn',
                    onClick: async () => {
                        if (job.paused) {
                            await post(`/api/jobs/${encodeURIComponent(job.name)}/resume`);
                        } else {
                            await post(`/api/jobs/${encodeURIComponent(job.name)}/pause`);
                        }
                        _refreshPanel();
                    },
                }, [text(job.paused ? 'resume' : 'pause')]),
                el('button', {
                    class: 'jobs-btn danger',
                    onClick: async () => {
                        if (confirm(`Remove job "${job.name}"?`)) {
                            await del(`/api/jobs/${encodeURIComponent(job.name)}`);
                            _refreshPanel();
                        }
                    },
                }, [text('delete')]),
            ]),
        ]);
        wrapper.appendChild(item);
    }

    function renderEdit() {
        clear(wrapper);
        const cronInput = el('input', { type: 'text', value: job.cron_expr });
        const editorHost = el('div', { class: 'jobs-editor-host' });
        let editorInstance = null;

        const modelSelect = el('select');
        modelSelect.appendChild(el('option', { value: '' }, [text('Default (system model)')]));
        const current = models?.current || '';
        for (const m of (models?.models || [])) {
            const label = m.id === current ? `${m.id} (current)` : m.id;
            const opt = el('option', { value: m.id }, [text(label)]);
            if (m.id === job.model) opt.selected = true;
            modelSelect.appendChild(opt);
        }
        if (job.model === '') modelSelect.value = '';

        const statusMsg = el('span', { style: { fontSize: 'var(--text-xs)', color: 'var(--text-dim)' } });
        const saveBtn = el('button', {
            class: 'btn btn-primary',
            style: { padding: '4px 12px', fontSize: 'var(--text-sm)', display: 'none' },
        }, [text('Save')]);

        function checkDirty() {
            const newCron = cronInput.value.trim();
            const newPrompt = editorInstance ? editorInstance.getValue().trim() : job.prompt;
            const newModel = modelSelect.value;
            const dirty = (newCron !== job.cron_expr) ||
                          (newPrompt !== job.prompt) ||
                          (newModel !== (job.model || ''));
            saveBtn.style.display = dirty ? '' : 'none';
            if (dirty) statusMsg.textContent = '';
        }

        cronInput.addEventListener('input', checkDirty);
        modelSelect.addEventListener('change', checkDirty);

        const saveAction = async () => {
            const body = {};
            const newCron = cronInput.value.trim();
            const newPrompt = editorInstance ? editorInstance.getValue().trim() : '';
            const newModel = modelSelect.value;

            if (newCron && newCron !== job.cron_expr) body.cron_expr = newCron;
            if (newPrompt && newPrompt !== job.prompt) body.prompt = newPrompt;
            if (newModel !== (job.model || '')) body.model = newModel;

            if (Object.keys(body).length === 0) return;
            try {
                await apiJson('PUT', `/api/jobs/${encodeURIComponent(job.name)}`, body);
                statusMsg.textContent = 'Saved';
                statusMsg.style.color = 'var(--success)';
                saveBtn.style.display = 'none';
                setTimeout(() => _refreshPanel(), 500);
            } catch (e) {
                statusMsg.textContent = e.message;
                statusMsg.style.color = 'var(--error)';
            }
        };

        saveBtn.addEventListener('click', saveAction);

        const editForm = el('div', { class: 'jobs-edit-form' }, [
            el('div', { class: 'jobs-edit-header' }, [
                el('span', { style: { fontWeight: '500', color: 'var(--text-bright)' } }, [text(job.name)]),
                statusMsg,
            ]),
            el('div', { class: 'jobs-add-row' }, [
                el('label', { style: { fontSize: 'var(--text-xs)', color: 'var(--text-dim)', minWidth: '40px' } }, [text('Cron (UTC)')]),
                cronInput,
            ]),
            el('div', { style: { display: 'flex', flexDirection: 'column', gap: '4px' } }, [
                el('label', { style: { fontSize: 'var(--text-xs)', color: 'var(--text-dim)' } }, [text('Prompt')]),
                editorHost,
            ]),
            el('div', { class: 'jobs-add-row' }, [
                el('label', { style: { fontSize: 'var(--text-xs)', color: 'var(--text-dim)', minWidth: '40px' } }, [text('Model')]),
                modelSelect,
            ]),
            el('div', { style: { display: 'flex', gap: 'var(--sp-2)', justifyContent: 'flex-end' } }, [
                el('button', {
                    class: 'jobs-btn',
                    onClick: () => {
                        if (editorInstance) {
                            editorInstance.dispose();
                            _activeEditors = _activeEditors.filter(e => e !== editorInstance);
                        }
                        renderView();
                    },
                }, [text('cancel')]),
                saveBtn,
            ]),
        ]);
        wrapper.appendChild(editForm);

        createCodeEditor(editorHost, job.prompt, 'markdown', checkDirty).then(inst => {
            editorInstance = inst;
            _activeEditors.push(inst);
            inst.addSaveCommand(saveAction);
        });
    }

    renderView();
    return wrapper;
}


// ---------------------------------------------------------------------------
// Add job — collapsed button that expands into form
// ---------------------------------------------------------------------------

/** Convert a LOCAL wall-clock time (and optional local weekday) to a UTC
 * cron expression — the scheduler runs cron in UTC, but users think in
 * local time. Weekday conversion goes through a real Date so day shifts
 * across the UTC boundary are handled. */
function _localToUtcCron(hour, minute, localDow = null) {
    const d = new Date();
    d.setHours(hour, minute, 0, 0);
    if (localDow !== null) {
        const delta = (localDow - d.getDay() + 7) % 7;
        d.setDate(d.getDate() + delta);
        return `${d.getUTCMinutes()} ${d.getUTCHours()} * * ${d.getUTCDay()}`;
    }
    return `${d.getUTCMinutes()} ${d.getUTCHours()} * * *`;
}

const _CRON_PRESETS = [
    ['', 'Preset…'],
    ['*/15 * * * *', 'Every 15 minutes'],
    ['0 * * * *', 'Every hour'],
    ['daily:9:0', 'Daily at 9:00 (local)'],
    ['daily:18:0', 'Daily at 18:00 (local)'],
    ['weekly:1:9:0', 'Mondays at 9:00 (local)'],
];

/** Human-readable description of common cron shapes, with the UTC time
 * translated back to local so the user can sanity-check the schedule. */
function _cronHint(expr) {
    const parts = expr.trim().split(/\s+/);
    if (parts.length !== 5) return '';
    const [min, hr, , , dow] = parts;
    let stepM = min.match(/^\*\/(\d+)$/);
    if (stepM && hr === '*') return `runs every ${stepM[1]} minutes`;
    if (/^\d+$/.test(min) && hr === '*') return `runs hourly at :${min.padStart(2, '0')}`;
    if (/^\d+$/.test(min) && /^\d+$/.test(hr)) {
        const d = new Date();
        d.setUTCHours(+hr, +min, 0, 0);
        const local = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
        const when = /^\d+$/.test(dow) ? `${days[+dow % 7]}s (UTC day)` : 'daily';
        return `runs ${when} at ${local} local (${hr.padStart(2, '0')}:${min.padStart(2, '0')} UTC)`;
    }
    return '';
}

function _buildAddSection(models) {
    const wrapper = el('div');
    let editorInstance = null;

    function renderButton() {
        clear(wrapper);
        if (editorInstance) {
            editorInstance.dispose();
            _activeEditors = _activeEditors.filter(e => e !== editorInstance);
            editorInstance = null;
        }
        const btn = el('button', {
            class: 'jobs-btn',
            style: { margin: 'var(--sp-3)', fontSize: 'var(--text-sm)' },
            onClick: renderForm,
        }, [text('+ Add Job')]);
        wrapper.appendChild(btn);
    }

    function renderForm() {
        clear(wrapper);
        const nameInput = el('input', { type: 'text', placeholder: 'Job name' });
        const cronInput = el('input', { type: 'text', placeholder: '*/5 * * * *' });
        const editorHost = el('div', { class: 'jobs-editor-host' });
        const statusMsg = el('span', { style: { fontSize: 'var(--text-xs)', color: 'var(--text-dim)' } });

        // Schedule presets + live description — hand-writing UTC cron is a
        // wall for non-developers, so common schedules are one click and the
        // hint translates whatever lands in the field back to local time.
        const cronHint = el('span', { style: { fontSize: 'var(--text-xxs)', color: 'var(--text-faint)' } });
        const presetSelect = el('select');
        for (const [value, label] of _CRON_PRESETS) {
            presetSelect.appendChild(el('option', { value }, [text(label)]));
        }
        presetSelect.addEventListener('change', () => {
            let v = presetSelect.value;
            const daily = v.match(/^daily:(\d+):(\d+)$/);
            const weekly = v.match(/^weekly:(\d+):(\d+):(\d+)$/);
            if (daily) v = _localToUtcCron(+daily[1], +daily[2]);
            else if (weekly) v = _localToUtcCron(+weekly[2], +weekly[3], +weekly[1]);
            if (v) {
                cronInput.value = v;
                cronHint.textContent = _cronHint(v);
            }
        });
        cronInput.addEventListener('input', () => { cronHint.textContent = _cronHint(cronInput.value); });

        const modelSelect = el('select');
        modelSelect.appendChild(el('option', { value: '' }, [text('Default (system model)')]));
        const current = models?.current || '';
        for (const m of (models?.models || [])) {
            const label = m.id === current ? `${m.id} (current)` : m.id;
            modelSelect.appendChild(el('option', { value: m.id }, [text(label)]));
        }

        const form = el('div', { class: 'jobs-add-form' }, [
            el('div', { style: { fontSize: 'var(--text-sm)', color: 'var(--text-dim)', marginBottom: '4px' } },
                [text('Add Job — cron schedule is UTC')]),
            el('div', { class: 'jobs-add-row' }, [nameInput, cronInput, presetSelect]),
            el('div', { style: { margin: '2px 0 4px' } }, [cronHint]),
            editorHost,
            el('div', { class: 'jobs-add-row' }, [
                modelSelect,
                el('button', {
                    class: 'btn btn-primary',
                    style: { padding: '4px 12px', fontSize: 'var(--text-sm)' },
                    onClick: async () => {
                        const name = nameInput.value.trim();
                        const cron_expr = cronInput.value.trim();
                        const prompt = editorInstance ? editorInstance.getValue().trim() : '';
                        if (!name || !cron_expr || !prompt) {
                            statusMsg.textContent = 'Name, cron, and prompt are required';
                            statusMsg.style.color = 'var(--error)';
                            return;
                        }
                        try {
                            await post('/api/jobs', {
                                name, cron_expr, prompt,
                                model: modelSelect.value,
                            });
                            statusMsg.textContent = `Job "${name}" created`;
                            statusMsg.style.color = 'var(--success)';
                            if (_setSubTabCallback) _setSubTabCallback('scheduled');
                            _refreshPanel();
                        } catch (e) {
                            statusMsg.textContent = e.message;
                            statusMsg.style.color = 'var(--error)';
                        }
                    },
                }, [text('Add')]),
            ]),
            el('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' } }, [
                statusMsg,
                el('button', { class: 'jobs-btn', onClick: renderButton }, [text('cancel')]),
            ]),
        ]);
        wrapper.appendChild(form);

        createCodeEditor(editorHost, '', 'markdown').then(inst => {
            editorInstance = inst;
            _activeEditors.push(inst);
        });
    }

    renderButton();
    return wrapper;
}

// ---------------------------------------------------------------------------
// History tab
// ---------------------------------------------------------------------------

export async function buildHistoryTab() {
    const container = el('div', { class: 'jobs-tab-content' });

    try {
        const runsData = await get('/api/jobs/runs?limit=50');
        const runs = runsData.items || [];
        const rlmRuns = (await fetchRlmRuns(50)).filter(r => r.status !== 'running');

        // Derive job names from run history (includes old/deleted jobs)
        const jobNameSet = new Set(runs.map(r => r.job_name));
        if (rlmRuns.length > 0) jobNameSet.add('rlm');
        const jobNames = [...jobNameSet].sort();

        const totalAll = (runsData.total || 0) + rlmRuns.length;
        let filter = '';
        const countEl = el('span', {
            style: { color: 'var(--text-faint)', fontSize: 'var(--text-xs)' },
        }, [text(`${totalAll} runs`)]);

        const select = el('select', {
            onChange: (e) => {
                filter = e.target.value;
                renderRuns();
            },
        }, [
            el('option', { value: '' }, [text('All jobs')]),
            ...jobNames.map(n => el('option', { value: n }, [text(n)])),
        ]);

        const clearBtn = el('button', {
            class: 'jobs-btn danger',
            onClick: async () => {
                if (filter === 'rlm') {
                    alert('RLM runs are purged automatically by retention (rlm_run_retention_days).');
                    return;
                }
                const target = filter || 'all';
                if (!confirm(`Clear ${target} run history?`)) return;
                try {
                    const qs = filter ? `?job_name=${encodeURIComponent(filter)}` : '';
                    await del(`/api/jobs/runs${qs}`);
                    _refreshPanel();
                } catch (e) {
                    alert(`Error: ${e.message}`);
                }
            },
        }, [text('clear')]);

        const header = el('div', { class: 'jobs-history-header' }, [
            el('div', { style: { display: 'flex', alignItems: 'center', gap: 'var(--sp-2)' } }, [
                el('label', { style: { fontSize: 'var(--text-xs)', color: 'var(--text-dim)' } }, [text('Filter')]),
                select,
            ]),
            el('div', { style: { display: 'flex', alignItems: 'center', gap: 'var(--sp-2)', marginLeft: 'auto' } }, [
                countEl,
                clearBtn,
            ]),
        ]);
        container.appendChild(header);

        const listEl = el('div');
        container.appendChild(listEl);

        function jobRunItem(run) {
            const status = run.status || 'unknown';
            return el('div', { class: 'jobs-item' }, [
                statusBadge(status),
                el('div', { class: 'jobs-item-main' }, [
                    el('div', { class: 'jobs-item-name' }, [text(run.job_name)]),
                    el('div', { class: 'jobs-item-meta' }, [
                        run.started_at ? el('span', {}, [text(relativeTime(run.started_at))]) : null,
                        run.completed_at && run.started_at ? el('span', {}, [
                            text(formatDuration(
                                new Date(run.completed_at).getTime() - new Date(run.started_at).getTime()
                            )),
                        ]) : null,
                        run.session_id ? el('span', {
                            class: 'jobs-session-link',
                            onClick: () => _goToSession(run.session_id),
                        }, [text(run.session_id.slice(0, 8))]) : null,
                    ]),
                    run.error ? el('div', { class: 'jobs-error-detail' }, [text(run.error)]) : null,
                ]),
            ]);
        }

        function renderRuns() {
            clear(listEl);
            const jobsFiltered = filter
                ? (filter === 'rlm' ? [] : runs.filter(r => r.job_name === filter))
                : runs;
            const rlmFiltered = (!filter || filter === 'rlm') ? rlmRuns : [];

            // Update count to reflect filter
            const shown = jobsFiltered.length + rlmFiltered.length;
            countEl.textContent = filter ? `${shown} of ${totalAll} runs` : `${totalAll} runs`;

            if (shown === 0) {
                listEl.appendChild(el('div', { class: 'jobs-empty' }, [text('No runs yet')]));
                return;
            }

            // Merge cron-job and RLM runs into one newest-first timeline.
            const entries = [
                ...jobsFiltered.map(r => ({ ts: r.started_at || '', build: () => jobRunItem(r) })),
                ...rlmFiltered.map(r => ({ ts: r.created_at || '', build: () => rlmRunItem(r) })),
            ].sort((a, b) => (a.ts < b.ts ? 1 : -1));
            for (const entry of entries) listEl.appendChild(entry.build());
        }

        renderRuns();
    } catch (e) {
        container.appendChild(el('div', { class: 'jobs-empty' }, [text(`Error: ${e.message}`)]));
    }

    return container;
}
