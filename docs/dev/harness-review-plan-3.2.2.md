# Stability, performance and UX audit 3.2.2 — remediation plan

Third external audit of the day (`atra_findings_3.2.2.md`, untracked in the
repo root), filed against baseline `f16007f` — the tip left by the 3.2 review.
Where 3.2 and 3.2.1 scoped the agent harness, this one scopes the *product
around* it: frontend streaming and navigation, persistence and API scaling,
scheduling, provider fallback, MCP, and unattended maintenance.

22 findings: 11 P1, 1 P1-UX, 10 P2.

The report is explicit that it is "a remediation handoff, not an implementation
or a claim that fixes have passed verification", that its reproductions were
"audit experiments, not committed regression tests", and that the fixing agent
should "reproduce each issue before editing". So that is what we did first.

## Verification

Six read-only agents reproduced every finding against `e3f7908` before any fix
was designed — deterministic barriers and fake timers rather than sleeps, the
real functions rather than paraphrases, temp databases and stubbed transports
rather than live data. Performance findings were re-measured rather than
inherited.

**Result: 22 confirmed, 0 refuted.** Fifteen are broader than filed. Four carry
impact corrections where the audit's mechanism was right but its stated
consequence was not reproducible, or was already safe.

| ID | Filed | Verdict |
| --- | --- | --- |
| S01 | P1 attachment upload can send to the wrong session | CONFIRMED-BROADER |
| S02 | P1 history-to-SSE handoff can omit answer content | CONFIRMED-BROADER |
| S03 | P1 late recovery requests disconnect a new session | CONFIRMED + correction |
| S04 | P1 soft reload has a second token-loss window | CONFIRMED-BROADER |
| S05 | P1 memory-editor conflict checks permit lost updates | CONFIRMED-BROADER |
| S06 | P1 memory reindex races live mutations | CONFIRMED |
| S07 | P1 database optimization does not exclude writers | CONFIRMED-BROADER + correction |
| S08 | P1 incomplete backups count as fresh | CONFIRMED-BROADER |
| S09 | P1 cron overrides attach to the wrong turn | CONFIRMED-BROADER |
| S10 | P1 router fallback bypasses routing, removes tools | CONFIRMED-BROADER |
| S11 | P1 executor cancel does not cancel the MCP bridge | CONFIRMED-BROADER |
| S12 | P1-UX trailing-edge debounce starves streaming paints | CONFIRMED-BROADER |
| S13 | P2 long open code fences retain quadratic work | CONFIRMED-BROADER + correction |
| S14 | P2 context status reads full history twice on-loop | CONFIRMED |
| S15 | P2 sidebar pagination does not bound enrichment | CONFIRMED-BROADER + correction |
| S16 | P2 bulk purge bypasses async session deletion | CONFIRMED |
| S17 | P2 cron reports completion without the outcome | CONFIRMED-BROADER |
| S18 | P2 concurrent MCP disable/enable orphans connections | CONFIRMED |
| S19 | P2 new prompts start after shutdown's snapshot | CONFIRMED |
| S20 | P2 daily maintenance marked run before it completes | CONFIRMED |
| S21 | P2 image-preview object URLs leak | CONFIRMED-BROADER |
| S22 | P2 horizontal code scrolling closes mobile Explorer | CONFIRMED-BROADER |

### The four impact corrections

These matter because a fix justified by a symptom that does not occur is a fix
nobody can evaluate later.

**S07 — the busy-timeout failure needs a database ~10x larger than production.**
Measured with the real `_vacuum()` against a real writer at the codebase's own
`busy_timeout=5000`: at 178 MB (the live box holds 168 MB) `VACUUM` owns the
writer lock for 0.70 s and the writer succeeds. At 1269 MB it is 4.10 s and
still succeeds. Only at 2537 MB (10.56 s) does a writer fail. The four
structural defects — admission gap, no serialization, cancel-does-not-stop,
wrong predicate — are all real and stand on their own. The lost turn asserted
by `storage.py`'s own docstring does not currently happen.

**S13 — fixing S12 very nearly fixes S13.** The audit's operation counts
reproduce to three digits (8.02M / 32.04M / 128.08M characters at
1000/2000/4000 ticks). But the real cost is 304 ms of parser CPU spread across
4000 paints, worst single paint 1.5 ms — never a dropped frame. And today it
costs nothing at all, because S12 means those paints never happen. With a
bounded 100 ms cadence the same stream costs 59 ms. The fence rewrite is not
urgent; the fence *correctness* bugs found underneath it are.

**S15 — this is not an event-loop problem.** `api/routers/sessions.py:81-93`
already dispatches every one of these queries through `asyncio.to_thread`;
measured heartbeat gap 9.16 ms against a 5.15 ms baseline across a 315.8 ms
route. The waste is real — 245 ms and 1.13 MB per poll every 10 s per visible
tab, with 92,400 of 116,402 aggregated messages belonging to archived sessions
— but it is bandwidth and thread occupancy, not responsiveness.

**S03 — the delayed-*success* case is already safe.** The audit's acceptance
criteria ask that a late probe success also not disturb the newly selected
session. It doesn't: `connectSSE` → `disconnectSSE` zeroes `sse.js`'s own
`_lastSeq` (`sse.js:223`), so `behind` at `:281` is false. Only the 404 and
network-error results are dangerous. We are not adding machinery for a case
that already holds.

### What the audit did not find

Eleven findings turned out wider once reproduced. The ones that change the fix
rather than merely its rating:

- **S17 has eight paths, not three**, and five of them never run a turn. The
  sharpest is unfiled: a cron firing within the 3-second rapid-fire window has
  its prompt text **appended to the user's own message row**, so the machine's
  instructions are injected into the human's turn, the job never runs, and the
  row says `completed`.
- **S08 is worse than "no completion manifest".** A failure inside
  `VACUUM INTO` leaves a **zero-byte** file bearing the current scheme name.
  It ranks newest, freshness reads it as ~0 hours old, and maintenance
  therefore refuses to retry for 24 hours — on the exact condition (disk full)
  where backups matter most.
- **S21 hides a correctness bug, not just a leak.** `viewFile` is async with no
  sequence guard, so two requests resolving in reverse order leave the viewer
  showing the file the user navigated *away* from. Affects text files too.
- **S18 already orphans a connection with no concurrency at all.**
  `api/app.py:411` bounds shutdown with `wait_for(..., timeout=8)`; the
  cancellation lands on the `gather`, so `connections.clear()` never runs.
- **S11's cooperative escape hatch already exists and is already wired.**
  `core/tools/executor.py:566` puts a `threading.Event` into
  `ctx["_cancel_event"]` and the web extension already reads it. The bridge
  simply never does.
- **S12 silently drops received text.** `stream.error` and
  `stream.budget_exhausted` never do a final render, and
  `_dropEmptyStreamingBubble()` returns early when `_collected` is truthy — so
  a turn that errors six seconds in leaves an empty assistant card holding
  1500 characters the browser already had.
- **S22's sibling.** The sidebar drawer has the identical gesture bug in both
  directions, and wide tables fail *twice*: `preventDefault` blocks the scroll
  **and** the panel toggles anyway.
- **S09 destroys a user's model pin permanently.** Cleanup assigns `None`
  rather than restoring the prior override, and the pin is in-memory only.
- **S02's missing answer is permanent, not transient.** When a whole round
  completes inside the handoff window the answer is not truncated but absent —
  and nothing repairs it, because `_reconcile()` compares `event_seq` against a
  `_lastSeq` that was set *to* the server's value, so drift reads as zero
  forever. It survives until the user re-selects the session.
- **S04 is a cross-session content leak, not only a lost-token race.**
  `_softReload` has no generation token at all, and `selectSession` resets
  neither `_reloading` nor `_bufferedDuringReload` — so a reload of A that
  straddles a switch renders **A's buffered text into B's transcript**. That
  puts S04 in the same bug family as S03.
- **S03 disables deleted-session detection for good.** The watchdog's rebuild
  at `sse.js:323-326` installs a replacement `onerror` that neither counts
  errors nor probes. After any watchdog reconnect, 12 consecutive errors
  produced **zero** probes and the status dot spins on "reconnecting" forever —
  precisely the failure the probe exists to end.
- **S01 silently swaps the user's session.** Navigation during new-session
  creation does not merely misroute a request: `app.js:2174` reassigns
  `state.sid`, moving the composer, the SSE connection and the cursor to a
  session the user never picked while the previous transcript stays on screen.

## Verification tooling gap

`check.sh` runs **no JavaScript**. The only JS-aware test greps `sse.js` for
event names. The UI gate has no swipe primitive, seeds no image, and replaces
`EventSource` with an inert class precisely so nothing streams — so it would
catch none of S01-S04, S12, S13, S21 or S22.

Eight of 22 findings therefore live in code no existing test can see. The
frontend workstreams write their regressions as pytest tests that shell out to
`node` and drive the real `static/js/*.js` source, extracted by content anchor
so the test cannot drift into testing a transcription of the logic. That puts
these fixes under `check.sh` for the first time.

## Workstreams

Grouped by **code region, not by finding** — the technique that produced near
zero merge conflicts across the two earlier batches today. Each stream owns its
files outright.

| # | Stream | Findings | Primary files |
| --- | --- | --- | --- |
| W1 | streaming and selection ownership | S01-S04, S12, S13 | `static/js/app.js`, `sse.js`, `render.js`, `api/streaming.py` |
| W2 | browser resources and touch | S21, S22 | `file-panel.js` (viewer), `mobile.js`, `touch.css` |
| W3 | memory writer ownership | S05, S06 | `core/memory/store.py`, `api/routers/memory.py`, `file-panel.js` (editor) |
| W4 | maintenance exclusivity and backups | S07, S08, S20 | `api/routers/storage.py`, `scripts/backup.py`, `maintenance.py` |
| W5 | the admitted-turn contract | S09, S17, S19 | `scheduling/__init__.py`, `sessions/manager.py`, `api/app.py` |
| W6 | provider failover | S10 | `core/llm/router.py`, `stream_ladder.py`, `client.py` |
| W7 | MCP cancellation and lifecycle | S11, S18 | `core/extensions/mcp/*`, `core/tools/executor.py` |
| W8 | bounded API and storage work | S14, S15, S16 | `api/routers/context.py`, `compiler.py`, `db/models.py`, `api/routers/sessions.py` |

Two deliberate groupings follow the audit's own sequencing advice:

- **S09 and S17 share one admission/execution handle.** Splitting them would
  produce two incompatible notions of "which turn is this job's turn".
- **S01-S04 plus S12 are one owner.** The audit warns that "separate agents
  should not independently patch global cursor behavior in `app.js`", and it is
  right: all four are symptoms of one missing concept. The mechanism already
  exists in-house — `app.js:818` has a `_selectSeq` generation token guarding
  `selectSession`, with a comment describing this exact bug class. It was never
  applied to `send()`, the upload path, watchdog recovery, or `_softReload`.
  The fix extends a proven local pattern rather than introducing a new one.

Known shared files, all in different functions: `file-panel.js` (W2 viewer vs
W3 editor), `api/app.py` (W5 shutdown ordering vs W7's MCP block),
`sessions/manager.py` (W5 owns it; W8 reads it).

### Merge order

Least-contended first, so conflicts surface against a smaller diff:

`W6 → W7 → W4 → W3 → W8 → W5 → W2 → W1`

Same-name/different-meaning collisions are the class to fear — a clean git
merge dropped an entire finding in the 3.2.1 batch. Every stream's own
acceptance tests are re-run after the merge, not just before.

## Scope declined

The audit's closing sections ask for verification we cannot honestly produce
here and we do not claim it:

- **Device acceptance.** S22 asks for a physical iOS/Android touch run and S21
  for measured browser heap. We ship deterministic harnesses, not device
  results, and say so rather than declaring the device behaviour verified.
- **Throttled-network and browser acceptance** for S01-S04. The node harnesses
  cover the interleavings; they do not establish what a real browser paints.
- **Latency claims from operation counts.** Where the audit counted operations,
  we report operation counts. Where we measured wall time and heartbeat gaps,
  we report the dataset and environment alongside.

## Follow-ups not taken in this batch

- The five other non-cancellable `run_coroutine_threadsafe` bridges
  (`memory_tools.py:580`, `rlm/__init__.py:522`, `evaluation/__init__.py:127`,
  `model_mgmt/__init__.py:36`, `dream/probe.py:221`). W7's registry is designed
  to be reusable by them; none has remote side effects, so none is urgent.
- `has_active_work()` sees only in-memory agent sessions. Roughly 237
  `connect_sessions()` call sites are invisible to it. W4 coordinates the
  sharpest pair (optimize vs the daily tier); a full writer inventory is a
  larger piece of work.
- The `_ENRICHED_SELECT` first-message `ROW_NUMBER()` window is the single
  biggest query cost (86.8 ms of a 245 ms page) and is paid twice whenever a
  space exists. W8 bounds it by selecting IDs first; a maintained aggregate
  would remove it entirely.
- `/api/context/{sid}` and `/payload` return HTTP 200 for a session that does
  not exist, compiling a full system prompt and tool schemas to do it.
