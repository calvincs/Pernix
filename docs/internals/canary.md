# Canary Suite — Measured Self-Checks

> **Notifications.** Every "notification" this page mentions goes through `core/notices.py` and lands in the tier its category is registered under — most self-maintenance receipts are *log* tier (the bell's Activity tab, never a badge); only things that need you interrupt. See [guides/notifications.md](../guides/notifications.md) for the full list.

The **canary suite** (`core/canary/`) is Pernix's measurement substrate:
golden tasks with deterministic gates, run headlessly through the full
pipeline, answering the question no ledger of anecdotes can — *is the agent
actually getting better or worse?* It complements the observation half of
self-improvement — post-mortems, scout signals, [Dream](dream.md), the refine
pass.

Off by default (`canary_enabled`) and inert when off: zero rows written.

Until 3.2 the suite was paired with the **adaptive layer**, a
machine-editable policy store whose tripwire read post-batch canary runs. The
adaptive layer was retired in 3.2 (its tables stay in the database as
history; see [upgrade.md](../upgrade.md#whats-gone-in-32)); the suite now runs
on change, on demand and on a small heartbeat.

---

## The canary suite

### What a canary is

One directory per canary under `data/canaries/`, each holding a `CANARY.md`:
frontmatter defining the task, a markdown body of free-form notes for the
humans who review it. A full example:

```markdown
---
name: fix-failing-test
prompt: |
  The test in tests/test_math.py fails. Find the bug and fix it.
gates:
  - name: pytest
    command: python -m pytest tests/test_math.py -q
    watch_paths: [src/]
files:
  src/mathlib.py: |
    def add(a, b):
        return a - b   # the planted bug
  tests/test_math.py: |
    from src.mathlib import add
    def test_add():
        assert add(2, 2) == 4
model: ""            # optional model override
timeout: 600         # optional per-run wall clock (seconds)
tags: [coding, debug]
covers: []           # change surfaces this canary tests, e.g. [skill:foo]
flaky: false         # flaky canaries inform, never count as failures
parked: false        # parked = off the heartbeat; still coverage/full/manual-run
max_runs: 0          # probe: auto-retire after N total runs (0 = never)
expires: ""          # probe: auto-retire after this ISO date
last_reviewed: 2026-08-06
---
Checks that the agent can localize a one-line arithmetic bug from a failing
test and fix it without breaking the test file.
```

`name`, `prompt`, and a non-empty `gates` list are required — a canary
without gates cannot be scored. The optional `files:` map seeds the run's
workspace with deterministic fixtures (workspace-relative paths only), so
canaries are self-contained: fixtures over live URLs, per the flakiness
discipline. Gates locally deterministic; anything that can't be, tag `flaky`
— flaky canaries inform but never count as failures. `covers:` is the
targeting index: change-driven triggers (below) select canaries whose
`covers` matches what changed. Invalid files log a warning and are skipped;
one bad canary never sinks a sweep.

### How a run works

Each canary runs as a headless `session_type="canary"` session through the
**full pipeline** — scout → agent → gates → reflect — because a canary that
skips what real turns exercise measures nothing. The workspace is a temp
directory per run (seeded from `files:`), the gates materialize as
canary-scoped rows for the run and are deleted after, and the score is the
gates re-run against the final workspace state. A run that triggered reflect
retries scores the final attempt; the retry count is recorded.

Every run records an **outcome**: `pass`, `gate_fail` (the agent ran and
the work was wrong), `timeout` (killed at the wall clock), `error` (the
harness broke), or `noop` (zero tokens, sub-second — the agent never
executed). Only `gate_fail` is evidence about the agent; the rest are
suite-health trouble. Results land in the
`canary_runs` table (gate results, outcome, error, pass/fail, retries,
tokens, duration) and in the Explorer's **Self-tuning → Self-checks (Canary)**
tab.

### Isolation guarantees

Canary sessions are deliberately hard synthetic tasks; letting them leak into
the stores that shape live behavior would poison the very signals they exist
to guard. The isolation is an enumerated predicate list, not a vibe:

- **No memory at all** — writes *and* reads. The memory-write tools are
  denied to the session type; `recall`/`deep_recall` are off the allowlist
  and the scout does no memory preload (see *Canary isolation* below).
- **Invisible to search** — canary messages are excluded from session FTS.
- **Excluded from distill/refine sweeps**.
- **Post-mortems are written but stamped** `session_type='canary'` and
  excluded from synthesis and model-routing aggregation.
- **Snooze-transparent** — canary sessions neither cancel a snooze cycle nor
  block its idle gate, so the nightly sweep and idle housekeeping coexist.
- **Hidden from the session sidebar** like Dream journals.
- **Tool-allowlisted** — every canary session runs under
  `CANARY_TOOL_ALLOWLIST` (computation and workspace reads only: file/search/
  repl tools plus read-only skill and tool discovery), enforced at the same
  three points as scheduled-job charters. Canary prompts carry
  machine-authored content — auto-admitted tasks, injected SKILL.md bodies
  during skill-verify runs — so workers, jobs, notifications, and every
  skill/tool/memory mutation are fenced off for the whole session type.

### Canary isolation

The suite exists to measure one thing: how the pipeline performs **under the
treatment** — the skills and tools in force right now. Anything
that lets a canary read the learning stores measures the stores instead, and
anything that lets the learning stores read a canary poisons the live agent
with synthetic, deliberately-hard transcripts. The 2026-09-04 hardening
audited every path in both directions and turned the list into assertions
(`tests/test_canary_isolation_hardening.py`).

**Nothing learns from a canary session.**

| Path | Guarantee |
|---|---|
| Memory distillation | `distill_session` returns immediately for `session_type == "canary"` — the guard is on the funnel, not only on `sessions/hooks._maybe_distill`, so no future caller can reopen it. The snooze catch-up selector excludes the type in SQL. |
| Refine | `run_for_session` skips with `skipped_reason="canary_session"`, so no lesson, no SKILL.md proposal, and no *canary* proposal is ever derived from a scored run. `db.get_unrefined_sessions` excludes the type as well. |
| User-profile sweep | Snooze's insight extraction excludes canary sessions (it excluded workers only until W5). |
| Distill-coverage audit | Excludes the type in SQL. |
| Dream observation, model-routing aggregation | Early-return on the type; post-mortems are written but stamped `session_type='canary'` and skipped by synthesis. |
| Space suggestions | The candidate query is `session_type = 'normal'`. |
| Auto-title | The runner names every session `Canary: <name>` at creation, and the titler only fires on sessions still called `New session`. |
| Cross-session search | `search_messages_fts` excludes canary rows, so a canary transcript cannot surface in another session's scout. |
| Retention | Canary sessions are pruned with no distill-before-delete digest. |

**No canary session learns from us.**

| Path | Guarantee |
|---|---|
| Scout preload | For a canary brief, the memory baseline, deep-memory, cross-session and lessons gatherers all return `None` (`scout.runner.memory_recall_denied`). The non-memory preload — tools, skills, models, workspace state — is the treatment and stays. |
| Scout tools | `search_memory` is removed from the scout's tool schema, and `_exec_scout_tool` refuses it as a backstop. |
| Scout fallback | The deterministic fallback report skips its `store.recall` too. |
| Agent tools | `recall` and `deep_recall` are off `CANARY_TOOL_ALLOWLIST`; the memory-write tools were already denied by `denied_session_types`. |
| The answer key | `list_gates` is off the allowlist. It prints each gate's command verbatim, and a canary gate command *is* the expected answer (`grep -qx '13' answer.txt`) — one tool call used to turn every scored task into an open-book exam. |

What a canary deliberately *keeps*: bash, the file/search/repl tools, skill
discovery and loading. Those are the treatment under measurement, not
contamination.

#### The holdout rule

A canary tagged `holdout` is the suite's honest ground: a task the system
cannot have trained itself against, because the learning loop is never
allowed to see it.

- **Never quoted into a producer prompt.** Nothing renders canary names into
  a refine or dream prompt today; `core.canary.prompt_safe_canaries()` is the
  list anything that starts must use, and a test fails the day a holdout name
  appears in one.
- **Never the target of a proposal-derived edit.** `materialize_canary`
  refuses a spec that resembles a holdout, so an auto-admitted proposal
  cannot land on one under a new name.
- **Never re-described by a proposal.** Refine reads transcripts that may
  contain a canary prompt verbatim; `queue_canary_proposals` drops a
  lookalike outright rather than queueing it, because the reviewable artifact
  would itself carry the answer. Resemblance is a normalised, windowed
  substring check (`propose.resembles_holdout`): case, punctuation and
  whitespace are folded away and a shared 10-word window against any holdout
  prompt is a match — an exact-substring test never fires on a reworded copy,
  and a bag-of-words test fires on everything. The proposal's seed files are
  compared too, since a task can be copied into a fixture as easily as into a
  prompt. A generated holdout's prompt is materialised from a fixed reference
  seed for this comparison only; a scored run always draws a fresh one.
- **Transcripts excluded like every canary's**, by the guarantees above.

The three generated sentinels ship tagged `[sentinel, generated, holdout]`:
they are the regression floor precisely because nothing can teach to them.

### Generated fixtures

A saturated suite proves nothing, and every hand-written canary ships its
answer in the repository: `grep-count`'s expected count is `8`, in the gate
command, forever. A model that has seen the transcript once passes it without
doing the work.

So a canary directory may carry a `generate.py` beside its `CANARY.md`:

```python
def generate(seed: int) -> dict:
    return {
        "prompt": "...",                                     # the task
        "files": {"logs/app.log": "..."},                    # workspace seed
        "gates": [{"name": "...", "command": "grep -qx '13' answer.txt",
                   "watch_paths": ["answer.txt"]}],          # the scoring
    }
```

Such a `CANARY.md` omits `prompt`, `gates` and `files` and carries
`generated: true`; everything else (name, timeout, tags, flaky, parked,
covers, probe fields) is read normally. The runner draws a fresh seed per run
(`core/canary/fixtures.py`), calls `generate(seed)` in-process — the same
trust level as the gate shell commands it produces, so generated canaries are
a hand-authored, repository-reviewed surface that auto-admission never writes
— and takes the prompt, the seed files and the gates from that one call.

**Where the expected value lives:** only inside the gate command, built at run
time. Not in the workspace (input files only), not in the prompt, not in a
tool result — `list_gates` is off the canary allowlist precisely because it
printed gate commands verbatim. What *is* persisted is the seed, appended to
`gate_results_json` as `{"seed": n, "generated": true}`: enough to reproduce a
failed run by hand (`generate(seed)`), useless to a model that memorised last
week's answer. A rerun therefore runs on a *different* fixture, which makes
a repeated failure stronger evidence, not weaker.

Three ship with the suite, all tagged `[sentinel, generated, holdout]`:
`gen-file-create` (random target filename and exact line, plus a near-miss
decoy in the workspace), `gen-grep-count` (2-4 log files, random ERROR count,
with the lowercase-`error` and `ERRORS-SUMMARY` traps guaranteed present), and
`gen-json-transform` (random orders to aggregate, with at least one customer
that never ships). Each generator's arithmetic is checked against its own
rendered fixture across 20 seeds in `tests/test_canary_isolation_hardening.py`
— the gate must pass a correct answer and fail an off-by-one.

### The contamination scan

Isolation enforced at three points is exactly the kind of claim that quietly
stops being true — a new tool ships without `denied_session_types`, an MCP
server exposes a memory-shaped verb, an injected `SKILL.md` steers the agent
out of its workspace — and the suite keeps reporting green while measuring
something other than the pipeline. So every run is read back afterwards
(`core/canary/contamination.py`) and asked three questions:

1. Did it call a **memory tool**? It has none on its allowlist, so one that
   answered means the fence has a hole.
2. Did a tool argument name an **absolute path outside the temp workspace**?
   Toolchain paths (`/usr`, `/bin`, `/etc`, …) do not count; Pernix's own data
   directory, another session's files, or the repository do.
3. Does the transcript name **another canary**, or `data/canaries`? That is
   the suite reading its own answer key, however it got there. Matching is
   boundary-aware, so `gen-grep-count` naming itself is not a finding.

A hit sets `canary_runs.outcome = 'contaminated'` and posts one notification.
The scored `passed` value is preserved exactly as the gates returned it — the
run is not rewritten, it is **disqualified**: maintenance and the Trust tab
count it apart from passes and failures, so a compromised run cannot vouch
for the pipeline. The finding is appended to `gate_results_json` as an `isolation` row so the tab
can say why. Detection, not prevention: bash is on the allowlist because the
seed tasks need it, so the workspace is a fence — the scan is what makes it
observable.

### Triggers — change-driven, not wall-clock

Canaries run when something they cover **changes**; the only standing
schedule is a small heartbeat. This replaced the original nightly-full-suite
+ full-post-batch design after a live audit showed 80% of run volume
re-testing tasks nothing had touched, at a 99% pass rate. (The `post_batch`
trigger, which probed after every adaptive apply, went with the adaptive
layer in 3.2; `canary_runs` rows written before then may still carry a
`batch_id`.)

| Trigger | When |
|---|---|
| `scheduled` | The nightly **heartbeat** (`canary_schedule`, default `0 3 * * *`): the `canary_heartbeat_per_night` (2) least-recently-run non-parked canaries. Keeps every active canary's history warm enough that a post-change failure is provably the change's fault. |
| `manual` | The `canary_run(name)` tool, `POST /api/canary/run`, the Self-checks tab's run buttons, or a coverage-triggered targeted sweep (e.g. a skill edit). `canary_status` reads recent results. |
| `full` | The-world-changed sweeps: a model swap (both switch paths), a deploy (the boot version stamp), or the tab's "Run all". Runs **everything including parked canaries** and carries `must_run`, so a sweep already in flight defers it instead of eating it (the lock is otherwise skip-not-queue). |

One sweep runs at a time; Snooze prunes runs past
`canary_retention_days` (30).

### Growing the suite

Start with a small hand-written seed covering your daily-driver categories.
From there the suite grows the way a regression-test suite does — from real
failures: while `canary_enabled` is on, the refine pass may **propose** a new
canary distilled from a genuinely failed turn (name, prompt, gates,
fixtures, rationale). Only a proposal that clears the allowlist proof below
is admitted; anything else is logged and dropped. (Until 3.2 those waited in
the adaptive layer's proposal queue for a human; that queue is retired.)

**Auto-admission.** `canary_auto_admit` defaults to **true**, so a proposal
that clears the allowlist proof (`core/canary/propose.py`) is materialized
into `data/canaries/` immediately, without waiting for you. The proof is
what makes that safe, and it is deliberately narrow: every gate command must
parse, resolve to a closed set of known-safe binaries, run
`python -m pytest` / `python -m unittest` only, carry no shell metacharacters
(no pipes, redirects, substitution, chaining), and reference only
workspace-relative paths — plus no model override, a timeout under the auto
cap, and room left under `canary_max_suite`. Anything outside that set is
logged and dropped; an admission posts a notification.
Auto-admitted canaries land tagged `vetting` + `flaky: true`, so
they inform but cannot count as failures until `canary_vetting_runs`
consistent passes promote them. Set `canary_auto_admit=false` to stop the
suite growing on its own; write canaries yourself in the Self-checks tab.

**Long-green canaries are parked, never removed.** After
`canary_park_after_passes` (25) consecutive passes, maintenance writes
`parked: true`: the canary leaves the heartbeat rotation but stays in the
suite — coverage triggers, full sweeps and manual runs still fire it, and
**any red run auto-unparks it** (the one mutation allowed while a canary is
red, because it amplifies the alarm instead of silencing it). It is never
deleted: a stable canary is exactly the one whose first red run after a
change means something. (This replaced cadence demotion, which replaced
retirement — same invariant, third mechanism.)

**Full lifecycle control** lives in the Self-checks tab and the API: create
(raw CANARY.md or structured spec — gate commands are checked against the
auto-admission allowlist proof and the verdicts returned as *warnings*,
never blockers), edit (`PUT`, validated round-trip), park/unpark
(`PATCH`), mark reviewed, and retire (`DELETE` — the directory moves to
`.retired/` and is purged only after `canary_purge_after_days`, so a
retirement is reversible for the whole window).

**One-off probes**: a canary with `max_runs: N` or an `expires:` date is a
probe — "occasionally test something" without suite residue. Maintenance
retires an exhausted probe with a pass/fail tally notification;
retirement-with-the-tally IS the probe's report, so this pass is
deliberately exempt from the Goodhart lock (nothing is silenced — the red
runs are in the tally). The tab has a one-click probe template.

**Skill verify blocks** (`core/canary/skill_verify.py`): a skill may embed
its own behavioral test in SKILL.md frontmatter —

```yaml
verify:
  prompt: |
    Use the technique this skill teaches on the seeded fixture...
  gates:
    - name: check
      command: python -m pytest tests/test_expected.py -q
  files: { ... }       # optional fixtures
  timeout: 600         # optional
```

Maintenance materializes it as the MANAGED canary `skill--<name>` with
`covers: [skill:<name>]`, resyncs it whenever the skill changes, and
retires it when the block (or the skill) goes away. A sha256 watermark over
each SKILL.md (`snooze_state['skill_hash:<name>']`, the `skill_reqs_hash`
precedent) detects every mutation path including hand edits; a changed
skill fires one targeted sweep of its covering canaries at the next idle
window. **Security boundary**: verify-gate commands execute on the host and
SKILL.md is machine-editable, so every gate must pass the same allowlist
proof as canary auto-admission — a skill whose gates fail the proof gets a
once-per-content notification and no canary.

**Skill rollback** (trust-loop hardening W5). Skill auto-apply had a veto
window and a timestamped backup, and an instruction telling a human to copy
the file back by hand — which is not an undo. `restore_skill_backup()`
restores the backup taken at *that* apply (not merely the newest: a skill
with several applies would otherwise roll back to the wrong generation),
marks the proposal `rolled_back`, and copies the state it replaced to
`SKILL.md.<ts>.pre-rollback` first, so the rollback is itself reversible.
Reachable three ways:

| Trigger | Path |
|---|---|
| A human | `POST /api/skills/proposals/{id}/rollback`, beside approve/reject/apply |
| Code | `core.skills.proposals.restore_skill_backup(proposal_id)` |
| Measured regression | The skill's own `verify:` canary gate-failing within 7 days of an auto-apply, when `skill_proposal_auto_rollback` (default **off**) is on |

The automatic path is deliberately narrow. Only the canary's **latest** run
counts — a skill that failed and then went green has already been fixed, and
rolling it back would undo the fix. Only an honest `gate_fail` implicates the
edit: a timeout, a harness error, or a `contaminated` run measures the suite,
not the skill. And only a proposal auto-applied inside the window is blamed.
The flag stays off until the signal earns trust; manual rollback works
either way.

Staleness is curated, not automated away: 90 days past a canary's
`last_reviewed` date, Snooze nudges you with a notification. Bump the date
(the tab's *Reviewed ✓* button) after reviewing; the nudge re-arms when it
goes stale again.

---

## The Trust tab

The Explorer's **Self-tuning** group carries a second tab, **Trust**, beside
Self-checks. Self-checks shows the suite doing its job; Trust answers the
question underneath it — how much of the grading is grounded in something
that happened, and how much is the model agreeing with itself. It reads
`GET /api/trust` and renders counts only, no charts:

- **Grader agreement** — how often reflect's verdict matches the user's thumbs
  on the same turn, over the number of turns that carry both; plus the
  grader's hold-out accuracy against the fixture set when a run has recorded
  one.
- **Where outcomes come from** — turns whose outcome came from a thumbs, from
  the user's next message, and from reflect alone, in that precedence order,
  plus turns graded against turns sent in the last 7 days.
- **Self-checks (14 days)** — runs, failures, and runs marked contaminated
  (one that reached memory or a file outside its own workspace, and is
  therefore excluded from every measurement).

The tab is read-only apart from its Refresh button, and every field is
optional: a subsystem that is off reports zeros. On a server that predates the
trust loop the endpoint is a 404 and the tab is one line saying so.

## Settings

See [configuration.md](../configuration.md#canary-suite). API surface:
[api.md](../api.md#canary-suite).
