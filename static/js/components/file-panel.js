// Pernix — Explorer panel (workspace, memory, skills, jobs)

import { el, text, clear, renderMarkdown } from '../render.js';
import { get, post, del, getAuthToken } from '../api.js';
import { isMobile } from '../mobile.js';
import { notify } from '../feedback.js';
import { openSettings } from './modals/settings.js';

function _authHdr() { const t = getAuthToken(); return t ? { 'Authorization': `Bearer ${t}` } : {}; }
import {
    buildActiveTab, buildScheduledTab, buildHistoryTab,
    setJobsCallbacks, clearElapsedTimers,
} from './modals/jobs.js';
import { renderAdaptiveTab } from './modals/adaptive.js';
import { renderCanaryTab } from './modals/canary.js';
import { renderTelosTab } from './modals/telos.js';

// ---------------------------------------------------------------------------
// Monaco Editor (vendored) with lightweight textarea fallback
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
        // Monaco is served from this origin now, so the old offline hang is
        // gone — but the timeout stays as the fallback's trigger for any
        // AMD load that stalls (a partially-populated SW cache, a half-open
        // connection), since a hung require() otherwise leaves createCodeEditor
        // awaiting forever and the file panel without any editor.
        const timer = setTimeout(() => {
            _monacoReady = null;
            reject(new Error('Monaco load timed out'));
        }, 8000);
        const _resolve = resolve;
        resolve = (m) => { clearTimeout(timer); _resolve(m); };
        window.require.config({
            paths: { vs: '/static/vendor/monaco/vs' },
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
// Upload rows: an entry per file being sent, kept OUTSIDE the DOM so a
// directory re-render cannot wipe an error the user has not read yet.
// [{ name, state: 'uploading' | 'error', detail }] (F1/S8)
let _wsUploads = [];
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
    _syncPanelInert();
}

// A closed Explorer is width:0 + overflow:hidden — invisible, but every tab
// button, tree row and editor inside it stayed in the tab order and in the
// accessibility tree. inert removes both; it is cleared the moment the panel
// opens. (A12)
// Tree rows, breadcrumb segments and sortable column headers were plain
// <div>/<span> elements carrying a click handler: visible, clickable by mouse,
// and completely unreachable from a keyboard or a screen reader. role=button +
// a tab stop + Enter/Space is the minimum that makes them real controls. (A1)
function _makeActivatable(node, label, onActivate) {
    node.setAttribute('role', 'button');
    node.setAttribute('tabindex', '0');
    if (label) node.setAttribute('aria-label', label);
    node.addEventListener('click', onActivate);
    node.addEventListener('keydown', (e) => {
        // A real <button> nested inside the row (delete) handles its own keys;
        // without this the row would fire a second time on the way up.
        if (e.target !== node) return;
        if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
        e.preventDefault();
        onActivate(e);
    });
    return node;
}

function _syncPanelInert() {
    _panel?.toggleAttribute('inert', !_state.open);
}

// One question in one place. Every path that throws away an in-progress edit
// — closing the panel, switching tabs, opening another file, Back/Cancel in
// either editor — asks through this, and a confirmed discard clears the flag
// so the beforeunload handler stops prompting for changes the user already
// agreed to lose. (S1)
function guardDirty() {
    if (!_state.dirty) return true;
    if (!confirm('Discard unsaved changes?')) return false;
    _state.dirty = false;
    return true;
}

export function toggleFilePanel() {
    if (_state.open && !guardDirty()) return;
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
    _syncPanelInert();
    saveState();
}

export function openFilePanel(opts = {}) {
    if (!_state.open) {
        _state.open = true;
        _panel.classList.add('open');
        if (!isMobile()) _panel.style.width = _state.width + 'px';
        document.getElementById('files-btn')?.classList.add('active');
        _syncPanelInert();
    }
    if (opts.tab) {
        _state.tab = opts.tab;
        renderTabs();
    }
    if (opts.file) {
        if (!guardDirty()) return;
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
    const mcpContent = el('div', { class: 'fp-tab-content', 'data-tab': 'mcp', id: 'fp-mcp' });
    const adaptiveContent = el('div', { class: 'fp-tab-content', 'data-tab': 'adaptive', id: 'fp-adaptive' });
    const canaryContent = el('div', { class: 'fp-tab-content', 'data-tab': 'canary', id: 'fp-canary' });
    const telosContent = el('div', { class: 'fp-tab-content', 'data-tab': 'telos', id: 'fp-telos' });

    _panel.appendChild(handle);
    _panel.appendChild(header);
    _panel.appendChild(tabBar);
    _panel.appendChild(wsContent);
    _panel.appendChild(memContent);
    _panel.appendChild(skillContent);
    _panel.appendChild(toolsContent);
    _panel.appendChild(mcpContent);
    _panel.appendChild(jobsContent);
    _panel.appendChild(adaptiveContent);
    _panel.appendChild(canaryContent);
    _panel.appendChild(telosContent);

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
        { key: 'mcp', label: 'MCP' },
        { key: 'jobs', label: 'Jobs' },
        { key: 'adaptive', label: 'Adaptive' },
        { key: 'canary', label: 'Canary' },
        { key: 'telos', label: 'Telos' },
    ];

    // A row of <button>s is not a tab strip to anything but a mouse: nothing
    // said which one was current, and Tab walked through all nine. (A1)
    tabBar.setAttribute('role', 'tablist');
    tabBar.setAttribute('aria-label', 'Explorer sections');

    tabs.forEach(t => {
        const selected = t.key === _state.tab;
        const btn = el('button', {
            class: `fp-tab-btn${selected ? ' active' : ''}`,
            'data-tab': t.key,
            id: `fp-tab-${t.key}`,
            role: 'tab',
            'aria-selected': String(selected),
            'aria-controls': `fp-${t.key}`,
            // Roving tabindex: one tab stop for the strip, arrows move inside
            // it — the contract role="tab" commits you to.
            tabindex: selected ? '0' : '-1',
        }, [text(t.label)]);
        btn.addEventListener('click', () => {
            if (t.key !== _state.tab && !guardDirty()) return;
            if (_state.tab === 'jobs' && t.key !== 'jobs') clearElapsedTimers();
            _state.tab = t.key;
            _state.viewMode = 'tree';
            _state.currentFile = null;
            renderTabs();
            loadTabData();
            saveState();
        });
        btn.addEventListener('keydown', (e) => {
            const delta = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
            if (!delta) return;
            e.preventDefault();
            const at = tabs.findIndex(x => x.key === t.key);
            const nextKey = tabs[(at + delta + tabs.length) % tabs.length].key;
            document.getElementById(`fp-tab-${nextKey}`)?.click();
            // The click rebuilt the strip — focus the NEW node, not this one.
            document.getElementById(`fp-tab-${nextKey}`)?.focus();
        });
        tabBar.appendChild(btn);
    });

    // Show/hide tab content
    ['workspace', 'memory', 'skills', 'tools', 'mcp', 'jobs', 'adaptive', 'canary', 'telos'].forEach(key => {
        const container = document.getElementById(`fp-${key}`);
        if (!container) return;
        container.classList.toggle('active', key === _state.tab);
        container.setAttribute('role', 'tabpanel');
        container.setAttribute('aria-labelledby', `fp-tab-${key}`);
        // The panel scrolls, so it needs to be reachable in its own right.
        // Inactive panels are display:none and therefore not focusable.
        container.setAttribute('tabindex', '0');
    });
}

async function loadTabData() {
    if (_state.tab === 'workspace') await loadWorkspace();
    else if (_state.tab === 'memory') await loadMemory();
    else if (_state.tab === 'skills') await loadSkills();
    else if (_state.tab === 'tools') await loadTools();
    else if (_state.tab === 'mcp') await loadMcp();
    else if (_state.tab === 'jobs') await loadJobs();
    else if (_state.tab === 'adaptive') await renderAdaptiveTab(document.getElementById('fp-adaptive'));
    else if (_state.tab === 'canary') await renderCanaryTab(document.getElementById('fp-canary'));
    else if (_state.tab === 'telos') await renderTelosTab(document.getElementById('fp-telos'));
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
        // Already at the root and it still failed — that is not a stale
        // bookmark, it is the workspace being unreadable.
        notify('error', `Could not list the workspace: ${e.message || e}`);
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

    const uploadBtn = el('button', {
        class: 'fp-icon-btn',
        title: _wsCurrentPath ? `Upload into ${_wsCurrentPath}/` : 'Upload into the workspace root',
    }, [text('\u2191')]);
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
        'Files live at data/workspace/ and are accessible to agent tools. Navigate directories with the tree, view file contents inline, or open the editor to modify them directly. Uploads land in the folder you are currently looking at.',
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

    if (_wsUploads.length) {
        const upEl = el('div', { class: 'fp-tree' });
        _renderUploadRows(upEl);
        container.appendChild(upEl);
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

// One row per file in flight. It becomes the real file (row removed, listing
// refreshed) or an error row that stays until dismissed. (F1/S8)
function _renderUploadRows(parent) {
    for (const up of _wsUploads) {
        const isErr = up.state === 'error';
        const row = el('div', { class: `fp-tree-item fp-upload-row${isErr ? ' error' : ''}` }, [
            el('span', { class: 'fp-tree-icon' }, [text(isErr ? '\u26A0' : '\u2191')]),
            el('span', { class: 'fp-tree-name' }, [text(up.name)]),
            el('span', { class: 'fp-tree-count' }),
            el('span', { class: 'fp-tree-meta' }, [text(isErr ? up.detail : 'uploading\u2026')]),
            el('span', { class: 'fp-tree-date' }),
        ]);
        if (isErr) {
            const dismiss = el('button', {
                class: 'fp-tree-action',
                title: 'Dismiss',
                'aria-label': `Dismiss the upload error for ${up.name}`,
            }, [text('\u00d7')]);
            dismiss.addEventListener('click', () => {
                _wsUploads = _wsUploads.filter(u => u !== up);
                renderWorkspace();
            });
            row.appendChild(el('span', { class: 'fp-tree-actions' }, [dismiss]));
        }
        parent.appendChild(row);
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
            _makeActivatable(span, `Sort by ${label.toLowerCase()}`, () => {
                _state.wsSortBy = key;
                saveState();
                renderWorkspace();
            });
            span.setAttribute('aria-pressed', String(!!isActive));
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
        _makeActivatable(rootLink, 'Go to the workspace root', () => loadWorkspace({ path: '' }));
    } else {
        rootLink.setAttribute('aria-label', 'Workspace root');
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
            _makeActivatable(seg, `Go to ${segPath}`, () => loadWorkspace({ path: segPath }));
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
        _makeActivatable(upItem, 'Go up one folder', () => loadWorkspace({ path: _wsParent }));
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
            _makeActivatable(item, `Open the folder ${entry.name}`, () => {
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

            _makeActivatable(item, `Open ${displayName}`, () => viewFile(entry.path, 'workspace'));
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
                    const mtime = _mtimeOf(resp);
                    // Double-check: if the fetched text is huge (chunked response with no content-length)
                    if (content.length > MAX_TEXT_SIZE) {
                        _state.currentFile = {
                            path, content: content.slice(0, MAX_TEXT_SIZE),
                            source, type: 'text', name, truncated: true, mtime,
                        };
                    } else {
                        _state.currentFile = { path, content, source, type: 'text', name, mtime };
                    }
                }
            }
        }
        _state.viewMode = 'viewer';
        _state.dirty = false;
        renderCurrentTab();
    } catch (e) {
        notify('error', `Could not open ${path}: ${e.message || e}`);
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

// The mtime the server stamped on the file we read. Handed back as base_mtime
// on save so a PUT can tell "nobody touched it" from "the agent rewrote it
// while you were typing". Missing/garbled header = no conflict detection,
// i.e. exactly the old behaviour. (S3)
// The server's own explanation of a failure, when it has one. Every fetch()
// in this file used to swallow the body and print a status code to a console
// nobody has open. (F1/S8)
async function _errorDetail(resp) {
    try {
        const data = await resp.json();
        const d = data && data.detail;
        if (d) return typeof d === 'string' ? d : JSON.stringify(d);
        if (data && data.error) return String(data.error);
    } catch { /* not JSON — fall through to the status line */ }
    return `${resp.status} ${resp.statusText || 'request failed'}`;
}

function _mtimeOf(resp) {
    const v = parseFloat(resp.headers.get('X-File-Mtime') || '');
    return Number.isFinite(v) ? v : null;
}

// Inline result line for a 409, same shape as the MCP add form's: say what
// happened, and offer the only two honest choices. Never a confirm() — the
// user needs to see which file this is about. (S3)
function _showSaveConflict(container, { onReload, onOverwrite }) {
    container.querySelector('.fp-conflict')?.remove();
    const reloadBtn = el('button', { class: 'fp-btn' }, [text('Reload')]);
    reloadBtn.addEventListener('click', () => { box.remove(); onReload(); });
    const overwriteBtn = el('button', { class: 'fp-btn fp-btn-danger' }, [text('Overwrite')]);
    overwriteBtn.addEventListener('click', () => { box.remove(); onOverwrite(); });
    const box = el('div', { class: 'fp-conflict', role: 'alert' }, [
        el('span', { class: 'fp-conflict-msg' }, [text('Changed on disk since you opened it')]),
        el('span', { class: 'fp-conflict-actions' }, [reloadBtn, overwriteBtn]),
    ]);
    const toolbar = container.querySelector('.fp-toolbar');
    if (toolbar) toolbar.after(box);
    else container.prepend(box);
    return box;
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
        if (!guardDirty()) return;
        disposeActiveEditor();
        _state.viewMode = 'viewer';
        _state.dirty = false;
        renderCurrentTab();
    });

    const saveBtn = el('button', { class: 'fp-btn save-btn', disabled: true }, [text('save')]);
    saveBtn.addEventListener('click', () => { if (_state.dirty) saveFile(container); });

    const cancelBtn = el('button', { class: 'fp-btn' }, [text('cancel')]);
    cancelBtn.addEventListener('click', () => {
        if (!guardDirty()) return;
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

// Pull the on-disk text back into the open editor, discarding local edits.
// The Reload half of a conflict — the user chose the other writer's version.
async function _reloadWorkspaceFile(container) {
    const file = _state.currentFile;
    if (!file) return;
    const statusEl = container.querySelector('.fp-editor-status');
    try {
        const resp = await fetch(`/workspace/${file.path}`, { headers: _authHdr() });
        if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
        file.content = await resp.text();
        file.mtime = _mtimeOf(resp);
        _state.originalContent = file.content;
        _state.dirty = false;
        renderCurrentTab();
    } catch (e) {
        if (statusEl) {
            statusEl.className = 'fp-editor-status error';
            statusEl.textContent = `Reload failed: ${e.message}`;
        }
        notify('error', `Could not reload ${file.path}: ${e.message}`);
    }
}

async function saveFile(container, { force = false } = {}) {
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
        const body = { content };
        // Overwrite = resend without base_mtime, which is the server's own
        // opt-out and therefore literally last-writer-wins again.
        if (!force && file.mtime != null) body.base_mtime = file.mtime;
        const resp = await fetch(url, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', ..._authHdr() },
            body: JSON.stringify(body),
        });
        if (resp.status === 409) {
            let payload = {};
            try { payload = await resp.json(); } catch { /* body is optional */ }
            if (payload.mtime != null) file.mtime = payload.mtime;
            if (saveBtn) { saveBtn.disabled = false; saveBtn.className = 'fp-btn save-btn dirty'; saveBtn.textContent = 'save'; }
            if (statusEl) {
                statusEl.className = 'fp-editor-status error';
                statusEl.textContent = 'Changed on disk';
            }
            _showSaveConflict(container, {
                onReload: () => _reloadWorkspaceFile(container),
                onOverwrite: () => saveFile(container, { force: true }),
            });
            return;
        }
        if (!resp.ok) throw new Error(`Save failed: ${resp.statusText}`);
        const saved = await resp.json().catch(() => ({}));
        if (saved.mtime != null) file.mtime = saved.mtime;

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
    if (!confirm(`Delete ${label} \u2014 this cannot be undone.`)) return;
    try {
        await del(`/workspace/${path.split('/').map(encodeURIComponent).join('/')}`);
        if (_state.currentFile?.path === path ||
            _state.currentFile?.path?.startsWith(path + '/')) {
            _state.currentFile = null;
            _state.viewMode = 'tree';
        }
        await loadWorkspace({ path: _wsCurrentPath });
    } catch (e) {
        notify('error', `Could not delete ${path}: ${e.message || e}`);
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
            const entry = { name: file.name, state: 'uploading', detail: '' };
            _wsUploads.push(entry);
            renderWorkspace();

            let detail = null;
            try {
                const formData = new FormData();
                formData.append('file', file);
                // Uploads always landed at the workspace root, wherever you
                // were standing — so a file dropped into a folder simply was
                // not there afterwards. (S12)
                if (_wsCurrentPath) formData.append('path', _wsCurrentPath);
                const resp = await fetch('/api/upload', {
                    method: 'POST', body: formData, headers: _authHdr(),
                });
                // A 2xx was assumed here, so a rejected upload (too large,
                // blocked extension, collision) looked exactly like a success
                // that had not appeared in the listing yet.
                if (!resp.ok) detail = await _errorDetail(resp);
            } catch (e) {
                detail = e.message || String(e);
            }

            if (detail) {
                // One rejection almost always applies to the rest of the batch
                // (same size cap, same extension rule), so stop instead of
                // firing the identical error once per file.
                entry.state = 'error';
                entry.detail = detail;
                notify('error', `Upload failed — ${file.name}: ${detail}`);
                break;
            }
            _wsUploads = _wsUploads.filter(u => u !== entry);
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
                // Space badge (v33): pernix.space.<slug>.* files carry their
                // space's label + color from /api/memory/files.
                f.space ? el('span', {
                    class: 'space-chip-labeled',
                    style: `--space-color: ${f.space_color || '#888'}`,
                    title: `Space memory bucket (${f.space})`,
                }, [text(f.space_label || f.space)]) : null,
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
        notify('error', `Could not open memory file ${name}: ${e.message || e}`);
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
            get('/api/skills/proposals?status=pending&limit=20').catch(() => ({ proposals: [] })),
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
            // Historical rows may still carry source_origin='workflow' from
            // before the workflow engine was removed; they render by their
            // stored origin name rather than being relabelled.
            const origin = proposal.source_origin || 'session';
            const originLabel = origin === 'session'
                ? `SESSION \u00b7 ${(proposal.session_id || '').slice(0, 8)}`
                : `${origin.toUpperCase()} \u00b7 ${(proposal.session_id || '').slice(0, 8) || '?'}`;
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
                        mtime: skillData.mtime ?? null,
                        skillData,
                        pendingProposal: proposal,
                    };
                    _state.viewMode = 'viewer';
                    renderSkills();
                } catch (e) {
                    // Inline on the button — the skill may have been deleted
                    // since the proposal was written — plus the reason, which
                    // "skill not found" alone cannot carry.
                    reviewBtn.textContent = 'skill not found';
                    reviewBtn.disabled = true;
                    notify('error', `Could not open ${proposal.skill_name}: ${e.message || e}`);
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
        if (skill.valid === false) {
            const issues = (skill.validation_issues || []).join('\n');
            nameChildren.push(el('span', {
                class: 'fp-skill-pending-badge',
                title: issues || 'Skill failed validation',
            }, [text('✗ broken')]));
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
            mtime: data.mtime ?? null,
            skillData: data,
        };
        _state.viewMode = 'viewer';
        renderSkills();
    } catch (e) {
        notify('error', `Could not open skill ${name}: ${e.message || e}`);
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
                await post(`/api/skills/proposals/${proposal.id}/approve`, {});
            } catch (e) { /* non-fatal */ }
            _state.viewMode = 'editor';
            _state.originalContent = file.content;
            _state.dirty = false;
            renderSkills();
        });

        const applyBtn = el('button', { class: 'fp-btn fp-btn-primary' }, [text('apply')]);
        applyBtn.title = 'Insert the suggested change into this skill\'s SKILL.md under the referenced section. Re-run the skill to validate the fix.';
        applyBtn.addEventListener('click', async () => {
            if (!confirm(`Apply this proposal to ${proposal.skill_name}'s SKILL.md?\n\nSection: ${proposal.section || '(new section)'}\n\nNothing re-runs automatically — invoke the skill again to validate the fix.`)) {
                return;
            }
            try {
                const res = await post(`/api/skills/proposals/${proposal.id}/apply`, {});
                _pendingProposals = _pendingProposals.filter(p => p.id !== proposal.id);
                file.pendingProposal = null;
                const delta = (res.bytes_after || 0) - (res.bytes_before || 0);
                console.log(`[skills] proposal applied: +${delta} bytes into ${res.skill_md_path}`);
                // Reload file data so the editor shows the updated skill body
                await loadSkills();
                renderSkills();
            } catch (e) {
                notify('error', `Could not apply the proposal: ${e.message || e}`);
            }
        });

        const rejectBtn = el('button', { class: 'fp-btn fp-btn-danger' }, [text('reject')]);
        rejectBtn.addEventListener('click', async () => {
            try {
                await post(`/api/skills/proposals/${proposal.id}/reject`, {});
                _pendingProposals = _pendingProposals.filter(p => p.id !== proposal.id);
                file.pendingProposal = null;
                renderSkills();
            } catch (e) {
                notify('error', `Could not reject the proposal: ${e.message || e}`);
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
        if (!guardDirty()) return;
        disposeActiveEditor();
        _state.viewMode = 'viewer';
        _state.dirty = false;
        renderSkills();
    });

    const saveBtn = el('button', { class: 'fp-btn save-btn', disabled: true }, [text('save')]);
    saveBtn.addEventListener('click', () => { if (_state.dirty) saveSkill(container); });

    const cancelBtn = el('button', { class: 'fp-btn' }, [text('cancel')]);
    cancelBtn.addEventListener('click', () => {
        if (!guardDirty()) return;
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

// Reload half of a SKILL.md conflict — same contract as the workspace one.
async function _reloadSkillFile(container) {
    const file = _state.currentFile;
    if (!file) return;
    const statusEl = container.querySelector('.fp-editor-status');
    try {
        const data = await get(`/api/skills/${encodeURIComponent(file.path)}`);
        file.content = data.raw_content || '';
        file.mtime = data.mtime ?? null;
        file.skillData = data;
        _state.originalContent = file.content;
        _state.dirty = false;
        renderSkills();
    } catch (e) {
        if (statusEl) {
            statusEl.className = 'fp-editor-status error';
            statusEl.textContent = `Reload failed: ${e.message}`;
        }
        notify('error', `Could not reload ${file.path}/SKILL.md: ${e.message}`);
    }
}

async function saveSkill(container, { force = false } = {}) {
    const file = _state.currentFile;
    if (!file || !_activeEditor) return;

    const statusEl = container.querySelector('.fp-editor-status');
    const saveBtn = container.querySelector('.save-btn');
    const pathLabel = container.querySelector('.fp-toolbar-path');
    const content = _activeEditor.getValue();

    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'saving\u2026'; }
    if (statusEl) { statusEl.className = 'fp-editor-status'; statusEl.textContent = 'Saving\u2026'; }

    try {
        const body = { content };
        if (!force && file.mtime != null) body.base_mtime = file.mtime;
        const resp = await fetch(`/api/skills/${encodeURIComponent(file.path)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', ..._authHdr() },
            body: JSON.stringify(body),
        });
        if (resp.status === 409) {
            let payload = {};
            try { payload = await resp.json(); } catch { /* body is optional */ }
            if (payload.mtime != null) file.mtime = payload.mtime;
            if (saveBtn) { saveBtn.disabled = false; saveBtn.className = 'fp-btn save-btn dirty'; saveBtn.textContent = 'save'; }
            if (statusEl) {
                statusEl.className = 'fp-editor-status error';
                statusEl.textContent = 'Changed on disk';
            }
            _showSaveConflict(container, {
                onReload: () => _reloadSkillFile(container),
                onOverwrite: () => saveSkill(container, { force: true }),
            });
            return;
        }
        if (!resp.ok) throw new Error(`Save failed: ${resp.statusText}`);
        const saved = await resp.json().catch(() => ({}));
        if (saved.mtime != null) file.mtime = saved.mtime;

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
        const resp = await fetch(`/api/skills/${encodeURIComponent(name)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', ..._authHdr() },
            body: JSON.stringify({ enabled }),
        });
        // fetch() only rejects on a network error, so a 404/500 used to leave
        // the toggle looking flipped after a reload that quietly undid it.
        if (!resp.ok) throw new Error(await _errorDetail(resp));
        await loadSkills();
    } catch (e) {
        notify('error', `Could not ${enabled ? 'enable' : 'disable'} ${name}: ${e.message || e}`);
        await loadSkills();
    }
}

async function deleteSkill(name) {
    if (!confirm(`Delete the skill "${name}" and all of its files \u2014 this cannot be undone.`)) return;
    try {
        await del(`/api/skills/${encodeURIComponent(name)}`);
        if (_state.currentFile?.path === name) {
            _state.currentFile = null;
            _state.viewMode = 'tree';
        }
        await loadSkills();
    } catch (e) {
        notify('error', `Could not delete skill ${name}: ${e.message || e}`);
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
        el('option', { value: 'uses',     ...(_toolsSortBy === 'uses'     ? { selected: '' } : {}) }, [text('Uses')]),
        el('option', { value: 'failures', ...(_toolsSortBy === 'failures' ? { selected: '' } : {}) }, [text('Failures')]),
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
        if (_toolsSortBy === 'uses' || _toolsSortBy === 'failures') {
            // Descending — the most-used / most-failing tools are the ones
            // worth looking at; never-observed tools sink to the bottom.
            const key = _toolsSortBy === 'uses' ? 'uses' : 'failures';
            const d = (b.performance?.[key] || 0) - (a.performance?.[key] || 0);
            return d !== 0 ? d : a.name.localeCompare(b.name);
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
                const resp = await fetch('/api/tools/toggle', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', ..._authHdr() },
                    body: JSON.stringify({ name: tool.name, enabled: !tool.enabled }),
                });
                if (!resp.ok) throw new Error(await _errorDetail(resp));
                await loadTools();
            } catch (err) {
                notify('error', `Could not ${tool.enabled ? 'disable' : 'enable'} ${tool.name}: ${err.message || err}`);
                await loadTools();
            }
        });

        // Safety level select
        const level = tool.safety_level || 'safe';
        // The value to fall back to when a change is rejected — the last one
        // the server actually acknowledged, not the one this row rendered with.
        let acceptedLevel = level;
        const safetySelect = el('select', {
            class: `fp-tool-safety sl-${level}`,
            title: 'Safety level — controls whether auto_approve_dangerous gate applies',
        });
        for (const lvl of ['safe', 'caution', 'dangerous']) {
            const opt = el('option', { value: lvl }, [text(lvl)]);
            if (lvl === level) opt.selected = true;
            safetySelect.appendChild(opt);
        }
        // A rejected change used to repaint the colour but leave the select
        // showing the level the user picked — the UI claiming a safety level
        // the server never took. Put the VALUE back too. (F1/S8)
        const revertSafety = (why) => {
            safetySelect.value = acceptedLevel;
            safetySelect.className = `fp-tool-safety sl-${acceptedLevel}`;
            notify('error', `Safety level for ${tool.name} not changed: ${why}`);
        };
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
                if (!res.ok) { revertSafety(await _errorDetail(res)); return; }
                const data = await res.json().catch(() => ({}));
                if (data.error) { revertSafety(String(data.error)); return; }
                acceptedLevel = newLevel;
            } catch (err) {
                revertSafety(err.message || String(err));
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
// MCP tab — external tool servers (Model Context Protocol)
// ---------------------------------------------------------------------------

let _mcpData = null;
let _mcpShowAdd = false;
let _mcpBusy = false;
let _mcpDraft = '';          // form text survives re-renders (status refreshes)
let _mcpEditTarget = null;   // server name when the form is editing, null = adding
let _mcpFormJustOpened = false;
let _mcpRefreshTimer = null;

async function loadMcp() {
    try {
        _mcpData = await get('/api/mcp/servers');
    } catch {
        _mcpData = null;
    }
    renderMcp();
}

const _MCP_STATUS_META = {
    ready:      { cls: 'ok',   label: 'connected' },
    connecting: { cls: 'wait', label: 'connecting…' },
    idle:       { cls: 'wait', label: 'idle (suspended)' },
    degraded:   { cls: 'err',  label: 'unreachable' },
    disabled:   { cls: 'off',  label: 'disabled' },
    stopped:    { cls: 'off',  label: 'stopped' },
    offline:    { cls: 'off',  label: 'offline (MCP disabled)' },
};

function renderMcp() {
    const container = document.getElementById('fp-mcp');
    if (!container) return;
    clear(container);

    const refreshBtn = el('button', { class: 'fp-icon-btn', title: 'Refresh' }, [text('↻')]);
    refreshBtn.addEventListener('click', loadMcp);
    const addBtn = el('button', { class: 'fp-icon-btn', title: 'Add server' }, [text('+')]);
    addBtn.addEventListener('click', () => {
        if (_mcpShowAdd && _mcpEditTarget === null) {
            _mcpShowAdd = false;                    // toggle a plain add form closed
        } else {
            _mcpShowAdd = true;                     // (re)open as a fresh add
            if (_mcpEditTarget !== null) _mcpDraft = '';
            _mcpEditTarget = null;
            _mcpFormJustOpened = true;
        }
        renderMcp();
    });

    const servers = _mcpData?.servers || [];
    // A connecting server settles within seconds — refresh once on its own
    // instead of making the user hammer the refresh button.
    if (_mcpRefreshTimer) { clearTimeout(_mcpRefreshTimer); _mcpRefreshTimer = null; }
    if (servers.some(s => s.status === 'connecting') && _state.tab === 'mcp') {
        _mcpRefreshTimer = setTimeout(() => { _mcpRefreshTimer = null; loadMcp(); }, 3000);
    }
    const connected = servers.filter(s => s.status === 'ready').length;
    container.appendChild(el('div', { class: 'fp-section-header' }, [
        el('div', {}, [
            el('span', { class: 'fp-section-label' }, [text('MCP Servers')]),
            el('div', { class: 'fp-section-sub' }, [text(
                _mcpData ? `${servers.length} configured · ${connected} connected` : 'unavailable',
            )]),
        ]),
        el('div', { class: 'fp-section-actions' }, [addBtn, refreshBtn]),
    ]));
    container.appendChild(_buildTabDesc(
        'External tool servers speaking the Model Context Protocol.',
        'Each connected server\'s tools register as mcp_<server>_<tool> and show up in the Tools ' +
        'tab with the normal enable/safety controls. Paste a standard mcpServers config from ' +
        'Claude Code, Cursor, or VS Code — it works verbatim. Secrets belong in .env, referenced ' +
        'as "${VAR}", never pasted into the config.',
    ));

    if (_mcpData && _mcpData.mcp_enabled === false) {
        const banner = el('div', { class: 'fp-danger-mode-banner' }, [
            el('div', { class: 'fp-danger-banner-inner' }, [
                el('span', { class: 'fp-danger-banner-icon' }, [text('◌')]),
                el('span', { class: 'fp-danger-banner-text' }, [
                    text('MCP is disabled — servers will not connect. Click to open Settings.'),
                ]),
            ]),
        ]);
        banner.addEventListener('click', () => openSettings());
        container.appendChild(banner);
    }

    if (_mcpShowAdd) container.appendChild(_buildMcpAddForm());

    if (!_mcpData) {
        container.appendChild(el('div', { class: 'fp-empty' }, [text('Could not load MCP status')]));
        return;
    }
    if (servers.length === 0) {
        container.appendChild(el('div', { class: 'fp-empty' }, [
            text('No MCP servers configured. Click + to add one, or ask the agent to run mcp_add_server.'),
        ]));
        return;
    }

    const listEl = el('div', { class: 'fp-skills-list' });
    for (const s of servers) listEl.appendChild(_buildMcpServerItem(s));
    container.appendChild(listEl);
}

function _buildMcpServerItem(s) {
    const meta = _MCP_STATUS_META[s.status] || { cls: 'off', label: s.status };
    const item = el('div', { class: `fp-skill-item${s.enabled ? '' : ' disabled'}` });

    const toggle = el('button', {
        class: `fp-skill-toggle${s.enabled ? ' on' : ''}`,
        title: s.enabled ? 'Disable server (unregisters its tools)' : 'Enable server',
    }, [text(s.enabled ? 'on' : 'off')]);
    toggle.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (_mcpBusy) return;
        _mcpBusy = true;
        try { await post(`/api/mcp/servers/${encodeURIComponent(s.name)}/toggle`, { enabled: !s.enabled }); }
        catch (err) { notify('error', `Could not ${s.enabled ? 'disable' : 'enable'} ${s.name}: ${err.message || err}`); }
        _mcpBusy = false;
        await loadMcp();
    });

    const editBtn = el('button', { class: 'fp-icon-btn', title: 'Edit server config' }, [text('✎')]);
    editBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const cfg = _mcpData?.configs?.[s.name];
        if (!cfg) return;
        _mcpEditTarget = s.name;
        _mcpDraft = JSON.stringify({ mcpServers: { [s.name]: cfg } }, null, 2);
        _mcpShowAdd = true;
        _mcpFormJustOpened = true;
        renderMcp();
    });

    const reloadBtn = el('button', { class: 'fp-icon-btn', title: 'Reconnect + re-discover tools' }, [text('↻')]);
    reloadBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (_mcpBusy) return;
        _mcpBusy = true;
        reloadBtn.textContent = '…';
        try { await post(`/api/mcp/servers/${encodeURIComponent(s.name)}/reload`, {}); }
        catch (err) { notify('error', `Could not reconnect ${s.name}: ${err.message || err}`); }
        _mcpBusy = false;
        await loadMcp();
    });

    const delBtn = el('button', { class: 'fp-icon-btn', title: 'Remove server' }, [text('×')]);
    delBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!confirm(`Remove the MCP server "${s.name}" and unregister its tools \u2014 this cannot be undone.`)) return;
        try { await del(`/api/mcp/servers/${encodeURIComponent(s.name)}`); }
        catch (err) { notify('error', `Could not remove ${s.name}: ${err.message || err}`); }
        await loadMcp();
    });

    item.appendChild(el('div', { class: 'fp-skill-header' }, [
        el('div', { class: 'fp-skill-name' }, [
            el('span', { class: `fp-mcp-dot ${meta.cls}`, title: meta.label }),
            text(s.name),
            el('span', { class: 'fp-skill-version' }, [text(s.transport)]),
            el('span', { class: 'fp-skill-version' }, [text(`safety: ${s.safety}`)]),
        ]),
        el('div', { class: 'fp-skill-actions' }, [editBtn, reloadBtn, delBtn, toggle]),
    ]));

    const statusBits = [meta.label];
    if (s.server_info) statusBits.push(s.server_info);
    if (s.tool_count) statusBits.push(`${s.tool_count} tools`);
    if (s.last_used) statusBits.push(`used ${timeAgo(s.last_used)}`);
    item.appendChild(el('div', { class: 'fp-skill-desc' }, [
        text(`${statusBits.join(' · ')}${s.target ? ` — ${s.target}` : ''}`),
    ]));
    if (s.error && (s.status === 'degraded' || s.status === 'stopped')) {
        item.appendChild(el('div', { class: 'fp-mcp-error' }, [text(s.error)]));
    }
    if (s.tools && s.tools.length) {
        const tagsEl = el('div', { class: 'fp-skill-tags' });
        for (const t of s.tools.slice(0, 8)) tagsEl.appendChild(el('span', { class: 'fp-skill-tag' }, [text(t)]));
        if (s.tools.length > 8) tagsEl.appendChild(el('span', { class: 'fp-skill-tag' }, [text(`+${s.tools.length - 8} more`)]));
        item.appendChild(tagsEl);
    }
    return item;
}

function _normalizeMcpPaste(raw) {
    // Accept a full {"mcpServers": {...}} blob or a bare {name: entry} map.
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && parsed.mcpServers) return { mcpServers: parsed.mcpServers };
    if (parsed && typeof parsed === 'object') {
        const values = Object.values(parsed);
        if (values.length && values.every(v => v && typeof v === 'object' && (v.command || v.url))) {
            return { mcpServers: parsed };
        }
    }
    throw new Error('Expected {"mcpServers": {...}} or {"<name>": {"command"|"url": ...}}');
}

function _buildMcpAddForm() {
    const editing = _mcpEditTarget !== null;
    const ta = el('textarea', {
        class: 'fp-mcp-add-input',
        rows: String(Math.min(16, Math.max(8, _mcpDraft.split('\n').length + 1))),
        placeholder: '{\n  "mcpServers": {\n    "github": {\n      "url": "https://api.githubcopilot.com/mcp/",\n      "headers": { "Authorization": "Bearer ${GITHUB_MCP_TOKEN}" }\n    }\n  }\n}',
        spellcheck: 'false',
    });
    ta.value = _mcpDraft;
    ta.addEventListener('input', () => { _mcpDraft = ta.value; });

    const result = el('div', { class: 'fp-mcp-add-result' });
    const setResult = (msg, isErr) => {
        result.textContent = msg;
        result.classList.toggle('err', !!isErr);
    };

    const buttons = [];
    const setBusy = (busy, activeBtn, busyLabel) => {
        for (const b of buttons) b.disabled = busy;
        if (activeBtn) activeBtn.textContent = busy ? busyLabel : activeBtn.dataset.label;
    };

    const testBtn = el('button', { class: 'fp-mcp-add-btn', 'data-label': 'Test' }, [text('Test')]);
    testBtn.addEventListener('click', async () => {
        let body;
        try { body = _normalizeMcpPaste(ta.value); } catch (e) { setResult(e.message, true); return; }
        setBusy(true, testBtn, 'Testing…');
        setResult('Connecting to the server without saving…');
        try {
            const res = await post('/api/mcp/test', body);
            setResult(res.ok
                ? `OK — ${res.server_info || 'server'} exposes ${res.tools.length} tool(s): ${res.tools.slice(0, 10).join(', ')}${res.tools.length > 10 ? '…' : ''}`
                : `Failed: ${res.error}`, !res.ok);
        } catch (e) { setResult(`Test failed: ${e.message || e}`, true); }
        setBusy(false, testBtn);
    });

    const saveBtn = el('button', {
        class: 'fp-mcp-add-btn primary',
        'data-label': editing ? 'Save changes' : 'Save & Connect',
    }, [text(editing ? 'Save changes' : 'Save & Connect')]);
    saveBtn.addEventListener('click', async () => {
        let body;
        try { body = _normalizeMcpPaste(ta.value); } catch (e) { setResult(e.message, true); return; }
        if (editing && !(_mcpEditTarget in (body.mcpServers || {}))) {
            setResult(`This form is editing '${_mcpEditTarget}' but the config names ${Object.keys(body.mcpServers).join(', ')} — renaming creates a new server; remove the old one after.`, true);
        }
        setBusy(true, saveBtn, 'Connecting…');
        try {
            const res = await post('/api/mcp/servers', body);
            const lines = Object.entries(res.results || {}).map(([name, r]) =>
                `${name}: ${r.ok ? r.status : (r.error || r.status)}`);
            const allOk = Object.values(res.results || {}).every(r => r.ok);
            setResult(lines.join(' · ') || 'saved', !allOk);
            if (allOk) {
                _mcpShowAdd = false; _mcpDraft = ''; _mcpEditTarget = null;
                await loadMcp();
                return;
            }
        } catch (e) { setResult(`Save failed: ${e.message || e}`, true); }
        // On failure the form stays open with the message — no re-render,
        // which would wipe both the result line and the user's edits' focus.
        setBusy(false, saveBtn);
    });

    const cancelBtn = el('button', { class: 'fp-mcp-add-btn', 'data-label': 'Cancel' }, [text('Cancel')]);
    cancelBtn.addEventListener('click', () => {
        _mcpShowAdd = false; _mcpDraft = ''; _mcpEditTarget = null;
        renderMcp();
    });
    buttons.push(testBtn, saveBtn, cancelBtn);

    const form = el('div', { class: 'fp-mcp-add' }, [
        el('div', { class: 'fp-mcp-add-title' }, [
            text(editing ? `Edit server: ${_mcpEditTarget}` : 'Add MCP server'),
        ]),
        el('div', { class: 'fp-mcp-add-hint' }, [
            text(editing
                ? 'Adjust the entry and Save — the server reconnects with the new config. Secrets stay in .env as "${VAR}".'
                : 'Paste a standard mcpServers config (Claude Code / Cursor format works verbatim). Secrets go in .env, referenced as "${VAR}".'),
        ]),
        ta,
        el('div', { class: 'fp-mcp-add-actions' }, [testBtn, saveBtn, cancelBtn]),
        result,
    ]);
    if (_mcpFormJustOpened) {
        _mcpFormJustOpened = false;
        requestAnimationFrame(() => { ta.focus(); form.scrollIntoView({ block: 'nearest' }); });
    }
    return form;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderCurrentTab() {
    if (_state.tab === 'workspace') renderWorkspace();
    else if (_state.tab === 'memory') renderMemory();
    else if (_state.tab === 'skills') renderSkills();
    else if (_state.tab === 'tools') renderTools();
    else if (_state.tab === 'mcp') renderMcp();
    else if (_state.tab === 'jobs') renderJobs();
}

function sessionTypeDot(sessionType) {
    const typeMap = {
        normal: 'chat',
        worker: 'worker',
        cron: 'cron',
        rlm: 'rlm',
        snooze: 'snooze',
        canary: 'canary',
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
