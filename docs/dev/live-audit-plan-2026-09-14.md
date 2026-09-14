# Live audit 2026-09-14 — remediation plan

Audit of the 449 commits from `08-31` to `2dee81b` against the running
instance (box, up since 09-11). Method: three days of container logs (zero
tracebacks), database probes, an authenticated sweep of all 46 GET routes
(all 200, slowest 479 ms), a headless Playwright pass over the live UI at
desktop and phone widths (zero console errors), and a read-only code review
of `2dee81b`, the one commit no earlier batch had reviewed.

Thirteen findings. Seven were observed live, six were found by reading
`2dee81b` and have not fired on the box yet (zero rejected transitions in
`session_state_log` since the deploy).

| ID | Where seen | Finding |
| --- | --- | --- |
| L01 | live, user report | Notification **Dismiss hangs** — browser starvation, not the backend |
| L02 | live, reproduced | Sidebar hover overlay: a click at the row centre **pins** instead of opening |
| L03 | live | A lone `~` in a reply renders the rest of the line as strikethrough |
| L04 | live | Self-checks toolbar overlaps its own buttons in the desktop Explorer |
| L05 | live, logs | Memory consolidation prompt overflows the model context every cycle |
| L06 | live, logs | Tripwire re-judges two Aug 13 batches on every tick, 164 warnings |
| L07 | live, logs | A generated canary is contaminated by design and alerts nightly |
| L09 | code review | `PAUSE_REQUESTED` has no `compaction-failed` edge; rejections are silent |
| L10 | code review | Cancel and delete load the whole transcript on the event loop |
| L11 | code review | An unguarded delivery-acknowledge write can fail a good round |
| L12 | code review | `_injectedMessages` leaks across sessions: stale "queued" chip |
| L13 | code review | `dismiss_question` can 500 and can leave `AWAITING_USER` |
| L14 | code review | Worker steering reports success before delivery |

(L08, reflect and file-split JSON parse retries, is model noise that already
recovers. Declined.)

## Findings in detail

### L01 — Dismiss hangs: Chrome's six-connection ceiling

The user's POST to `/api/notifications/{id}/dismiss` never appears in the
uvicorn access log; the same POST from inside the container answers in 2 ms.
At the time of the report the user's machine held **exactly six** established
connections to `:8090`. Uvicorn speaks HTTP/1.1 only, and Chrome allows six
connections per host on HTTP/1.1. Every Pernix tab opens three `EventSource`
streams — `/api/sessions/{id}/events`, `/api/notifications/events`,
`/api/jobs/events` — so two windows (a second tab, or the installed app plus
a tab) consume the whole budget and every further fetch waits in the browser
forever, showing as "pending" in devtools. Session switching does **not**
leak streams (verified: streams stay at two shared plus one session across
six switches).

**Fix (W1).** Page-visibility aware shared streams. When `document.hidden`
becomes true, close the notifications and jobs streams after a short grace
(5 s, so a tab switch and back costs nothing). When the tab becomes visible
again, reopen them and refetch `/api/notifications` and `/api/jobs/status`
so anything missed while closed is caught up. A hidden tab then holds one
connection, not three, and two windows fit inside the budget with room for
requests. Keep the session stream open in hidden tabs — it is what lets a
background tab show a finished turn. Add a one-line note in
`docs/internals/` (streams per tab, the six-connection budget, why hidden
tabs give theirs back). Test in the JS harness: hidden → both shared sources
closed after the grace; visible → reopened and the two catch-up fetches
issued; a hide/show inside the grace closes nothing.

### L02 — Hover overlay pins instead of opening

`ec5f2d7` (09-02) moved the session row's hover controls into an absolutely
positioned `.session-actions` overlay. On a 253 px row it holds seven 24 px
buttons (184 px) starting at x=57, so on hover the title collapses to about
four characters and the overlay covers the rest. Playwright clicking the
centre of six rows pinned six sessions (`elementFromPoint` at the centre is
`button.session-pin`). Delete is also under the cursor at the top-right.

**Fix (W1).** On pointer (non-touch) hover show at most **two** controls at
the right edge: pin and a `⋯` button that opens the existing row action menu
(the same items list the touch action sheet uses — `sidebar.js` already
builds `items` with `pin`, `rename`, `move`, `archive`, `delete`). The title
keeps at least 60 % of the row width clickable and visible. Keyboard users
keep every action reachable through the menu. The `Delete` control leaves
the hover strip entirely. Test in the JS harness or with a DOM assertion in
`tools/ui-gate/check.py`: hovering a row leaves `.session-title-text` as the
element at the row centre, and the overlay's width is ≤ 64 px. Re-record the
ui-gate baseline only for the rows' hover state.

### L03 — Lone tilde strikethrough

`render.js` sets `marked.setOptions({ breaks: true, gfm: true })`. marked's
GFM strikethrough accepts a **single** tilde, so a reply such as
"the ~256-byte vocabulary (vs ~100K tokens) …" renders the span between the
two tildes struck through and the bold that follows shows raw asterisks.
182 of the 4 121 assistant messages since 08-31 contain a lone tilde;
approximate numbers are a normal way for the model to write.

**Fix (W1).** Register a `del` tokenizer override through `marked.use` that
only matches `~~text~~` (double tilde, no leading or trailing whitespace
inside), so `~5` and `~100K` are plain text. Keep `~~strike~~` working. Test
through the JS harness (`tests/js_harness.py` can load `render.js`): the
example line renders with no `<del>` and with the trailing bold intact; a
double-tilde span still yields `<del>`.

### L04 — Self-checks toolbar overlap

`.adaptive-head` is `display:flex` with no wrap; wrapping is only enabled
under `body[data-compact]` and `body[data-touch]`. The desktop **docked**
Explorer is the same 360 px as the tablet one, so the heartbeat chip and
four buttons overlap on a plain desktop browser.

**Fix (W1).** Make the wrap rule unconditional: `flex-wrap: wrap`,
`row-gap`, children `flex: 0 0 auto; max-width: 100%`. Drop the now-redundant
compact/touch duplicate. Check the Learning, Goals and Trust heads use the
same class and get the same behaviour. Screenshot check in ui-gate at the
desktop width with the Explorer docked.

### L05 — Consolidation prompt overflows the context window

`core/memory/consolidate.build_llm_merge_prompt` concatenates every entry of
every file in the cluster (1 024 chars each) with no total cap. The two
largest clusters on the box (twelve-plus research/macro files, and the
curiosity-drive family) produce a prompt vLLM rejects with
`maximum context length is 196608 tokens … prompt contains at least 194609`.
`consolidate_files` processes one cluster per cycle, so the failing cluster
is retried every cycle and never consolidates; the log shows the identical
400 on 09-11 and 09-12.

**Fix (W3).** Budget the prompt. Resolve the window for
`settings.background_model or settings.llm_model` through
`core/llm/budget.py`; target `window − max_tokens(2000) − 10 % margin`,
measured with the codebase's existing chars-per-token estimate. Build the
prompt file by file; when the budget runs out, shorten previews evenly
(1 024 → 512 → 256 chars) and then drop the oldest entries per file, keeping
at least the newest three per file, so every file in the cluster is still
represented. If even the floor does not fit, consolidate the cluster's
largest-overlap **pair** this cycle instead of the whole cluster (a partial
merge is still progress and the cluster shrinks). On a provider 400 that
names the context length, record `consolidation_skip:<cluster-key>` in
`snooze_state` with a 7-day expiry and pick the next cluster, so one bad
cluster cannot block all consolidation. Tests: a synthetic cluster that
exceeds the budget produces a prompt under it with every file represented;
the pair fallback fires when the floor is exceeded; the skip marker is
honoured and expires.

### L06 — Tripwire re-judges dead batches forever

`core/adaptive/tripwire.py` returns `None` ("no usable signal") for
`ab-0f4a6cbd1725` and `ab-614c36e552b9` (candor, applied 2026-08-13, no
`cleared_at`) on every maintenance tick, logging the same WARNING each time:
164 lines between 09-10 and 09-12. The canary suite behind them was retired
on 08-27, so no task can ever testify; the batches will never resolve.

**Fix (W3).** Two changes. (a) A batch whose post-batch sweep could not
testify within `tripwire_window_hours` (new setting, default 72 h) of
`applied_at` is settled as `cleared_at = now`, `flagged_reason =
"no-signal"` — logged once at INFO, surfaced in the Adaptive panel's batch
row as "unjudged (no canary could testify)". Rollback stays available to a
human. (b) The per-tick WARNING becomes one WARNING per batch (remember the
batch id in module state or `snooze_state`) and DEBUG thereafter. Tests: an
old batch with no usable rows is cleared exactly once with the reason; a
young batch is left alone; the warning fires once.

### L07 — A canary contaminated by design

`data/canaries/workspace-organizer-evidence-gate` (generated by the
skill-change sweep) instructs the agent to operate on `./data/workspace`,
which is outside the canary sandbox. Every run is `contaminated` (3 of 3),
which is correct, but the suite then raises a high-urgency "Needs attention"
plus a "chronically failing task(s)" notification **every night** at 03:05
UTC. `gen-grep-count` and `gen-json-transform` were also contaminated once
each for naming sibling canaries in their transcript.

**Fix (W3).** (a) Validate at generation time: a proposed canary whose prompt
references an absolute path, `data/workspace`, `/app/`, or another canary's
name is rejected with the reason recorded on the proposal — the generator
must produce workspace-relative tasks. (b) In `core/canary/maintain.py`, a
task contaminated on three consecutive runs is **parked** automatically with
a single normal-urgency notification that names the contamination reason;
parked tasks do not raise the nightly high-urgency alert. (c) Deploy step,
not code: park `workspace-organizer-evidence-gate` on the box through
`/api/canary/{name}/park` so the new rule does not have to wait three more
nights. Tests: the validator rejects each pattern; the third consecutive
contaminated run parks the task and notifies once; a fourth run does not
notify again.

### L09 — Missing state edges, silent rejection

`2dee81b` routes `PAUSE_REQUESTED` through the same termination branch as
`PROCESSING` (`sessions/manager.py` ~2533 and ~3110), which maps
`compaction_failed` to the reason `compaction-failed`, but
`sessions/state_v2.TRANSITIONS` declares `PAUSE_REQUESTED →` only for
`loop-complete`, `round-ceiling`, `stuck-loop`, `agent-error`. The same
commit made `transition()` return `False` on an undeclared edge instead of
forcing it, so the `except Exception` wrappers never fire. A paused turn
that ends in `compaction_failed` therefore stays in `PAUSE_REQUESTED`: no
post hooks, queued prompts never start, the UI hides pause and cancel, until
the reaper's 60 s unstick. Second instance: the reaper picks
`reaper-unstick` for whatever state finalization died in, and there is no
`(CANCELLING, "reaper-unstick")` edge.

**Fix (W2).** Add `(PAUSE_REQUESTED, "compaction-failed") → FINALIZING` and
`(CANCELLING, "reaper-unstick") → FINALIZING` (or whatever target the reaper
uses for the other states — match, do not invent). Update `MAP_EDGES` so the
timeline map stays in parity (there is a test for that). Then close the
class: add a test that enumerates every `(state, reason)` a dynamic producer
can emit — `_map_termination_to_v2_reason` × the states that route through
it, and the reaper's reason table × the states it unsticks — and asserts
each pair is declared. Finally make a rejected transition **loud**: log at
ERROR with the pair, and have `transition()` keep returning `False` so the
turn does not crash, but count it in `/api/health/detailed` so it is
observable.

### L10 — Cancel and delete load the whole transcript

`drop_pending_for_cancel` (`manager.py` ~1625) now calls
`db.get_orphaned_user_messages`, which materialises every row of the session
including content, on the event loop, and is reached from `/cancel` and from
the delete path — so a bulk purge of N sessions does N full loads with JSON
parsing on the loop. It also bypasses the `turn_has_assistant_row` guard, so
a cancel landing between the assistant row save and the delivery acknowledge
stamps `cancelled` on an answered message.

**Fix (W2).** A narrow query: `db.get_pending_user_message_ids(session_id)`
selecting only `id, metadata` for `role='user'` rows whose delivery is
pending, run in `asyncio.to_thread`. Skip the scan entirely on the delete
path (the rows are dropped anyway). Restore the `turn_has_assistant_row`
guard before stamping. Test: the cancel path issues no full-transcript
read (assert on the DB call, not a stopwatch, per the 3.2.2 lesson) and does
not stamp a message that already has an assistant row.

### L11 — A bookkeeping write can fail a good round

`core/agent.py` awaits `_acknowledge_delivery` unguarded at two sites; inside,
`db.set_message_delivery` runs in a thread. A transient sqlite error after a
successful model reply propagates to `_run_agent_safe`, classifies the turn
`agent-error`, and discards the round's tool calls.

**Fix (W2).** Make the acknowledge best-effort: catch, log at WARNING with
the message id, continue. Every other side-channel write in that region is
already best-effort; this one should match. Test: a failing
`set_message_delivery` leaves the round's outcome intact.

### L12 — Stale queued chip across sessions

`app.js` stopped emptying `_injectedMessages` on `turn.complete`; only
`session.cancelled` clears it and `selectSession` never does. A correction
injected in session A followed by a switch to B before A's
`message.consumed` arrives leaves a detached element in the array forever,
so B shows a "queued" chip until a token, cancel or reload.

**Fix (W1).** Key the injected-message bookkeeping by session id (a
`Map<sessionId, element[]>`), clear the chip and the entry on
`selectSession`, and drop entries whose element is no longer in the
document. JS-harness test: inject in A, switch to B, no chip in B; switch
back to A, the chip is restored from A's entry if still pending.

### L13 — dismiss_question can 500 or leave AWAITING_USER

`api/routers/questions.py` now calls `manager.get_or_create(session_id)`,
which raises `ValueError` for a question whose session row is gone (an
unhandled 500 where the old code returned dismissed), and when admission is
refused (`queue_full`, `shutting_down`) it 409s with the question row still
open and the session still `AWAITING_USER`.

**Fix (W2).** Session missing → delete the question row, return
`{"status":"dismissed"}`. Admission refused → still delete the row and apply
the existing `question-dismissed → IDLE_READY` fallback edge so the session
is not stranded, then return the 409 detail as an informational field, not
an error. Tests for both.

### L14 — Worker steering reports success before delivery

`core/extensions/orchestration/__init__.py` ~1406: on the loop it returns
"Message submitted" before the detached `steer` runs, so a rejection is
invisible; off the loop `.result(timeout=10)` raises into the tool while the
coroutine keeps running, so the agent retries and duplicates the message.

**Fix (W2).** On the loop: return "queued for worker; delivery is confirmed
by a `worker.steered` event" and have the detached task emit
`worker.steered` / `worker.steer_rejected` on the parent's stream and a
turn-ledger note the agent can see. Off the loop: on timeout cancel the
future before raising, and word the error as "not delivered", so a retry is
safe. Test both branches with a fake worker.

## Workstreams

Grouped by code region, as in the three 09-08 batches (zero conflicts).

| # | Stream | Findings | Primary files |
| --- | --- | --- | --- |
| W1 | web client | L01, L02, L03, L04, L12 | `static/js/notifications.js`, `components/jobs-indicator.js`, `components/sidebar.js`, `render.js`, `app.js`, `static/css/layout.css`, `file-panel.css`, `tools/ui-gate/*` |
| W2 | session state machine | L09, L10, L11, L13, L14 | `sessions/state_v2.py`, `sessions/manager.py`, `core/agent.py`, `db/models.py`, `api/routers/questions.py`, `core/extensions/orchestration/__init__.py` |
| W3 | background sweeps | L05, L06, L07 | `core/memory/consolidate.py`, `core/memory/sweeps.py`, `core/adaptive/tripwire.py`, `core/canary/*`, `config/settings` |

No file is shared between streams. Each stream commits one fix per finding
with a dated regression test (`tests/test_<slug>_2026_09_14.py`), on its own
branch in its own worktree outside the main checkout. The full gate
(`./check.sh`, `tools/ui-gate/run.sh`) runs once in the main tree after the
merge; the worktree lesson from 09-08 (tests resolve relative writes to the
checkout root, so worktree runs show phantom failures) means streams run
their own tests plus the directly related files, not the whole suite.

### Merge order

`W3 → W2 → W1`, least contended first.

## Deploy and validation

1. `./check.sh` green and `tools/ui-gate/run.sh` green in the main tree.
2. Push `next-3.2-testing`.
3. Box: backup, busy check (`/api/sessions?limit=500` states), `git pull
   --ff-only`, `docker compose up -d --build pernix`, verify `Args` still
   `["run.py","--dangerous"]`, `/api/health`, sha check of tracked files.
4. Park `workspace-organizer-evidence-gate` (L07c).
5. Live assertions, one per finding where the box can show it: connection
   count from a hidden headless tab drops to one (L01); hover centre element
   is the title (L02); a lone-tilde message renders without `<del>` (L03);
   toolbar has no overlapping buttons (L04); next consolidation cycle logs
   no 400 (L05, may need a manual snooze cycle); tripwire warning count stops
   growing and the two batches carry `no-signal` (L06); the canary is parked
   and no new high-urgency alert (L07); `TRANSITIONS` contains the two new
   edges in the container (L09); a cancel on a long session issues the narrow
   query (L10, log line); a smoke turn completes end to end.
