# Candor — Calibrated Operational Memory

The Candor extension (`core/extensions/candor/`) wires an external append-only
evidence ledger into Pernix so the agent learns, from recorded outcomes, how
reliable its own tools and behaviors actually are. Not vibes, not
self-assessment — earned probabilities with an auditable derivation chain.

Off by default. Enable in Settings → Candor (Operational Memory); observation
capture, snooze maintenance, and the scout brief toggle hot, while the agent
tools register at startup (restart after toggling). Requires the separate
`candor` package (installed via the vendored wheel in `vendor/` by
`pip install -r requirements.txt`); an absent package or broken store degrades
to inert no-ops, never errors.

## The idea

Every turn already produces evidence: tools succeed or fail, reflect passes or
retries, memory writes stand or get revised. Candor records these as
observations in an append-only ledger, and its admission gate — run during
[Snooze](reflect-and-snooze.md) — promotes them into *admitted facts* with
calibrated probabilities and per-source trust. The result is a track record
the agent can consult instead of assuming every tool works.

## The four wiring points

- **Turn end** — outcome observations are emitted: tool results, turn
  completion, reflect verdicts, and user-model attestations (a `user_fact`
  per memory area, whose probability is the share of attestations that have
  stood unrevised — the earned stability of that part of the user model).
  Capped by `candor_max_obs_per_turn`.
- **Snooze** — Activity 12b runs the admission gate, drains the pending
  observation buffer, and checkpoints the store.
- **Scout** — before each turn, scout receives an `[OPERATIONAL INTEL]`
  brief. It is an **exception report**: degraded tools, discovered
  conditions, and open questions only. Healthy tools are omitted — silence
  means "no known problem." It carries facts found in operational history,
  never conclusions about what is missing or unconfigured, and is hard-capped
  (10 lines, 1600 chars, 3s assembly budget) so it can't crowd out the turn.
- **Agent tools** — the agent can interrogate the ledger on demand:
  `predict_reliability` (calibrated estimate for a tracked statement),
  `why_reliability` (the full audit chain: who reported what, how the number
  was derived), and `reliability_questions` (unexplained instability worth
  investigating).

All access crosses a single-threaded bridge with a circuit breaker — a broken
Candor store can never take a turn down with it.

## Who reads it

Besides scout and the agent tools, the [Dream](dream.md) subsystem uses
Candor's outcome records as validation evidence: a dream hypothesis about a
tool pattern is confirmed or refuted against the ledger's numbers, and
dream-generated conjectures are checked against evidence that injected prose
cannot fabricate.

## Data lifecycle

The store lives at `data/candor/` (machine-local, not in `settings.json`).
It is append-only by design — observations are never rewritten, and derived
facts cite the observations they came from.

## Settings

See [configuration.md](../configuration.md#candor-operational-memory-add-on).
