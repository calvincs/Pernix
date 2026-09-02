// Pernix — the icon set. (V8 / N10)
//
// Before this file the app drew its icons with text: ☰ ◎ ⎘ ⤓ 📁 ⚙ ❚❚ ↓ in the
// status bar, ✎ ⚲ ▣ ↻ ⧉ ◐ ◌ ▼ ▶ ⚠ ← ↑ ↓ scattered through the panels. Three
// problems, all of them real on a user's machine rather than in theory:
//
//   1. A glyph is only as good as the font that has it. DM Mono does not
//      carry ⚲, ▣, ⤓ or ⎘, so those fell through to whatever the OS
//      substituted — a different weight, a different baseline, a different
//      size, and on some Linux boxes a .notdef box.
//   2. 📁 is an EMOJI. It rendered in full colour, at emoji size, in a
//      monochrome gold-on-black status bar.
//   3. None of them scaled with the control, aligned on a common baseline,
//      or took the text colour reliably.
//
// One function, one grid, one stroke weight. Every icon is a 24-unit
// viewBox, `stroke="currentColor"`, `fill="none"`, stroke-width 1.75, round
// caps and joins — so an icon inherits its colour from the button it sits in
// and matches every other icon beside it.
//
// Decorative by default: `aria-hidden="true"`, because the button around it
// already carries the accessible name. Pass `label` only when the SVG is the
// ONLY thing naming the control, and it becomes role="img" + aria-label.
//
// index.html's eight status-bar buttons and three composer buttons carry the
// same markup inline and statically, on purpose: they are on screen before
// any module has run, and painting them from JS would flash an empty status
// bar on every load. If you change one of those shapes here, change it there
// too — the names are menu, arrow-down, pause, bell, copy, download, folder,
// settings, attach, mic, send.

const NS = 'http://www.w3.org/2000/svg';

// name -> array of child specs. A string is a <path d="…">; an object is any
// other SVG element, {t: tag, ...attributes}.
const PATHS = {
    menu: ['M4 7h16', 'M4 12h16', 'M4 17h16'],

    bell: [
        'M18 8.5a6 6 0 1 0-12 0c0 6-2.5 7.5-2.5 7.5h17S18 14.5 18 8.5z',
        'M10.3 19.5a2 2 0 0 0 3.4 0',
    ],

    copy: [
        { t: 'rect', x: 9, y: 9, width: 11, height: 11, rx: 2 },
        'M6 15H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1',
    ],

    download: ['M12 3.5v11.5', 'M7.5 11l4.5 4.5 4.5-4.5', 'M4.5 20.5h15'],

    home: [
        'M3.5 10.5L12 3.5l8.5 7',
        'M6 9.4V19a1.5 1.5 0 0 0 1.5 1.5h9A1.5 1.5 0 0 0 18 19V9.4',
    ],

    folder: [
        'M3.5 6.5a2 2 0 0 1 2-2h3.6l2 2.4h7.4a2 2 0 0 1 2 2v9.6a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z',
    ],

    settings: [
        { t: 'circle', cx: 12, cy: 12, r: 3 },
        'M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33'
        + ' 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06'
        + 'a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09'
        + 'A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9'
        + 'a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06'
        + 'a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09'
        + 'a1.65 1.65 0 0 0-1.51 1z',
    ],

    pin: ['M12 15.5V21', 'M8 3.5h8l-1 6.5 2.5 3v2h-11v-2l2.5-3-1-6.5z'],
    'pin-filled': [
        'M12 15.5V21',
        { t: 'path', d: 'M8 3.5h8l-1 6.5 2.5 3v2h-11v-2l2.5-3-1-6.5z', fill: 'currentColor' },
    ],

    edit: ['M4.5 19.5h4L19 9a2.12 2.12 0 0 0-3-3L5.5 16.5v3z', 'M14.5 7.5l3 3'],

    trash: [
        'M4 6.5h16',
        'M9.5 6.5v-2a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v2',
        'M6.5 6.5l.8 12.2a2 2 0 0 0 2 1.8h5.4a2 2 0 0 0 2-1.8l.8-12.2',
        'M10.5 10.5v6',
        'M13.5 10.5v6',
    ],

    move: [
        'M12 3.5v17', 'M3.5 12h17',
        'M9 6.5L12 3.5l3 3', 'M9 17.5l3 3 3-3',
        'M6.5 9l-3 3 3 3', 'M17.5 9l3 3-3 3',
    ],

    refresh: ['M20 12a8 8 0 1 1-2.4-5.7', 'M20.5 4v5h-5'],

    // A box with its lid on: archiving files a session away rather than
    // ending it. The pair differ only in what is inside the box — nothing
    // (a line, the label on the lid) or an arrow coming back out — so the
    // two states of one control read as one control.
    archive: [
        { t: 'rect', x: 3.5, y: 4.5, width: 17, height: 4, rx: 1.2 },
        'M5.5 8.5v10a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5v-10',
        'M10 12.5h4',
    ],
    unarchive: [
        { t: 'rect', x: 3.5, y: 4.5, width: 17, height: 4, rx: 1.2 },
        'M5.5 8.5v10a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5v-10',
        'M12 18v-6',
        'M9.5 14.5L12 12l2.5 2.5',
    ],

    pause: ['M9.5 5v14', 'M14.5 5v14'],
    play: ['M7.5 4.8L19 12 7.5 19.2z'],
    // Filled, because a hollow square next to a filled send arrow reads as
    // "disabled" rather than "stop".
    stop: [{ t: 'rect', x: 6.5, y: 6.5, width: 11, height: 11, rx: 2,
             fill: 'currentColor', stroke: 'none' }],

    check: ['M4.5 12.5l5 5 10-11'],
    x: ['M6 6l12 12', 'M18 6L6 18'],

    'chevron-down': ['M6.5 9.5L12 15l5.5-5.5'],
    'chevron-right': ['M9.5 6.5L15 12l-5.5 5.5'],

    search: [{ t: 'circle', cx: 11, cy: 11, r: 6.5 }, 'M16 16l4.5 4.5'],

    plus: ['M12 5v14', 'M5 12h14'],

    'arrow-down': ['M12 4v15.5', 'M5.5 13L12 19.5 18.5 13'],
    'arrow-up': ['M12 20V4.5', 'M5.5 11L12 4.5 18.5 11'],
    'arrow-left': ['M20 12H4.5', 'M11 5.5L4.5 12 11 18.5'],

    clock: [{ t: 'circle', cx: 12, cy: 12, r: 8.5 }, 'M12 6.8V12l3.6 2.2'],

    external: [
        'M14 4h6v6', 'M20 4l-8.5 8.5',
        'M18.5 14.5V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7.5a2 2 0 0 1 2-2h4.5',
    ],

    // The overflow "⋯". Dots are the one shape a stroke cannot draw.
    more: [
        { t: 'circle', cx: 5, cy: 12, r: 1.4, fill: 'currentColor', stroke: 'none' },
        { t: 'circle', cx: 12, cy: 12, r: 1.4, fill: 'currentColor', stroke: 'none' },
        { t: 'circle', cx: 19, cy: 12, r: 1.4, fill: 'currentColor', stroke: 'none' },
    ],

    warning: ['M12 3.8l9.2 16.4H2.8z', 'M12 10v4', 'M12 17.2h.01'],

    // "This subsystem is switched off" — the MCP-disabled banner.
    ban: [{ t: 'circle', cx: 12, cy: 12, r: 8.5 }, 'M6 6l12 12'],

    // Snooze / idle-time maintenance.
    moon: ['M20.5 14.6A8.6 8.6 0 0 1 9.4 3.5a8.6 8.6 0 1 0 11.1 11.1z'],

    // --- the three the composer already had, moved here verbatim ---
    attach: [
        'M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19'
        + 'a2 2 0 0 1-2.83-2.83l8.49-8.48',
    ],
    mic: [
        'M12 1.5a3 3 0 0 0-3 3v7.5a3 3 0 0 0 6 0V4.5a3 3 0 0 0-3-3z',
        'M19 10.5V12a7 7 0 0 1-14 0v-1.5',
        'M12 19v3.5',
        'M8.5 22.5h7',
    ],
    send: ['M22 2L11 13', 'M22 2l-7 20-4-9-9-4z'],
};

export const ICON_NAMES = Object.keys(PATHS);

/**
 * Build one icon.
 *
 * @param {string} name  one of ICON_NAMES.
 * @param {object} [opts]
 * @param {string} [opts.label]  accessible name. Omit for the normal case —
 *        an icon inside a button that already has an aria-label — and the
 *        SVG is hidden from assistive tech instead of read out twice.
 * @param {number} [opts.size=16]  rendered edge, in px. The geometry is a
 *        24-unit grid regardless.
 * @returns {SVGElement}
 */
export function icon(name, { label, size = 16 } = {}) {
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', String(size));
    svg.setAttribute('height', String(size));
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.75');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('class', `pxi pxi-${name}`);

    if (label) {
        svg.setAttribute('role', 'img');
        svg.setAttribute('aria-label', label);
    } else {
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('focusable', 'false');
    }

    const spec = PATHS[name];
    if (!spec) {
        // A typo must not take the surface down with it — an empty box is a
        // visible bug, a thrown TypeError is a blank panel.
        console.error(`icon(): no such icon "${name}"`);
        return svg;
    }

    for (const child of spec) {
        if (typeof child === 'string') {
            const path = document.createElementNS(NS, 'path');
            path.setAttribute('d', child);
            svg.appendChild(path);
            continue;
        }
        const { t, ...attrs } = child;
        const node = document.createElementNS(NS, t);
        for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
        svg.appendChild(node);
    }
    return svg;
}
