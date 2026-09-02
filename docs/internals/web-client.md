# The web client on phones and tablets

The UI in `static/` is one vanilla-JS PWA, not a desktop build plus a mobile
build. What changes between a phone, an iPad and a 27" monitor is decided by
**two independent questions**, each with its own stylesheet and its own
predicate in JavaScript.

Getting those two questions confused is the bug this layer exists to prevent:
they used to be one question called "mobile", and a 1180px iPad in landscape —
a big screen with a finger on it — got the phone layout.

---

## The two questions

| | Question | CSS | JS |
|---|---|---|---|
| **Compact** | Is the viewport narrower than 900px? | `static/css/compact.css` | `isCompact()` |
| **Touch** | Is the pointer a finger? | `static/css/touch.css` | `isTouch()` |

They are orthogonal, which gives three tiers that actually ship:

| Tier | Example | compact | touch |
|---|---|---|---|
| **Compact** | phone (any orientation), narrow desktop window | yes | phone yes, window no |
| **Wide touch** | iPad landscape, iPad Air portrait (820px is under 900 → compact; 1180px landscape is not) | no | yes |
| **Desktop** | mouse, 900px or wider | no | no |

- A phone is **both**: drawer *and* 44px targets.
- A landscape iPad is **touch but not compact**: it docks the sidebar and keeps
  the desktop shapes, at finger sizes.
- A narrow desktop window is **compact but not touch**: it gets the drawer, and
  keeps hover affordances and mouse-sized controls.

900px is the tablet line. Every iPad in landscape is above it; every iPad in
portrait and every phone is below it.

---

## The gates, and why they are different

Both stylesheets are gated on the `media` attribute of their `<link>` in
`index.html`, and **neither file wraps its own rules in an `@media` block.**

```html
<link rel="stylesheet" href="/static/css/compact.css"
      media="(max-width: 899px)">
<link rel="stylesheet" href="/static/css/touch.css"
      media="(max-width: 768px), (hover: none) and (pointer: coarse)">
<script src="/static/js/touch-boot.js"></script>
```

`compact.css` asks a question every browser answers honestly — the viewport
width — so its gate is a plain width query and **no JavaScript ever touches
it**.

`touch.css` asks a question a browser will lie about. iPadOS defaults to
desktop-class browsing in Safari *and* Chrome (Chrome on iOS is a WebKit
wrapper; Apple requires it), and in that mode it reports `hover: hover`,
`pointer: fine`, a `Macintosh` user agent and a layout width over 768px. No
media query can see an iPad. So `static/js/touch-boot.js` runs synchronously in
`<head>`, detects the one signal desktop mode does not fake — a touchscreen on
a "Mac", via `navigator.maxTouchPoints` — and **flips that one link to
`media="all"`**. It also stamps `<html data-touch-ui>`, which `mobile.js`,
`sigil.js` and `notification-bell.js` read.

Two rules follow, and both have cost real bugs:

1. **Never wrap `touch.css`'s contents in an `@media` block.** The link gate is
   what `touch-boot.js` can reach; an internal query is not, and every rule
   inside it would silently stop applying on an iPad.
2. **Never give `compact.css` the same flip.** An iPad in landscape is *meant*
   to miss it. Flipping it to `media="all"` would put a 1180px tablet back into
   the phone layout — the original bug, restored.

`touch.css` is linked **last** so it wins ties against `compact.css`.

**Which file does a rule belong in?** Ask what the rule is *for*:

- fitting a narrow width — drawer, scrim, full-screen Explorer, bottom sheets,
  stacked rows, wrapped toolbars → **compact.css**
- input modality — 44px targets, 16px inputs (below that iOS zooms on focus and
  never zooms back), hover-revealed controls made visible, safe-area insets,
  momentum scrolling, the +1px font scale → **touch.css**

A rule filed wrongly in `touch.css` shows up as a phone layout on a 1180px
iPad. A rule filed wrongly in `compact.css` disappears on the tablet that
needed it.

---

## The JavaScript mirror

`static/js/mobile.js` is the JS half of the same split and exports both
predicates:

```js
import { isTouch, isCompact } from './mobile.js';
```

- `isTouch()` — `matchMedia('(max-width: 768px), (hover: none) and (pointer: coarse)')`
  **OR** `<html data-touch-ui>`. The attribute is read once at load, so touch
  can never flip *off*: rotating an iPad cannot tear the touch UI down.
- `isCompact()` — `matchMedia('(max-width: 899px)')`, live.

There is no `isMobile()`. It was removed once every caller had been
reclassified; a single predicate is what produced the tablet bug.

`initMobile()` stamps the answers onto the body so CSS can join in:

```
body[data-touch]     the pointer is a finger
body[data-compact]   the viewport is under 900px
```

and re-stamps on either media-query change. Anything that has to *rebuild* on a
tier flip listens for the event:

```js
window.addEventListener('pernix:tier-change', (e) => {
    // e.detail = { compact: boolean, touch: boolean }
});
```

Fired after every crossing of the 900px line, in both directions. Listeners
today: the worker strip (chips ↔ one summary line), the session-header title
(rename ↔ session sheet), and `file-panel.js` (an Explorer that was a
full-screen overlay is a sibling column now, and has to give `#main` back).

### Viewport custom properties

Three properties are maintained on `<html>` and read only by `touch.css`:

| Property | Set by | What it is for |
|---|---|---|
| `--vvh` | `visualViewport.height` | iOS ignores `interactive-widget=resizes-content`: `100dvh` does **not** shrink when the on-screen keyboard opens, so the shell keeps full height and the composer ends up underneath the keyboard. `--vvh` is the height that knows about the keyboard. |
| `--vv-top` | `visualViewport.offsetTop` | A `position: fixed` shell is laid out against the *layout* viewport, so after a pinch-zoom scroll it drifts off the visible one unless offset back. |
| `--bottom-stack` | `ResizeObserver` on `#input-wrapper` + `#worker-strip` | How much bottom chrome the floating layer has to clear. Five things sit above the composer — status line, recovery notice, toasts, the service-worker update pill, jump-to-bottom — and all five used to carry a hard-coded 76px measured against an *empty* composer. |

Both viewport values are debounced by 2px: the visual viewport jitters while a
page settles (address-bar animation, rubber-banding, a moving caret), and
relayouting the shell for that is free of any visible benefit.

---

## What each tier actually changes

**Compact only** (`isCompact()` / `compact.css`)

- The sidebar is an off-canvas **drawer** with a scrim, opened by the
  hamburger or a left-edge swipe (25px edge zone, 60px threshold) and closed by
  the scrim, Escape or a swipe back. Open, it is a real modal surface: `#main`
  goes `inert`, focus moves inside, and focus returns on close. Closed, the
  drawer itself is `inert`, so Tab and a screen reader do not walk an invisible
  session list.
- The **Explorer** is a full-screen overlay that inerts the transcript behind
  it. `#main`'s `inert` is reference-counted (`setMainInert(reason, on)`)
  because the drawer and the Explorer can both cover the screen, and either may
  open while the other is closing.
- **Modals become bottom sheets**, Settings included.
- The **model picker is a bottom sheet** rather than a menu anchored to a 19px
  badge: full width, 48px rows, a scrim and Escape.
- The **worker strip is one line** — `N workers · R running · P paused · M RLM ·
  K finished`, each part present only when its count is non-zero, with a
  chevron — instead of three rows of chips. Tapping it opens the worker sheet.
- The **session-header title opens the session sheet** (pin, rename, move, copy
  id, delete) instead of swapping in an inline rename field.
- In landscape, and in portrait while the composer has focus
  (`body.composer-focused`), the session header is hidden: it repeats what the
  drawer already says, and a landscape phone has about 260px of usable height.

**Touch only** (`isTouch()` / `touch.css`)

- 44px targets on primary controls, a 28px floor everywhere else, an 11px
  legibility floor, and every hover-revealed control made permanently visible.
- Every input is at least 16px. Below that, iOS Safari zooms the page on focus
  and never zooms back out.
- Safe-area insets, momentum scrolling, and the status bar moved to the *top*
  of the screen (`order: -1`).
- One **`⋯` overflow control per row** — sessions, spaces, Explorer files —
  opening an action sheet, replacing three or four hover-revealed icon buttons.
- The **editor is a plain textarea**, never Monaco: `createEditor()` returns
  the `.ce-fallback` textarea when `isTouch()`. Monaco's own touch handling
  fights iOS text selection, and native selection handles are what a finger
  actually has. 16px, `pre-wrap`, `min-height: max(200px, 50dvh)`.
- Code blocks get a header strip carrying the language and the copy button, so
  the button stops covering the first line of code; code blocks and tables that
  really do overflow (`.is-scrollable`, set by `app.js`) get a soft right edge
  saying so.
- The Explorer opens with a right-edge swipe (on **any** touch width — on a
  tablet it is a docked column, but the swipe is still how you reach it).

**Wide touch** gets the touch file and not the compact one: the sidebar
**docks** as a column, the Explorer is a **side column beside the chat**
(360–600px wide) rather than a lid over it, modals stay centred cards, and the
worker strip keeps its chips — all at finger sizes.

---

## The action sheet

`static/js/components/modals/sheet.js` exports one primitive:

```js
const choice = await actionSheet({
    title: 'Deploy the new build',              // the sheet's accessible name
    items: [
        { id: 'pin', label: 'Pin to top', icon: 'pin' },
        { id: 'delete', label: 'Delete', icon: 'trash', danger: true },
    ],
});   // -> 'pin' | 'delete' | null  (cancel, Escape, backdrop)
```

Deliberately **not** `role="menu"`: that asks a screen reader for arrow-key
navigation the app does not implement, while `openOverlay()` already provides a
named dialog, a focus trap, Escape and focus restoration. The sheet is a
`.modal-card`, so `compact.css` renders it as a bottom sheet and `modals.css`
centres it as a card above 900px with no work in the component. Two calls never
stack — the first is superseded and resolves `null`.

`sidebar.js` exports `openSessionSheet(sessionId)` for the one session menu
that the drawer row, the header title and anything else can share.

---

## Sending a message from a keyboard

`shouldSendOnKey(e, { enterSends, touch })` in `app.js` is the whole decision,
and it is pure so a test can ask it directly:

- **Ctrl+Enter / Cmd+Enter always sends**, on every device and in either
  setting. This is the answer for a tablet with a keyboard attached.
- Shift+Enter is a newline; Alt+Enter is not ours to take.
- An Enter that commits an IME candidate (`isComposing`, or `keyCode === 229`)
  is never a send.
- Otherwise the preference decides.

**Enter sends the message** lives in Settings → Providers & models → *This
browser*. It is stored in `localStorage['pernix:enter-sends']`, announced as
`window` event `pernix:enter-sends`, and is **not** part of the settings
payload or of Save — like Appearance, it belongs to the device.

Its default differs by device, which is the point: on a desktop Enter has
always sent and Shift+Enter is the newline everyone knows, but on a phone Enter
*is* the on-screen keyboard's newline key with a send button an inch away. So
the default is `!isTouch()`. The composer's visible hint follows the resolved
state — "Enter to send · Shift+Enter for a new line", "Ctrl+Enter to send", or
"Tap send" — because a phone that claims "Enter to send" while its Enter makes
a new line is worse than a phone that says nothing.

---

## Gotchas

- **Deploy skew.** `static/sw.js` precaches the stylesheets and modules by
  name. Straight after a deploy the first load can pair a cached `mobile.js`
  with new stylesheets (or the reverse) and look subtly wrong. One reload
  clears it. If you add or rename a file under `static/`, add it to the
  precache list.
- **`isMobile()` is gone.** Reach for `isCompact()` for anything about layout
  and `isTouch()` for anything about the pointer. An import of the old name is
  a load error, which is the intended outcome — a silent default to one of the
  two is exactly the bug the split was for.
- **A headless browser cannot reproduce the iOS keyboard.** `--vvh` exists for
  a failure (iOS 15–18 not shrinking `dvh` for the on-screen keyboard) that no
  automated check sees, and the same is true of native text selection, rotation
  with the keyboard up, and VoiceOver's reading order. Those have to be checked
  by hand on real hardware:
  [`docs/mobile-device-checklist.md`](../mobile-device-checklist.md).
- **There is an acceptance gate for the rest.** `tools/ui-gate/run.sh` drives
  an isolated seeded instance across seven viewports and asserts the tier
  contracts, the touch floors and a desktop layout baseline. Run it before and
  after any change to these files; see
  [`tools/ui-gate/README.md`](../../tools/ui-gate/README.md).
