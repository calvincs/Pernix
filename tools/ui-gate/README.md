# The UI gate

An acceptance gate for the web client's device tiers. It boots a throwaway
Pernix, seeds it with realistic transcript shapes, drives seven viewports with
Playwright, and asserts what each tier is supposed to do — including that the
**desktop layout has not moved**.

It exists because every rule in `static/css/touch.css` and
`static/css/compact.css` is invisible from a 1280px window with a mouse, which
is where changes to them get made. See
[`docs/internals/web-client.md`](../../docs/internals/web-client.md) for what
the tiers are and why there are two stylesheets.

```bash
tools/ui-gate/run.sh my-tag          # level m2 — everything
LEVEL=m1 tools/ui-gate/run.sh my-tag # foundation only
```

The tag names this run's output files; anything works except `baseline`, which
is reserved (see below). Exit status is 0 only when every check at the
requested level passed.

## Requirements

The repo's own `.venv`, with Playwright and a Chromium browser:

```bash
.venv/bin/pip install playwright
.venv/bin/playwright install chromium
```

Also `curl` and `lsof`, which run.sh uses to wait for the server and to make
sure the port is free afterwards.

## Knobs

| | Default | What it does |
|---|---|---|
| `LEVEL` | `m2` | `m1` asserts the foundation only: the tier stamps, which stylesheet loaded, `inert` while the drawer or the Explorer covers the screen, the measured bottom stack, the model menu staying on screen, and the desktop baseline. `m2` adds everything else: the row sheets, the plain touch editor, the one-line worker strip, the 28px target floor, the 16px input floor, the landscape header hide, and the desktop sidebar's resize handle (which must be absent on every touch viewport). Can also be given as the second positional argument: `run.sh my-tag m1`. |
| `PORT` | `8790` | Where the throwaway instance listens. Change it to run two gates at once, or if 8790 is taken. |
| `REPO` | the checkout this script lives in | Which checkout to test. Useful from a worktree, or to point a gate at a branch you have checked out elsewhere. |

## What it writes

Everything lands in `tools/ui-gate/out/`, which is git-ignored:

| Path | What |
|---|---|
| `out/app-$PORT/` | the throwaway instance: its own `data/`, a symlink to the repo's `static/`, a SQLite database deleted and re-seeded on every run |
| `out/shots/<tag>-<viewport>-<label>.png` | screenshots at every step of every viewport, which is how you see *why* a check failed |
| `out/check-<tag>.json` | every check with its measured value, for diffing two runs |
| `out/server-<tag>.log` | the instance's own log |

Your real `data/` directory and any running Pernix are untouched: the gate
copies `data/agent` into its own tree and never opens your database.

## The desktop baseline

`desktop-baseline.json` — next to this README, and **committed** — is the
geometry of fourteen elements across five desktop states (home, a chat,
Settings open, the Explorer open, the sidebar collapsed) at 1280×800 with a
mouse. For each: left, top, width, height, `position`, `display`, `font-size`
and `padding` — of the sidebar, the main column, the status bar, the composer,
the transcript and its inner column, the Explorer, the modal card, the sidebar
toggle, the session header, the model chip, the message box, the first session
row and the settings button.

Every run compares against it and fails on any element that moved by more than
a pixel. That is the check that makes a mobile change safe to land: a rule
filed in the wrong stylesheet, or one whose specificity reaches further than
its author thought, shows up here as a desktop regression rather than as a bug
report three weeks later.

**Regenerating it is not a way to make a failure go away.** If the baseline
fails, the desktop moved, and that is either the bug or a deliberate change
that needs saying out loud. Regenerate only when the desktop layout was
*meant* to change:

```bash
tools/ui-gate/run.sh baseline m1     # writes desktop-baseline.json
```

The tag `baseline` is what switches the desktop pass from comparing to
writing. Use `m1`: the level does not affect what is captured, and m1 is
faster. Commit the regenerated file in the same commit as the change that
moved the desktop, and say in the message what moved and why.

## The viewports

| Name | Size | Tier |
|---|---|---|
| `phone-s` | 360×780 | compact + touch |
| `phone` | 390×844 | compact + touch |
| `phone-l` | 844×390 | compact + touch (landscape) |
| `tab-p` | 768×1024 | compact + touch |
| `tab-air-p` | 820×1180 | compact + touch |
| `tab-air-l` | 1180×820 | **wide touch** |
| `ipad-desk` | 1024×768, Mac UA, no `is_mobile` | **wide touch**, and the one that only passes because `touch-boot.js` recognises it — it is an iPad claiming to be a Mac, exactly as iPadOS desktop mode does |
| desktop | 1280×800, mouse | the baseline comparison |
| `state-map` | 1280×800, mouse, reduced motion | one m1 check on the State timeline's Map tab, in its own context because it opens a session no other pass touches |
| `timeline-lane` | 1280×900 dark and light, plus 390×844 touch | three m2 checks on the State timeline's Lane tab and the Story under it, and one on the phone |

### The state-map pass

`readColor()` in `static/js/theme.js` resolves a `--token` by setting `color:
var(--token)` on a probe span and reading the computed value back, and it has
twice been handed something it did not expect — a `color(srgb 0.8 0.84 0.86)`
from a `color-mix()` token, and an interpolated `oklab(…)` from the `.01ms`
transition the reduced-motion block puts on `*`. Both painted every node, node
label and time-in-state bar in the State timeline black. The check asks for
reduced motion so one pass covers both, and asserts that no `--state-*` token
reads as black.

The map itself no longer goes through that bridge at all: it is inline SVG
whose rects carry `tl-map-state tl-map-<name>` and take their `--state-*` pair
from `layout.css`, so the token half of this pass now guards `theme.js` on
behalf of the sigil and Monaco while the map half asserts that the CSS route is
wired up. It draws all ten states of `sessions/state_v2.py` and all 31 edges of
its `TRANSITIONS` table whatever the session did, so the check pins the count
of each, that no box is within a hair of `#000`, that every label clears 3:1
against its own box, that exactly the seven edges the seeded arc took are drawn
solid and carry the right count, that the parked session lights `idle_ready`
and pulses nothing, that the drawing does not scroll the page sideways — and
that opening the tab asks for no `mermaid.min.js`, which is not vendored any
more.

### The timeline-lane pass

`seed.py`'s last block builds a session with three turns of state-log rows
and the messages `db.get_turns` parses back into a scout report, a reflect
chain, an eval gate, a compaction and a notice. It is its own session on
purpose: the lane needs matching messages, and the desktop baseline pins the
height of the main session's transcript, so eleven more rows there would fail
a check that is not about the timeline at all. `check.py` finds it by title in
the throwaway's own sqlite, because `run.sh` forwards only `main` and
`parent`.

The phase durations are chosen to be round shares of their own turn —
10/30/5/10/40/5, 10/25/20/40/5, 10/70/20 — so the pass can assert that every
segment is within 2px of its share of that row's bar. It also pins the tick
counts, the compaction segment, the Story's four cards against the newest
turn's seeded text, Arrow Up moving the selection, and — in both themes —
that no segment paints black and the story's prose clears 4.5:1 on its card.
The touch half asserts 44px rows and no sideways scroll at 390px.

## Files

| | |
|---|---|
| `run.sh` | boots, seeds, runs, tears down |
| `seed.py` | the fixture: a chat with markdown, a code block, a wide table, three tool rounds, a 120-message session for paging, a fan-out parent with three workers, three spaces, cron/snooze/canary sessions, and a three-turn state log for the timeline |
| `check.py` | the checks themselves; run directly if you already have a seeded instance |
| `desktop-baseline.json` | see above |
