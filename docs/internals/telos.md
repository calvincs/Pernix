# TELOS — The Operational Question Loop

`core/telos` turns turn-time anomalies the rest of the system cannot explain into **falsifiable hypotheses**, evaluates them against the recorded evidence, and commits what survives as claims — with the strongest ones feeding the scout's routing hints. The root objective is a *question with no satisfaction predicate* (never completable, re-expressed only with operator co-sign); it anchors the question tree.

**Carved down in v3.1.** The original design also carried a goal DAG with daily audits — Ordo re-ranking, a Binding/Goodhart monitor, the Hevel discharge audit, weekly autobiography reconciliation and its divergence-alarm discharge (~950 LOC). A live audit found the goal tree only ever held the root, making ordo/binding/hevel provably total no-ops, while reconciliation spent the layer's only weekly LLM call narrating routine bookkeeping to itself; questions were 89% abandoned "why did tool X fail" templates duplicating Candor. The goal machinery was deleted; the question loop was kept and its yield fixed. The full original derivation remains in [docs/dev/telos-spec.md](../dev/telos-spec.md) as a historical design record.

Off by default. Master switch: `telos_enabled` (`config.py`). While off the layer is fully inert: no directories are created, snooze Activity 16 is skipped, the cron never installs, and the post-task hook never fires. All call sites gate at runtime (hot toggle) except tool registration, which follows the Candor pattern (restart).

## State: markdown as database

Everything lives under `data/telos/` as markdown files with YAML frontmatter — greppable, diffable, no schema migration:

```
data/telos/
  config/telos.yaml          the layer's own provenance record (readable tier)
  config/state.yaml          runtime knobs actuated by Entropy Control
  questions/q_*.md           first-class Questions with provenance
  soup/h_*.md                hypotheses: gated, speculation pool, resolved
  soup/archive/h_*.md        terminal hypotheses (untestable | expired)
  goals/g_root.md            the root question (the only goal object)
  claims/c_*.md              committed claims with epistemic-class caps
  alarms/a_*.md              acedia (the entropy control's alarm)
  ledgers/trace/             append-only JSONL, one file per UTC day
```

The trace ledger is the authority: `TelosStore.trace_append` (`core/telos/store.py`) is the only writer in the codebase, nothing exposes a rewrite path, and the agent-facing tools get no trace write access. (An existing `ledgers/first_person/` directory from before the carve is inert — never written again.)

## The fast loop

**Turn end** (`sessions/hooks.py` → `core/telos/anomaly.py`): mechanical, no LLM, one question-corpus scan per turn. Every turn appends a trace event. Anomalies mint Questions — but only anomalies the rest of the system cannot already explain:

- **A tool failure mints a question only when Candor has NO calibrated record for the tool.** Candor is the system that actually closes reliability loops (ledger, intel brief, degraded-tool hints); a tracked tool failing is Candor's business, and re-asking about it here produced the 16-of-18-abandoned question class on the live box.
- Reflect retries and round-ceiling terminations mint their own question classes (fixed surprise priors).
- One OPEN line of inquiry per source, full stop — plus the remint cooldown (`telos_anomaly_remint_cooldown_days`) for closed ones. Open questions cap at 24; per-turn minting at 2; near-duplicates are rejected. A slice of minted questions is tagged `serendipity`: high-surprise, deliberately unbound.

**Idle** (snooze Activity 16 → `core/telos/__init__.py:run_step`): one bounded unit per cycle, round-robined like Dream:

- **Generate** (`core/telos/soup.py`): the scheduler pulls the next question (85/15 with serendipity). SOUP samples memory at three analogical distances — near / mid / far — and one LLM call produces structure-mapped hypotheses, each with a falsifier, an EIG estimate, and a cost estimate. The **testability gate** admits a hypothesis iff the falsifier is defined, cost fits `telos_max_eval_tokens`, and EIG ≥ `telos_eig_floor` after the generator's **calibration discount** (`calibration.py` — over-claiming is discounted, never inflated, floored at 0.25×, rolling window). Rejected hypotheses enter the speculation pool (`status: soup`): searchable, zero execution rights, a retained record with the archival exits below.
- **Evaluate** (`core/telos/evaluate.py`): the oldest gated hypothesis has its falsifier applied by one LLM judge against gathered evidence (memory, trace, Candor brief). Supported/refuted verdicts commit a claim; two inconclusive attempts archive the hypothesis `untestable`. Resolving hypotheses narrows their parent question — committed knowledge changes what counts as an anomaly, which closes the loop.

## The slow loops

Installed by `ensure_telos_schedule()` as a transient daily cron (`telos_schedule`, default 04:00 UTC), recreated each boot from settings:

| Loop | Cadence | What it does |
|---|---|---|
| **Retirement sweeps** (`retire.py`) | daily | Release adaptive routing-hint slots the layer no longer has evidence for; archive `untestable` pool entries; age unexamined pool entries out as `expired` (`telos_soup_retention_days`); hard-delete only from `soup/archive/` past `telos_soup_archive_retention_days` — the sole unlink in the layer. |
| **Entropy Control** (`entropy.py`) | weekly | The acedia detector — hypothesis-coupled, not goal-coupled, which is why it survived the carve. If novelty entropy over executed hypotheses < 0.2 or the far band's realized share < 0.10, shift the band mix toward far and bump the serendipity budget until the floor recovers; decay back when healthy. Mints and clears its own `acedia` alarms. |

## Pool lifecycle

Two exits, both **archiving** (moving the file to `soup/archive/`), never deleting: `untestable` (examined and unresolvable — a verdict on evaluability) and `expired` (aged out unexamined — a fact about the queue). EIG-floor rejections are deliberately NOT terminal: low expected payoff is a prior, not a claim the hypothesis cannot be checked. `TelosStore.list()` globs one directory level, so an archived entry disappears from every hot-path scan with no filter to remember. There is no un-archive path — a hypothesis still worth asking re-mints cheaply from its question.

## Humility layer

Claims commit through `TelosStore.commit_claim` with hard confidence caps by epistemic class: observation 0.99, inference 0.95, testimony 0.90, analogy 0.70, **self_report 0.60**. A self-report corroborated by the trace is reclassified `observation_of_self` and escapes the cap — the path to confident self-knowledge runs through the external record, not introspection. The substrate tier (model weights) stays opaque, deliberately.

## Surfaces

- **Agent tools** (`core/extensions/telos/`): `telos_status`, `telos_ask`. Deliberately absent: trace writes, root re-expression, alarm clearing.
- **API** (`api/routers/telos.py`): `GET /api/telos` (overview), `/questions`, `/hypotheses`, `/claims`, `/trace` (read-only ledger window), `POST /api/telos/run` (manual slow-loop pass), `POST /api/telos/alarms/{id}/ack`.
- **UI**: the Explorer's **Self-tuning → Goals (Telos)** tab; settings section "Goals (Telos)" under Autonomy & idle work.

## Actuation ports

1. **Adaptive output** — a `supported` claim with recorded evidence and judge confidence ≥ 0.65 becomes a `routing_hint` candidate through `queue_producer_edits("telos", …)` — and must pass the adaptive content lint (an instruction, not a diagnosis) or it stands as a claim only. The adaptive engine's risk gating, caps, usage-based retirement and rollback apply unchanged.
2. **Context port** — up to three open questions (by surprise) plus a live alarm count render into the **volatile tail** (`_build_telos_drive_block`), fully behind a 60-second cache since v3.1 — the scans used to run raw on every compile, up to `max_tool_rounds` times per turn. The block is the empty string when `telos_enabled` is off, so compiled output is byte-identical.

## Isolation and safety properties

- Canary sessions never mint questions (`anomaly.py` early-return) — synthetic turns must not seed the drive.
- Every entry point is guarded; a TELOS failure logs a warning and the turn/cycle completes normally.
- The post-task hook is delta-tracked per turn (reflect retries never double-trace), bounded by a 10s wait, and does one corpus scan.
- The root cannot be completed, the trace cannot be rewritten, and the agent cannot clear alarms — invariants that hold by construction, not policy.
