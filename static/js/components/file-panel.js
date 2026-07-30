// Pernix — Explorer panel (workspace, memory, skills, jobs)

import { el, text, clear, renderMarkdown } from '../render.js';
import { get, post, del, getAuthToken } from '../api.js';
import { isMobile } from '../mobile.js';
import { openSettings } from './modals/settings.js';

function _authHdr() { const t = getAuthToken(); return t ? { 'Authorization': `Bearer ${t}` } : {}; }
import {
    buildActiveTab, buildScheduledTab, buildHistoryTab,
    setJobsCallbacks, clearElapsedTimers,
} from './modals/jobs.js';

// ---------------------------------------------------------------------------
// Monaco Editor (CDN) with lightweight textarea fallback
// ---------------------------------------------------------------------------

const MONACO_LANG = {
    'python': 'python', 'javascript': 'javascript', 'typescript': 'typescript',
    'jsx': 'javascript', 'tsx': 'typescript', 'html': 'html', 'css': 'css',
    'json': 'json', 'markdown': 'markdown', 'yaml': 'yaml', 'bash': 'shell',
    'sql': 'sql', 'toml': 'ini', 'xml': 'xml', 'text': 'plaintext',
    'csv': 'plaintext', 'rust': 'rust', 'go': 'go', 'ruby': 'ruby',
    'java': 'java', 'c': 'c', 'cpp': 'cpp', 'lua': 'lua', 'r': 'r',
    'swift': 'swift', 'ini': 'ini',
};

let _monacoReady = null;

function loadMonaco() {
    if (_monacoReady) return _monacoReady;
    _monacoReady = new Promise((resolve, reject) => {
        if (window.monaco) { resolve(window.monaco); return; }
        if (!window.require) { reject(new Error('Monaco loader not available')); return; }
        // On a LAN without internet the AMD module fetch can hang (rather
        // than fail), which left createCodeEditor awaiting forever and the
        // file panel without any editor. Time out to the textarea fallback,
        // and clear the memoized promise so a later attempt can retry.
        const timer = setTimeout(() => {
            _monacoReady = null;
            reject(new Error('Monaco load timed out (offline/LAN?)'));
        }, 8000);
        const _resolve = resolve;
        resolve = (m) => { clearTimeout(timer); _resolve(m); };
        window.require.config({
            paths: { vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs' },
        });
        window.require(['vs/editor/editor.main'], () => {
            // Define Pernix dark theme once
            window.monaco.editor.defineTheme('pernix-dark', {
                base: 'vs-dark',
                inherit: true,
                rules: [],
                colors: {
                    'editor.background': '#0e0e0e',
                    'editor.foreground': '#e8e3d6',
                    'editor.lineHighlightBackground': '#1a1a1a',
                    'editor.selectionBackground': 'rgba(212, 168, 67, 0.15)',
                    'editorCursor.foreground': '#d4a843',
                    'editorLineNumber.foreground': '#504b40',
                    'editorLineNumber.activeForeground': '#7a7568',
                    'editorIndentGuide.background': '#252525',
                    'editorWidget.background': '#151515',
                    'editorWidget.border': '#252525',
                    'input.background': '#151515',
                    'input.border': '#252525',
                    'scrollbarSlider.background': 'rgba(37, 37, 37, 0.6)',
                    'scrollbarSlider.hoverBackground': 'rgba(80, 75, 64, 0.6)',
                },
            });
            resolve(window.monaco);
        }, reject);
    });
    return _monacoReady;
}

/**
 * Create a code editor inside `host`. Uses Monaco if available, falls back to textarea.
 * Returns { getValue, focus, dispose } — always synchronous after resolve.
 * `lang` is the EXT_LANG value (e.g. 'python', 'javascript').
 */
async function createCodeEditor(host, content, lang, onChange) {
    try {
        const monaco = await loadMonaco();
        return createMonacoEditor(host, monaco, content, lang, onChange);
    } catch {
        return createFallbackEditor(host, content, onChange);
    }
}

function createMonacoEditor(host, monaco, content, lang, onChange) {
    const monacoLang = MONACO_LANG[lang] || 'plaintext';
    const editor = monaco.editor.create(host, {
        value: content,
        language: monacoLang,
        theme: 'pernix-dark',
        fontSize: 13,
        fontFamily: "'DM Mono', 'SF Mono', 'Fira Code', monospace",
        lineNumbers: 'on',
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        wordWrap: 'on',
        automaticLayout: true,
        tabSize: 2,
        renderWhitespace: 'selection',
        bracketPairColorization: { enabled: true },
        padding: { top: 8 },
        smoothScrolling: true,
        cursorBlinking: 'smooth',
        folding: true,
        overviewRulerLanes: 0,
        hideCursorInOverviewRuler: true,
        overviewRulerBorder: false,
        scrollbar: { verticalScrollbarSize: 6, horizontalScrollbarSize: 6 },
    });

    if (onChange) {
        editor.onDidChangeModelContent(() => onChange(editor.getValue()));
    }

    return {
        getValue: () => editor.getValue(),
        focus: () => editor.focus(),
        addSaveCommand: (fn) => {
            editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, fn);
        },
        dispose: () => editor.dispose(),
    };
}

/** Plain textarea fallback if Monaco fails to load */
function createFallbackEditor(host, content, onChange) {
    const ta = document.createElement('textarea');
    ta.className = 'ce-fallback';
    ta.value = content;
    ta.spellcheck = false;
    host.appendChild(ta);

    ta.addEventListener('keydown', (e) => {
        if (e.key === 'Tab') {
            e.preventDefault();
            const s = ta.selectionStart, end = ta.selectionEnd;
            ta.value = ta.value.substring(0, s) + '  ' + ta.value.substring(end);
            ta.selectionStart = ta.selectionEnd = s + 2;
        }
    });

    if (onChange) ta.addEventListener('input', () => onChange(ta.value));

    return {
        getValue: () => ta.value,
        focus: () => ta.focus(),
        addSaveCommand: () => {},  // handled via keydown listener on host
        dispose: () => ta.remove(),
    };
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const STORAGE_KEY = 'pernix:file-panel';
const MIN_WIDTH = 260;
const DEFAULT_WIDTH = 360;

let _panel = null;        // root DOM element
let _state = {
    open: false,
    width: DEFAULT_WIDTH,
    tab: 'workspace',     // workspace | memory | skills | jobs | signals
    viewMode: 'tree',     // tree | viewer | editor
    expandedDirs: new Set(),
    currentFile: null,    // { path, content, source, modified }
    dirty: false,
    originalContent: '',
    wsSortBy: 'name',     // name | date | size — workspace file sort key
};

let _activeEditor = null; // current Monaco editor instance { editor, getValue, dispose }
let _wsEntries = [];      // current directory entries from API
let _wsCurrentPath = '';   // current workspace directory path
let _wsParent = null;      // parent path (null at root)
let _wsSearchQuery = '';   // active search query
let _wsSearchTimer = null;  // debounce timer for workspace search
let _wsSeq = 0;           // request sequencing — discard stale directory responses
let _jobRenderTimer = null; // debounce timer for job panel re-renders
let _memoryFiles = [];
let _memoryResults = [];
let _memorySeq = 0;       // request sequencing for search
let _selectSessionFn = null;
let _jobsSubTab = 'scheduled'; // active | scheduled | history
let _skills = [];
let _tools = [];
let _autoApproveDangerous = false;
let _toolsSearchQuery = '';
let _toolsSearchTimer = null;
let _toolsSortBy = 'name';
let _toolsListEl = null;  // stable container for the filtered list; updated in-place on search
let _skillsSearchQuery = '';
let _skillsSearchTimer = null;
let _skillsListEl = null;
let _workflowsSearchQuery = '';
let _workflowsSearchTimer = null;
let _workflowsListEl = null;
let _jobsSearchQuery = '';
let _jobsSearchTimer = null;
let _jobsContentEl = null;
let _lastSnoozeActivity = null; // latest snooze.activity payload while a cycle runs
let _pendingProposals = [];
let _searchTimer = null;

// ---------------------------------------------------------------------------
// Extension maps
// ---------------------------------------------------------------------------

const EXT_LANG = {
    '.py': 'python', '.js': 'javascript', '.ts': 'typescript', '.jsx': 'jsx',
    '.tsx': 'tsx', '.html': 'html', '.htm': 'html', '.css': 'css',
    '.json': 'json', '.md': 'markdown', '.yaml': 'yaml', '.yml': 'yaml',
    '.sh': 'bash', '.bash': 'bash', '.zsh': 'bash', '.sql': 'sql',
    '.toml': 'toml', '.xml': 'xml', '.txt': 'text', '.csv': 'csv',
    '.rs': 'rust', '.go': 'go', '.rb': 'ruby', '.java': 'java',
    '.c': 'c', '.cpp': 'cpp', '.h': 'c', '.hpp': 'cpp',
    '.lua': 'lua', '.r': 'r', '.swift': 'swift',
    '.env': 'text', '.cfg': 'ini', '.ini': 'ini', '.conf': 'text',
};

const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.bmp']);
const VIDEO_EXTS = new Set(['.mp4', '.webm', '.ogg', '.mov', '.avi', '.mkv']);
const AUDIO_EXTS = new Set(['.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma']);
const BINARY_EXTS = new Set([
    '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar',
    '.exe', '.dll', '.so', '.dylib', '.bin', '.dat',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.woff', '.woff2', '.ttf', '.otf', '.eot',
    '.sqlite', '.db', '.pyc', '.class', '.o', '.a',
]);
const MAX_TEXT_SIZE = 2 * 1024 * 1024; // 2MB — don't load larger files as text

function getExt(name) {
    const dot = name.lastIndexOf('.');
    return dot >= 0 ? name.slice(dot).toLowerCase() : '';
}

function getLang(name) { return EXT_LANG[getExt(name)] || 'text'; }
function isImage(name) { return IMAGE_EXTS.has(getExt(name)); }
function isVideo(name) { return VIDEO_EXTS.has(getExt(name)); }
function isAudio(name) { return AUDIO_EXTS.has(getExt(name)); }
function isBinary(name) { return BINARY_EXTS.has(getExt(name)); }
function isMedia(name) { return isImage(name) || isVideo(name) || isAudio(name); }
function isMarkdown(name) { return ['.md', '.markdown'].includes(getExt(name)); }

const BROWSER_VIEWABLE = new Set([
    '.html', '.htm', '.svg', '.pdf',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.bmp',
    '.mp4', '.webm', '.ogg', '.mp3', '.wav',
    '.txt', '.json', '.xml', '.csv',
]);
function canOpenInBrowser(name) { return BROWSER_VIEWABLE.has(getExt(name)); }

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

function saveState() {
    try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify({
            open: _state.open,
            width: _state.width,
            tab: _state.tab,
            expandedDirs: [..._state.expandedDirs],
            wsPath: _wsCurrentPath,
            wsSortBy: _state.wsSortBy,
        }));
    } catch {}
}

function loadState() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return;
        const s = JSON.parse(raw);
        if (typeof s.open === 'boolean') _state.open = s.open;
        if (typeof s.width === 'number') _state.width = Math.max(MIN_WIDTH, s.width);
        if (typeof s.tab === 'string') _state.tab = s.tab;
        if (Array.isArray(s.expandedDirs)) _state.expandedDirs = new Set(s.expandedDirs);
        if (typeof s.wsPath === 'string') _wsCurrentPath = s.wsPath;
        if (s.wsSortBy === 'name' || s.wsSortBy === 'date' || s.wsSortBy === 'size') {
            _state.wsSortBy = s.wsSortBy;
        }
    } catch {}
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

export { createCodeEditor };

export function initFilePanel({ selectSession } = {}) {
    loadState();
    _panel = document.getElementById('file-panel');
    if (!_panel) return;

    // Wire up jobs tab callbacks
    _selectSessionFn = selectSession || null;
    setJobsCallbacks({
        refresh: () => loadJobs(),
        setSubTab: (tab) => { _jobsSubTab = tab; },
        selectSession: (sid) => {
            toggleFilePanel();
            if (_selectSessionFn) _selectSessionFn(sid);
        },
    });

    // Listen for SSE job events to refresh jobs tab
    window.addEventListener('pernix:job-event', _onJobEvent);

    buildPanelDOM();

    if (_state.open) {
        _panel.classList.add('open');
        _panel.style.width = _state.width + 'px';
        document.getElementById('files-btn')?.classList.add('active');
        loadTabData();
    }
}

export function toggleFilePanel() {
    _state.open = !_state.open;
    if (_state.open) {
        _panel.classList.add('open');
        if (!isMobile()) _panel.style.width = _state.width + 'px';
        document.getElementById('files-btn')?.classList.add('active');
        loadTabData();
    } else {
        _panel.classList.remove('open');
        _panel.style.width = '';
        document.getElementById('files-btn')?.classList.remove('active');
    }
    saveState();
}

export function openFilePanel(opts = {}) {
    if (!_state.open) {
        _state.open = true;
        _panel.classList.add('open');
        if (!isMobile()) _panel.style.width = _state.width + 'px';
        document.getElementById('files-btn')?.classList.add('active');
    }
    if (opts.tab) {
        _state.tab = opts.tab;
        renderTabs();
    }
    if (opts.file) {
        viewFile(opts.file, 'workspace');
    }
    loadTabData();
    saveState();
}

// ---------------------------------------------------------------------------
// DOM construction
// ---------------------------------------------------------------------------

function buildPanelDOM() {
    // Resize handle
    const handle = el('div', { class: 'fp-resize-handle' });
    initResize(handle);

    // Header
    const closeBtn = el('button', { class: 'fp-close', title: 'Close' }, [text('\u00d7')]);
    closeBtn.addEventListener('click', toggleFilePanel);
    const header = el('div', { class: 'fp-header' }, [
        el('span', { class: 'fp-header-title' }, [text('Explorer')]),
        closeBtn,
    ]);

    // Tab bar
    const tabBar = el('div', { class: 'fp-tab-bar', id: 'fp-tab-bar' });

    // Tab content containers
    const wsContent = el('div', { class: 'fp-tab-content', 'data-tab': 'workspace', id: 'fp-workspace' });
    const memContent = el('div', { class: 'fp-tab-content', 'data-tab': 'memory', id: 'fp-memory' });
    const skillContent = el('div', { class: 'fp-tab-content', 'data-tab': 'skills', id: 'fp-skills' });
    const jobsContent = el('div', { class: 'fp-tab-content', 'data-tab': 'jobs', id: 'fp-jobs' });
    const toolsContent = el('div', { class: 'fp-tab-content', 'data-tab': 'tools', id: 'fp-tools' });
    const workflowsContent = el('div', { class: 'fp-tab-content', 'data-tab': 'workflows', id: 'fp-workflows' });

    _panel.appendChild(handle);
    _panel.appendChild(header);
    _panel.appendChild(tabBar);
    _panel.appendChild(wsContent);
    _panel.appendChild(memContent);
    _panel.appendChild(skillContent);
    _panel.appendChild(toolsContent);
    _panel.appendChild(jobsContent);
    _panel.appendChild(workflowsContent);

    renderTabs();
}

function renderTabs() {
    const tabBar = document.getElementById('fp-tab-bar');
    if (!tabBar) return;
    clear(tabBar);

    const tabs = [
        { key: 'workspace', label: 'Workspace' },
        { key: 'memory', label: 'Memory' },
        { key: 'skills', label: 'Skills' },
        { key: 'tools', label: 'Tools' },
        { key: 'workflows', label: 'Workflows' },
        { key: 'jobs', label: 'Jobs' },
    ];

    tabs.forEach(t => {
        const btn = el('button', {
            class: `fp-tab-btn${t.key === _state.tab ? ' active' : ''}`,
            'data-tab': t.key,
        }, [text(t.label)]);
        btn.addEventListener('click', () => {
            if (_state.tab === 'jobs' && t.key !== 'jobs') clearElapsedTimers();
            _state.tab = t.key;
            _state.viewMode = 'tree';
            _state.currentFile = null;
            renderTabs();
            loadTabData();
            saveState();
        });
        tabBar.appendChild(btn);
    });

    // Show/hide tab content
    ['workspace', 'memory', 'skills', 'tools', 'workflows', 'jobs'].forEach(key => {
        const container = document.getElementById(`fp-${key}`);
        if (container) container.classList.toggle('active', key === _state.tab);
    });
}

async function loadTabData() {
    if (_state.tab === 'workspace') await loadWorkspace();
    else if (_state.tab === 'memory') await loadMemory();
    else if (_state.tab === 'skills') await loadSkills();
    else if (_state.tab === 'workflows') await loadWorkflows();
    else if (_state.tab === 'tools') await loadTools();
    else if (_state.tab === 'jobs') await loadJobs();
}

// ---------------------------------------------------------------------------
// Workspace tab
// ---------------------------------------------------------------------------

async function loadWorkspace(opts = {}) {
    const seq = ++_wsSeq;
    const query = opts.query ?? '';
    const navPath = opts.path ?? _wsCurrentPath;

    try {
        let url = '/api/workspace';
        const params = [];
        if (query) params.push(`q=${encodeURIComponent(query)}`);
        if (navPath) params.push(`path=${encodeURIComponent(navPath)}`);
        if (params.length) url += '?' + params.join('&');

        const data = await get(url);
        if (seq !== _wsSeq) return; // stale — a newer request is already in flight
        _wsEntries = data.entries || [];
        _wsParent = data.parent ?? null;
        if (!query) {
            _wsCurrentPath = data.path || '';
            saveState();
        }
    } catch (e) {
        if (seq !== _wsSeq) return; // stale
        _wsEntries = [];
        _wsParent = null;
        // If we tried to navigate to a stale path, fall back to root
        if (navPath && navPath !== '') {
            _wsCurrentPath = '';
            _wsParent = null;
            saveState();
            return loadWorkspace();
        }
    }
    renderWorkspace();
}

function renderWorkspace() {
    const container = document.getElementById('fp-workspace');
    if (!container) return;
    disposeActiveEditor();
    clear(container);

    if (_state.viewMode === 'viewer' && _state.currentFile) {
        renderViewer(container);
        return;
    }
    if (_state.viewMode === 'editor' && _state.currentFile) {
        renderEditor(container);
        return;
    }

    // Section header with refresh + upload buttons
    const refreshBtn = el('button', { class: 'fp-icon-btn', title: 'Refresh' }, [text('\u21bb')]);
    refreshBtn.addEventListener('click', () => {
        _wsSearchQuery = '';
        loadWorkspace({ path: _wsCurrentPath });
    });

    const uploadBtn = el('button', { class: 'fp-icon-btn', title: 'Upload file' }, [text('\u2191')]);
    uploadBtn.addEventListener('click', triggerUpload);

    const fileCount = _wsEntries.filter(e => e.type === 'file').length;
    const dirCount = _wsEntries.filter(e => e.type === 'dir').length;
    const totalSize = _wsEntries.reduce((sum, e) => sum + (e.size || 0), 0);
    const countParts = [];
    if (dirCount) countParts.push(`${dirCount} folder${dirCount !== 1 ? 's' : ''}`);
    if (fileCount) countParts.push(`${fileCount} file${fileCount !== 1 ? 's' : ''}`);
    if (totalSize) countParts.push(formatSize(totalSize));
    const subtitle = countParts.join(' \u00b7 ') || 'empty';

    container.appendChild(el('div', { class: 'fp-section-header' }, [
        el('div', {}, [
            el('span', { class: 'fp-section-label' }, [text('Workspace')]),
            el('div', { class: 'fp-section-sub' }, [text(subtitle)]),
        ]),
        el('div', { class: 'fp-section-actions' }, [refreshBtn, uploadBtn]),
    ]));
    container.appendChild(_buildTabDesc(
        'Your agent\'s working directory — browse, upload, and edit files.',
        'Files live at data/workspace/ and are accessible to agent tools. Navigate directories with the tree, view file contents inline, or open the editor to modify them directly. Uploads land at the workspace root.',
    ));

    // Search bar
    const searchInput = el('input', {
        class: 'fp-search-input', type: 'text',
        placeholder: 'Search files\u2026', value: _wsSearchQuery,
    });
    searchInput.addEventListener('input', () => {
        _wsSearchQuery = searchInput.value.trim();
        if (_wsSearchTimer) clearTimeout(_wsSearchTimer);
        _wsSearchTimer = setTimeout(() => {
            if (_wsSearchQuery) {
                loadWorkspace({ query: _wsSearchQuery });
            } else {
                loadWorkspace({ path: _wsCurrentPath });
            }
        }, 300);
    });
    container.appendChild(el('div', { class: 'fp-search-bar' }, [searchInput]));

    // Breadcrumb path bar (only in directory mode, not search)
    if (!_wsSearchQuery) {
        const breadcrumb = _buildBreadcrumb();
        container.appendChild(breadcrumb);
    }

    if (_wsEntries.length === 0) {
        const label = _wsSearchQuery ? `No results for "${_wsSearchQuery}"` : 'Empty directory';
        container.appendChild(el('div', { class: 'fp-empty' }, [text(label)]));
        return;
    }

    container.appendChild(_buildColumnHeaders());

    // Render entries
    const treeEl = el('div', { class: 'fp-tree' });
    _renderEntries(treeEl, _sortedWsEntries(_wsEntries, _state.wsSortBy));
    container.appendChild(treeEl);

    // Restore focus to search input if search is active
    if (_wsSearchQuery) {
        requestAnimationFrame(() => {
            searchInput.focus();
            searchInput.setSelectionRange(searchInput.value.length, searchInput.value.length);
        });
    }
}

const _WS_SORT_INDICATOR = { name: '↑', size: '↓', date: '↓' };

function _buildColumnHeaders() {
    const sortBy = _state.wsSortBy;

    function makeCol(label, key, colClass) {
        const isActive = key && sortBy === key;
        const indicator = isActive ? ` ${_WS_SORT_INDICATOR[key] || '↑'}` : '';
        const classes = ['fp-col-h', colClass, key ? 'sortable' : '', isActive ? 'active' : ''].filter(Boolean).join(' ');
        const span = el('span', { class: classes }, [text(label + indicator)]);
        if (key) {
            span.addEventListener('click', () => {
                _state.wsSortBy = key;
                saveState();
                renderWorkspace();
            });
        }
        return span;
    }

    return el('div', { class: 'fp-col-headers' }, [
        el('span', { class: 'fp-col-h col-h-icon' }),
        makeCol('Name', 'name', 'col-h-name'),
        el('span', { class: 'fp-col-h col-h-count' }),
        makeCol('Size', 'size', 'col-h-meta'),
        makeCol('Modified', 'date', 'col-h-date'),
    ]);
}

function _buildBreadcrumb() {
    const parts = _wsCurrentPath ? _wsCurrentPath.split('/') : [];
    const bar = el('div', { class: 'fp-breadcrumb' });

    // Root link
    const rootLink = el('span', { class: `fp-breadcrumb-part${parts.length === 0 ? ' active' : ''}` }, [text('\u2302')]);
    if (parts.length > 0) {
        rootLink.style.cursor = 'pointer';
        rootLink.addEventListener('click', () => loadWorkspace({ path: '' }));
    }
    bar.appendChild(rootLink);

    // Path segments
    for (let i = 0; i < parts.length; i++) {
        bar.appendChild(el('span', { class: 'fp-breadcrumb-sep' }, [text(' / ')]));
        const isLast = i === parts.length - 1;
        const segPath = parts.slice(0, i + 1).join('/');
        const seg = el('span', { class: `fp-breadcrumb-part${isLast ? ' active' : ''}` }, [text(parts[i])]);
        if (!isLast) {
            seg.style.cursor = 'pointer';
            seg.addEventListener('click', () => loadWorkspace({ path: segPath }));
        }
        bar.appendChild(seg);
    }
    return bar;
}

function _sortedWsEntries(entries, sortBy) {
    const all = entries.slice();
    if (sortBy === 'date') {
        // Sort all entries (dirs + files) by modified date, newest first.
        all.sort((a, b) => (b.modified || 0) - (a.modified || 0));
    } else if (sortBy === 'size') {
        // Sort all entries by size, largest first.
        all.sort((a, b) => (b.size || 0) - (a.size || 0));
    } else {
        // Name: dirs first (alpha), then files (alpha) — standard file-explorer behavior.
        const dirs  = all.filter(e => e.type === 'dir').sort((a, b) => a.name.localeCompare(b.name));
        const files = all.filter(e => e.type !== 'dir').sort((a, b) => a.name.localeCompare(b.name));
        return [...dirs, ...files];
    }
    return all;
}

function _renderEntries(parent, entries) {
    // "Go up" entry when not at root and not searching
    if (!_wsSearchQuery && _wsParent !== null) {
        const upItem = el('div', { class: 'fp-tree-item dir' }, [
            el('span', { class: 'fp-tree-icon' }, [text('\u2190')]),
            el('span', { class: 'fp-tree-name' }, [text('..')]),
        ]);
        upItem.addEventListener('click', () => loadWorkspace({ path: _wsParent }));
        parent.appendChild(upItem);
    }

    for (const entry of entries) {
        if (entry.type === 'dir') {
            const childLabel = `${entry.children} item${entry.children !== 1 ? 's' : ''}`;
            const item = el('div', { class: 'fp-tree-item dir' }, [
                el('span', { class: 'fp-tree-icon' }, [text('\u25A0')]),
                el('span', { class: 'fp-tree-name' }, [text(entry.name)]),
                el('span', { class: 'fp-tree-count' }, [text(childLabel)]),
                el('span', { class: 'fp-tree-meta' }, [text(entry.size > 0 ? formatSize(entry.size) : '')]),
                el('span', { class: 'fp-tree-date' }, [text(formatDate(entry.modified))]),
            ]);
            const dirDelBtn = el('button', { class: 'fp-tree-action danger', title: 'Delete' }, [text('\u00d7')]);
            dirDelBtn.addEventListener('click', (e) => { e.stopPropagation(); deleteEntry(entry.path, 'dir'); });
            item.appendChild(el('span', { class: 'fp-tree-actions' }, [dirDelBtn]));
            item.addEventListener('click', () => {
                _wsSearchQuery = '';
                loadWorkspace({ path: entry.path });
            });
            const dirWrap = el('div', { class: 'fp-tree-item-wrap' }, [
                el('div', { class: 'fp-swipe-delete-bg' }, [text('Delete')]),
                item,
            ]);
            if (isMobile()) _attachSwipeDelete(item, entry.path, 'dir');
            parent.appendChild(dirWrap);
        } else {
            const displayName = _wsSearchQuery ? entry.path : entry.name;
            const isActive = _state.currentFile?.path === entry.path;
            const item = el('div', { class: `fp-tree-item file${isActive ? ' active' : ''}` }, [
                el('span', { class: 'fp-tree-icon' }, [text(isImage(entry.name) ? '\u25A3' : '\u25AB')]),
                el('span', { class: 'fp-tree-name' }, [text(displayName)]),
                el('span', { class: 'fp-tree-count' }),
                el('span', { class: 'fp-tree-meta' }, [text(formatSize(entry.size))]),
                el('span', { class: 'fp-tree-date' }, [text(formatDate(entry.modified))]),
            ]);

            const delBtn = el('button', { class: 'fp-tree-action danger', title: 'Delete' }, [text('\u00d7')]);
            delBtn.addEventListener('click', (e) => { e.stopPropagation(); deleteEntry(entry.path); });
            item.appendChild(el('span', { class: 'fp-tree-actions' }, [delBtn]));

            item.addEventListener('click', () => viewFile(entry.path, 'workspace'));
            const fileWrap = el('div', { class: 'fp-tree-item-wrap' }, [
                el('div', { class: 'fp-swipe-delete-bg' }, [text('Delete')]),
                item,
            ]);
            if (isMobile()) _attachSwipeDelete(item, entry.path, 'file');
            parent.appendChild(fileWrap);
        }
    }
}

// ---------------------------------------------------------------------------
// Viewer
// ---------------------------------------------------------------------------

async function viewFile(path, source = 'workspace') {
    const url = `/workspace/${path}`;
    try {
        const name = path.split('/').pop();
        const fileUrl = url;

        // Media files — don't load into memory, just reference the URL
        if (isVideo(name)) {
            _state.currentFile = { path, content: fileUrl, source, type: 'video', name };
        } else if (isAudio(name)) {
            _state.currentFile = { path, content: fileUrl, source, type: 'audio', name };
        } else if (isBinary(name)) {
            _state.currentFile = { path, content: '', source, type: 'binary', name, fileUrl };
        } else {
            // Fetch the file — check size first via HEAD for non-image files
            const resp = await fetch(url, { headers: _authHdr() });
            if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);

            if (isImage(name)) {
                const blob = await resp.blob();
                const blobUrl = URL.createObjectURL(blob);
                _state.currentFile = { path, content: blobUrl, source, type: 'image', name };
            } else {
                // Check content-length to avoid loading huge text files
                const size = parseInt(resp.headers.get('content-length') || '0', 10);
                if (size > MAX_TEXT_SIZE) {
                    _state.currentFile = {
                        path, content: '', source, type: 'too-large', name,
                        fileUrl, fileSize: size,
                    };
                } else {
                    const content = await resp.text();
                    // Double-check: if the fetched text is huge (chunked response with no content-length)
                    if (content.length > MAX_TEXT_SIZE) {
                        _state.currentFile = {
                            path, content: content.slice(0, MAX_TEXT_SIZE),
                            source, type: 'text', name, truncated: true,
                        };
                    } else {
                        _state.currentFile = { path, content, source, type: 'text', name };
                    }
                }
            }
        }
        _state.viewMode = 'viewer';
        _state.dirty = false;
        renderCurrentTab();
    } catch (e) {
        console.error('Failed to load file:', e);
    }
}

function renderViewer(container) {
    const file = _state.currentFile;
    if (!file) return;

    // Toolbar
    const backBtn = el('button', { class: 'fp-toolbar-back', title: 'Back to tree' }, [text('\u2190')]);
    backBtn.addEventListener('click', () => {
        if (file.type === 'image' && file.content.startsWith('blob:')) {
            URL.revokeObjectURL(file.content);
        }
        _state.viewMode = 'tree';
        _state.currentFile = null;
        renderCurrentTab();
    });

    const actions = el('div', { class: 'fp-toolbar-actions' });

    // Edit button — not for memory files (read-only)
    if (file.type === 'text' && file.source !== 'memory-file') {
        const editBtn = el('button', { class: 'fp-btn' }, [text('edit')]);
        editBtn.addEventListener('click', () => {
            _state.viewMode = 'editor';
            _state.originalContent = file.content;
            _state.dirty = false;
            renderCurrentTab();
        });
        actions.appendChild(editBtn);
    }

    // Open in new browser tab — for browser-viewable file types
    if (canOpenInBrowser(file.name)) {
        const openBtn = el('button', { class: 'fp-btn' }, [text('open')]);
        openBtn.addEventListener('click', () => {
            window.open(`/workspace/${file.path}`, '_blank');
        });
        actions.appendChild(openBtn);
    }

    const dlBtn = el('button', { class: 'fp-btn' }, [text('download')]);
    dlBtn.addEventListener('click', () => {
        const a = document.createElement('a');
        a.href = `/workspace/${file.path}`;
        a.download = file.name;
        a.click();
    });
    actions.appendChild(dlBtn);

    const toolbar = el('div', { class: 'fp-toolbar' }, [
        backBtn,
        el('span', { class: 'fp-toolbar-path', title: file.path }, [text(file.path)]),
        actions,
    ]);
    container.appendChild(toolbar);

    // Content
    const viewer = el('div', { class: 'fp-viewer' });

    if (file.type === 'image') {
        const img = el('img', { src: file.content, alt: file.name });
        viewer.appendChild(img);
    } else if (file.type === 'video') {
        const video = el('video', { src: file.content, controls: '', style: 'max-width: 100%' });
        viewer.appendChild(video);
    } else if (file.type === 'audio') {
        const audio = el('audio', { src: file.content, controls: '', style: 'width: 100%' });
        viewer.appendChild(audio);
    } else if (file.type === 'binary') {
        viewer.appendChild(el('div', { class: 'fp-binary-info' }, [
            el('div', { class: 'fp-binary-icon' }, [text('\uD83D\uDCC4')]),
            el('div', {}, [text(`Binary file: ${file.name}`)]),
            el('div', { class: 'fp-binary-hint' }, [text('This file cannot be previewed.')]),
        ]));
    } else if (file.type === 'too-large') {
        viewer.appendChild(el('div', { class: 'fp-binary-info' }, [
            el('div', { class: 'fp-binary-icon' }, [text('\u26A0')]),
            el('div', {}, [text(`${file.name} (${formatSize(file.fileSize)})`)]),
            el('div', { class: 'fp-binary-hint' }, [text('File too large to preview in browser.')]),
        ]));
    } else if (isMarkdown(file.name)) {
        // Markdown with raw toggle
        let showRaw = false;
        const toggleBtn = el('button', { class: 'fp-btn', style: 'margin-bottom: 8px' }, [text('raw')]);
        const contentWrap = el('div', { class: 'fp-viewer-md' });
        contentWrap.appendChild(renderMarkdown(file.content));

        toggleBtn.addEventListener('click', () => {
            showRaw = !showRaw;
            clear(contentWrap);
            if (showRaw) {
                contentWrap.className = '';
                contentWrap.appendChild(el('pre', {}, [text(file.content)]));
                toggleBtn.textContent = 'rendered';
            } else {
                contentWrap.className = 'fp-viewer-md';
                contentWrap.appendChild(renderMarkdown(file.content));
                toggleBtn.textContent = 'raw';
            }
        });

        viewer.appendChild(toggleBtn);
        viewer.appendChild(contentWrap);
        if (file.truncated) {
            viewer.appendChild(el('div', { class: 'fp-binary-hint' }, [text('File truncated at 2MB')]));
        }
    } else if (file.source === 'memory-file') {
        // Memory file with hyperkb highlighting
        viewer.appendChild(renderHkbContent(file.content));
    } else {
        // Code / text file
        const lang = getLang(file.name);
        if (lang !== 'text') {
            viewer.appendChild(el('div', { class: 'fp-lang-label' }, [text(lang)]));
        }
        viewer.appendChild(el('pre', {}, [text(file.content)]));
        if (file.truncated) {
            viewer.appendChild(el('div', { class: 'fp-binary-hint' }, [text('File truncated at 2MB')]));
        }
    }

    container.appendChild(viewer);
}

// ---------------------------------------------------------------------------
// Editor
// ---------------------------------------------------------------------------

function disposeActiveEditor() {
    if (_activeEditor) {
        _activeEditor.dispose();
        _activeEditor = null;
    }
}

// Warn on browser close/refresh with unsaved changes
window.addEventListener('beforeunload', (e) => {
    if (_state.dirty) { e.preventDefault(); }
});

function renderEditor(container) {
    const file = _state.currentFile;
    if (!file) return;

    // Toolbar
    const backBtn = el('button', { class: 'fp-toolbar-back', title: 'Back' }, [text('\u2190')]);
    backBtn.addEventListener('click', () => {
        if (_state.dirty && !confirm('Discard unsaved changes?')) return;
        disposeActiveEditor();
        _state.viewMode = 'viewer';
        _state.dirty = false;
        renderCurrentTab();
    });

    const saveBtn = el('button', { class: 'fp-btn save-btn', disabled: true }, [text('save')]);
    saveBtn.addEventListener('click', () => { if (_state.dirty) saveFile(container); });

    const cancelBtn = el('button', { class: 'fp-btn' }, [text('cancel')]);
    cancelBtn.addEventListener('click', () => {
        if (_state.dirty && !confirm('Discard unsaved changes?')) return;
        disposeActiveEditor();
        _state.viewMode = 'viewer';
        _state.dirty = false;
        renderCurrentTab();
    });

    const pathLabel = el('span', { class: 'fp-toolbar-path' }, [text(file.path)]);

    const toolbar = el('div', { class: 'fp-toolbar' }, [
        backBtn,
        pathLabel,
        el('div', { class: 'fp-toolbar-actions' }, [saveBtn, cancelBtn]),
    ]);
    container.appendChild(toolbar);

    const statusEl = el('div', { class: 'fp-editor-status' }, [text('Ready')]);
    const editorWrap = el('div', { class: 'fp-editor' });
    const editorHost = el('div', { class: 'fp-editor-host' });
    editorWrap.appendChild(editorHost);
    editorWrap.appendChild(statusEl);
    container.appendChild(editorWrap);

    function onDirtyChange(dirty) {
        _state.dirty = dirty;
        // Save button
        saveBtn.disabled = !dirty;
        saveBtn.className = `fp-btn save-btn${dirty ? ' dirty' : ''}`;
        // Status bar
        statusEl.className = `fp-editor-status${dirty ? ' dirty' : ''}`;
        statusEl.textContent = dirty ? 'Modified' : 'Ready';
        // Path label shows dot when modified
        pathLabel.textContent = dirty ? `\u25CF ${file.path}` : file.path;
    }

    const lang = getLang(file.name);
    createCodeEditor(editorHost, file.content, lang, (value) => {
        onDirtyChange(value !== _state.originalContent);
    }).then(inst => {
        _activeEditor = inst;
        inst.addSaveCommand(() => saveFile(container));
        inst.focus();
    });

    // Fallback Ctrl+S for textarea editor
    editorHost.addEventListener('keydown', (e) => {
        if (e.key === 's' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            saveFile(container);
        }
    });
}

async function saveFile(container) {
    const file = _state.currentFile;
    if (!file || !_activeEditor) return;

    const statusEl = container.querySelector('.fp-editor-status');
    const saveBtn = container.querySelector('.save-btn');
    const pathLabel = container.querySelector('.fp-toolbar-path');
    const content = _activeEditor.getValue();
    const url = `/workspace/${file.path}`;

    // Indicate saving in progress
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'saving\u2026'; }
    if (statusEl) { statusEl.className = 'fp-editor-status'; statusEl.textContent = 'Saving\u2026'; }

    try {
        const resp = await fetch(url, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', ..._authHdr() },
            body: JSON.stringify({ content }),
        });
        if (!resp.ok) throw new Error(`Save failed: ${resp.statusText}`);

        file.content = content;
        _state.originalContent = content;
        _state.dirty = false;

        if (saveBtn) { saveBtn.className = 'fp-btn save-btn'; saveBtn.textContent = 'save'; }
        if (pathLabel) pathLabel.textContent = file.path;
        if (statusEl) {
            statusEl.className = 'fp-editor-status saved';
            statusEl.textContent = 'Saved';
            setTimeout(() => {
                statusEl.className = 'fp-editor-status';
                statusEl.textContent = 'Ready';
            }, 1500);
        }
    } catch (e) {
        // Re-enable save button on error so user can retry
        if (saveBtn) { saveBtn.disabled = false; saveBtn.className = 'fp-btn save-btn dirty'; saveBtn.textContent = 'save'; }
        if (statusEl) {
            statusEl.className = 'fp-editor-status error';
            statusEl.textContent = `Error: ${e.message}`;
        }
    }
}

// ---------------------------------------------------------------------------
// File operations
// ---------------------------------------------------------------------------

async function deleteEntry(path, type = 'file') {
    const label = type === 'dir' ? `folder "${path}" and all its contents` : path;
    if (!confirm(`Delete ${label}?`)) return;
    try {
        await del(`/workspace/${path.split('/').map(encodeURIComponent).join('/')}`);
        if (_state.currentFile?.path === path ||
            _state.currentFile?.path?.startsWith(path + '/')) {
            _state.currentFile = null;
            _state.viewMode = 'tree';
        }
        await loadWorkspace({ path: _wsCurrentPath });
    } catch (e) {
        console.error('Delete failed:', e);
    }
}

function _attachSwipeDelete(item, path, type) {
    let startX = 0, startY = 0;
    let tracking = false, direction = null;
    const THRESHOLD = 60;
    const MAX_SHIFT = 80;

    const _resetItem = () => {
        item.style.transition = '';
        item.style.transform = '';
        item.parentElement?.classList.remove('fp-swipe-active');
    };

    item.addEventListener('touchstart', (e) => {
        const touch = e.touches[0];
        startX = touch.clientX;
        startY = touch.clientY;
        direction = null;
        tracking = true;
        item.style.transition = 'none';
    }, { passive: true });

    item.addEventListener('touchmove', (e) => {
        if (!tracking) return;
        const touch = e.touches[0];
        const dx = touch.clientX - startX;
        const dy = touch.clientY - startY;
        if (!direction && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
            direction = Math.abs(dx) > Math.abs(dy) ? 'horizontal' : 'vertical';
        }
        if (direction === 'horizontal' && dx < 0) {
            e.preventDefault();
            item.parentElement?.classList.add('fp-swipe-active');
            const offset = Math.max(-MAX_SHIFT, dx);
            item.style.transform = `translateX(${offset}px)`;
        } else if (direction === 'vertical') {
            tracking = false;
            _resetItem();
        }
    }, { passive: false });

    item.addEventListener('touchend', (e) => {
        if (!tracking) return;
        tracking = false;
        const touch = e.changedTouches[0];
        const dx = touch.clientX - startX;
        if (direction === 'horizontal') {
            e.preventDefault();
            _resetItem();
            if (dx < -THRESHOLD) {
                deleteEntry(path, type);
            }
        } else {
            _resetItem();
        }
    }, { passive: false });

    item.addEventListener('touchcancel', () => {
        tracking = false;
        _resetItem();
    }, { passive: true });
}

function triggerUpload() {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.addEventListener('change', async () => {
        for (const file of input.files) {
            const formData = new FormData();
            formData.append('file', file);
            try {
                await fetch('/api/upload', { method: 'POST', body: formData, headers: _authHdr() });
            } catch (e) {
                console.error('Upload failed:', e);
            }
        }
        await loadWorkspace({ path: _wsCurrentPath });
    });
    input.click();
}

// ---------------------------------------------------------------------------
// Memory tab
// ---------------------------------------------------------------------------

async function loadMemory() {
    const container = document.getElementById('fp-memory');
    if (!container) return;

    try {
        const data = await get('/api/memory/files');
        _memoryFiles = data.files || [];
    } catch {
        _memoryFiles = [];
    }

    renderMemory();
}

function renderMemory() {
    const container = document.getElementById('fp-memory');
    if (!container) return;
    disposeActiveEditor();
    clear(container);

    if (_state.viewMode === 'viewer' && _state.currentFile) {
        renderViewer(container);
        return;
    }

    // Section header with stats
    const totalEntries = _memoryFiles.reduce((sum, f) => sum + (f.entry_count || 0), 0);
    container.appendChild(el('div', { class: 'fp-section-header' }, [
        el('div', {}, [
            el('span', { class: 'fp-section-label' }, [text('Memory')]),
            el('div', { class: 'fp-section-sub' }, [text(`${_memoryFiles.length} files \u00b7 ${totalEntries} entries`)]),
        ]),
    ]));
    container.appendChild(_buildTabDesc(
        'Persistent knowledge the agent builds up across sessions.',
        'Memory files are markdown documents in data/memories/. The agent writes to them during distillation and memory tool calls. Use search to retrieve past observations, decisions, or any context it has retained.',
    ));

    // Search bar
    const searchInput = el('input', {
        class: 'fp-search-input',
        type: 'text',
        placeholder: 'Search memory...',
    });
    searchInput.addEventListener('input', () => {
        if (_searchTimer) clearTimeout(_searchTimer);
        _searchTimer = setTimeout(() => searchMemory(searchInput.value.trim()), 300);
    });
    container.appendChild(el('div', { class: 'fp-search-bar' }, [searchInput]));

    // Results area (shared between search results and file list)
    const listEl = el('div', { class: 'fp-memory-list', id: 'fp-memory-list' });

    if (_memoryResults.length > 0) {
        renderSearchResults(listEl);
    } else {
        renderMemoryFiles(listEl);
    }

    container.appendChild(listEl);
}

function renderMemoryFiles(listEl) {
    if (_memoryFiles.length === 0) {
        listEl.appendChild(el('div', { class: 'fp-empty' }, [text('No memory files')]));
        return;
    }

    for (const f of _memoryFiles) {
        const item = el('div', { class: 'fp-memory-item' }, [
            el('div', { class: 'fp-memory-name' }, [text(f.name)]),
            el('div', { class: 'fp-memory-desc' }, [text(f.description || '')]),
            el('div', { class: 'fp-memory-meta' }, [
                el('span', {}, [text(`${f.entry_count || 0} entries`)]),
                f.keywords ? el('span', {}, [text(f.keywords)]) : text(''),
            ]),
        ]);
        item.addEventListener('click', () => viewMemoryFile(f.name));
        listEl.appendChild(item);
    }
}

async function searchMemory(query) {
    if (!query) {
        _memoryResults = [];
        const listEl = document.getElementById('fp-memory-list');
        if (listEl) { clear(listEl); renderMemoryFiles(listEl); }
        return;
    }

    const seq = ++_memorySeq;
    try {
        const data = await get(`/api/memory/search?q=${encodeURIComponent(query)}&limit=10`);
        if (seq !== _memorySeq) return; // stale response
        _memoryResults = data.results || [];
    } catch {
        if (seq !== _memorySeq) return;
        _memoryResults = [];
    }

    const listEl = document.getElementById('fp-memory-list');
    if (listEl) { clear(listEl); renderSearchResults(listEl); }
}

function renderSearchResults(listEl) {
    if (_memoryResults.length === 0) {
        listEl.appendChild(el('div', { class: 'fp-empty' }, [text('No results')]));
        return;
    }

    for (const r of _memoryResults) {
        const item = el('div', { class: 'fp-search-result' }, [
            el('div', { class: 'fp-search-result-header' }, [
                el('span', { class: 'fp-search-result-file' }, [text(r.file)]),
                el('span', { class: 'fp-search-result-score' }, [text(`${r.score}`)]),
            ]),
            el('div', { class: 'fp-search-result-content' }, [text(r.content || '')]),
        ]);
        item.addEventListener('click', () => viewMemoryFile(r.file));
        listEl.appendChild(item);
    }
}

async function viewMemoryFile(name) {
    try {
        const data = await get(`/api/memory/files/${encodeURIComponent(name)}`);
        if (data.error) return;
        _state.currentFile = {
            path: name,
            content: data.content,
            source: 'memory-file',
            type: 'text',
            name: name + '.md',
        };
        _state.viewMode = 'viewer';
        renderMemory();
    } catch (e) {
        console.error('Failed to load memory file:', e);
    }
}

function renderHkbContent(content) {
    const pre = document.createElement('pre');
    pre.style.margin = '0';
    pre.style.fontFamily = 'var(--mono)';
    pre.style.fontSize = 'var(--text-xs)';
    pre.style.lineHeight = '1.55';
    pre.style.whiteSpace = 'pre-wrap';
    pre.style.wordBreak = 'break-word';

    // Highlight hyperkb markers
    const lines = content.split('\n');
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        let span;
        if (line.startsWith('>>>') || line.startsWith('<<<')) {
            span = el('span', { class: 'fp-hkb-entry' }, [text(line)]);
        } else if (line.startsWith('@')) {
            span = el('span', { class: 'fp-hkb-meta' }, [text(line)]);
        } else if (line.trim() === '---') {
            span = el('span', { class: 'fp-hkb-sep' }, [text(line)]);
        } else {
            span = document.createTextNode(line);
        }
        pre.appendChild(span);
        if (i < lines.length - 1) pre.appendChild(document.createTextNode('\n'));
    }
    return pre;
}

// ---------------------------------------------------------------------------
// Skills tab
// ---------------------------------------------------------------------------

async function loadSkills() {
    try {
        const [skillsData, proposalsData] = await Promise.all([
            get('/api/skills'),
            get('/api/workflows/proposals?status=pending&limit=20').catch(() => ({ proposals: [] })),
        ]);
        _skills = skillsData.skills || [];
        _pendingProposals = proposalsData.proposals || [];
    } catch {
        _skills = [];
        _pendingProposals = [];
    }
    renderSkills();
}

function renderSkills() {
    const container = document.getElementById('fp-skills');
    if (!container) return;
    disposeActiveEditor();
    clear(container);

    if (_state.viewMode === 'viewer' && _state.currentFile) {
        renderSkillViewer(container);
        return;
    }
    if (_state.viewMode === 'editor' && _state.currentFile) {
        renderSkillEditor(container);
        return;
    }

    // Section header
    const refreshBtn = el('button', { class: 'fp-icon-btn', title: 'Refresh' }, [text('\u21bb')]);
    refreshBtn.addEventListener('click', loadSkills);

    container.appendChild(el('div', { class: 'fp-section-header' }, [
        el('div', {}, [
            el('span', { class: 'fp-section-label' }, [text('Skills')]),
            el('div', { class: 'fp-section-sub' }, [text(
                `${_skills.length} installed \u00b7 ${_skills.filter(s => s.enabled).length} enabled`
            )]),
        ]),
        el('div', { class: 'fp-section-actions' }, [refreshBtn]),
    ]));
    container.appendChild(_buildTabDesc(
        'Capability packages the agent can discover and invoke.',
        'Skills live in data/skills/ — each a self-contained package with instructions and optional scripts. Scout auto-detects the most relevant skill each turn and can inject it into context. Disable to remove a skill from consideration without deleting it.',
    ));

    // Pending skill improvement proposals banner
    if (_pendingProposals.length > 0) {
        const banner = el('div', { class: 'fp-proposals-banner' });
        banner.appendChild(el('div', { class: 'fp-proposals-banner-title' }, [
            text(`\u{1F4A1} ${_pendingProposals.length} skill improvement proposal${_pendingProposals.length > 1 ? 's' : ''}`),
        ]));
        for (const proposal of _pendingProposals) {
            const row = el('div', { class: 'fp-proposal-row' });
            const origin = proposal.source_origin || 'workflow';
            const originLabel = origin === 'session'
                ? `SESSION \u00b7 ${(proposal.session_id || '').slice(0, 8)}`
                : `WORKFLOW \u00b7 ${proposal.workflow_name || '?'}`;
            const trialUses = proposal.trial_uses || 0;
            const trialSuccesses = proposal.trial_successes || 0;
            const trialLabel = trialUses > 0
                ? ` \u00b7 trial: ${trialUses} use${trialUses === 1 ? '' : 's'} \u00b7 ${trialSuccesses} helped`
                : '';
            const info = el('div', { class: 'fp-proposal-info' }, [
                el('span', { class: `fp-proposal-origin fp-proposal-origin-${origin}` }, [text(originLabel)]),
                el('span', { class: 'fp-proposal-skill' }, [text(proposal.skill_name)]),
                el('span', { class: 'fp-proposal-section' }, [text(proposal.section ? ` \u00b7 ${proposal.section}` : '')]),
                el('span', { class: 'fp-proposal-problem' }, [text(` — ${(proposal.problem || '').slice(0, 80)}${(proposal.problem || '').length > 80 ? '\u2026' : ''}`)]),
                el('span', { class: 'fp-proposal-trial' }, [text(trialLabel)]),
            ]);
            const reviewBtn = el('button', { class: 'fp-btn fp-btn-xs' }, [text('review')]);
            reviewBtn.addEventListener('click', async () => {
                try {
                    const skillData = await get(`/api/skills/${encodeURIComponent(proposal.skill_name)}`);
                    _state.currentFile = {
                        path: proposal.skill_name,
                        content: skillData.raw_content || '',
                        source: 'skill',
                        type: 'text',
                        name: 'SKILL.md',
                        skillData,
                        pendingProposal: proposal,
                    };
                    _state.viewMode = 'viewer';
                    renderSkills();
                } catch (e) {
                    console.error('Failed to load skill for proposal review:', e);
                    // Show inline error — skill may have been deleted since proposal was created
                    reviewBtn.textContent = 'skill not found';
                    reviewBtn.disabled = true;
                }
            });
            row.appendChild(info);
            row.appendChild(reviewBtn);
            banner.appendChild(row);
        }
        container.appendChild(banner);
    }

    // Search input
    const skillSearch = el('input', {
        class: 'fp-search-input',
        type: 'text',
        placeholder: 'Search skills…',
        value: _skillsSearchQuery,
    });
    skillSearch.addEventListener('input', () => {
        _skillsSearchQuery = skillSearch.value.trim();
        if (_skillsSearchTimer) clearTimeout(_skillsSearchTimer);
        _skillsSearchTimer = setTimeout(_renderSkillsFiltered, 150);
    });
    container.appendChild(el('div', { class: 'fp-search-bar' }, [skillSearch]));

    _skillsListEl = el('div', {});
    container.appendChild(_skillsListEl);
    _renderSkillsFiltered();
}

function _renderSkillsFiltered() {
    if (!_skillsListEl) return;
    clear(_skillsListEl);

    const q = _skillsSearchQuery.toLowerCase();
    const visible = q
        ? _skills.filter(s =>
            s.name.toLowerCase().includes(q) ||
            (s.description || '').toLowerCase().includes(q) ||
            (s.tags || []).some(t => t.toLowerCase().includes(q))
          )
        : [..._skills];

    if (visible.length === 0) {
        _skillsListEl.appendChild(el('div', { class: 'fp-empty' }, [
            text(q ? `No skills match "${q}"` : 'No skills installed'),
        ]));
        return;
    }

    const listEl = el('div', { class: 'fp-skills-list' });
    for (const skill of visible) {
        const item = el('div', { class: `fp-skill-item${skill.enabled ? '' : ' disabled'}` });

        const toggle = el('button', {
            class: `fp-skill-toggle${skill.enabled ? ' on' : ''}`,
            title: skill.enabled ? 'Disable skill' : 'Enable skill',
        }, [text(skill.enabled ? 'on' : 'off')]);
        toggle.addEventListener('click', async (e) => {
            e.stopPropagation();
            await toggleSkill(skill.name, !skill.enabled);
        });

        const deleteBtn = el('button', {
            class: 'fp-tree-action danger',
            title: 'Delete skill',
        }, [text('\u00d7')]);
        deleteBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            await deleteSkill(skill.name);
        });

        const nameChildren = [
            text(skill.name),
            el('span', { class: 'fp-skill-version' }, [text(`v${skill.version}`)]),
        ];
        const pendingCount = skill.pending_proposals_count || 0;
        if (pendingCount > 0) {
            nameChildren.push(el('span', {
                class: 'fp-skill-pending-badge',
                title: `${pendingCount} pending proposal${pendingCount === 1 ? '' : 's'} — see banner above to review`,
            }, [text(`⚠ ${pendingCount} pending`)]));
        }
        item.appendChild(el('div', { class: 'fp-skill-header' }, [
            el('div', { class: 'fp-skill-name' }, nameChildren),
            el('div', { class: 'fp-skill-actions' }, [toggle, deleteBtn]),
        ]));

        item.appendChild(el('div', { class: 'fp-skill-desc' }, [text(skill.description)]));

        if (skill.tags && skill.tags.length > 0) {
            const tagsEl = el('div', { class: 'fp-skill-tags' });
            for (const tag of skill.tags.slice(0, 6)) {
                tagsEl.appendChild(el('span', { class: 'fp-skill-tag' }, [text(tag)]));
            }
            item.appendChild(tagsEl);
        }

        if (skill.performance && skill.performance.uses > 0) {
            const p = skill.performance;
            const perfText = p.failures > 0
                ? `${p.uses} uses · ⚠ ${p.failures} failure${p.failures !== 1 ? 's' : ''}`
                : `${p.uses} uses`;
            item.appendChild(el('div', {
                class: `fp-perf-line${p.failures > 0 ? ' fp-perf-warn' : ''}`,
            }, [text(perfText)]));
        }

        item.addEventListener('click', () => viewSkill(skill.name));
        listEl.appendChild(item);
    }
    _skillsListEl.appendChild(listEl);
}

async function viewSkill(name) {
    try {
        const data = await get(`/api/skills/${encodeURIComponent(name)}`);
        _state.currentFile = {
            path: name,
            content: data.raw_content || '',
            source: 'skill',
            type: 'text',
            name: 'SKILL.md',
            skillData: data,
        };
        _state.viewMode = 'viewer';
        renderSkills();
    } catch (e) {
        console.error('Failed to load skill:', e);
    }
}

function renderSkillViewer(container) {
    const file = _state.currentFile;
    if (!file || !file.skillData) return;
    const data = file.skillData;

    // Toolbar
    const backBtn = el('button', { class: 'fp-toolbar-back', title: 'Back' }, [text('\u2190')]);
    backBtn.addEventListener('click', () => {
        _state.viewMode = 'tree';
        _state.currentFile = null;
        renderSkills();
    });

    const editBtn = el('button', { class: 'fp-btn' }, [text('edit')]);
    editBtn.addEventListener('click', () => {
        _state.viewMode = 'editor';
        _state.originalContent = file.content;
        _state.dirty = false;
        renderSkills();
    });

    const toolbar = el('div', { class: 'fp-toolbar' }, [
        backBtn,
        el('span', { class: 'fp-toolbar-path' }, [text(data.name + '/SKILL.md')]),
        el('div', { class: 'fp-toolbar-actions' }, [editBtn]),
    ]);
    container.appendChild(toolbar);

    // Pending proposal callout (if this skill has a pending proposal being reviewed)
    const proposal = file.pendingProposal;
    if (proposal) {
        const callout = el('div', { class: 'fp-proposal-callout' });
        callout.appendChild(el('div', { class: 'fp-proposal-callout-label' }, [text('Skill Improvement Proposal')]));
        callout.appendChild(el('div', { class: 'fp-proposal-callout-row' }, [
            el('span', { class: 'fp-proposal-callout-key' }, [text('Problem: ')]),
            el('span', {}, [text(proposal.problem || '')]),
        ]));
        if (proposal.section) {
            callout.appendChild(el('div', { class: 'fp-proposal-callout-row' }, [
                el('span', { class: 'fp-proposal-callout-key' }, [text('Section: ')]),
                el('span', {}, [text(proposal.section)]),
            ]));
        }
        callout.appendChild(el('div', { class: 'fp-proposal-callout-row' }, [
            el('span', { class: 'fp-proposal-callout-key' }, [text('Suggestion: ')]),
        ]));
        callout.appendChild(el('div', { class: 'fp-proposal-callout-change' }, [text(proposal.proposed_change || '')]));

        const actions = el('div', { class: 'fp-proposal-callout-actions' });

        const editBtn = el('button', { class: 'fp-btn' }, [text('edit skill')]);
        editBtn.addEventListener('click', async () => {
            // Mark approved and open Monaco editor
            try {
                await post(`/api/workflows/proposals/${proposal.id}/approve`, {});
            } catch (e) { /* non-fatal */ }
            _state.viewMode = 'editor';
            _state.originalContent = file.content;
            _state.dirty = false;
            renderSkills();
        });

        const applyBtn = el('button', { class: 'fp-btn fp-btn-primary' }, [text('apply')]);
        applyBtn.title = 'Insert the suggested change into this skill\'s SKILL.md under the referenced section. Re-run the workflow to validate the fix.';
        applyBtn.addEventListener('click', async () => {
            if (!confirm(`Apply this proposal to ${proposal.skill_name}'s SKILL.md?\n\nSection: ${proposal.section || '(new section)'}\n\nThe workflow will NOT re-run automatically — you'll need to invoke it again to validate the fix.`)) {
                return;
            }
            try {
                const res = await post(`/api/workflows/proposals/${proposal.id}/apply`, {});
                _pendingProposals = _pendingProposals.filter(p => p.id !== proposal.id);
                file.pendingProposal = null;
                const delta = (res.bytes_after || 0) - (res.bytes_before || 0);
                console.log(`[workflow] proposal applied: +${delta} bytes into ${res.skill_md_path}`);
                // Reload file data so the editor shows the updated skill body
                await loadSkills();
                renderSkills();
            } catch (e) {
                alert(`Failed to apply proposal: ${e.message || e}`);
            }
        });

        const rejectBtn = el('button', { class: 'fp-btn fp-btn-danger' }, [text('reject')]);
        rejectBtn.addEventListener('click', async () => {
            try {
                await post(`/api/workflows/proposals/${proposal.id}/reject`, {});
                _pendingProposals = _pendingProposals.filter(p => p.id !== proposal.id);
                file.pendingProposal = null;
                renderSkills();
            } catch (e) {
                console.error('Failed to reject proposal:', e);
            }
        });

        actions.appendChild(editBtn);
        actions.appendChild(applyBtn);
        actions.appendChild(rejectBtn);
        callout.appendChild(actions);
        container.appendChild(callout);
    }

    // Skill info card
    const info = el('div', { class: 'fp-skill-info' });
    info.appendChild(el('div', { class: 'fp-skill-info-name' }, [text(data.name)]));
    info.appendChild(el('div', { class: 'fp-skill-desc' }, [text(data.description)]));

    if (data.tags && data.tags.length > 0) {
        const tagsEl = el('div', { class: 'fp-skill-tags' });
        for (const tag of data.tags) {
            tagsEl.appendChild(el('span', { class: 'fp-skill-tag' }, [text(tag)]));
        }
        info.appendChild(tagsEl);
    }

    // Resources summary
    const res = data.resources || {};
    const resParts = [];
    if (res.scripts) resParts.push(`${res.scripts.length} scripts`);
    if (res.references) resParts.push(`${res.references.length} references`);
    if (res.assets) resParts.push(`${res.assets.length} assets`);
    if (resParts.length > 0) {
        info.appendChild(el('div', { class: 'fp-skill-resources' }, [text(resParts.join(' \u00b7 '))]));
    }

    container.appendChild(info);

    // Instructions content (rendered as markdown)
    if (data.instructions) {
        const viewer = el('div', { class: 'fp-viewer' });
        const contentWrap = el('div', { class: 'fp-viewer-md' });
        contentWrap.appendChild(renderMarkdown(data.instructions));
        viewer.appendChild(contentWrap);
        container.appendChild(viewer);
    }
}

function renderSkillEditor(container) {
    const file = _state.currentFile;
    if (!file) return;

    // Toolbar
    const backBtn = el('button', { class: 'fp-toolbar-back', title: 'Back' }, [text('\u2190')]);
    backBtn.addEventListener('click', () => {
        if (_state.dirty && !confirm('Discard unsaved changes?')) return;
        disposeActiveEditor();
        _state.viewMode = 'viewer';
        _state.dirty = false;
        renderSkills();
    });

    const saveBtn = el('button', { class: 'fp-btn save-btn', disabled: true }, [text('save')]);
    saveBtn.addEventListener('click', () => { if (_state.dirty) saveSkill(container); });

    const cancelBtn = el('button', { class: 'fp-btn' }, [text('cancel')]);
    cancelBtn.addEventListener('click', () => {
        if (_state.dirty && !confirm('Discard unsaved changes?')) return;
        disposeActiveEditor();
        _state.viewMode = 'viewer';
        _state.dirty = false;
        renderSkills();
    });

    const pathLabel = el('span', { class: 'fp-toolbar-path' }, [text(`${file.path}/SKILL.md`)]);

    const toolbar = el('div', { class: 'fp-toolbar' }, [
        backBtn,
        pathLabel,
        el('div', { class: 'fp-toolbar-actions' }, [saveBtn, cancelBtn]),
    ]);
    container.appendChild(toolbar);

    const statusEl = el('div', { class: 'fp-editor-status' }, [text('Ready')]);
    const editorWrap = el('div', { class: 'fp-editor' });
    const editorHost = el('div', { class: 'fp-editor-host' });
    editorWrap.appendChild(editorHost);
    editorWrap.appendChild(statusEl);
    container.appendChild(editorWrap);

    function onDirtyChange(dirty) {
        _state.dirty = dirty;
        saveBtn.disabled = !dirty;
        saveBtn.className = `fp-btn save-btn${dirty ? ' dirty' : ''}`;
        statusEl.className = `fp-editor-status${dirty ? ' dirty' : ''}`;
        statusEl.textContent = dirty ? 'Modified' : 'Ready';
        pathLabel.textContent = dirty ? `\u25CF ${file.path}/SKILL.md` : `${file.path}/SKILL.md`;
    }

    createCodeEditor(editorHost, file.content, 'markdown', (value) => {
        onDirtyChange(value !== _state.originalContent);
    }).then(inst => {
        _activeEditor = inst;
        inst.addSaveCommand(() => saveSkill(container));
        inst.focus();
    });

    editorHost.addEventListener('keydown', (e) => {
        if (e.key === 's' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            saveSkill(container);
        }
    });
}

async function saveSkill(container) {
    const file = _state.currentFile;
    if (!file || !_activeEditor) return;

    const statusEl = container.querySelector('.fp-editor-status');
    const saveBtn = container.querySelector('.save-btn');
    const pathLabel = container.querySelector('.fp-toolbar-path');
    const content = _activeEditor.getValue();

    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'saving\u2026'; }
    if (statusEl) { statusEl.className = 'fp-editor-status'; statusEl.textContent = 'Saving\u2026'; }

    try {
        const resp = await fetch(`/api/skills/${encodeURIComponent(file.path)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', ..._authHdr() },
            body: JSON.stringify({ content }),
        });
        if (!resp.ok) throw new Error(`Save failed: ${resp.statusText}`);

        file.content = content;
        _state.originalContent = content;
        _state.dirty = false;

        if (saveBtn) { saveBtn.className = 'fp-btn save-btn'; saveBtn.textContent = 'save'; }
        if (pathLabel) pathLabel.textContent = `${file.path}/SKILL.md`;
        if (statusEl) {
            statusEl.className = 'fp-editor-status saved';
            statusEl.textContent = 'Saved';
            setTimeout(() => {
                statusEl.className = 'fp-editor-status';
                statusEl.textContent = 'Ready';
            }, 1500);
        }
    } catch (e) {
        if (saveBtn) { saveBtn.disabled = false; saveBtn.className = 'fp-btn save-btn dirty'; saveBtn.textContent = 'save'; }
        if (statusEl) {
            statusEl.className = 'fp-editor-status error';
            statusEl.textContent = `Error: ${e.message}`;
        }
    }
}

async function toggleSkill(name, enabled) {
    try {
        await fetch(`/api/skills/${encodeURIComponent(name)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', ..._authHdr() },
            body: JSON.stringify({ enabled }),
        });
        await loadSkills();
    } catch (e) {
        console.error('Toggle failed:', e);
    }
}

async function deleteSkill(name) {
    if (!confirm(`Delete skill "${name}"? This cannot be undone.`)) return;
    try {
        await del(`/api/skills/${encodeURIComponent(name)}`);
        if (_state.currentFile?.path === name) {
            _state.currentFile = null;
            _state.viewMode = 'tree';
        }
        await loadSkills();
    } catch (e) {
        console.error('Delete failed:', e);
    }
}

// ---------------------------------------------------------------------------
// Resize
// ---------------------------------------------------------------------------

function initResize(handle) {
    let startX = 0;
    let startWidth = 0;

    handle.addEventListener('mousedown', (e) => {
        if (isMobile()) return;
        e.preventDefault();
        startX = e.clientX;
        startWidth = _panel.offsetWidth;
        handle.classList.add('dragging');
        document.body.classList.add('fp-resizing');

        const onMove = (e) => {
            const delta = startX - e.clientX;
            const newWidth = Math.min(
                Math.max(MIN_WIDTH, startWidth + delta),
                window.innerWidth * 0.6
            );
            _panel.style.width = newWidth + 'px';
            _state.width = newWidth;
        };

        const onUp = () => {
            handle.classList.remove('dragging');
            document.body.classList.remove('fp-resizing');
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            saveState();
        };

        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });

    // Double-click resets to default
    handle.addEventListener('dblclick', () => {
        _state.width = DEFAULT_WIDTH;
        _panel.style.width = DEFAULT_WIDTH + 'px';
        saveState();
    });
}

// ---------------------------------------------------------------------------
// Jobs tab
// ---------------------------------------------------------------------------

async function loadJobs() {
    if (_jobRenderTimer) { clearTimeout(_jobRenderTimer); _jobRenderTimer = null; }
    clearElapsedTimers();
    renderJobs();
}

async function renderJobs() {
    const container = document.getElementById('fp-jobs');
    if (!container) return;
    clear(container);

    container.appendChild(_buildTabDesc(
        'Scheduled tasks and agent run history.',
        'Scheduled jobs run the agent on a cron schedule without user input. Active shows currently running worker sessions. History records past runs — open any entry to browse its full transcript.',
    ));

    // Live snooze activity line — fed by snooze.activity SSE events (which
    // previously reached the browser and were discarded). Surfaces Candor
    // maintenance, RLM run cleanup, and the other idle-time activities.
    if (_lastSnoozeActivity && _lastSnoozeActivity.detail) {
        container.appendChild(el('div', { class: 'fp-snooze-activity' }, [
            el('span', { class: 'fp-snooze-activity-icon' }, [text('◐')]),
            text(` Snooze: ${_lastSnoozeActivity.detail}`),
        ]));
    }

    // Search input (filters visible items across all sub-tabs)
    const jobSearch = el('input', {
        class: 'fp-search-input',
        type: 'text',
        placeholder: 'Filter jobs…',
        value: _jobsSearchQuery,
    });
    jobSearch.addEventListener('input', () => {
        _jobsSearchQuery = jobSearch.value.trim();
        if (_jobsSearchTimer) clearTimeout(_jobsSearchTimer);
        _jobsSearchTimer = setTimeout(_refreshJobsContent, 200);
    });
    container.appendChild(el('div', { class: 'fp-search-bar' }, [jobSearch]));

    // Sub-tab bar
    const subTabs = [
        { key: 'scheduled', label: 'Scheduled' },
        { key: 'active', label: 'Active' },
        { key: 'history', label: 'History' },
    ];

    const subTabBar = el('div', { class: 'fp-jobs-subtabs' });
    for (const st of subTabs) {
        const btn = el('button', {
            class: `fp-jobs-subtab${st.key === _jobsSubTab ? ' active' : ''}`,
        }, [text(st.label)]);
        btn.addEventListener('click', () => {
            _jobsSubTab = st.key;
            clearElapsedTimers();
            renderJobs();
        });
        subTabBar.appendChild(btn);
    }

    const refreshBtn = el('button', { class: 'fp-icon-btn', title: 'Refresh' }, [text('↻')]);
    refreshBtn.addEventListener('click', () => loadJobs());

    const subTabRow = el('div', { class: 'fp-jobs-header-row' }, [subTabBar, refreshBtn]);
    container.appendChild(subTabRow);

    // Stable content wrapper — updated in-place by _refreshJobsContent
    _jobsContentEl = el('div', { class: 'fp-jobs-content' });
    container.appendChild(_jobsContentEl);

    await _refreshJobsContent();
    await _appendRlmRunsSection(container);
}

// Recent RLM runs — compact list under the jobs sub-tabs (workflow "Recent
// runs" precedent). Only rendered when runs exist; absent entirely when the
// RLM add-on has never produced one.
async function _appendRlmRunsSection(container) {
    let data;
    try {
        data = await get('/api/rlm/runs?limit=8');
    } catch {
        return; // endpoint unavailable — old server, nothing to show
    }
    const runs = data.runs || [];
    if (runs.length === 0) return;

    const section = el('div', { class: 'fp-rlm-runs' });
    section.appendChild(el('div', { class: 'fp-wf-runs-label' }, [text('Recent RLM runs')]));
    for (const run of runs) {
        const badge = run.status === 'completed' ? 'fp-wf-run-pass'
                    : run.status === 'running'   ? 'fp-wf-run-running'
                    : 'fp-wf-run-fail';
        const meta = `${run.iterations || 0} it · ${run.subcalls || 0} calls · `
                   + `${run.created_at ? new Date(run.created_at).toLocaleString() : ''} · ${run.run_id}`;
        const header = el('div', { class: 'fp-wf-run-row fp-rlm-run-row' }, [
            el('span', { class: `fp-wf-run-badge ${badge}` }, [text(run.status)]),
            el('span', { class: 'fp-wf-run-meta' }, [text(meta)]),
        ]);
        const detail = el('div', { class: 'fp-rlm-run-detail', style: 'display:none' });
        header.addEventListener('click', async () => {
            const open = detail.style.display !== 'none';
            detail.style.display = open ? 'none' : '';
            if (!open && !detail.childNodes.length) {
                try {
                    const d = await get(`/api/rlm/runs/${encodeURIComponent(run.run_id)}`);
                    const lines = [
                        `task: ${d.task || ''}`,
                        `source: ${d.source_desc || ''}`,
                        `models: root=${d.root_model || '?'} sub=${d.sub_model || '?'}`,
                        d.error ? `error: ${d.error}` : `answer: ${d.answer_preview || ''}`,
                        d.has_trace ? `trace: ${d.trace_path} (workspace)` : 'trace: (purged)',
                    ];
                    detail.appendChild(el('pre', { class: 'fp-rlm-run-pre' }, [text(lines.join('\n'))]));
                } catch (e) {
                    detail.appendChild(el('div', {}, [text(`Failed to load run: ${e.message}`)]));
                }
            }
        });
        section.appendChild(header);
        section.appendChild(detail);
    }
    container.appendChild(section);
}

async function _refreshJobsContent() {
    if (!_jobsContentEl) return;
    clear(_jobsContentEl);
    _jobsContentEl.appendChild(el('div', { class: 'jobs-empty' }, [text('Loading...')]));

    try {
        const builders = { active: buildActiveTab, scheduled: buildScheduledTab, history: buildHistoryTab };
        const built = await builders[_jobsSubTab]();
        clear(_jobsContentEl);

        // Apply text filter to .jobs-item elements if a query is active
        if (_jobsSearchQuery) {
            const q = _jobsSearchQuery.toLowerCase();
            built.querySelectorAll('.jobs-item').forEach(item => {
                const matches = item.textContent.toLowerCase().includes(q);
                item.style.display = matches ? '' : 'none';
            });
        }

        _jobsContentEl.appendChild(built);
    } catch (e) {
        clear(_jobsContentEl);
        _jobsContentEl.appendChild(el('div', { class: 'jobs-empty' }, [text(`Error: ${e.message}`)]));
    }
}

function _onJobEvent(e) {
    // Track live snooze activity regardless of which tab is visible, so the
    // banner is current the moment the jobs tab renders.
    const { type, data } = e?.detail || {};
    if (type === 'snooze.activity') _lastSnoozeActivity = data || null;
    else if (type === 'snooze.done' || type === 'snooze.start') _lastSnoozeActivity = null;

    if (_state.tab !== 'jobs') return;
    const container = document.getElementById('fp-jobs');
    if (!container) return;
    // Don't destroy in-progress edit forms
    if (container.querySelector('.jobs-edit-form')) return;
    // Don't destroy add form with dirty inputs
    const addForm = container.querySelector('.jobs-add-form');
    if (addForm) {
        const inputs = addForm.querySelectorAll('input, textarea');
        if ([...inputs].some(i => i.value.trim() !== '')) return;
    }
    // Debounce: coalesce rapid SSE bursts (snooze.activity, job.started, etc.)
    // into a single render — prevents visible flicker on the jobs panel.
    if (_jobRenderTimer) clearTimeout(_jobRenderTimer);
    _jobRenderTimer = setTimeout(() => renderJobs(), 150);
}

// ---------------------------------------------------------------------------
// Tab description — shared helper used by all tabs
// ---------------------------------------------------------------------------

function _buildTabDesc(brief, full) {
    let open = false;
    const fullEl = el('div', { class: 'fp-tab-desc-full' }, [text(full)]);
    const toggle = el('button', { class: 'fp-tab-desc-toggle' }, [text('more \u203a')]);
    toggle.addEventListener('click', () => {
        open = !open;
        fullEl.classList.toggle('open', open);
        toggle.textContent = open ? 'less \u2039' : 'more \u203a';
    });
    return el('div', { class: 'fp-tab-desc' }, [
        el('div', { class: 'fp-tab-desc-brief' }, [
            el('span', {}, [text(brief)]),
            toggle,
        ]),
        fullEl,
    ]);
}

// ---------------------------------------------------------------------------
// Workflows tab
// ---------------------------------------------------------------------------

let _workflows = [];
let _workflowRuns = {};   // { [name]: runs[] } cached per workflow

async function loadWorkflows() {
    try {
        const data = await get('/api/workflows');
        _workflows = data.workflows || [];
    } catch {
        _workflows = [];
    }
    renderWorkflows();
}

function renderWorkflows() {
    const container = document.getElementById('fp-workflows');
    if (!container) return;
    disposeActiveEditor();
    clear(container);

    if (_state.viewMode === 'viewer' && _state.currentFile && _state.currentFile.source === 'workflow') {
        renderWorkflowViewer(container);
        return;
    }
    if (_state.viewMode === 'editor' && _state.currentFile && _state.currentFile.source === 'workflow') {
        renderWorkflowEditor(container);
        return;
    }

    // Section header
    const refreshBtn = el('button', { class: 'fp-icon-btn', title: 'Refresh' }, [text('\u21bb')]);
    refreshBtn.addEventListener('click', loadWorkflows);
    const newBtn = el('button', { class: 'fp-btn fp-btn-xs', title: 'Create new workflow' }, [text('+ new')]);
    newBtn.addEventListener('click', () => {
        _state.currentFile = {
            path: '',
            content: _workflowTemplate(),
            source: 'workflow',
            type: 'text',
            name: 'WORKFLOW.md',
            isNew: true,
        };
        _state.viewMode = 'editor';
        _state.dirty = false;
        renderWorkflows();
    });

    container.appendChild(el('div', { class: 'fp-section-header' }, [
        el('div', {}, [
            el('span', { class: 'fp-section-label' }, [text('Workflows')]),
            el('div', { class: 'fp-section-sub' }, [text(
                `${_workflows.length} workflow${_workflows.length !== 1 ? 's' : ''} installed`
            )]),
        ]),
        el('div', { class: 'fp-section-actions' }, [newBtn, refreshBtn]),
    ]));
    container.appendChild(_buildTabDesc(
        'Reusable multi-step pipelines that chain skills together.',
        'Each workflow is a WORKFLOW.md file in data/workflows/. Define steps as skill or instruction types. Steps with no shared dependencies run in parallel. Run with: run_workflow(name, inputs).',
    ));

    // Search input
    const wfSearch = el('input', {
        class: 'fp-search-input',
        type: 'text',
        placeholder: 'Search workflows…',
        value: _workflowsSearchQuery,
    });
    wfSearch.addEventListener('input', () => {
        _workflowsSearchQuery = wfSearch.value.trim();
        if (_workflowsSearchTimer) clearTimeout(_workflowsSearchTimer);
        _workflowsSearchTimer = setTimeout(_renderWorkflowsFiltered, 150);
    });
    container.appendChild(el('div', { class: 'fp-search-bar' }, [wfSearch]));

    _workflowsListEl = el('div', {});
    container.appendChild(_workflowsListEl);
    _renderWorkflowsFiltered();
}

function _renderWorkflowsFiltered() {
    if (!_workflowsListEl) return;
    clear(_workflowsListEl);

    const q = _workflowsSearchQuery.toLowerCase();
    const visible = q
        ? _workflows.filter(w =>
            w.name.toLowerCase().includes(q) ||
            (w.description || '').toLowerCase().includes(q) ||
            (w.tags || []).some(t => t.toLowerCase().includes(q))
          )
        : [..._workflows];

    if (visible.length === 0) {
        _workflowsListEl.appendChild(el('div', { class: 'fp-empty' }, [
            text(q ? `No workflows match "${q}"` : 'No workflows installed'),
        ]));
        return;
    }

    const listEl = el('div', { class: 'fp-wf-list' });
    for (const wf of visible) {
        const item = el('div', { class: 'fp-wf-item' });

        const deleteBtn = el('button', { class: 'fp-tree-action danger', title: 'Delete workflow' }, [text('\u00d7')]);
        deleteBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            if (!confirm(`Delete workflow '${wf.name}'?`)) return;
            try {
                await del(`/api/workflows/${encodeURIComponent(wf.name)}`);
                await loadWorkflows();
            } catch (err) {
                console.error('Failed to delete workflow:', err);
            }
        });

        item.appendChild(el('div', { class: 'fp-wf-header' }, [
            el('div', { class: 'fp-wf-name' }, [
                text(wf.name),
                el('span', { class: 'fp-skill-version' }, [text(`v${wf.version || '1.0'}`)]),
            ]),
            el('div', { class: 'fp-skill-actions' }, [deleteBtn]),
        ]));
        item.appendChild(el('div', { class: 'fp-skill-desc' }, [text(wf.description)]));

        const meta = el('div', { class: 'fp-wf-meta' });
        meta.appendChild(el('span', { class: 'fp-wf-steps' }, [
            text(`${wf.step_count} step${wf.step_count !== 1 ? 's' : ''}`)
        ]));
        if (wf.tags && wf.tags.length > 0) {
            const tagsEl = el('div', { class: 'fp-skill-tags' });
            for (const tag of wf.tags.slice(0, 5)) {
                tagsEl.appendChild(el('span', { class: 'fp-skill-tag' }, [text(tag)]));
            }
            meta.appendChild(tagsEl);
        }
        item.appendChild(meta);

        item.addEventListener('click', () => viewWorkflow(wf.name));
        listEl.appendChild(item);
    }
    _workflowsListEl.appendChild(listEl);
}

async function viewWorkflow(name) {
    try {
        const [wfData, runsData] = await Promise.all([
            get(`/api/workflows/${encodeURIComponent(name)}`),
            get(`/api/workflows/${encodeURIComponent(name)}/runs?limit=5`).catch(() => ({ runs: [] })),
        ]);
        _state.currentFile = {
            path: name,
            content: wfData.raw_content || '',
            source: 'workflow',
            type: 'text',
            name: 'WORKFLOW.md',
            wfData,
            recentRuns: runsData.runs || [],
            validationResult: null,
        };
        _state.viewMode = 'viewer';
        renderWorkflows();
    } catch (e) {
        console.error('Failed to load workflow:', e);
    }
}

function renderWorkflowViewer(container) {
    const file = _state.currentFile;
    if (!file || !file.wfData) return;
    const data = file.wfData;

    // Toolbar
    const backBtn = el('button', { class: 'fp-toolbar-back' }, [text('\u2190')]);
    backBtn.addEventListener('click', () => {
        _state.viewMode = 'tree';
        _state.currentFile = null;
        renderWorkflows();
    });
    const editBtn = el('button', { class: 'fp-btn' }, [text('edit')]);
    editBtn.addEventListener('click', () => {
        _state.viewMode = 'editor';
        _state.originalContent = file.content;
        _state.dirty = false;
        renderWorkflows();
    });
    const validateBtn = el('button', { class: 'fp-btn' }, [text('validate')]);
    validateBtn.addEventListener('click', async () => {
        validateBtn.disabled = true;
        validateBtn.textContent = 'validating\u2026';
        try {
            const result = await post('/api/workflows/validate', { content: file.content });
            file.validationResult = result;
            renderWorkflows();
        } catch (e) {
            console.error('Validation failed:', e);
        } finally {
            validateBtn.disabled = false;
            validateBtn.textContent = 'validate';
        }
    });

    container.appendChild(el('div', { class: 'fp-toolbar' }, [
        backBtn,
        el('span', { class: 'fp-toolbar-path' }, [text(`${data.name}/WORKFLOW.md`)]),
        el('div', { class: 'fp-toolbar-actions' }, [validateBtn, editBtn]),
    ]));

    // Validation result banner
    if (file.validationResult) {
        const vr = file.validationResult;
        const bannerClass = vr.valid ? 'fp-wf-validation valid' : 'fp-wf-validation invalid';
        const banner = el('div', { class: bannerClass });
        banner.appendChild(el('div', { class: 'fp-wf-validation-title' }, [
            text(vr.valid ? '\u2713 Valid' : '\u2717 Invalid'),
        ]));
        if (vr.errors && vr.errors.length > 0) {
            for (const err of vr.errors) {
                const loc = err.step_id ? ` [step '${err.step_id}']` : '';
                banner.appendChild(el('div', { class: 'fp-wf-validation-error' }, [text(`\u2022${loc} ${err.message}`)]));
            }
        }
        if (vr.warnings && vr.warnings.length > 0) {
            for (const w of vr.warnings) {
                banner.appendChild(el('div', { class: 'fp-wf-validation-warning' }, [text(`\u26a0 ${w.message}`)]));
            }
        }
        if (vr.valid && vr.info) {
            banner.appendChild(el('div', { class: 'fp-wf-validation-info' }, [
                text(`${vr.info.step_count} step(s) in ${vr.info.wave_count} execution wave(s)`)
            ]));
        }
        container.appendChild(banner);
    }

    // Info card
    const info = el('div', { class: 'fp-skill-info' });
    info.appendChild(el('div', { class: 'fp-skill-info-name' }, [text(data.name)]));
    info.appendChild(el('div', { class: 'fp-skill-desc' }, [text(data.description)]));
    if (data.tags && data.tags.length > 0) {
        const tagsEl = el('div', { class: 'fp-skill-tags' });
        for (const tag of data.tags) tagsEl.appendChild(el('span', { class: 'fp-skill-tag' }, [text(tag)]));
        info.appendChild(tagsEl);
    }
    container.appendChild(info);

    // Recent runs
    if (file.recentRuns && file.recentRuns.length > 0) {
        const runsEl = el('div', { class: 'fp-wf-runs' });
        runsEl.appendChild(el('div', { class: 'fp-wf-runs-label' }, [text('Recent runs')]));
        for (const run of file.recentRuns) {
            const badge = run.status === 'complete' ? 'fp-wf-run-pass'
                        : run.status === 'running'  ? 'fp-wf-run-running'
                        : 'fp-wf-run-fail';
            const passedStr = run.steps_passed != null
                ? `${run.steps_passed}/${run.step_count} steps`
                : '';
            const date = run.started_at ? new Date(run.started_at).toLocaleString() : '';
            runsEl.appendChild(el('div', { class: 'fp-wf-run-row' }, [
                el('span', { class: `fp-wf-run-badge ${badge}` }, [text(run.status)]),
                el('span', { class: 'fp-wf-run-meta' }, [text(`${passedStr} \u00b7 ${date}`)]),
            ]));
        }
        container.appendChild(runsEl);
    }

    // Step visualization — grouped by execution wave
    if (data.steps && data.steps.length > 0) {
        _renderStepGraph(container, data.steps);
    }

    // Usage notes (body markdown)
    if (data.body && data.body.trim()) {
        const viewer = el('div', { class: 'fp-viewer' });
        viewer.appendChild(el('div', { class: 'fp-wf-body-label' }, [text('Usage notes')]));
        const md = el('div', { class: 'fp-viewer-md' });
        md.appendChild(renderMarkdown(data.body));
        viewer.appendChild(md);
        container.appendChild(viewer);
    }
}

function _renderStepGraph(container, steps) {
    // Compute waves from depends_on (topological sort in JS)
    const stepMap = {};
    steps.forEach(s => { stepMap[s.id] = s; });

    const inDegree = {};
    const adjacency = {};
    steps.forEach(s => { inDegree[s.id] = 0; adjacency[s.id] = []; });
    steps.forEach(s => {
        (s.depends_on || []).forEach(dep => {
            if (inDegree[s.id] !== undefined) inDegree[s.id]++;
            if (adjacency[dep]) adjacency[dep].push(s.id);
        });
    });

    const waves = [];
    let queue = steps.filter(s => inDegree[s.id] === 0).map(s => s.id);
    while (queue.length > 0) {
        waves.push([...queue]);
        const next = [];
        queue.forEach(sid => {
            (adjacency[sid] || []).forEach(child => {
                inDegree[child]--;
                if (inDegree[child] === 0) next.push(child);
            });
        });
        queue = next;
    }

    const graphEl = el('div', { class: 'fp-wf-graph' });
    graphEl.appendChild(el('div', { class: 'fp-wf-graph-label' }, [text('Execution plan')]));

    waves.forEach((waveIds, i) => {
        const waveEl = el('div', { class: 'fp-wf-wave' });
        waveEl.appendChild(el('div', { class: 'fp-wf-wave-label' }, [
            text(`Wave ${i + 1}${waveIds.length > 1 ? ' (parallel)' : ''}`)
        ]));
        const stepsRow = el('div', { class: 'fp-wf-wave-steps' });
        waveIds.forEach(sid => {
            const step = stepMap[sid];
            if (!step) return;
            const card = el('div', { class: 'fp-wf-step-card' });

            // Type badge
            const badgeClass = step.type === 'skill' ? 'fp-wf-badge-skill' : 'fp-wf-badge-instruction';
            card.appendChild(el('div', { class: 'fp-wf-step-top' }, [
                el('span', { class: `fp-wf-step-id` }, [text(step.id)]),
                el('span', { class: `fp-wf-step-badge ${badgeClass}` }, [text(step.type)]),
            ]));

            if (step.skill) {
                card.appendChild(el('div', { class: 'fp-wf-step-skill' }, [
                    el('span', { class: 'fp-wf-step-skill-icon' }, [text('\u2699')]),
                    text(` ${step.skill}`),
                ]));
            }

            if (step.description) {
                card.appendChild(el('div', { class: 'fp-wf-step-desc' }, [text(step.description)]));
            }

            if (step.instructions) {
                card.appendChild(el('div', { class: 'fp-wf-step-instructions' }, [
                    text(step.instructions.length > 120
                        ? step.instructions.slice(0, 120) + '\u2026'
                        : step.instructions)
                ]));
            }

            const foot = el('div', { class: 'fp-wf-step-foot' });
            if (step.output_file) {
                foot.appendChild(el('span', { class: 'fp-wf-step-output' }, [text(`\u2192 ${step.output_file}`)]));
            }
            if (step.depends_on && step.depends_on.length > 0) {
                foot.appendChild(el('span', { class: 'fp-wf-step-deps' }, [
                    text(`after: ${step.depends_on.join(', ')}`)
                ]));
            }
            if (foot.children.length > 0) card.appendChild(foot);

            stepsRow.appendChild(card);
        });
        waveEl.appendChild(stepsRow);

        // Arrow between waves
        if (i < waves.length - 1) {
            waveEl.appendChild(el('div', { class: 'fp-wf-wave-arrow' }, [text('\u2193')]));
        }
        graphEl.appendChild(waveEl);
    });

    container.appendChild(graphEl);
}

function renderWorkflowEditor(container) {
    const file = _state.currentFile;
    if (!file) return;
    const isNew = file.isNew || false;
    const pathText = isNew ? 'new WORKFLOW.md' : `${file.path}/WORKFLOW.md`;

    const backBtn = el('button', { class: 'fp-toolbar-back', title: 'Back' }, [text('\u2190')]);
    backBtn.addEventListener('click', () => {
        if (_state.dirty && !confirm('Discard unsaved changes?')) return;
        disposeActiveEditor();
        _state.dirty = false;
        _state.viewMode = isNew ? 'tree' : 'viewer';
        if (isNew) _state.currentFile = null;
        renderWorkflows();
    });

    const saveBtn = el('button', { class: 'fp-btn save-btn', disabled: true }, [text('save')]);
    saveBtn.addEventListener('click', () => { if (_state.dirty) saveWorkflow(container, isNew); });

    const validateBtn = el('button', { class: 'fp-btn' }, [text('validate')]);
    validateBtn.addEventListener('click', async () => {
        validateBtn.disabled = true;
        validateBtn.textContent = 'validating\u2026';
        const statusEl = container.querySelector('.fp-editor-status');
        try {
            const content = _activeEditor ? _activeEditor.getValue() : file.content;
            const result = await post('/api/workflows/validate', { content });
            if (statusEl) {
                if (result.valid) {
                    const waves = (result.info && result.info.wave_count) || 0;
                    statusEl.className = 'fp-editor-status saved';
                    statusEl.textContent = `\u2713 Valid \u00b7 ${result.info.step_count} steps, ${waves} wave(s)`;
                } else {
                    const errCount = (result.errors || []).length;
                    statusEl.className = 'fp-editor-status error';
                    statusEl.textContent = `\u2717 ${errCount} error(s) \u2014 ${(result.errors[0] || {}).message || ''}`;
                }
            }
        } catch (e) {
            if (statusEl) { statusEl.className = 'fp-editor-status error'; statusEl.textContent = 'Validation failed'; }
        } finally {
            validateBtn.disabled = false;
            validateBtn.textContent = 'validate';
        }
    });

    const cancelBtn = el('button', { class: 'fp-btn' }, [text('cancel')]);
    cancelBtn.addEventListener('click', () => {
        if (_state.dirty && !confirm('Discard unsaved changes?')) return;
        disposeActiveEditor();
        _state.dirty = false;
        _state.viewMode = isNew ? 'tree' : 'viewer';
        if (isNew) _state.currentFile = null;
        renderWorkflows();
    });

    const pathLabel = el('span', { class: 'fp-toolbar-path' }, [text(pathText)]);

    container.appendChild(el('div', { class: 'fp-toolbar' }, [
        backBtn,
        pathLabel,
        el('div', { class: 'fp-toolbar-actions' }, [validateBtn, saveBtn, cancelBtn]),
    ]));

    // Editor wrap: host div for Monaco + status bar inside — matches workspace/skill pattern
    const editorWrap = el('div', { class: 'fp-editor' });
    const editorHost = el('div', { class: 'fp-editor-host' });
    const statusEl = el('div', { class: 'fp-editor-status' }, [text('Ready')]);
    editorWrap.appendChild(editorHost);
    editorWrap.appendChild(statusEl);
    container.appendChild(editorWrap);

    function onDirtyChange(dirty) {
        _state.dirty = dirty;
        saveBtn.disabled = !dirty;
        saveBtn.className = `fp-btn save-btn${dirty ? ' dirty' : ''}`;
        statusEl.className = `fp-editor-status${dirty ? ' dirty' : ''}`;
        statusEl.textContent = dirty ? 'Modified' : 'Ready';
        pathLabel.textContent = dirty ? `\u25CF ${pathText}` : pathText;
    }

    createCodeEditor(editorHost, file.content, 'yaml', (value) => {
        onDirtyChange(value !== (file.content || ''));
    }).then(inst => {
        _activeEditor = inst;
        inst.addSaveCommand(() => saveWorkflow(container, isNew));
        inst.focus();
    });

    editorHost.addEventListener('keydown', (e) => {
        if (e.key === 's' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            saveWorkflow(container, isNew);
        }
    });
}

async function saveWorkflow(container, isNew) {
    if (!_activeEditor) return;
    const content = _activeEditor.getValue();
    const statusEl = container.querySelector('.fp-editor-status');
    const saveBtn = container.querySelector('.save-btn');
    const pathLabel = container.querySelector('.fp-toolbar-path');

    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'saving\u2026'; }
    if (statusEl) { statusEl.className = 'fp-editor-status'; statusEl.textContent = 'Saving\u2026'; }

    // Validate before writing
    try {
        const result = await post('/api/workflows/validate', { content });
        if (!result.valid) {
            const errCount = (result.errors || []).length;
            const firstErr = (result.errors[0] || {}).message || 'unknown error';
            if (statusEl) { statusEl.className = 'fp-editor-status error'; statusEl.textContent = `\u2717 Cannot save: ${errCount} error(s) \u2014 ${firstErr}`; }
            if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'save'; saveBtn.className = 'fp-btn save-btn dirty'; }
            return;
        }
    } catch (e) {
        console.error('Pre-save validation failed:', e);
        // Non-fatal — server will also validate
    }

    try {
        if (isNew) {
            await post('/api/workflows', { content });
        } else {
            const resp = await fetch(`/api/workflows/${encodeURIComponent(_state.currentFile.path)}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', ..._authHdr() },
                body: JSON.stringify({ content }),
            });
            if (!resp.ok) throw new Error(`Save failed: ${resp.statusText}`);
        }

        _state.currentFile.content = content;
        _state.dirty = false;
        if (saveBtn) { saveBtn.disabled = true; saveBtn.className = 'fp-btn save-btn'; saveBtn.textContent = 'save'; }
        if (statusEl) { statusEl.className = 'fp-editor-status saved'; statusEl.textContent = 'Saved'; }
        const pathText = isNew ? 'new WORKFLOW.md' : `${_state.currentFile.path}/WORKFLOW.md`;
        if (pathLabel) pathLabel.textContent = pathText;

        if (isNew) {
            // Navigate to the workflow list so the new workflow is visible
            disposeActiveEditor();
            _state.dirty = false;
            _state.viewMode = 'tree';
            _state.currentFile = null;
            await loadWorkflows();
        }
    } catch (e) {
        console.error('Failed to save workflow:', e);
        if (statusEl) { statusEl.className = 'fp-editor-status error'; statusEl.textContent = `Save failed: ${e.message || e}`; }
        if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = 'save'; saveBtn.className = 'fp-btn save-btn dirty'; }
    }
}

function _workflowTemplate() {
    return `---
name: my-workflow
description: Describe what this workflow does
tags: []
version: "1.0"
steps:
  - id: step-one
    type: instruction
    description: First step — describe what to do
    output_file: step_one_output.md
    depends_on: []

  - id: step-two
    skill: my-skill
    instructions: |
      Use the skill to process the output from step-one.
      Read: step_one_output.md
    output_file: step_two_output.md
    depends_on: [step-one]
---

## Usage

Describe when and how to run this workflow.
`;
}

// ---------------------------------------------------------------------------
// Tools tab
// ---------------------------------------------------------------------------

async function loadTools() {
    try {
        const [toolsData, settingsData] = await Promise.all([
            get('/api/tools'),
            get('/api/settings'),
        ]);
        _tools = toolsData.tools || [];
        _autoApproveDangerous = settingsData.auto_approve_dangerous || false;
    } catch {
        _tools = [];
        _autoApproveDangerous = false;
    }
    renderTools();
}

function renderTools() {
    const container = document.getElementById('fp-tools');
    if (!container) return;
    clear(container);

    // Header
    const refreshBtn = el('button', { class: 'fp-icon-btn', title: 'Refresh' }, [text('↻')]);
    refreshBtn.addEventListener('click', loadTools);

    const sortSelect = el('select', { class: 'fp-ws-sort', title: 'Sort tools by…' }, [
        el('option', { value: 'name',     ...(_toolsSortBy === 'name'     ? { selected: '' } : {}) }, [text('Name')]),
        el('option', { value: 'safety',   ...(_toolsSortBy === 'safety'   ? { selected: '' } : {}) }, [text('Safety')]),
        el('option', { value: 'category', ...(_toolsSortBy === 'category' ? { selected: '' } : {}) }, [text('Category')]),
        el('option', { value: 'status',   ...(_toolsSortBy === 'status'   ? { selected: '' } : {}) }, [text('Status')]),
    ]);
    sortSelect.addEventListener('change', () => {
        _toolsSortBy = sortSelect.value;
        _renderToolsFiltered();
    });

    const enabledCount = _tools.filter(t => t.enabled).length;
    container.appendChild(el('div', { class: 'fp-section-header' }, [
        el('div', {}, [
            el('span', { class: 'fp-section-label' }, [text('Tools')]),
            el('div', { class: 'fp-section-sub' }, [text(
                `${_tools.length} registered · ${enabledCount} enabled`,
            )]),
        ]),
        el('div', { class: 'fp-section-actions' }, [sortSelect, refreshBtn]),
    ]));
    container.appendChild(_buildTabDesc(
        'Registered capabilities the agent can call during a turn.',
        'Tools are the agent\'s hands — file operations, shell commands, web access, memory, orchestration, and more. Disable to remove a tool from the active schema without deleting it. Adjust the safety level to control whether the dangerous-tool gate applies.',
    ));

    // Run Dangerously banner — shown when auto_approve_dangerous is on
    if (_autoApproveDangerous) {
        const tip = el('div', { class: 'fp-danger-banner-tip' }, [
            el('strong', {}, [text('Run Dangerously is ON')]),
            el('br', {}),
            text(
                'All dangerous-tool approvals are bypassed. Shell commands, file writes, and ' +
                'web requests execute without confirmation in every session, including workers ' +
                'and cron jobs. Click to open Security settings.'
            ),
        ]);
        const helpBtn = el('button', { class: 'fp-danger-banner-help', 'aria-label': 'More info' }, [text('?')]);
        const banner = el('div', { class: 'fp-danger-mode-banner' }, [
            el('div', { class: 'fp-danger-banner-inner' }, [
                el('span', { class: 'fp-danger-banner-icon' }, [text('⚠')]),
                el('span', { class: 'fp-danger-banner-text' }, [
                    text('Run Dangerously mode is enabled — all tool approvals are bypassed'),
                ]),
                el('span', { class: 'fp-danger-banner-help-wrap' }, [helpBtn, tip]),
            ]),
        ]);
        banner.addEventListener('click', (e) => {
            // ? button shows tooltip via CSS hover — clicking it shouldn't open settings
            if (e.target === helpBtn || helpBtn.contains(e.target)) return;
            openSettings({ tab: 'security' });
        });
        container.appendChild(banner);
    }

    // Search
    const searchInput = el('input', {
        class: 'fp-search-input',
        type: 'text',
        placeholder: 'Search tools…',
        value: _toolsSearchQuery,
    });
    searchInput.addEventListener('input', () => {
        _toolsSearchQuery = searchInput.value.trim();
        if (_toolsSearchTimer) clearTimeout(_toolsSearchTimer);
        // Update the list in-place: don't call renderTools() which would destroy
        // the search input and steal focus on every keystroke.
        _toolsSearchTimer = setTimeout(_renderToolsFiltered, 150);
    });
    container.appendChild(el('div', { class: 'fp-search-bar' }, [searchInput]));

    // Stable wrapper for the filtered list — only this gets cleared on re-filter.
    _toolsListEl = el('div', {});
    container.appendChild(_toolsListEl);
    _renderToolsFiltered();
}

function _renderToolsFiltered() {
    if (!_toolsListEl) return;
    clear(_toolsListEl);

    const q = _toolsSearchQuery.toLowerCase();
    const safetyOrder = { dangerous: 0, caution: 1, safe: 2 };
    let visible = q
        ? _tools.filter(t =>
            t.name.toLowerCase().includes(q) ||
            (t.description || '').toLowerCase().includes(q) ||
            (t.category || '').toLowerCase().includes(q) ||
            (t.tags || []).some(tag => tag.toLowerCase().includes(q))
          )
        : [..._tools];
    visible.sort((a, b) => {
        if (_toolsSortBy === 'safety') {
            const d = (safetyOrder[a.safety_level] ?? 3) - (safetyOrder[b.safety_level] ?? 3);
            return d !== 0 ? d : a.name.localeCompare(b.name);
        }
        if (_toolsSortBy === 'category') {
            const d = (a.category || '').localeCompare(b.category || '');
            return d !== 0 ? d : a.name.localeCompare(b.name);
        }
        if (_toolsSortBy === 'status') {
            if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
            return a.name.localeCompare(b.name);
        }
        return a.name.localeCompare(b.name);
    });

    if (visible.length === 0) {
        _toolsListEl.appendChild(el('div', { class: 'fp-empty' }, [
            text(q ? `No tools match "${q}"` : 'No tools loaded'),
        ]));
        return;
    }

    const listEl = el('div', { class: 'fp-skills-list' });
    for (const tool of visible) {
        const item = el('div', { class: `fp-skill-item${tool.enabled ? '' : ' disabled'}` });

        // Toggle
        const toggle = el('button', {
            class: `fp-skill-toggle${tool.enabled ? ' on' : ''}`,
            title: tool.enabled ? 'Disable tool' : 'Enable tool',
        }, [text(tool.enabled ? 'on' : 'off')]);
        toggle.addEventListener('click', async (e) => {
            e.stopPropagation();
            try {
                await fetch('/api/tools/toggle', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ..._authHdr() },
                    body: JSON.stringify({ name: tool.name, enabled: !tool.enabled }),
                });
                await loadTools();
            } catch (err) { console.error('Tool toggle failed:', err); }
        });

        // Safety level select
        const level = tool.safety_level || 'safe';
        const safetySelect = el('select', {
            class: `fp-tool-safety sl-${level}`,
            title: 'Safety level — controls whether auto_approve_dangerous gate applies',
        });
        for (const lvl of ['safe', 'caution', 'dangerous']) {
            const opt = el('option', { value: lvl }, [text(lvl)]);
            if (lvl === level) opt.selected = true;
            safetySelect.appendChild(opt);
        }
        safetySelect.addEventListener('change', async (e) => {
            e.stopPropagation();
            const newLevel = e.target.value;
            safetySelect.className = `fp-tool-safety sl-${newLevel}`;
            try {
                const res = await fetch('/api/tools/set-safety', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ..._authHdr() },
                    body: JSON.stringify({ name: tool.name, safety_level: newLevel }),
                });
                const data = await res.json();
                if (data.error) {
                    console.error('Safety level update failed:', data.error);
                    safetySelect.className = `fp-tool-safety sl-${level}`;
                }
            } catch (err) {
                console.error('Safety level update failed:', err);
                safetySelect.className = `fp-tool-safety sl-${level}`;
            }
        });

        // Category badge
        const catBadge = tool.category
            ? el('span', { class: 'fp-skill-version' }, [text(tool.category)])
            : null;

        item.appendChild(el('div', { class: 'fp-skill-header' }, [
            el('div', { class: 'fp-skill-name' }, [
                text(tool.name),
                ...(catBadge ? [catBadge] : []),
            ]),
            el('div', { class: 'fp-skill-actions' }, [safetySelect, toggle]),
        ]));

        // Description
        if (tool.description) {
            item.appendChild(el('div', { class: 'fp-skill-desc' }, [text(tool.description)]));
        }

        // Tags
        if (tool.tags && tool.tags.length > 0) {
            const tagsEl = el('div', { class: 'fp-skill-tags' });
            for (const tag of tool.tags.slice(0, 6)) {
                tagsEl.appendChild(el('span', { class: 'fp-skill-tag' }, [text(tag)]));
            }
            item.appendChild(tagsEl);
        }

        // Performance indicator
        if (tool.performance && tool.performance.uses > 0) {
            const p = tool.performance;
            const perfText = p.failures > 0
                ? `${p.uses} uses · ⚠ ${p.failures} failure${p.failures !== 1 ? 's' : ''}`
                : `${p.uses} uses`;
            item.appendChild(el('div', {
                class: `fp-perf-line${p.failures > 0 ? ' fp-perf-warn' : ''}`,
            }, [text(perfText)]));
        }

        listEl.appendChild(item);
    }
    _toolsListEl.appendChild(listEl);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderCurrentTab() {
    if (_state.tab === 'workspace') renderWorkspace();
    else if (_state.tab === 'memory') renderMemory();
    else if (_state.tab === 'skills') renderSkills();
    else if (_state.tab === 'tools') renderTools();
    else if (_state.tab === 'jobs') renderJobs();
}

function sessionTypeDot(sessionType) {
    const typeMap = {
        normal: 'chat',
        worker: 'worker',
        cron: 'cron',
    };
    const cls = typeMap[sessionType] || 'chat';
    return el('span', { class: `session-dot ${cls}`, title: sessionType || 'session' });
}

function formatDate(epoch) {
    if (!epoch) return '';
    const d = new Date(epoch * 1000);
    const now = new Date();
    const diff = now - d;
    if (diff < 86400000) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (diff < 86400000 * 365) return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    return d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
}

function formatSize(bytes) {
    if (bytes == null) return '';
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)}K`;
    return `${(bytes / 1048576).toFixed(1)}M`;
}

function timeAgo(isoOrEpoch) {
    if (!isoOrEpoch) return '';
    const date = typeof isoOrEpoch === 'number'
        ? new Date(isoOrEpoch * 1000)
        : new Date(isoOrEpoch);
    const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
    return date.toLocaleDateString();
}
