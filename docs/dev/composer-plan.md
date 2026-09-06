# Composer (chat input) redesign plan — 2026-09-05

Scope: the message input at the bottom of a chat on desktop, tablet and phone.
Status: PLAN for discussion. Nothing built.

## 1. What exists today (audit)

Markup: `#input-wrapper > #file-chips + #input-bar (attach · textarea · voice · send)`
(static/index.html:183). Behaviour in static/js/app.js ~1796-1935; CSS in
layout.css 3284-3474 (desktop), touch.css 989-1028 (touch), compact.css:42.

| Aspect | Desktop | Touch (phone / tablet) |
|---|---|---|
| Rest height | 1 line: textarea `min-height 24px`, `rows=1` | 1 line: `min-height 22px` |
| Growth | auto-grow to `200px` (~9 lines), then scroll | to `min(120px, 30dvh)` |
| Type | `--mono` at `--text-base` = 13px, line-height 1.5 | 16px (iOS zoom guard), line-height 1.4 |
| Shape | pill, `border-radius 1.5rem`, width capped at `--chat-max-w` (960px) | pill, 1.25rem |
| Targets | attach / send 36px round; voice hidden unless configured | 40px |
| Bindings | Enter sends, Shift+Enter newline (toggle in Settings → Appearance, stored client-side) | Enter = newline, tap send; `Ctrl+Enter` when the toggle is on |
| Hint | placeholder "Message Pernix… Enter to send"; full binding only in tooltip + aria-description | placeholder "Message Pernix" under 400px |
| State | send button turns into Stop while streaming; typing while streaming injects into the running turn | same |
| Drafts | per-session in localStorage; ArrowUp recalls prompt history | same |
| Attach | paperclip, drag and drop, paste; chips row above the bar | same |
| Limits | 1,000,000 chars, rejected at send with a system message | same |
| Mobile chrome | — | safe-area padding; session header hides on focus (compact) |

What is already right and should stay: drafts, history recall, inject-while-streaming,
16px on touch, safe-area handling, chips, the focus ring, the aria-description.

## 2. Problems, ranked by user impact

1. **The main control has the least presence on the screen.** A one-line pill at 13px
   monospace reads as a search field, not a place to write. Everything else in the
   transcript is larger (assistant text is serif at ~16px). Affordance says "short
   query"; the product wants long, careful prompts. (Affordance, Fitts's law.)
2. **Nowhere to write something long.** Growth stops at ~9 lines on desktop and ~5 on
   a phone, with no way to open a bigger editor. Users who write multi-paragraph
   briefs fight a scrolling slot. (Slack, Linear, Claude.ai all offer an expand.)
3. **Bindings are invisible.** Shift+Enter lives only in a tooltip and the accessible
   description. The Enter-sends toggle is two menus away. (Nielsen: recognition over
   recall; flexibility and efficiency.)
4. **Streaming state is implicit.** While the agent runs, the send button silently
   becomes Stop and typed text is injected into the running turn, with no visible
   cue that this is what will happen. (Nielsen: visibility of system status; error
   prevention — Stop and Send share one slot.)
5. **Touch targets are under the platform floors.** 40px on touch versus Apple HIG
   44pt and Material 48dp; 36px on desktop. The gate's 28px floor is a minimum, not
   a target. (WCAG 2.5.8 AA is 24px; 2.5.5 AAA is 44px.)
6. **No feedback on size until a hard rejection at a million characters.** A
   ten-thousand-line paste silently balloons the request. (Progressive disclosure;
   Claude.ai and ChatGPT convert long pastes to attachments.)
7. **Controls sit inline with the text.** Attach and send share the text row, so
   every extra control narrows the writing area and the row height is tied to the
   buttons. The dominant modern pattern is text on top, a control row beneath.
8. **Phone keyboard has no send affordance** (`enterkeyhint`) even when Enter-sends
   is on, and the return key says "return" while it actually sends.

## 3. Design principles we lean on

- Affordance and Fitts's law: a bigger, squarer target invites writing; the send
  action should be the largest, nearest target.
- Nielsen heuristics: visibility of system status (streaming, queued, size),
  recognition over recall (bindings visible), user control (expand, stop is a
  separate act), error prevention (long paste offered as a file, not rejected).
- Platform floors: Apple HIG 44pt, Material 48dp, WCAG 2.5.8 / 2.5.5 target size;
  16px minimum input text on iOS; safe-area insets; `enterkeyhint`.
- Progressive disclosure: hints and counters appear when relevant, not always.
- Comfortable measure: 60–75 characters per line is easiest to read and write; a
  960px composer at 13px mono is ~150 characters per line, too wide for the type.

## 4. Proposal

### Phase A — Presence and size (the direct ask)

- **Layout**: text block on top, a control row beneath (attach · voice · hint text ·
  send). The textarea gets the full width; the row holds the targets at platform
  size. Container becomes a rounded rectangle (12px radius), not a pill. On a phone
  at rest the row collapses into the same line as one-line text (space is scarce);
  the moment text wraps or a second line is typed, the row drops beneath.
- **Rest height**: desktop and tablet 3 lines (about 76px of text area); phone 1
  line at rest, 2 once focused.
- **Growth**: desktop to 40dvh (~14 lines at the new size) then internal scroll;
  phone stays at `min(30dvh, …)`, which already tracks the keyboard.
- **Type**: `--text-lg` (15px) on desktop, 16px on touch, line-height 1.55. Keep the
  monospace family (it is Pernix's writing voice and distinguishes input from
  output); decision point below.
- **Measure**: cap the composer at ~76ch of the input font (roughly 720px at 15px
  mono) rather than the 960px chat width, centred under the transcript.
- **Expand**: an expand control on desktop and tablet opens the same text in a
  large centred editor (70vw × 70vh) with the same bindings; on a phone it opens as
  a full-screen sheet with a top bar (Cancel · Send). Draft stays shared.
- **Targets**: 44px on touch, 40px on desktop, all round or square with an 8px gap.

### Phase B — State and bindings

- **Hint row**: faint text in the control row, desktop only: "Enter to send ·
  Shift+Enter new line". Clicking it toggles the preference in place and the text
  flips to "Ctrl+Enter to send · Enter new line". Hidden under 400px; the
  aria-description keeps the binding for screen readers.
- **Streaming**: while the agent runs, the composer border takes the accent hue and
  the placeholder reads "Reply now — goes to the running turn". Send keeps its icon
  (it still sends); Stop becomes its own control at the left of the row, red,
  with the same 44/40px target. Two acts, two buttons.
- **Queued cue**: after an inject, a one-line "Sent to the running turn" note in the
  live region and a brief inline chip above the composer that clears when the agent
  picks it up (the existing `_markPendingQueued` path renders it).
- **Size feedback**: a counter appears at 20,000 characters ("24,310 characters") and
  turns amber at 200,000 with an "Attach as file instead" link that moves the text
  into a .txt attachment. A paste over 50,000 characters offers the same in a small
  banner rather than doing it silently.
- **Send affordance on phones**: `enterkeyhint="send"` when Enter-sends is on,
  `"enter"` otherwise; the keyboard's return key then says what it does.

### Phase C — Attachments and voice

- Drag-over highlight of the whole composer ("Drop to attach"); paste of an image
  shows a thumbnail chip. Both exist in part; make the states visible.
- Voice: keep hidden until configured, but the expand editor and the control row
  leave its slot free so nothing shifts when it appears.

### Phase D — Accessibility and gates

- `role="group"` with `aria-label="Message composer"` on the wrapper; focus order
  attach → text → voice → stop/send → expand; `aria-keyshortcuts` on send.
- Placeholder contrast: `--text-faint` must reach 4.5:1 against the composer
  surface in both themes, or the placeholder moves to `--text-dim`.
- Gate: re-record the desktop baseline deliberately (this is a designed change);
  add checks for rest height ≥ 3 lines on desktop, ≥ 44px targets on touch, the
  hint row's presence by width, `enterkeyhint`, the expand editor open/close, and
  the streaming placeholder.

## 5. Decisions to make together

1. **Input typeface**: keep monospace (terminal identity, code-friendly) or switch to
   the UI sans (reads as prose, matches the app chrome)? Recommendation: keep mono
   at 15px; revisit if the measure feels wide.
2. **Rest height on desktop**: 3 lines (recommended) or 2.
3. **Controls below the text** (recommended, the modern pattern) or keep them inline.
4. **Stop as a separate button** (recommended) or keep it in the send slot.
5. **Long-paste threshold** for the attach-as-file offer: 50,000 characters is a
   guess; pick a number you would want to see the banner at.
6. **Expand editor on desktop**: modal (recommended) or grow-in-place to 70vh.

## 6. Effort and rollout

Phase A + B together is one focused UI change: layout.css / touch.css / compact.css,
app.js composer block, index.html markup, settings toggle wiring, ui-gate checks and
baseline. Two Opus agents (one for the composer, one for the gate) in one session,
then a both-theme smoke and a real-device pass on Calvin's phone and iPad. Phases
C and D ride in the same change where cheap, else follow next.
