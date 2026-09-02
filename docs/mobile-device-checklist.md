# Mobile device checklist

Everything a headless browser can check about the phone and tablet layouts is
checked by `tools/ui-gate/run.sh`. This file is the rest: the things that need
real hardware because the failure is in the operating system's behaviour, not
in the page.

Run it after any change to `static/css/touch.css`, `static/css/compact.css`,
`static/js/mobile.js` or `static/js/touch-boot.js`, and before a release. Ten
minutes with a phone and a tablet.

Background on the tiers, the gates and the custom properties named below:
[`internals/web-client.md`](internals/web-client.md).

---

## iPhone — Safari, and again as a Home Screen app

Do the whole section twice. The Home Screen app (standalone mode) has no
address bar, gets different safe-area insets, and reports a different viewport
height from the same phone in Safari — several of these have failed in one and
passed in the other.

- [ ] **Tap the composer.** The keyboard opens, and the composer is *above* it,
      not under it. This is what `--vvh` exists for: iOS ignores
      `interactive-widget=resizes-content`, so `100dvh` does not shrink when
      the keyboard opens and the shell keeps its full height. No desktop
      browser and no emulator reproduces this.
- [ ] **The page did not zoom in** when the field took focus. Any input under
      16px makes iOS zoom and never zoom back out; you end up typing off the
      right edge.
- [ ] **Type two lines.** The composer grows, the transcript gives up the room,
      and the last message is still visible above it.
- [ ] **Rotate to landscape with those two lines still in the box.** The send
      button and the last message are both on screen. The session header is
      gone (a landscape phone has about 260px of usable height, and the header
      repeats what the drawer already says).
- [ ] **Rotate back.** The header returns; nothing is left half-offscreen.
- [ ] **Scroll the transcript up and down.** The header does not scroll away
      with it, and the status strip and the composer stay put.
- [ ] **Send a long answer and scroll up while it streams.** The
      jump-to-bottom button is at the *right* edge, clear of the text, and
      above the composer rather than over it (`--bottom-stack`).
- [ ] **Open the drawer and scroll the session list to the bottom.** It stops
      at the last row instead of dragging the whole page with it.
- [ ] **Home Screen app only:** the status bar area is not overlapped by the
      app's own header, and the composer clears the home indicator.

## iPad — landscape

The tier this whole layer exists for: a large screen with a finger on it.
Nothing here should look like a phone.

- [ ] **The sidebar is docked**, not a drawer, and there is no scrim.
- [ ] **Open the Explorer.** It is a column *beside* the conversation, and the
      conversation is still readable next to it — not a lid over the whole
      screen.
- [ ] **Targets are still finger-sized** while all of that is true: the row
      `⋯` buttons, the status bar controls, the tab strips.
- [ ] **Tap a session row's `⋯`.** The action sheet opens as a centred card
      (not a bottom sheet), naming every action in words.
- [ ] **Open a file and tap edit.** It is a plain textarea, not Monaco.
      Long-press a word: the **native iOS selection handles** appear and drag
      normally. This is the whole reason Monaco is off on touch.
- [ ] **Swipe in from the right edge.** The Explorer opens.

### With a hardware keyboard attached

- [ ] **Cmd+Enter sends** from the composer, in either setting of "Enter sends
      the message".
- [ ] **Ctrl+Enter sends** too.
- [ ] **Enter alone** does what the preference says — and the hint under the
      composer says the same thing.
- [ ] **Escape closes** an open sheet, the model picker and Settings, and
      focus lands back where it started.
- [ ] **Tab walks** the composer, the send button and the visible controls in a
      sensible order, with a visible focus ring, and never reaches anything
      behind an open dialog.

## iPad — portrait

820×1180 is *under* the 900px line, so a portrait iPad is compact: it gets the
phone's shapes at tablet sizes. That is intentional.

- [ ] **The sidebar is a drawer** with a scrim.
- [ ] **A swipe in from the left edge opens it**; a swipe back closes it.
- [ ] **Escape closes it** (external keyboard), and the hamburger toggles it.
- [ ] **Rotate to landscape while the drawer is open.** The drawer becomes the
      docked sidebar; no scrim is left over, and nothing is inert that should
      not be.
- [ ] **Rotate back.** The sidebar is a drawer again, closed.

## VoiceOver

Turn VoiceOver on (triple-click the side button if you have set that shortcut).
Both of these are about what a swipe can *reach*, which no automated check in
this repo verifies.

- [ ] **Drawer open:** swiping right through the page walks the drawer's
      contents and stops there. It must not reach the transcript, the composer
      or the status bar behind it — `#main` is `inert` while the drawer is a
      modal surface.
- [ ] **Drawer closed:** swiping never reaches the session list. A closed
      drawer is off-canvas, not gone, and without its own `inert` VoiceOver
      reads out an invisible list of every session.
- [ ] **Same two checks with the Explorer open** on a phone: it covers the
      screen, so the transcript behind it must be unreachable too.
- [ ] **Open a session's `⋯` sheet.** It is announced as a dialog with the
      session's name, and every row is read as a button with a real label.
- [ ] **Send a message.** The turn finishing is announced, not silent.

## After a deploy

- [ ] **The first load may look wrong.** `static/sw.js` precaches the modules
      and stylesheets by name, so immediately after a deploy a device can pair
      a cached `mobile.js` with new stylesheets or the reverse. **One reload
      clears it.** Reload before reporting anything as a bug — and if you have
      just added or renamed a file under `static/`, check it is in the
      precache list.
- [ ] **Then re-run the top of this list** on the reloaded page.
