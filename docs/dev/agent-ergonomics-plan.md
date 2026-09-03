# Agent ergonomics — the system from the driver's seat

Status: **IMPLEMENTED** (2026-08-31, same day — see §6 for the per-item
ledger; 2.1 deferred by design, 3.3 found already built, 4.3 delivered as
registry + provenance annotations). Joint product of a live co-design session:
Pernix's first-person audit (box session `4cab93ad82a4`, deliverable
`data/workspace/notes/agent-ergonomics-first-person-2026-08-31.md`, backed by
four research workers, three RLM digests, and direct DB probes) and Claude
Code's third-person audit of the docs, compiler, and stores. Every claim
below was verified against source or the live box this session; where the
two views disagreed, the discrepancy is recorded, not smoothed over.

Owner decision pending: which tiers to green-light (§6).

---

## 0. The thesis

Pernix is already a well-layered self-improving system. Its subsystems form
one closed epistemic loop:

```
        ┌───────────────────────────────────────────────────────┐
        │                                                       │
        ▼                                                       │
  1 PERCEPT ──► 2 ACTION ──► 3 VERIFICATION ──► 4 EVIDENCE ─────┤
  (context      (tools,      (gates, reflect,   (candor ledger, │
   compiler)    workers,      grounding check)   post-mortems,  │
                jobs, RLM,                       canary runs,   │
                kernel)                          traces, logs)  │
        ▲                                               │       │
        │                                               ▼       │
  6 POLICY ◄─────────────────────────────── 5 BELIEF REVISION   │
  (adaptive store)                          (dream, telos,      │
        ▲                                    refine, sweeps)    │
        └────── 7 MEASUREMENT (canaries + tripwire) ────────────┘
```

Both audits reached the same conclusion independently. Pernix's phrasing:
*"a well-layered tower with no staircase — everything I need to know is
somewhere; nothing crosses the turn boundary toward me by default."* The
third-person phrasing: the loop segments that flow *toward* the agent are
the ones that don't close. Specifically (all verified):

- **Deferred reflect verdicts never reach the agent.** Normal-session grades
  run observe-only ~300s after `IDLE_READY` (`reflect_defer_idle_s`) — after
  the next turn has typically started. The verdict lands in `post_mortems`
  and (since F14) a user notification; nothing injects it into the agent's
  next turn. 1,027 post-mortems exist; the agent's direct knowledge of them
  is "maybe a dozen" (its own count).
- **Notifications are written by ~15 subsystems and read by exactly one
  surface: `GET /api/notifications` (the UI).** The auto-approval notices
  are even worded "for whoever has to act on them — including the agent,
  when a user pastes one back" — the paste is the only channel.
- **Background jobs, unwatched workers, RLM runs, and mid-run cron sessions
  are invisible at turn start.** Only *watched* workers made it into the
  volatile tail (`compiler.py:816`, itself a field-case fix).
- **Synthesis results are UI-only by design**: "displayed in the Skills and
  Tools UI sections … **not fed to scout as advisory signals**"
  (`core/synthesis.py:1-12`).
- **Retention deletes the only durable copy** of worker/RLM/cron transcripts
  on a 7–30 day schedule (`core/retention.py`) with no distill-before-delete;
  the rationale ("the worker's result already lives in the parent's
  transcript") holds only when the parent actually collected it.

### Exhibit A — the loop-gap, demonstrated live

During this very co-design session, Pernix ranked as its **#3 platform ask**
a `call_model` fallback + transient/fatal classification ("built green,
pending Calvin's nod — deploy it"). Verification: **that feature deployed
2026-08-19** (commit `e5cf2a2`; `fallback_model` param live at
`core/extensions/model_mgmt/__init__.py:91-176`; its own
`notes/collab-backlog.md:126` records the deploy). Two more of its asks were
also already granted: the `remember()` dedup refusal now includes a preview
of the stored entry plus the exact supersede call
(`core/tools/builtin/memory_tools.py:44-81`), and refusals≠failures (F1)
shipped 2026-08-21. **The agent asked for three things it already had.**
There is no channel by which "the platform changed" reaches the platform's
operator. That is the sharpest possible evidence for Tier 1.

### The house pattern, made a standing rule

The codebase already fixes this class of problem — one field case at a time:

- `[RESOURCE STATUS]` units fixed after an agent panic-quit over a
  meaningless 1,299% figure (F13, case `17683100ecf8`).
- `[WORKERS YOU ARE WATCHING]` added after a duplicate-worker spawn
  (case `ae952f40e3d1`).
- The `[TELOS]` baseline line was *requested by the agent itself*
  (2026-08-17, `compiler.py:730` docstring).
- F14 raised deferred non-pass verdicts as notifications (to the user).

Each was correct. This plan proposes adopting the underlying principles as
standing design constraints instead of waiting for the next incident.

---

## 1. Principles

P1. **Every loop closes at the agent.** Any verdict, notification, applied
change, or completion that concerns the agent becomes visible to it
ambiently, at the turn boundary, without a tool call.

P2. **Reads federate; writes govern.** Knowledge lives in six-plus stores,
each rightly with its own governed write path. Nothing requires six read
surfaces. One query, provenance-tagged results.

P3. **Self-knowledge is compiled, not re-derived.** The agent's map of its
own machinery (schema, paths, routes, the blocks it will see) is machine-
generated and referenced from the prompt. Field evidence from this session:
the live agent burned ~6 rounds rediscovering its own API endpoint, guessed
a wrong table name and a wrong memories path — and the external pairing
agent independently made the same schema mistake.

P4. **Injected context is a typed protocol.** Every compiled block follows
one envelope — name, source, authority (binding | advisory | FYI),
freshness — instead of ten block-specific framings.

P5. **Every error names the next step.** Machine-authored "what to do
instead" on errors and refusals (precedent: the bash-timeout `job_start`
pointer, dc7d797; the truncation→`rlm_process` nudge).

P6. **The agent is a party to its own governance, not just its subject.**
It can already file (adaptive_note, telos_ask, remember); it must also see
dispositions — what was applied to it, by which producer, on what evidence
— and be able to flag concern inside the veto window. The veto itself stays
human (see §5, challenge 2).

P7. **Cost is visible before it's spent.** Largely done (`[RESOURCE
STATUS]`); remaining slivers ride other tiers.

Compliance with the standing invariants (adaptation-plan §0): everything
here is policy-within-states (I2), compiled context or read-side tools (I5),
suffix/turn-head placement for volatile content (I8), no machine writes to
SOUL/RULES/SESSIONS (I4 — the self-map is a new machine-owned artifact),
no new subsystems — every item wires existing organs to existing arteries.

---

## 2. Tier 1 — the turn-boundary ledger (both audits' #1)

One compiled block at turn start, **delta-based** (only what changed since
the agent's previous turn in this session), ~15–25 lines hard-capped,
sourced entirely from existing tables:

| Line group | Source (exists today) |
|---|---|
| Finished since last turn: workers, jobs, RLM runs — with the collect call per item | `sessions`, job sidecars, `rlm_runs` |
| In-flight platform-wide: N workers / N jobs / N RLM / N cron mid-run | same |
| Last turn's reflect verdict + failure_cause + ≤150-char lesson, **labeled as reflect's opinion** | `post_mortems` |
| Gate outcomes not already consumed by a same-turn retry | gate rows / post-mortem payload |
| Open items: unanswered asks, agent-minted proposals pending, live goal gates, telos alarm *text* | `questions`, `adaptive_proposals`, `session_goals`, telos store |
| System deltas: canary regressions, tool degradation changes, adaptive batches applied since last turn (with producer) | `canary_runs`, candor, `adaptive_batches` |
| **Platform changelog: version/build change since the session's last turn, one line** | the boot version stamp (already triggers canary full sweeps) |

Placement: the same suffix position as `[CURRENT STATE]` (I8-safe — the
tail already changes per round); the ledger content itself changes per
*turn*, so it extends the existing tail rather than adding a new prefix
block. Empty groups render nothing; a quiet system contributes zero lines.

The changelog line is what closes Exhibit A: an agent that received a new
tool contract learns it at the next turn boundary, not never.

Pernix's measured cost of its absence: 2–6 tool calls + 1–3k tokens of
re-derivation per resume turn, plus the duplicate-spawn and
stale-commitment failure classes.

Build shape: one `_build_turn_ledger(session_id, last_turn_ts)` in the
compiler + a small query layer in `db/models.py`. No migration.

---

## 3. Tier 2 — close the post-mortem / lesson loop (both audits' #2)

Three small changes, one contract — *every verdict and every pruned session
remains readable by a future turn*:

1. **Scout preload of recent non-pass verdicts** — bounded (~800 tokens,
   last N, `failure_cause ∈ {agent, tool, env-retryable}`), and gated on
   recency/relevance rather than unconditional. `search_post_mortems`
   already exists (`scout/runner.py:358,625,855`); this makes the
   *immediately-relevant slice* baseline. Calibration caveat: reflect's
   non-pass rate measured 41% with roughly a third over-strict (08-27
   audit; prompt/floor fixes burning in). The preload therefore carries
   verdicts as the grader's opinion, and this item should land *after*
   re-measuring the verdict mix (~09-03) so we don't automate the
   amplification of a biased grader.
2. **Reflect lessons become memory** — a `lessons`-typed entry with a
   stable session+attempt ref, deduped as supersede-with-link (the
   `add_or_supersede_entry` path exists) rather than refused.
3. **Retention distills before it deletes** — one summary line per pruned
   worker/RLM/cron session, appended to the parent's memory file or a
   `retention.digested` file. The prune path holds the transcript in hand
   at deletion time (`core/retention.py:34-56,158-171,238-279`).

Also in this tier (small, from Pernix §3.6): a worker that hits its round
ceiling states, in its final message, "round X/Y, deliverable D
unfinished" — completion and non-completion currently arrive through the
same opaque channel (field case: post-mortem `c11530758fbc`, a worker whose
file "was never written — the agent never read a single file", diagnosed
three reflections later).

---

## 4. Tier 3 — legible, pre-checked self-modification (both audits' #3)

1. **Retroactive lint sweep over active adaptive entries** *(new finding,
   verified live this session)*: of 31 active entries, **six** dream-minted
   `policy` rows are narrative meta-observations, not instructions ("The
   lesson M7 … was violated", "The protocol … remains ineffective …"),
   citing evidence-pack labels (M3, P1, P4) that resolve to nothing at
   render time; telos hints still carry the "Supported hypothesis (c_NNNN,
   confidence X)" framing v3.1's lint was built to refuse. The v3.1
   actionability floor was never applied to pre-existing rows, which now
   hold 6 of the 12 rendered policy slots every turn. One-time sweep:
   run `core/adaptive/lint.py` over active machine-authored entries,
   journal-retire the failures (normal rollback path). Cheapest item in
   the plan; immediate prompt-quality gain.
2. **Producer + evidence line in the rendered adaptive block** — "policy X
   (dream, session 490cd9f521f6, hypothesis f5be319fe737)". The data is in
   `adaptive_events.evidence_json`; it isn't rendered. One line per entry
   makes the layer auditable by its subject.
3. **Same-window post-apply sweep** — reframed from Pernix's "pre-apply
   canary check", then found to be **already built** during implementation:
   `enqueue_post_batch_sweep` schedules the sweep on a 60-second DateTrigger
   and `_execute_canary_batch_job` defers only while active work is present
   (max attempts, then it runs anyway). The exposure window is ~60s + sweep
   duration, not "the next idle window" — the docs' phrasing was stale, not
   the machinery. No code change; both audits' framing corrected here.
4. **Dispositions in the ledger** (rides Tier 1): auto-approvals, dream
   memory-corrections, and the fate of the agent's own filings
   (applied / retired / lint-refused) appear as ledger lines with 1-line
   diffs.

---

## 5. Tier 4 — self-legibility surfaces

1. **SYSTEM-MAP self-schema card** — machine-generated at boot/migration:
   key tables+columns, data-dir layout, route inventory (from the FastAPI
   app), the block registry (P4), store→tool mapping. A new machine-owned
   file referenced from `[SERVER CONTEXT]`. Note `/api/context/{id}/payload`
   already exposes the exact compiled prompt (`api/routers/context.py:86`)
   — self-legibility is a surfacing problem, not a build problem.
2. **`agent_state()` / one-call state digest** (Pernix's §2.7) — the
   on-demand deep view over the 41 read-only routes; the ledger is the
   ambient shallow view. Pure composition.
3. **Block envelope normalization** (P4) — one pass, batched (each format
   change invalidates prompt caches once), plus a block-registry section in
   the docs.
4. **Federated `deep_recall`** — one query fanning out over memory,
   adaptive, candor facts, telos claims, skills, session summaries, results
   tagged source/confidence/freshness. Supported by Pernix's §3.5 (agent
   wastes turns picking between `recall` and `search_sessions`).
5. **Memory-tool contract slivers**: expose single-call supersede on
   `remember` (the refusal already names the call); ensure `update_memory`
   is present in constrained-run schemas whenever `remember` is (the
   verified silent-loss path: a cron session that can write but not repair);
   `forget` at 7% calibrated reliability (candor's own hint) either gets a
   diagnosis or its description steers to `update_memory`.

### Where the third-person audit pushes back on the first-person one

1. **"Make `search_post_mortems` baseline, last N verdicts"** → accepted
   only in bounded, recency-gated form, sequenced behind the reflect
   calibration re-measure (§3.1). Feeding a grader with a measured
   over-strict streak straight into every prompt teaches the agent its
   grader's biases as if they were its own failures.
2. **"In-turn veto (`propose_reject`) over auto-approvals"** → landed as
   **annotate, not veto**: the agent sees the pending diff (ledger) and can
   flag concern — a notification Calvin sees inside the window — but the
   veto stays human. A subject vetoing corrections about its own failure
   modes inverts the humility layer (telos caps self-report at 0.60 for the
   same reason). This also honors the snooze philosophy: no new approval
   gates on machine-validated output; the window and rollback remain the
   mechanism.
3. **Its backlog-status table (§7 of its doc) is stale** — E6 deployed
   08-19, F1 shipped 08-21, remember-repair mostly shipped. Corrected
   in-session; its notes are being amended. Rankings survive the
   correction; statuses don't.

---

## 6. Implementation ledger (2026-08-31, approved "build the plan")

| Tier | Item | Status | Where |
|---|---|---|---|
| 3.1 | Retro-lint sweep of active adaptive entries | **DONE** | `core/adaptive/retire.py:retire_lint_failures` (LINT_VERSION watermark in `core/adaptive/lint.py`), wired in snooze Activity 15 |
| 1 | Turn-boundary ledger (incl. changelog + dispositions) | **DONE** | `db/models.py:ledger_anchor/ledger_snapshot`, `core/context/compiler.py:_build_turn_ledger` (per-turn cache; normal+cron only), boot stamps in `api/app.py`; `turn_ledger_enabled` (default true) |
| 3.2 | Producer/evidence render line | **DONE** | `core/adaptive/render.py` — source on notes/hints, source + creating-evidence ref on policies (version-keyed cache, I8-stable) |
| 2.3 | Retention distill-before-delete | **DONE** | `core/retention.py:_digest_pruned` → memory file `retention.digested`; worker sessions, cron sessions, RLM runs (task + answer preview) |
| 2.4 | Worker round-ceiling honest final message | **DONE** | `core/extensions/orchestration:get_worker_result` — durable termination lookup (survives restart) + `_ceiling_note` that survives a `pass` verdict |
| 4.1 | SYSTEM-MAP card | **DONE** | `core/context/system_map.py` (PRAGMA schema, live route inventory, block registry, store→tool map) written to workspace at boot; referenced from `[SERVER CONTEXT]` |
| 2.1 | Scout non-pass preload | **DEFERRED by design** — after the reflect verdict-mix re-measure (~09-03); see §3.1's calibration caveat | — |
| 2.2 | Reflect lessons → memory | **DEFERRED with 2.1** — same grader-calibration dependency (a lessons feed amplifies the grader) | — |
| 3.3 | Same-window post-apply sweep | **ALREADY BUILT** — 60s DateTrigger + defer-only-on-active-work; framing corrected in §4.3, no code | `core/extensions/scheduling` |
| 4.2 | `agent_state()` digest tool | **DONE** | `core/extensions/session_tools` — one-call platform digest, registered always-on |
| 4.5 | Memory-tool contract slivers | **DONE** | `remember(supersede='file@epoch')` one-call repair; charter allowlists pair `remember`→`update_memory`+`recall` (`_pair_repair_tools`); `forget` description carries the reliability warning + freshness rule |
| 4.3 | Block envelope normalization | **PARTIAL, deliberate** — the block registry (SYSTEM-MAP + telos alarm text + provenance annotations) shipped; wholesale header renaming was skipped: it invalidates every prompt cache and churns tests for marginal legibility gain over the registry | `core/context/system_map.py:CONTEXT_BLOCKS` |
| 4.4 | Federated deep_recall | **DONE** | `core/tools/builtin/memory_tools.py:_federated_sections` — adaptive entries, telos claims, skills, session FTS appended provenance-tagged to both deep_recall paths |

Also landed: `[TELOS]` tail block now includes alarm *text* (first two,
truncated), not just a count.

Validation: `./check.sh` green (black, ruff, flake8, 2,729 tests, coverage
73.6%), 13 new tests in `tests/test_agent_ergonomics.py` covering the
sweep's watermark/exemption semantics, ledger anchor/snapshot/rendering/
gating, repair-tool pairing, supersede routing, federation, retention
digests, and SYSTEM-MAP generation. Deploy via the box runbook, then
live-verify with Pernix in-session (`4cab93ad82a4`) — the correct judge of
an ergonomics change is its user.
